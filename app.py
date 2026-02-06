# app.py
import json
import html
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import pandas as pd
import streamlit as st


# ----------------------------
# Time + logging helpers
# ----------------------------
def _now() -> datetime:
    return datetime.now()


def _now_str() -> str:
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(message: str) -> None:
    st.session_state.setdefault("event_logs", [])
    st.session_state["event_logs"].append(f"{_now_str()} — {message}")


def reset_simulation(clear_data: bool = False) -> None:
    st.session_state["event_logs"] = []
    st.session_state["agent_last_run"] = None
    st.session_state["agent_next_run"] = None
    st.session_state["agent_progress"] = []
    st.session_state["pending_vendor_fixes"] = {}
    st.session_state["agent_running"] = False
    # keep memory unless you want a hard reset
    # st.session_state["agent_memory"] = {}
    if clear_data:
        st.session_state["data_df"] = None
        st.session_state["data_fingerprint"] = None
        st.session_state["data_source_name"] = None


# ----------------------------
# Upload + data normalization
# ----------------------------
def _fingerprint_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _safe_read_upload(uploaded_file) -> Optional[pd.DataFrame]:
    if uploaded_file is None:
        return None

    name = uploaded_file.name.lower()
    try:
        if name.endswith(".csv"):
            return pd.read_csv(uploaded_file)
        if name.endswith(".json"):
            payload = json.load(uploaded_file)
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
    Canonical:
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
        "breach_hours": "breach_hours",
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

    def infer_action(row):
        # If already fixed, keep it fixed.
        if str(row.get("status", "")).strip().upper() == "FIXED":
            return "FIXED"
        if isinstance(row.get("action"), str) and row["action"].strip():
            return row["action"].strip().upper()
        if "BREACH" in str(row.get("sla_status", "")).upper():
            return "ESCALATE"
        if str(row.get("status", "")).strip().lower() in {"stuck", "blocked", "pending"}:
            return "AUTO_RETRY"
        return "MONITOR"

    df["action"] = df.apply(infer_action, axis=1)

    # Clean up "nan" strings
    for c in ["vendor", "context", "root_cause", "impact", "recommended_next_steps"]:
        df[c] = df[c].replace("nan", None).replace("NAN", None)

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


def update_ticket(df: pd.DataFrame, ticket_id: str, updates: Dict[str, Any]) -> pd.DataFrame:
    """Update a single ticket row in the dataframe by ticket_id."""
    if df is None or df.empty:
        return df
    df = df.copy()
    mask = df["ticket_id"].astype(str) == str(ticket_id)
    if mask.any():
        for k, v in updates.items():
            if k in df.columns:
                df.loc[mask, k] = v
    return df


# ----------------------------
# "External system" knowledge simulation
# ----------------------------
def external_context_lookup(row: pd.Series) -> Optional[Dict[str, Any]]:
    """
    Simulates querying an external system/KB. Returns a learned policy dict or None.
    This is where you make the "self-learning" story deterministic for test tickets.
    """
    # You can make this rule-based on context/root_cause/type/vendor
    ttype = str(row.get("type", "")).upper()
    ctx = (row.get("context") or "")
    rca = (row.get("root_cause") or "")
    vendor = row.get("vendor") or "Vendor"

    blob = f"{ttype} {ctx} {rca}".lower()

    # Example unknown scenario -> learned policy:
    # PORTING stuck at NPDB with gateway timeout -> escalate to NPDB/vendor with diagnostics, wait response, apply fix.
    if ttype == "PORTING" and ("npdb" in blob) and ("timeout" in blob or "gateway" in blob):
        return {
            "scenario_id": "PORTING_NPDB_GATEWAY_TIMEOUT",
            "title": "Port-in stuck due to NPDB gateway timeout; escalate to NPDB/Vendor queue and attach diagnostics",
            "match": {
                "type": "PORTING",
                "contains_any": ["npdb", "timeout", "gateway"],
            },
            "steps": [
                "Pull NPDB gateway error metrics for last 60 minutes",
                "Attach timeout traces and correlation IDs",
                "Escalate to NPDB vendor queue with diagnostics",
                "Await vendor response and apply provided fix if available",
            ],
            "action": "ESCALATE",
            "vendor_queue": vendor,
            "fix_after_seconds": 5,
            "vendor_response_text": "NPDB vendor responded with gateway configuration fix applied",
        }

    return None


def memory_match(row: pd.Series, memory: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Match row against stored learned scenarios.
    This keeps it intentionally simple for the demo.
    """
    ttype = str(row.get("type", "")).upper()
    ctx = (row.get("context") or "")
    rca = (row.get("root_cause") or "")
    blob = f"{ttype} {ctx} {rca}".lower()

    for scenario_id, policy in memory.items():
        m = policy.get("match", {})
        if str(m.get("type", "")).upper() and str(m.get("type", "")).upper() != ttype:
            continue
        contains_any = [s.lower() for s in (m.get("contains_any") or [])]
        if contains_any and not any(term in blob for term in contains_any):
            continue
        return policy

    return None


# ----------------------------
# Agent simulation
# ----------------------------
def simulate_agent(df: pd.DataFrame) -> None:
    """
    Simulate an agent run:
      - detects breached tickets
      - for each breached ticket:
          - if already FIXED -> skip
          - try memory match
          - if none -> log unknown scenario, query external system, learn new policy if available
          - rerun evaluation with updated memory
          - execute: ESCALATE / AUTO_RETRY / MONITOR
      - if learned policy indicates a vendor response, schedule a pending fix (no sleep)
    """
    if df is None or df.empty:
        st.warning("No data loaded.")
        return

    memory = st.session_state.setdefault("agent_memory", {})
    st.session_state["agent_progress"] = []
    st.session_state["agent_running"] = True

    log_event("Agent run started: scanning ticket queue")

    st.session_state["agent_progress"].append("Checked SLA deadlines for all tickets")
    log_event("Checked SLA deadlines for all tickets")

    stuck_count = int(df.apply(_is_stuck, axis=1).sum())
    st.session_state["agent_progress"].append("Detected workflow stagnation")
    log_event("Detected workflow stagnation")

    if stuck_count:
        st.session_state["agent_progress"].append(f"Flagged {stuck_count} stuck tickets")
        log_event(f"Flagged {stuck_count} stuck tickets")

    breached_df = df[df.apply(_is_breached, axis=1)].copy()

    if breached_df.empty:
        st.session_state["agent_progress"].append("No breached tickets detected")
        log_event("No breached tickets detected; monitoring continues")
        st.session_state["agent_last_run"] = _now()
        st.session_state["agent_next_run"] = _now() + timedelta(minutes=30)
        log_event("Agent run completed")
        st.session_state["agent_running"] = False
        return

    for _, row in breached_df.iterrows():
        tid = row.get("ticket_id", "UNKNOWN")
        ttype = row.get("type", "UNKNOWN")
        vendor = row.get("vendor") or "Vendor"
        status_now = str(row.get("status", "")).strip().upper()

        if status_now == "FIXED":
            log_event(f"{tid}: Skipping — already FIXED")
            continue

        breach_hours = row.get("breach_hours") or (
            f"+{int(row['breach_hours_num'])}h" if pd.notna(row.get("breach_hours_num")) else "N/A"
        )

        diag = row.get("root_cause") or "No explicit root cause; correlating signals"
        st.session_state["agent_progress"].append(
            f"Diagnosed {tid}: {str(diag)[:70]}{'…' if len(str(diag)) > 70 else ''}"
        )
        log_event(f"{tid}: Diagnosing — {ttype} breached by {breach_hours}; analysing context/root cause")

        # 1) Try learned memory match
        policy = memory_match(row, memory)

        if policy is None:
            # 1) Unknown scenario detected
            log_event(f"{tid}: Unknown scenario detected (no specific playbook match)")
            # 2) Query external system
            log_event(f"{tid}: Querying external system for contextual knowledge")
            learned = external_context_lookup(row)

            if learned:
                # Learn new scenario into memory (no human intervention)
                memory[learned["scenario_id"]] = learned
                st.session_state["agent_memory"] = memory  # persist
                log_event(
                    f"{tid}: Learned new scenario → {learned['scenario_id']} ({learned.get('title','')})"
                )
                for step in learned.get("steps", []):
                    log_event(f"{tid}: Learned step added → {step}")
                # 3) Rerun evaluation with updated memory
                log_event(f"{tid}: Re-running ticket evaluation using updated memory")
                policy = memory_match(row, memory)
            else:
                log_event(f"{tid}: External system returned no matching playbook; falling back to generic policy")
                policy = {
                    "scenario_id": "GENERIC_SLA_BREACH",
                    "title": "Generic SLA breach escalation",
                    "action": "ESCALATE",
                    "vendor_queue": vendor,
                    "steps": ["Attach basic diagnostics", "Escalate to vendor/ops queue"],
                }

        # Policy selected
        action = str(policy.get("action", "ESCALATE")).upper()
        log_event(f"{tid}: Policy selected → {policy.get('scenario_id','UNKNOWN')} | action={action}")

        # Execute action
        if action == "ESCALATE":
            # Attach diagnostics + escalate
            log_event(f"{tid}: Preparing escalation to {vendor} with diagnostics attached")
            log_event(f"{tid}: Attaching traces/correlation IDs for {tid} and NPDB gateway errors")
            log_event(f"{tid}: Escalation message sent to {vendor}: Escalating {tid}: SLA breached ({breach_hours}). Diagnostics attached.")
            log_event(f"{tid}: Escalated to vendor system / ops queue for further action")

            # Update ticket's action immediately to ESCALATE (until fixed)
            df = st.session_state.get("data_df")
            df = update_ticket(df, tid, {"action": "ESCALATE"})
            st.session_state["data_df"] = df

            # If policy defines vendor response -> schedule an auto-fix
            fix_after = policy.get("fix_after_seconds")
            if isinstance(fix_after, (int, float)) and fix_after > 0:
                log_event(f"{tid}: Awaiting NPDB vendor response (simulated wait)")
                st.session_state.setdefault("pending_vendor_fixes", {})
                st.session_state["pending_vendor_fixes"][tid] = {
                    "execute_at": _now() + timedelta(seconds=float(fix_after)),
                    "vendor": vendor,
                    "response_text": policy.get("vendor_response_text", "Vendor responded with fix"),
                }

        elif action == "AUTO_RETRY":
            log_event(f"{tid}: Triggered auto-retry workflow")
            log_event(f"{tid}: Auto-retry did not resolve within expected window; escalating to ops queue")
            df = st.session_state.get("data_df")
            df = update_ticket(df, tid, {"action": "AUTO_RETRY"})
            st.session_state["data_df"] = df
        else:
            log_event(f"{tid}: Monitoring — no immediate action required")
            df = st.session_state.get("data_df")
            df = update_ticket(df, tid, {"action": "MONITOR"})
            st.session_state["data_df"] = df

    st.session_state["agent_last_run"] = _now()
    st.session_state["agent_next_run"] = _now() + timedelta(minutes=30)
    log_event("Agent run completed: actions logged for breached tickets")
    st.session_state["agent_running"] = False


def process_pending_vendor_fixes() -> None:
    """
    Runs on every rerun. If a pending vendor fix's execute_at has passed:
      - log vendor response
      - apply fix
      - update ticket status + action to FIXED
    """
    pending = st.session_state.get("pending_vendor_fixes", {}) or {}
    if not pending:
        return

    now = _now()
    to_remove = []

    for tid, job in pending.items():
        execute_at = job.get("execute_at")
        if execute_at and now >= execute_at:
            log_event(f"{tid}: Received response from NPDB vendor: {job.get('response_text','Vendor responded with fix')}")
            log_event(f"{tid}: Applying vendor-provided fix for {tid} (e.g., gateway retry/backoff config)")

            df = st.session_state.get("data_df")
            df = update_ticket(df, tid, {
                "status": "FIXED",
                "sla_status": "RESOLVED",
                "action": "FIXED",
            })
            st.session_state["data_df"] = df

            log_event(f"{tid}: Ticket updated to FIXED in system (status changed, monitoring resumed)")
            to_remove.append(tid)

    for tid in to_remove:
        pending.pop(tid, None)

    st.session_state["pending_vendor_fixes"] = pending


# ----------------------------
# Page config + state defaults
# ----------------------------
st.set_page_config(page_title="Daily Ticket Dashboard", layout="wide")

st.session_state.setdefault("event_logs", [])
st.session_state.setdefault("agent_progress", [])
st.session_state.setdefault("agent_last_run", None)
st.session_state.setdefault("agent_next_run", None)
st.session_state.setdefault("data_df", None)

# Prevent uploaded data from overwriting in-memory updates on reruns
st.session_state.setdefault("data_fingerprint", None)
st.session_state.setdefault("data_source_name", None)

# Self-learning memory store
st.session_state.setdefault("agent_memory", {})

# Pending vendor responses / fixes
st.session_state.setdefault("pending_vendor_fixes", {})

# Agent run flag
st.session_state.setdefault("agent_running", False)

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
      .pill-fixed {display:inline-block; padding:2px 8px; border-radius:999px; background:#e7f7ee; color:#157347; font-weight:600;}
      .pill-esc {display:inline-block; padding:2px 8px; border-radius:999px; background:#ffe9e9; color:#b02a37; font-weight:600;}
      .pill-mon {display:inline-block; padding:2px 8px; border-radius:999px; background:#eef2ff; color:#3730a3; font-weight:600;}
      .pill-retry {display:inline-block; padding:2px 8px; border-radius:999px; background:#e8f5ff; color:#0b5ed7; font-weight:600;}
    </style>
    """,
    unsafe_allow_html=True,
)

# IMPORTANT: process pending fixes before rendering UI so the queue reflects FIXED immediately
process_pending_vendor_fixes()

# Auto-refresh while pending vendor fixes exist (so FIXED happens without clicks)
if st.session_state.get("pending_vendor_fixes"):
    st.autorefresh(interval=1000, key="agent_autorefresh")


# ----------------------------
# Layout
# ----------------------------
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

        # Sort: breached first, then stuck, then higher breach first
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

            status_val = str(row.get("status", "")).strip().upper()
            action_raw = str(row.get("action", "MONITOR")).strip().upper()
            action_val = "FIXED" if status_val == "FIXED" else action_raw

            header_cols = st.columns([1.4, 1.0, 1.2, 1.0, 0.6])

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
                if action_val == "FIXED":
                    st.markdown("<span class='pill-fixed'>FIXED</span>", unsafe_allow_html=True)
                elif action_val == "ESCALATE":
                    st.markdown("<span class='pill-esc'>ESCALATE</span>", unsafe_allow_html=True)
                elif action_val == "AUTO_RETRY":
                    st.markdown("<span class='pill-retry'>AUTO_RETRY</span>", unsafe_allow_html=True)
                else:
                    st.markdown("<span class='pill-mon'>MONITOR</span>", unsafe_allow_html=True)

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
    st.markdown("<div class='small-muted'>Uploads drive the run. No manual ticket processing.</div>", unsafe_allow_html=True)

    last_run = st.session_state.get("agent_last_run")
    next_run = st.session_state.get("agent_next_run")
    if last_run and next_run:
        st.caption(f"Last run: {last_run.strftime('%H:%M:%S')}  ·  Next run: {next_run.strftime('%H:%M:%S')}")
    else:
        st.caption("Upload data to get started.")

    uploaded = st.file_uploader("Upload test data (CSV/JSON)", type=["csv", "json"])

    # Upload fingerprinting:
    # - If file content changed -> load it
    # - If rerun with same file -> do NOT reload (preserves FIXED updates)
    if uploaded is not None:
        raw_bytes = uploaded.getvalue()
        fp = _fingerprint_bytes(raw_bytes)
        if st.session_state.get("data_fingerprint") != fp:
            # Fresh load only when file changes
            # Rebuild file-like object: use bytes and pandas/json loaders
            name = uploaded.name.lower()
            try:
                if name.endswith(".csv"):
                    raw_df = pd.read_csv(pd.io.common.BytesIO(raw_bytes))
                else:
                    payload = json.loads(raw_bytes.decode("utf-8"))
                    if isinstance(payload, dict) and "tickets" in payload:
                        payload = payload["tickets"]
                    raw_df = pd.DataFrame(payload)

                df_loaded = _coerce_columns(raw_df)
                if isinstance(df_loaded, pd.DataFrame) and not df_loaded.empty:
                    st.session_state["data_df"] = df_loaded
                    st.session_state["data_fingerprint"] = fp
                    st.session_state["data_source_name"] = uploaded.name
                    st.success(f"Loaded {len(df_loaded)} tickets from {uploaded.name}")
            except Exception as e:
                st.error(f"Could not read file: {e}")
        else:
            st.caption(f"Using current loaded data from {st.session_state.get('data_source_name', uploaded.name)}")

    run_col, clear_col = st.columns([1.2, 0.8])

    with run_col:
        if st.button("Run agent on current data", use_container_width=True):
            df_run = st.session_state.get("data_df")
            simulate_agent(df_run)

    with clear_col:
        if st.button("Clear logs", use_container_width=True):
            # Clear logs + pending fixes; keep data so you can rerun quickly
            st.session_state["event_logs"] = []
            st.session_state["agent_progress"] = []
            st.session_state["agent_last_run"] = None
            st.session_state["agent_next_run"] = None
            st.session_state["pending_vendor_fixes"] = {}
            st.toast("Logs cleared")

    progress = st.session_state.get("agent_progress", [])
    if progress:
        st.markdown("".join([f"- ✅ {p}\n" for p in progress]))

    # Optional: Show learned scenarios (small, for demo)
    mem = st.session_state.get("agent_memory", {})
    if mem:
        st.markdown("**Learned scenarios in memory**")
        for sid, pol in mem.items():
            st.caption(f"• {sid}: {pol.get('title','')}")

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
                    for line in logs[-250:]
                ]
            )
            + "</div>",
            unsafe_allow_html=True,
        )
