import json
import html
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import pandas as pd
import streamlit as st


# ----------------------------
# UI helpers
# ----------------------------
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(message: str) -> None:
    st.session_state.setdefault("event_logs", [])
    st.session_state["event_logs"].append(f"{_now_str()} — {message}")


def reset_simulation(clear_memory: bool = True) -> None:
    st.session_state["event_logs"] = []
    st.session_state["agent_last_run"] = None
    st.session_state["agent_next_run"] = None
    st.session_state["agent_progress"] = []
    if clear_memory:
        st.session_state["agent_memory"] = {}


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
        # If already fixed, ensure action reflects FIXED (prevents re-overwrite)
        if str(row.get("status", "")).strip().upper() == "FIXED":
            return "FIXED"

        a = row.get("action")
        if isinstance(a, str) and a.strip():
            return a.strip().upper()

        if "BREACH" in str(row.get("sla_status", "")).upper():
            return "ESCALATE"
        if str(row.get("status", "")).strip().lower() in {"stuck", "blocked"}:
            return "AUTO_RETRY"
        return "MONITOR"

    df["action"] = df.apply(infer_action, axis=1)

    return df


def _is_breached(row: pd.Series) -> bool:
    sla = str(row.get("sla_status", "")).upper()
    status = str(row.get("status", "")).upper().strip()
    if status == "FIXED" or "RESOLVED" in sla:
        return False
    return "BREACH" in sla


def _is_stuck(row: pd.Series) -> bool:
    return str(row.get("status", "")).strip().lower() in {"stuck", "blocked", "pending"}


def _ticket_types_summary(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "-"
    counts = df["type"].fillna("UNKNOWN").replace("NAN", "UNKNOWN").value_counts()
    return " · ".join([f"{k}:{v}" for k, v in counts.items()])


# ----------------------------
# "External system" contextual knowledge (simulated)
# ----------------------------
def external_context_lookup(ticket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Simulate querying an external system for playbooks/KB entries.
    Returns a playbook dict or None.
    """
    ttype = str(ticket.get("type", "")).upper()
    context = str(ticket.get("context", "")).lower()
    root_cause = str(ticket.get("root_cause", "")).lower()

    # Demo learning scenario: PORTING + NPDB gateway timeout signals
    if ttype == "PORTING" and (
        "npdb" in context
        or "gateway timeout" in context
        or "timeout" in root_cause
        or "gateway" in root_cause
    ):
        return {
            "scenario_id": "PORTING_NPDB_GATEWAY_TIMEOUT",
            "title": "Port-in stuck due to NPDB gateway timeout; escalate to NPDB/Vendor queue and attach diagnostics",
            "keywords": ["npdb", "gateway timeout", "timeout", "gateway"],
            "action": "ESCALATE",
            "steps": [
                "Pull NPDB gateway error metrics for last 60 minutes",
                "Attach timeout traces and correlation IDs",
                "Escalate to NPDB vendor queue with diagnostics",
                "Await vendor response and apply provided fix if available",
            ],
        }

    return None


def match_memory(ticket: Dict[str, Any], memory: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Very simple matching: keyword presence in context/root_cause.
    """
    context = (str(ticket.get("context", "")) + " " + str(ticket.get("root_cause", ""))).lower()
    for _, playbook in memory.items():
        kws = [k.lower() for k in playbook.get("keywords", [])]
        if any(k in context for k in kws):
            return playbook
    return None


# ----------------------------
# Agent simulation
# ----------------------------
def simulate_agent() -> None:
    """
    Simulate an agent run:
      - detect breached tickets and next steps required from data
      - self-learning loop:
          1) unknown scenario detected & logged
          2) query external system -> learn new scenario -> store to memory
          3) re-run evaluation -> escalate with diagnostics
          4) receive response after 5s -> execute fix -> update ticket to FIXED
      - ensure escalation logs include "message sent" and "escalated"
      - ensure FIXED reflects in queue (status + action)
    """
    df_main = st.session_state.get("data_df")
    if not isinstance(df_main, pd.DataFrame) or df_main.empty:
        st.warning("No data loaded.")
        return

    st.session_state.setdefault("agent_memory", {})
    memory = st.session_state["agent_memory"]

    st.session_state["agent_progress"] = []
    log_event("Agent run started: scanning ticket queue")

    st.session_state["agent_progress"].append("Checked SLA deadlines for all tickets")
    log_event("Checked SLA deadlines for all tickets")

    stuck_count = int(df_main.apply(_is_stuck, axis=1).sum())
    st.session_state["agent_progress"].append("Detected workflow stagnation")
    log_event("Detected workflow stagnation")
    if stuck_count:
        st.session_state["agent_progress"].append(f"Flagged {stuck_count} stuck tickets")
        log_event(f"Flagged {stuck_count} stuck tickets")

    breached_mask = df_main.apply(_is_breached, axis=1)
    breached_idxs = df_main.index[breached_mask].tolist()

    if not breached_idxs:
        st.session_state["agent_progress"].append("No breached tickets detected")
        log_event("No breached tickets detected; monitoring continues")
        st.session_state["agent_last_run"] = datetime.now()
        st.session_state["agent_next_run"] = datetime.now() + timedelta(minutes=30)
        log_event("Agent run completed")
        return

    # Process breached tickets (in-place updates to session df)
    for idx in breached_idxs:
        row = df_main.loc[idx]
        tid = str(row.get("ticket_id", "UNKNOWN"))
        ttype = str(row.get("type", "UNKNOWN"))
        vendor = row.get("vendor") or "Vendor"
        breach_hours = row.get("breach_hours") or (
            f"+{int(row['breach_hours_num'])}h" if pd.notna(row.get("breach_hours_num")) else "N/A"
        )

        # Lightweight diagnosis log
        diag = row.get("root_cause") or "No explicit root cause; correlating signals"
        st.session_state["agent_progress"].append(
            f"Diagnosed {tid}: {str(diag)[:70]}{'…' if len(str(diag)) > 70 else ''}"
        )
        log_event(f"{tid}: Diagnosing — {ttype} breached by {breach_hours}; analysing context/root cause")

        ticket_obj = {
            "ticket_id": tid,
            "type": ttype,
            "vendor": vendor,
            "context": row.get("context", ""),
            "root_cause": row.get("root_cause", ""),
            "impact": row.get("impact", ""),
            "sla_status": row.get("sla_status", ""),
            "status": row.get("status", ""),
        }

        # 1) Try match existing memory
        learned = match_memory(ticket_obj, memory)

        # If no memory match -> unknown scenario, attempt external lookup + learn
        if learned is None:
            log_event(f"{tid}: Unknown scenario detected (no specific playbook match)")
            log_event(f"{tid}: Querying external system for contextual knowledge")

            external_playbook = external_context_lookup(ticket_obj)
            if external_playbook:
                # 2) Learn and store
                memory[external_playbook["scenario_id"]] = external_playbook
                st.session_state["agent_memory"] = memory

                log_event(
                    f"{tid}: Learned new scenario → {external_playbook['scenario_id']} "
                    f"({external_playbook['title']})"
                )
                for step in external_playbook.get("steps", []):
                    log_event(f"{tid}: Learned step added → {step}")

                # 3) Re-run evaluation using updated memory
                log_event(f"{tid}: Re-running ticket evaluation using updated memory")
                learned = match_memory(ticket_obj, memory)
            else:
                log_event(f"{tid}: External system returned no matching playbook; falling back to generic policy")

        # Decide policy/action
        if learned is None:
            policy_id = "GENERIC_SLA_BREACH"
            action = "ESCALATE"
            policy_title = "Generic SLA breach escalation"
        else:
            policy_id = learned["scenario_id"]
            action = learned.get("action", "ESCALATE")
            policy_title = learned.get("title", "")

        log_event(f"{tid}: Policy selected → {policy_id} | action={action}")

        # Execute (with diagnostics attached)
        if action == "ESCALATE":
            log_event(f"{tid}: Preparing escalation to {vendor} with diagnostics attached")
            log_event(f"{tid}: Attaching traces/correlation IDs for {tid} and NPDB gateway errors")
            msg = f"Escalating {tid}: SLA breached ({breach_hours}). Diagnostics attached."
            log_event(f"{tid}: Escalation message sent to {vendor}: {msg}")
            log_event(f"{tid}: Escalated to vendor system / ops queue for further action")

            # Self-learning demo resolution: only for the learned NPDB scenario (or you can make it ticket-id specific)
            if policy_id == "PORTING_NPDB_GATEWAY_TIMEOUT":
                log_event(f"{tid}: Awaiting NPDB vendor response (simulated wait)")
                time.sleep(5)  # simulate async external response

                log_event(
                    f"{tid}: Received response from NPDB vendor: "
                    f"NPDB vendor responded with gateway configuration fix applied"
                )
                log_event(f"{tid}: Applying vendor-provided fix for {tid} (e.g., gateway retry/backoff config)")

                # IMPORTANT: update status + SLA + action so ticket queue reflects FIXED
                df_main.at[idx, "status"] = "FIXED"
                df_main.at[idx, "sla_status"] = "RESOLVED"
                df_main.at[idx, "action"] = "FIXED"
                if "recommended_next_steps" in df_main.columns:
                    df_main.at[idx, "recommended_next_steps"] = "Monitoring (post-fix)"

                st.session_state["data_df"] = df_main
                log_event(f"{tid}: Ticket updated to FIXED in system (status changed, monitoring resumed)")

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
st.session_state.setdefault("agent_memory", {})

# CSS
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

            status_val = str(row.get("status", "")).upper().strip()
            action_val = str(row.get("action", "MONITOR")).upper().strip()

            # Belt + suspenders:
            # Even if action didn't update, show FIXED when status is FIXED.
            display_action = "FIXED" if status_val == "FIXED" else action_val

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
                if display_action == "ESCALATE":
                    st.markdown(f"**:red[{display_action}]**")
                elif display_action == "AUTO_RETRY":
                    st.markdown(f"**:green[{display_action}]**")
                elif display_action == "FIXED":
                    st.markdown(f"**{display_action}**")
                else:
                    st.markdown(f"**{display_action}**")
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
        "<div class='small-muted'>Uploads drive the run. No manual ticket processing.</div>",
        unsafe_allow_html=True,
    )

    last_run = st.session_state.get("agent_last_run")
    next_run = st.session_state.get("agent_next_run")
    if last_run and next_run:
        st.caption(
            f"Last run: {last_run.strftime('%H:%M:%S')}  ·  Next run: {next_run.strftime('%H:%M:%S')}"
        )
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
            simulate_agent()

    with clear_col:
        if st.button("Clear logs", use_container_width=True):
            reset_simulation(clear_memory=True)
            st.toast("Logs cleared (memory reset)")

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
            + "".join(
                [
                    f"<div class='event-line'>{html.escape(str(line)).replace(chr(10), '<br/>')}</div>"
                    for line in logs[-200:]
                ]
            )
            + "</div>",
            unsafe_allow_html=True,
        )
