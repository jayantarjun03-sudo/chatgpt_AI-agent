import json
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


def reset_simulation() -> None:
    st.session_state["event_logs"] = []
    st.session_state["agent_last_run"] = None
    st.session_state["agent_next_run"] = None
    st.session_state["agent_progress"] = []
    st.session_state["data_df"] = None


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

    # Common alternative column names -> canonical
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

    # Add missing columns to avoid KeyError later
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

    # Clean up
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
        except:
            return None
    df["breach_hours_num"] = df["breach_hours"].apply(parse_breach)

    # Default action if not provided
    def infer_action(row):
        if isinstance(row.get("action"), str) and row["action"].strip():
            return row["action"].strip().upper()
        if "BREACH" in str(row.get("sla_status", "")).upper():
            return "ESCALATE"
        if str(row.get("status", "")).lower() in {"stuck", "blocked"}:
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
    # Compact, but avoid truncation by wrapping in markdown with line breaks.
    return " · ".join([f"{k}:{v}" for k, v in counts.items()])


# ----------------------------
# Agent simulation
# ----------------------------
def simulate_agent(df: pd.DataFrame) -> None:
    """
    Simulate an agent run:
      - detect breached tickets
      - decide & execute action
      - log steps with timestamps
      - ensure escalation logs include "message sent" and "escalated"
    """
    if df is None or df.empty:
        st.warning("No data loaded.")
        return

    breached_df = df[df.apply(_is_breached, axis=1)].copy()

    st.session_state["agent_progress"] = []
    log_event("Agent run started: scanning ticket queue")

    # Generic checks
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
        vendor = row.get("vendor") or "Vendor"
        breach_hours = row.get("breach_hours") or (
            f"+{int(row['breach_hours_num'])}h" if pd.notna(row.get("breach_hours_num")) else "N/A"
        )
        action = str(row.get("action", "ESCALATE")).upper()

        # Diagnose (lightweight)
        diag = row.get("root_cause") or "No explicit root cause; correlating signals"
        st.session_state["agent_progress"].append(f"Diagnosed {tid}: {diag[:70]}{'…' if len(str(diag))>70 else ''}")
        log_event(f"{tid}: Diagnosing — {ttype} breached by {breach_hours}; analysing context/root cause")

        # Decision
        log_event(f"Agent decision for {tid}: {action}")

        # Execute
        if action == "ESCALATE":
            # Required: show message sent and escalated
            log_event(f"{tid}: Escalation message sent to {vendor}")
            log_event(f"{tid}: Escalated to vendor system / ops queue for further action")
        elif action == "AUTO_RETRY":
            log_event(f"{tid}: Triggered auto-retry workflow")
            log_event(f"{tid}: Auto-retry did not resolve within expected window; escalating to ops queue")
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

    # FIX #1: Ticket types shouldn't truncate. Use wrapped markdown, not st.metric.
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
        df_view = df_view.sort_values(by=["is_breached", "is_stuck", "breach_hours_num"], ascending=[False, False, False])

        # FIX #2: Make ticket number clickable to reveal details (expanders), no cramped dropdown column.
        for _, row in df_view.iterrows():
            tid = row.get("ticket_id", "UNKNOWN")
            ttype = row.get("type", "UNKNOWN")
            sla_status = row.get("sla_status", "-")
            breach_hours = row.get("breach_hours") or (
                f"+{int(row['breach_hours_num'])}h" if pd.notna(row.get("breach_hours_num")) else ""
            )
            action = row.get("action", "MONITOR")

            # One-line header row
            header_cols = st.columns([1.4, 1.0, 1.2, 1.0, 0.6])
            with header_cols[0]:
                # Ticket ID is the clickable control: expander label.
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
                # placeholder for spacing / potential icons
                st.write("")

            with st.expander(f"{tid}", expanded=False):
                # Details panel
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

    if uploaded is not None:
        raw_df = _safe_read_upload(uploaded)
        df = _coerce_columns(raw_df) if raw_df is not None else None
        if isinstance(df, pd.DataFrame) and not df.empty:
            st.session_state["data_df"] = df
            st.success(f"Loaded {len(df)} tickets from {uploaded.name}")

    run_col, clear_col = st.columns([1.2, 0.8])

    with run_col:
        if st.button("Run agent on current data", use_container_width=True):
            df = st.session_state.get("data_df")
            simulate_agent(df)

    # FIX #3: Clear logs button resets simulation state
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

    st.markdown("### Agent Event Logs")
    logs = st.session_state.get("event_logs", [])

    if not logs:
        st.info("No events yet. Run the agent to generate logs.")
    else:
        st.markdown(
            "<div class='event-log'>"
            + "".join([f"<div class='event-line'>{st._utils.escape_markdown(line).replace(chr(10), '<br/>')}</div>" for line in logs[-200:]])
            + "</div>",
            unsafe_allow_html=True,
        )
