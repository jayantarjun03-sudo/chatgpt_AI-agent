import json
import html
import os
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import pandas as pd
import streamlit as st


# ============================
# Constants / Persistence
# ============================
LEARNING_STORE_PATH = "agent_learning_memory.json"


# ============================
# UI helpers
# ============================
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(message: str) -> None:
    """Append a timestamped message to the session event log."""
    st.session_state.setdefault("event_logs", [])
    st.session_state["event_logs"].append(f"{_now_str()} — {message}")


def reset_simulation(clear_data: bool = False) -> None:
    st.session_state["event_logs"] = []
    st.session_state["agent_last_run"] = None
    st.session_state["agent_next_run"] = None
    st.session_state["agent_progress"] = []
    if clear_data:
        st.session_state["data_df"] = None


# ============================
# Learning memory (self-learning)
# ============================
def load_learning_memory() -> List[Dict[str, Any]]:
    """Load learned scenario rules from disk (best-effort)."""
    if not os.path.exists(LEARNING_STORE_PATH):
        return []
    try:
        with open(LEARNING_STORE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict) and "rules" in payload:
            rules = payload["rules"]
        else:
            rules = payload
        if isinstance(rules, list):
            return rules
    except Exception:
        return []
    return []


def save_learning_memory(rules: List[Dict[str, Any]]) -> None:
    """Persist learned rules to disk (best-effort)."""
    try:
        with open(LEARNING_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump({"rules": rules, "saved_at": _now_str()}, f, indent=2)
    except Exception:
        # Non-fatal: the app still works within session
        pass


def _default_rules() -> List[Dict[str, Any]]:
    """
    Default policies the agent starts with.
    Anything not matched can trigger self-learning.
    """
    return [
        {
            "scenario_id": "GENERIC_SLA_BREACH",
            "title": "Generic SLA breach escalation",
            "patterns": ["sla breach", "breach", "overdue", "past due"],
            "action": "ESCALATE",
            "escalate_to": "OpsQueue",
            "message_template": (
                "Escalating {ticket_id}: SLA breached. Please review and prioritize resolution."
            ),
            "source": "builtin:default",
            "learned_at": _now_str(),
        },
        {
            "scenario_id": "STUCK_WORKFLOW",
            "title": "Stuck workflow (auto-retry then escalate)",
            "patterns": ["stuck", "blocked", "pending", "workflow stagnation"],
            "action": "AUTO_RETRY",
            "escalate_to": "OpsQueue",
            "message_template": "",
            "source": "builtin:default",
            "learned_at": _now_str(),
        },
    ]


# ============================
# Data ingestion
# ============================
def _safe_read_upload(uploaded_file) -> Optional[pd.DataFrame]:
    if uploaded_file is None:
        return None

    name = uploaded_file.name.lower()
    try:
        if name.endswith(".csv"):
            return pd.read_csv(uploaded_file)
        if name.endswith(".json"):
            payload = json.load(uploaded_file)
            # Accept either {"tickets":[...]} or a raw list
            if isinstance(payload, dict) and "tickets" in payload:
                payload = payload["tickets"]
            return pd.DataFrame(payload)
    except Exception as e:
        st.error(f"Could not read file: {e}")
        return None

    st.error("Unsupported file type. Please upload CSV or JSON.")
    return None


def _coerce_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize expected columns (best-effort).
    Expected:
      ticket_id, type, status, sla_status, breach_hours, vendor,
      context, root_cause, impact, recommended_next_steps, action
    """
    if df is None or df.empty:
        return df

    rename_map = {
        "id": "ticket_id",
        "ticket": "ticket_id",
        "order_id": "ticket_id",
        "ticket_type": "type",
        "category": "type",
        "sla": "sla_status",
        "sla_breached": "sla_status",
        "breach": "sla_status",
        "breach_hrs": "breach_hours",
        "rca": "root_cause",
        "rootcause": "root_cause",
        "next_steps": "recommended_next_steps",
        "recommended": "recommended_next_steps",
    }

    df = df.copy()
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={c: rename_map.get(c, c) for c in df.columns})

    # Ensure all required columns exist
    for col in [
        "ticket_id",
        "type",
        "status",
        "sla_status",
        "breach_hours",
        "vendor",
        "context",
        "root_cause",
        "impact",
        "recommended_next_steps",
        "action",
    ]:
        if col not in df.columns:
            df[col] = None

    df["ticket_id"] = df["ticket_id"].astype(str)
    df["type"] = df["type"].astype(str).str.upper()
    df["status"] = df["status"].astype(str)
    df["sla_status"] = df["sla_status"].astype(str).str.upper()

    # breach_hours can be numeric or strings like "+24h"
    def parse_breach(x):
        if pd.isna(x):
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip().lower().replace("+", "").replace("h", "")
        try:
            return float(s)
        except Exception:
            return None

    df["breach_hours_num"] = df["breach_hours"].apply(parse_breach)

    # Default action if not provided
    def infer_action(row):
        if isinstance(row.get("action"), str) and row["action"].strip():
            return row["action"].strip().upper()
        if "BREACH" in str(row.get("sla_status", "")).upper():
            return "ESCALATE"
        if str(row.get("status", "")).strip().lower() in {"stuck", "blocked"}:
            return "AUTO_RETRY"
        return "MONITOR"

    df["action"] = df.apply(infer_action, axis=1)

    return df


# ============================
# Ticket logic
# ============================
def _is_breached(row: pd.Series) -> bool:
    return "BREACH" in str(row.get("sla_status", "")).upper()


def _is_stuck(row: pd.Series) -> bool:
    return str(row.get("status", "")).strip().lower() in {"stuck", "blocked", "pending"}


def _ticket_types_summary(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "-"
    counts = df["type"].fillna("UNKNOWN").replace("NAN", "UNKNOWN").value_counts()
    return " · ".join([f"{k}:{v}" for k, v in counts.items()])


def _ticket_haystack(row: pd.Series) -> str:
    parts = [
        row.get("type", ""),
        row.get("status", ""),
        row.get("sla_status", ""),
        row.get("context", ""),
        row.get("root_cause", ""),
        row.get("impact", ""),
        row.get("recommended_next_steps", ""),
        row.get("vendor", ""),
    ]
    return " ".join([str(p) for p in parts if p is not None]).lower()


def classify_ticket_scenario(row: pd.Series, rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Match ticket text against learned scenario rules.
    - pattern "/.../" is treated as regex (case-insensitive)
    - otherwise substring match
    """
    haystack = _ticket_haystack(row)

    for rule in rules:
        patterns = rule.get("patterns", []) or []
        if not patterns:
            continue

        for p in patterns:
            p = str(p).strip()
            if not p:
                continue

            if len(p) >= 2 and p.startswith("/") and p.endswith("/"):
                try:
                    if re.search(p[1:-1], haystack, flags=re.IGNORECASE):
                        return {"matched": True, "rule": rule, "matched_pattern": p}
                except re.error:
                    # Fallback: treat inner as substring
                    if p[1:-1].lower() in haystack:
                        return {"matched": True, "rule": rule, "matched_pattern": p}
            else:
                if p.lower() in haystack:
                    return {"matched": True, "rule": rule, "matched_pattern": p}

    return {"matched": False, "rule": None, "matched_pattern": ""}


# ============================
# Simulated external knowledge (runbook/KB)
# ============================
def external_knowledge_lookup(row: pd.Series) -> Optional[Dict[str, Any]]:
    """
    Simulated external system knowledge (Vendor KB / Runbook).
    Returns a proposed remediation policy if it recognizes the symptoms.
    """
    text = _ticket_haystack(row)

    kb = [
        {
            "kb_id": "KB-PORTIN-TIMEOUT-001",
            "signals": ["port-in", "timeout", "npdb", "mnp", "vendor gateway timeout"],
            "scenario_id": "PORTIN_VENDOR_GATEWAY_TIMEOUT",
            "title": "Port-in blocked due to vendor gateway timeout",
            "action": "ESCALATE",
            "escalate_to": "PortingVendor",
            "message_template": (
                "Escalating {ticket_id}: Port-in failing due to vendor gateway timeout. "
                "Please check gateway logs and retry the transaction."
            ),
            "patterns": ["port-in", "vendor gateway timeout", "/timeout\\s*\\d+s/"],
        },
        {
            "kb_id": "KB-PAYMENT-CAPTURE-002",
            "signals": ["authorised but not captured", "capture failed", "auth not captured"],
            "scenario_id": "PAYMENT_AUTH_NOT_CAPTURED",
            "title": "Payment authorised but not captured",
            "action": "ESCALATE",
            "escalate_to": "PaymentVendor",
            "message_template": (
                "Escalating {ticket_id}: Payment authorised but not captured. "
                "Please investigate capture pipeline and reconcile transaction."
            ),
            "patterns": ["authorised but not captured", "capture failed", "auth not captured"],
        },
        {
            "kb_id": "KB-ESIM-EID-003",
            "signals": ["eid mismatch", "profile not found", "activation code invalid"],
            "scenario_id": "ESIM_EID_MISMATCH",
            "title": "eSIM activation fails due to EID mismatch",
            "action": "ESCALATE",
            "escalate_to": "eSIMVendor",
            "message_template": (
                "Escalating {ticket_id}: eSIM activation failing due to EID mismatch/profile not found. "
                "Please validate EID mapping and provisioning records."
            ),
            "patterns": ["eid mismatch", "profile not found", "activation code invalid"],
        },
        {
            "kb_id": "KB-RETRY-UPSTREAM-004",
            "signals": ["transient", "intermittent", "upstream 502", "upstream 503", "temporary failure"],
            "scenario_id": "TRANSIENT_UPSTREAM_FAILURE",
            "title": "Transient upstream failure (retry recommended)",
            "action": "AUTO_RETRY",
            "escalate_to": "",
            "message_template": "",
            "patterns": ["upstream 502", "upstream 503", "temporary failure", "intermittent"],
        },
    ]

    for item in kb:
        if any(sig in text for sig in item["signals"]):
            return item

    return None


def _derive_rule_from_kb(kb_hit: Dict[str, Any]) -> Dict[str, Any]:
    """Convert KB guidance into an agent memory rule."""
    return {
        "scenario_id": str(kb_hit.get("scenario_id", "")).strip().upper(),
        "title": (kb_hit.get("title") or "").strip(),
        "patterns": kb_hit.get("patterns", []) or [],
        "action": str(kb_hit.get("action", "MONITOR")).strip().upper(),
        "escalate_to": (kb_hit.get("escalate_to") or "").strip(),
        "message_template": (kb_hit.get("message_template") or "").strip(),
        "source": f"external:{kb_hit.get('kb_id', 'KB-UNKNOWN')}",
        "learned_at": _now_str(),
    }


# ============================
# Agent simulation (self-learning)
# ============================
def simulate_agent(df: pd.DataFrame) -> None:
    """
    Simulate an agent run:
      - detect breached tickets
      - classify using learned rules
      - if unknown: self-learn from external KB, update memory, rerun ticket
      - execute action and log steps with timestamps
      - ensure escalation logs include "message sent" and "escalated"
    """
    if df is None or df.empty:
        st.warning("No data loaded.")
        return

    rules: List[Dict[str, Any]] = st.session_state.get("learned_rules", [])
    breached_df = df[df.apply(_is_breached, axis=1)].copy()

    st.session_state["agent_progress"] = []
    log_event("Agent run started: scanning ticket queue")

    # Generic checks
    st.session_state["agent_progress"].append("Checked SLA deadlines for all tickets")
    log_event("Checked SLA deadlines for all tickets")

    st.session_state["agent_progress"].append("Detected workflow stagnation")
    log_event("Detected workflow stagnation")

    stuck_count = int(df.apply(_is_stuck, axis=1).sum())
    if stuck_count:
        st.session_state["agent_progress"].append(f"Flagged {stuck_count} stuck tickets")
        log_event(f"Flagged {stuck_count} stuck tickets")

    if breached_df.empty:
        st.session_state["agent_progress"].append("No breached tickets detected")
        log_event("No breached tickets detected; monitoring continues")
        st.session_state["agent_last_run"] = datetime.now()
        st.session_state["agent_next_run"] = datetime.now() + timedelta(minutes=30)
        log_event("Agent run completed")
        return

    # Process breached tickets
    for _, row in breached_df.iterrows():
        tid = row.get("ticket_id", "UNKNOWN")
        ttype = row.get("type", "UNKNOWN")
        vendor = row.get("vendor") or "Vendor"
        breach_hours = row.get("breach_hours") or (
            f"+{int(row['breach_hours_num'])}h" if pd.notna(row.get("breach_hours_num")) else "N/A"
        )
        default_action = str(row.get("action", "ESCALATE")).upper()

        # Diagnose
        diag = row.get("root_cause") or "No explicit root cause; correlating signals"
        st.session_state["agent_progress"].append(f"Diagnosed {tid}: {str(diag)[:70]}{'…' if len(str(diag))>70 else ''}")
        log_event(f"{tid}: Diagnosing — {ttype} breached by {breach_hours}; analysing context/root cause")

        # 1) initial classification
        policy_match = classify_ticket_scenario(row, rules)

        # 2) self-learning path
        if not policy_match["matched"]:
            log_event(f"{tid}: Unknown scenario detected — no learned policy matched. Initiating self-learning.")

            kb_hit = external_knowledge_lookup(row)
            if kb_hit is None:
                log_event(f"{tid}: External knowledge lookup returned no match. Defaulting to safest action → ESCALATE")
                action = "ESCALATE"
            else:
                log_event(f"{tid}: External knowledge matched {kb_hit.get('kb_id')} — deriving remediation policy and updating memory")

                new_rule = _derive_rule_from_kb(kb_hit)

                existing_ids = {str(r.get("scenario_id", "")).upper() for r in rules}
                if new_rule["scenario_id"] in existing_ids:
                    log_event(f"{tid}: Memory already contains scenario {new_rule['scenario_id']} — skipping add")
                else:
                    st.session_state["learned_rules"].append(new_rule)
                    st.session_state["learning_version"] = int(st.session_state.get("learning_version", 1)) + 1
                    save_learning_memory(st.session_state["learned_rules"])
                    log_event(
                        f"{tid}: Memory updated (v{st.session_state['learning_version']}) "
                        f"→ added {new_rule['scenario_id']} from {new_rule.get('source')}"
                    )

                # 3) rerun classification on the same ticket
                rules = st.session_state["learned_rules"]
                policy_match = classify_ticket_scenario(row, rules)

                if policy_match["matched"]:
                    rule = policy_match["rule"] or {}
                    action = str(rule.get("action", default_action)).upper()
                    log_event(
                        f"{tid}: Re-run after learning succeeded — matched {rule.get('scenario_id')} "
                        f"→ action={action}"
                    )
                else:
                    action = "ESCALATE"
                    log_event(f"{tid}: Re-run after learning still no match — fallback action={action}")

        # Normal (known) path
        else:
            rule = policy_match["rule"] or {}
            action = str(rule.get("action", default_action)).upper()
            log_event(
                f"{tid}: Learned scenario matched → {rule.get('scenario_id')} ({rule.get('title')}) "
                f"| matched='{policy_match['matched_pattern']}'"
            )
            log_event(f"{tid}: Applying learned policy → {action}")

        # Execute action
        if action == "ESCALATE":
            vendor_target = vendor
            msg = None

            if policy_match.get("matched"):
                rule = policy_match["rule"] or {}
                if (rule.get("escalate_to") or "").strip():
                    vendor_target = rule["escalate_to"].strip()
                tmpl = (rule.get("message_template") or "").strip()
                if tmpl:
                    msg = tmpl.format(ticket_id=tid)

            log_event(f"{tid}: Escalation message sent to {vendor_target}" + (f": {msg}" if msg else ""))
            log_event(f"{tid}: Escalated to vendor system / ops queue for further action")

        elif action == "AUTO_RETRY":
            log_event(f"{tid}: Triggered auto-retry workflow")
            # In a real run, retry outcome depends on telemetry; simulate a conditional:
            if "transient" in _ticket_haystack(row) or "intermittent" in _ticket_haystack(row):
                log_event(f"{tid}: Auto-retry succeeded; ticket stabilized")
            else:
                log_event(f"{tid}: Auto-retry did not resolve within expected window; escalating to ops queue")
                log_event(f"{tid}: Escalation message sent to OpsQueue")
                log_event(f"{tid}: Escalated to vendor system / ops queue for further action")

        else:
            log_event(f"{tid}: Monitoring — no immediate action required")

    st.session_state["agent_last_run"] = datetime.now()
    st.session_state["agent_next_run"] = datetime.now() + timedelta(minutes=30)
    log_event("Agent run completed: actions logged for breached tickets")


# ============================
# Page
# ============================
st.set_page_config(page_title="Daily Ticket Dashboard", layout="wide")

# Session state defaults
st.session_state.setdefault("event_logs", [])
st.session_state.setdefault("agent_progress", [])
st.session_state.setdefault("agent_last_run", None)
st.session_state.setdefault("agent_next_run", None)
st.session_state.setdefault("data_df", None)

# Learning defaults (load once)
st.session_state.setdefault("learning_version", 1)
if "learned_rules" not in st.session_state:
    persisted = load_learning_memory()
    if persisted:
        st.session_state["learned_rules"] = persisted
    else:
        st.session_state["learned_rules"] = _default_rules()
        save_learning_memory(st.session_state["learned_rules"])

# CSS
st.markdown(
    """
    <style>
      .ticket-types {white-space: normal; word-break: break-word; line-height: 1.4;}
      .event-log {
        background: #0b0f0d;
        border-radius: 14px;
        padding: 14px 14px;
        border: 1px solid rgba(255,255,255,0.08);
        max-height: 420px;
        overflow-y: auto;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        font-size: 12.5px;
        line-height: 1.45;
        color: #a6ffb6;
      }
      .event-line {margin-bottom: 6px;}
      .small-muted {color: rgba(0,0,0,0.55); font-size: 12px;}
      .pill {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 999px;
        font-size: 12px;
        border: 1px solid rgba(0,0,0,0.12);
        margin-left: 8px;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

left, right = st.columns([2.6, 1.2], gap="large")

with left:
    st.title("Daily Ticket Dashboard")

    df = st.session_state.get("data_df")
    total = int(len(df)) if isinstance(df, pd.DataFrame) else 0
    breached = int(df.apply(_is_breached, axis=1).sum()) if isinstance(df, pd.DataFrame) and not df.empty else 0
    stuck = int(df.apply(_is_stuck, axis=1).sum()) if isinstance(df, pd.DataFrame) and not df.empty else 0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Tickets", total)
    m2.metric("Breached Tickets", breached)
    m3.metric("Stuck Tickets", stuck)

    with m4:
        st.markdown("**Ticket Types**")
        st.markdown(f"<div class='ticket-types'>{_ticket_types_summary(df)}</div>", unsafe_allow_html=True)

    st.subheader("Ticket Queue")

    if not isinstance(df, pd.DataFrame) or df.empty:
        st.info("Upload test data (CSV/JSON) on the right to populate the dashboard.")
    else:
        df_view = df.copy()
        df_view["is_breached"] = df_view.apply(_is_breached, axis=1)
        df_view["is_stuck"] = df_view.apply(_is_stuck, axis=1)
        df_view = df_view.sort_values(
            by=["is_breached", "is_stuck", "breach_hours_num"],
            ascending=[False, False, False],
        )

        for _, row in df_view.iterrows():
            tid = row.get("ticket_id", "UNKNOWN")
            ttype = row.get("type", "UNKNOWN")
            sla_status = row.get("sla_status", "-")
            breach_hours = row.get("breach_hours") or (
                f"+{int(row['breach_hours_num'])}h" if pd.notna(row.get("breach_hours_num")) else ""
            )
            action = row.get("action", "MONITOR")

            header_cols = st.columns([1.4, 1.0, 1.2, 1.0, 0.6])

            # (1) ticket expander uses ticket id label (clickable)
            with header_cols[0]:
                pass
            with header_cols[1]:
                st.markdown(f"**{ttype}**")
            with header_cols[2]:
                if "BREACH" in str(sla_status).upper():
                    st.markdown(f"**:red[{sla_status}]**  \n{breach_hours}")
                else:
                    st.markdown(f"**{sla_status}**  \n{breach_hours}")
            with header_cols[3]:
                if str(action).upper() == "ESCALATE":
                    st.markdown(f"**:red[{action}]**")
                elif str(action).upper() == "AUTO_RETRY":
                    st.markdown(f"**:green[{action}]**")
                else:
                    st.markdown(f"**{action}**")
            with header_cols[4]:
                st.write("")

            with st.expander(f"{tid}", expanded=False):
                st.markdown(f"**Status**  \n{row.get('status','-')}")
                st.markdown(f"**Context**  \n{row.get('context','-')}")
                st.markdown(f"**Root cause**  \n{row.get('root_cause','-')}")
                st.markdown(f"**Impact**  \n{row.get('impact','-')}")
                st.markdown(f"**Recommended next steps**  \n{row.get('recommended_next_steps','-')}")
                vendor = row.get("vendor") or "-"
                st.markdown(f"**Vendor / system**  \n{vendor}")

            st.divider()

with right:
    st.markdown("### AI Agent Progress")
    st.markdown(
        "<div class='small-muted'>Self-learning is enabled: unknown scenarios trigger an external runbook lookup, memory update, and a re-run.</div>",
        unsafe_allow_html=True,
    )

    last_run = st.session_state.get("agent_last_run")
    next_run = st.session_state.get("agent_next_run")
    if last_run and next_run:
        st.caption(f"Last run: {last_run.strftime('%H:%M:%S')}  ·  Next run: {next_run.strftime('%H:%M:%S')}")
    else:
        st.caption("Upload data to get started.")

    uploaded = st.file_uploader("Upload test data (CSV/JSON)", type=["csv", "json"])

    if uploaded is not None:
        raw_df = _safe_read_upload(uploaded)
        df2 = _coerce_columns(raw_df) if raw_df is not None else None
        if isinstance(df2, pd.DataFrame) and not df2.empty:
            st.session_state["data_df"] = df2
            st.success(f"Loaded {len(df2)} tickets from {uploaded.name}")

    run_col, clear_col = st.columns([1.2, 0.8])

    with run_col:
        if st.button("Run agent on current data", use_container_width=True):
            df3 = st.session_state.get("data_df")
            simulate_agent(df3)

    with clear_col:
        if st.button("Clear logs", use_container_width=True):
            st.session_state["event_logs"] = []
            st.session_state["agent_progress"] = []
            st.session_state["agent_last_run"] = None
            st.session_state["agent_next_run"] = None
            st.toast("Logs cleared")

    # Progress checklist
    progress = st.session_state.get("agent_progress", [])
    if progress:
        st.markdown("".join([f"- ✅ {p}\n" for p in progress]))

    # Optional: Show learned scenarios (read-only)
    with st.expander("View learned scenarios (agent memory)", expanded=False):
        rules = st.session_state.get("learned_rules", [])
        st.caption(f"Memory size: {len(rules)} rules")
        for r in rules[-20:]:
            sid = r.get("scenario_id", "UNKNOWN")
            title = r.get("title", "")
            src = r.get("source", "")
            learned_at = r.get("learned_at", "")
            st.markdown(f"**{sid}** — {title}")
            st.markdown(f"<span class='small-muted'>Source: {src} · Learned: {learned_at}</span>", unsafe_allow_html=True)
            pats = r.get("patterns", []) or []
            if pats:
                st.code("\n".join([str(p) for p in pats]), language="text")

    st.markdown("### Agent Event Logs")
    logs = st.session_state.get("event_logs", [])

    if not logs:
        st.info("No events yet. Run the agent to generate logs.")
    else:
        st.markdown(
            "<div class='event-log'>"
            + "".join(
                [
                    f"<div class='event-line'>{html.escape(str(line)).replace(chr(10), '<br/>')}</div>"
                    for line in logs[-200:]
                ]
            )
            + "</div>",
            unsafe_allow_html=True,
        )
