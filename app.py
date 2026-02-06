import json
import html
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import pandas as pd
import streamlit as st


# ----------------------------
# UI helpers
# ----------------------------
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(message: str) -> None:
    """Append a timestamped message to the session event log."""
    st.session_state.setdefault("event_logs", [])
    st.session_state["event_logs"].append(f"{_now_str()} — {message}")


def reset_simulation(clear_data: bool = False) -> None:
    """Reset logs/progress/run times. Optionally clear loaded data and learned memory."""
    st.session_state["event_logs"] = []
    st.session_state["agent_progress"] = []
    st.session_state["agent_last_run"] = None
    st.session_state["agent_next_run"] = None
    if clear_data:
        st.session_state["data_df"] = None
        st.session_state["agent_memory"] = _default_agent_memory()
        st.session_state["external_kb"] = _default_external_kb()


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


def _is_breached(row: pd.Series) -> bool:
    return "BREACH" in str(row.get("sla_status", "")).upper()


def _is_stuck(row: pd.Series) -> bool:
    return str(row.get("status", "")).strip().lower() in {"stuck", "blocked", "pending"}


def _ticket_types_summary(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "-"
    counts = df["type"].fillna("UNKNOWN").replace("NAN", "UNKNOWN").value_counts()
    return " · ".join([f"{k}:{v}" for k, v in counts.items()])


def _row_text(row: pd.Series) -> str:
    """Text blob used for scenario matching."""
    parts = [
        str(row.get("type", "")),
        str(row.get("context", "")),
        str(row.get("root_cause", "")),
        str(row.get("impact", "")),
        str(row.get("recommended_next_steps", "")),
        str(row.get("status", "")),
        str(row.get("sla_status", "")),
    ]
    return " ".join([p for p in parts if p and p.lower() != "nan"]).lower()


# ----------------------------
# Self-learning: memory + external KB (simulated)
# ----------------------------
def _default_agent_memory() -> List[Dict[str, Any]]:
    """
    Agent long-term memory of scenarios (policies).
    priority: higher wins. Keep GENERIC_SLA_BREACH as fallback (priority=0).
    """
    return [
        {
            "id": "GENERIC_SLA_BREACH",
            "desc": "Generic SLA breach escalation (fallback)",
            "match": {"sla_status_contains": ["BREACH"]},
            "action": "ESCALATE",
            "priority": 0,
            "source": "builtin",
        },
        {
            "id": "WORKFLOW_STUCK_AUTORETRY",
            "desc": "Workflow stuck/blocked → attempt auto-retry, then escalate if needed",
            "match": {"status_in": ["STUCK", "BLOCKED", "PENDING"]},
            "action": "AUTO_RETRY",
            "priority": 10,
            "source": "builtin",
        },
    ]


def _default_external_kb() -> Dict[str, Dict[str, Any]]:
    """
    Simulated external system knowledge base.
    Keys are scenario IDs. Each entry yields a new learned memory policy.
    """
    return {
        # Unknown scenario example you want to demo:
        # PORTING tickets where context/root cause mentions NPDB gateway timeout.
        "PORTING_NPDB_GATEWAY_TIMEOUT": {
            "desc": "Port-in stuck due to NPDB gateway timeout; escalate to NPDB/Vendor queue and attach diagnostics",
            "match": {
                "type_is": ["PORTING"],
                "contains_any": ["npdb", "gateway", "timeout"],
            },
            "action": "ESCALATE",
            "priority": 100,
            "source": "external_kb",
            # Optional extra log hints (what agent 'learned' to do)
            "learned_steps": [
                "Pull NPDB gateway error metrics for last 60 minutes",
                "Attach timeout traces and correlation IDs",
                "Escalate to NPDB vendor queue with diagnostics",
            ],
        },
        # Another example (optional)
        "PAYMENTS_3DS_PROVIDER_DEGRADED": {
            "desc": "Payment failures due to 3DS provider degradation; escalate to provider + enable fallback routing",
            "match": {
                "type_is": ["PAYMENTS"],
                "contains_any": ["3ds", "acs", "provider", "degraded", "timeout"],
            },
            "action": "ESCALATE",
            "priority": 90,
            "source": "external_kb",
            "learned_steps": [
                "Check 3DS provider status dashboard",
                "Switch traffic to fallback route (if configured)",
                "Escalate to provider with failure rates + timestamps",
            ],
        },
    }


def _memory_has_scenario(scenario_id: str) -> bool:
    mem = st.session_state.get("agent_memory", [])
    return any(m.get("id") == scenario_id for m in mem)


def match_scenario(row: pd.Series, memory: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Return best matching scenario (highest priority).
    Supports:
      - type_is: list[str]
      - contains_any: list[str] (in row text blob)
      - status_in: list[str]
      - sla_status_contains: list[str]
    """
    text = _row_text(row)
    sla = str(row.get("sla_status", "")).upper()
    status = str(row.get("status", "")).upper().strip()
    ttype = str(row.get("type", "")).upper().strip()

    candidates = []
    for s in memory:
        m = s.get("match", {}) or {}
        ok = True

        if "type_is" in m:
            ok = ok and ttype in set([x.upper() for x in m["type_is"]])

        if "contains_any" in m:
            ok = ok and any(k.lower() in text for k in m["contains_any"])

        if "status_in" in m:
            ok = ok and status in set([x.upper() for x in m["status_in"]])

        if "sla_status_contains" in m:
            ok = ok and any(x.upper() in sla for x in m["sla_status_contains"])

        if ok:
            candidates.append(s)

    if not candidates:
        return None

    candidates.sort(key=lambda x: x.get("priority", 0), reverse=True)
    return candidates[0]


def external_kb_lookup(row: pd.Series) -> Optional[Dict[str, Any]]:
    """
    Simulate agent querying an external system for contextual knowledge
    to resolve an unknown scenario. Returns a NEW memory policy (scenario) if found.
    """
    text = _row_text(row)
    ttype = str(row.get("type", "")).upper().strip()

    # Heuristic mapping to external KB scenario IDs
    if ttype == "PORTING" and all(k in text for k in ["npdb", "timeout"]):
        scenario_id = "PORTING_NPDB_GATEWAY_TIMEOUT"
    elif ttype == "PAYMENTS" and ("3ds" in text or "acs" in text) and ("degrad" in text or "timeout" in text):
        scenario_id = "PAYMENTS_3DS_PROVIDER_DEGRADED"
    else:
        scenario_id = None

    if not scenario_id:
        return None

    if _memory_has_scenario(scenario_id):
        # Already learned earlier
        return None

    kb = st.session_state.get("external_kb", {})
    learned = kb.get(scenario_id)
    if not learned:
        return None

    # Package into memory schema
    return {
        "id": scenario_id,
        "desc": learned.get("desc", ""),
        "match": learned.get("match", {}),
        "action": learned.get("action", "ESCALATE"),
        "priority": int(learned.get("priority", 50)),
        "source": learned.get("source", "external_kb"),
        "learned_steps": learned.get("learned_steps", []),
        "learned_at": _now_str(),
    }


# ----------------------------
# Agent simulation
# ----------------------------
def simulate_agent(df: pd.DataFrame) -> None:
    """
    Simulate an agent run:
      - detect breached tickets
      - self-learning flow:
          1) unknown scenario detected → log
          2) query external KB → add learned policy into memory → log
          3) rerun evaluation for the ticket → apply fix / escalate → log
      - ensure escalation logs include "message sent" and "escalated"
    """
    if df is None or df.empty:
        st.warning("No data loaded.")
        return

    memory = st.session_state.get("agent_memory", [])
    breached_df = df[df.apply(_is_breached, axis=1)].copy()

    st.session_state["agent_progress"] = []
    log_event("Agent run started: scanning ticket queue")

    st.session_state["agent_progress"].append("Checked SLA deadlines for all tickets")
    log_event("Checked SLA deadlines for all tickets")

    stuck_count = int(df.apply(_is_stuck, axis=1).sum())
    st.session_state["agent_progress"].append("Detected workflow stagnation")
    log_event("Detected workflow stagnation")

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

    for _, row in breached_df.iterrows():
        tid = row.get("ticket_id", "UNKNOWN")
        ttype = row.get("type", "UNKNOWN")
        vendor = row.get("vendor") or "OpsQueue"
        breach_hours = row.get("breach_hours") or (
            f"+{int(row['breach_hours_num'])}h" if pd.notna(row.get("breach_hours_num")) else "N/A"
        )

        # Diagnose (lightweight)
        diag = row.get("root_cause") or "No explicit root cause; correlating signals"
        st.session_state["agent_progress"].append(
            f"Diagnosed {tid}: {str(diag)[:70]}{'…' if len(str(diag)) > 70 else ''}"
        )
        log_event(f"{tid}: Diagnosing — {ttype} breached by {breach_hours}; analysing context/root cause")

        # 1) First-pass match
        matched = match_scenario(row, st.session_state["agent_memory"])

        # Treat "only generic fallback" as unknown (to force self-learning demo)
        if matched is None or matched.get("id") == "GENERIC_SLA_BREACH":
            log_event(f"{tid}: Unknown scenario detected (no specific playbook match)")

            # 2) Consult external KB (simulated)
            log_event(f"{tid}: Querying external system for contextual knowledge")
            learned = external_kb_lookup(row)

            if learned:
                # Store learned policy in agent memory
                st.session_state["agent_memory"].append(learned)
                log_event(
                    f"{tid}: Learned new scenario → {learned['id']} ({learned.get('desc','')})"
                )
                if learned.get("learned_steps"):
                    for step in learned["learned_steps"]:
                        log_event(f"{tid}: Learned step added → {step}")

                # 3) Rerun ticket evaluation
                log_event(f"{tid}: Re-running ticket evaluation using updated memory")
                matched = match_scenario(row, st.session_state["agent_memory"])
            else:
                log_event(f"{tid}: External system returned no matching playbook; falling back to generic policy")
                matched = match_scenario(row, st.session_state["agent_memory"])

        # Apply matched policy
        if matched:
            scenario_id = matched.get("id", "UNKNOWN_POLICY")
            log_event(f"{tid}: Policy selected → {scenario_id} | action={matched.get('action','ESCALATE')}")
            action = str(matched.get("action", "ESCALATE")).upper()
        else:
            # Should rarely happen (generic exists), but keep safe
            action = "ESCALATE"
            log_event(f"{tid}: No policy matched; defaulting to ESCALATE")

        # Execute
        if action == "ESCALATE":
            # Required: show message sent and escalated
            msg = f"Escalating {tid}: SLA breached (+{breach_hours}). Please review and prioritize resolution."
            log_event(f"{tid}: Escalation message sent to {vendor}: {msg}")
            log_event(f"{tid}: Escalated to vendor system / ops queue for further action")
        elif action == "AUTO_RETRY":
            log_event(f"{tid}: Triggered auto-retry workflow")
            log_event(f"{tid}: Auto-retry did not resolve within expected window; escalating to ops queue")
            msg = f"Escalating {tid}: auto-retry unsuccessful. Please investigate and resolve."
            log_event(f"{tid}: Escalation message sent to {vendor}: {msg}")
            log_event(f"{tid}: Escalated to vendor system / ops queue for further action")
        else:
            log_event(f"{tid}: Monitoring — no immediate action required")

    st.session_state["agent_last_run"] = datetime.now()
    st.session_state["agent_next_run"] = datetime.now() + timedelta(minutes=30)
    log_event("Agent run completed: actions logged for breached tickets")


# ----------------------------
# Page
# ----------------------------
st.set_page_config(page_title="Daily Ticket Dashboard", layout="wide")

# Session state defaults
st.session_state.setdefault("event_logs", [])
st.session_state.setdefault("agent_progress", [])
st.session_state.setdefault("agent_last_run", None)
st.session_state.setdefault("agent_next_run", None)
st.session_state.setdefault("data_df", None)
st.session_state.setdefault("agent_memory", _default_agent_memory())
st.session_state.setdefault("external_kb", _default_external_kb())

# Basic CSS for readable wrapping + event log panel
st.markdown(
    """
    <style>
      .metric-wrap {white-space: normal !important;}
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
        display:inline-block;
        padding: 2px 8px;
        border-radius: 999px;
        font-size: 11px;
        border: 1px solid rgba(0,0,0,0.12);
        margin-right: 6px;
      }
      .pill-hi {background: rgba(255, 0, 0, 0.06);}
      .pill-lo {background: rgba(0, 0, 0, 0.03);}
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

    # Ticket types wrap (avoid truncation)
    with m4:
        st.markdown("**Ticket Types**")
        st.markdown(f"<div class='ticket-types'>{_ticket_types_summary(df)}</div>", unsafe_allow_html=True)

    st.subheader("Ticket Queue")

    if not isinstance(df, pd.DataFrame) or df.empty:
        st.info("Upload test data (CSV/JSON) on the right to populate the dashboard.")
    else:
        # Sort: breached first, then stuck, then others
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

            header_cols = st.columns([1.6, 1.0, 1.2, 1.0, 0.4])
            with header_cols[0]:
                st.write("")  # expander label below is the click target
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

            # Click ticket number to reveal details
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
    st.markdown("### AI Agent Control")
    st.markdown("<div class='small-muted'>Uploads drive the run. No manual ticket processing.</div>", unsafe_allow_html=True)

    last_run = st.session_state.get("agent_last_run")
    next_run = st.session_state.get("agent_next_run")
    if last_run and next_run:
        st.caption(f"Last run: {last_run.strftime('%H:%M:%S')}  ·  Next run: {next_run.strftime('%H:%M:%S')}")
    else:
        st.caption("Upload data to get started.")

    uploaded = st.file_uploader("Upload test data (CSV/JSON)", type=["csv", "json"])

    if uploaded is not None:
        raw_df = _safe_read_upload(uploaded)
        df_loaded = _coerce_columns(raw_df) if raw_df is not None else None
        if isinstance(df_loaded, pd.DataFrame) and not df_loaded.empty:
            st.session_state["data_df"] = df_loaded
            st.success(f"Loaded {len(df_loaded)} tickets from {uploaded.name}")

    run_col, clear_col = st.columns([1.2, 0.8])

    with run_col:
        if st.button("Run agent on current data", use_container_width=True):
            df_run = st.session_state.get("data_df")
            simulate_agent(df_run)

    with clear_col:
        if st.button("Clear logs", use_container_width=True):
            reset_simulation(clear_data=False)
            st.toast("Logs cleared")

    # Progress checklist
    progress = st.session_state.get("agent_progress", [])
    if progress:
        st.markdown("".join([f"- ✅ {p}\n" for p in progress]))

    # Show learned memory (so users can SEE learning happened)
    st.markdown("### Agent Memory (Learned Scenarios)")
    mem = st.session_state.get("agent_memory", [])
    # Show highest priority first; keep compact
    mem_sorted = sorted(mem, key=lambda x: x.get("priority", 0), reverse=True)
    for s in mem_sorted[:8]:
        sid = s.get("id", "-")
        desc = s.get("desc", "")
        pr = s.get("priority", 0)
        src = s.get("source", "unknown")
        pill_cls = "pill-hi" if pr >= 50 else "pill-lo"
        st.markdown(
            f"<span class='pill {pill_cls}'>prio:{pr}</span>"
            f"<span class='pill'>{html.escape(str(src))}</span>"
            f"**{html.escape(str(sid))}**  \n{html.escape(str(desc))}",
            unsafe_allow_html=True,
        )
        st.write("")

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
