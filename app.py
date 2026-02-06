import json
import html
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import pandas as pd
import streamlit as st


# ============================
# Helpers
# ============================

def _now() -> datetime:
    return datetime.now()


def _now_str() -> str:
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(message: str) -> None:
    st.session_state.setdefault("event_logs", [])
    st.session_state["event_logs"].append(f"{_now_str()} — {message}")

def _default_agent_memory() -> Dict[str, Dict[str, Any]]:
    return {
        "GENERIC_SLA_BREACH": {
            "match_keywords": [],  # ❗ IMPORTANT: empty list
            "description": "Generic SLA breach escalation (fallback only)",
            "action": "ESCALATE",
            "steps": [
                "Attach basic diagnostics",
                "Escalate to vendor/ops queue",
            ],
        }
    }



def reset_simulation(clear_data: bool = False) -> None:
    st.session_state["event_logs"] = []
    st.session_state["agent_last_run"] = None
    st.session_state["agent_next_run"] = None
    st.session_state["agent_progress"] = []
    st.session_state["pending_vendor_responses"] = []
    st.session_state["agent_memory"] = _default_agent_memory()
    if clear_data:
        st.session_state["data_df"] = None
        st.session_state["last_upload_fingerprint"] = None


def _fingerprint_upload(uploaded_file) -> Optional[str]:
    if uploaded_file is None:
        return None
    return f"{uploaded_file.name}:{getattr(uploaded_file, 'size', 'na')}"


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


# ============================
# External knowledge (simulated)
# ============================

def _external_system_lookup(row: pd.Series) -> Optional[Dict[str, Any]]:
    context = (str(row.get("context", "")) + " " + str(row.get("root_cause", ""))).lower()
    ttype = str(row.get("type", "")).upper()

    if ttype == "PORTING" and ("npdb" in context) and ("timeout" in context or "gateway" in context):
        return {
            "policy_id": "PORTING_NPDB_GATEWAY_TIMEOUT",
            "description": "Port-in stuck due to NPDB gateway timeout; escalate to NPDB/Vendor queue and attach diagnostics",
            "action": "ESCALATE",
            "match_keywords": ["npdb", "timeout", "gateway"],
            "steps": [
                "Pull NPDB gateway error metrics for last 60 minutes",
                "Attach timeout traces and correlation IDs",
                "Escalate to NPDB vendor queue with diagnostics",
                "Await vendor response and apply provided fix if available",
            ],
            "vendor_response": "NPDB vendor responded with gateway configuration fix applied",
            "fix_action": "Applying vendor-provided fix (e.g., gateway retry/backoff config)",
        }

    return None


def _memory_match_policy(memory: Dict[str, Dict[str, Any]], row: pd.Series) -> Optional[str]:
    haystack = (
        str(row.get("context", "")) + " " +
        str(row.get("root_cause", "")) + " " +
        str(row.get("sla_status", ""))
    ).lower()

    for pid, pol in memory.items():
        if pid == "GENERIC_SLA_BREACH":
            continue  # ❗ fallback only

        kws = [k.lower() for k in pol.get("match_keywords", []) if k]
        if not kws:
            continue

        if any(k in haystack for k in kws):
            return pid

    return None




def _attach_diagnostics(tid: str) -> None:
    log_event(f"{tid}: Attaching traces/correlation IDs for {tid} and NPDB gateway errors")


def _schedule_vendor_response(ticket_id: str, vendor_response: str, fix_action: str, delay_seconds: int = 5) -> None:
    st.session_state.setdefault("pending_vendor_responses", [])
    st.session_state["pending_vendor_responses"].append(
        {
            "ticket_id": ticket_id,
            "execute_at": (_now() + timedelta(seconds=delay_seconds)).timestamp(),
            "vendor_response": vendor_response,
            "fix_action": fix_action,
        }
    )


def _mark_ticket_fixed(ticket_id: str) -> None:
    df = st.session_state.get("data_df")
    if not isinstance(df, pd.DataFrame) or df.empty:
        return

    mask = df["ticket_id"].astype(str) == str(ticket_id)
    if not mask.any():
        return

    df.loc[mask, "status"] = "FIXED"
    df.loc[mask, "sla_status"] = "RESOLVED"
    df.loc[mask, "action"] = "FIXED"
    df.loc[mask, "breach_hours"] = "0h"
    df.loc[mask, "breach_hours_num"] = 0.0

    st.session_state["data_df"] = df


def process_pending_vendor_responses() -> None:
    pending = st.session_state.get("pending_vendor_responses", [])
    if not pending:
        return

    now_ts = _now().timestamp()
    remaining = []
    for item in pending:
        if now_ts >= float(item["execute_at"]):
            tid = item["ticket_id"]
            log_event(f"{tid}: Received response from NPDB vendor: {item['vendor_response']}")
            log_event(f"{tid}: {item['fix_action']} for {tid}")
            _mark_ticket_fixed(tid)
            log_event(f"{tid}: Ticket updated to FIXED in system (status changed, monitoring resumed)")
        else:
            remaining.append(item)

    st.session_state["pending_vendor_responses"] = remaining


def _pending_countdown_seconds() -> Optional[int]:
    pending = st.session_state.get("pending_vendor_responses", [])
    if not pending:
        return None
    soonest = min(float(p["execute_at"]) for p in pending)
    return max(0, int(round(soonest - _now().timestamp())))


# ============================
# Agent simulation
# ============================

def simulate_agent(df: pd.DataFrame) -> None:
    if df is None or df.empty:
        st.warning("No data loaded.")
        return

    memory = st.session_state.get("agent_memory") or _default_agent_memory()

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

    breached_df = df[df.apply(_is_breached, axis=1)].copy()
    if breached_df.empty:
        st.session_state["agent_progress"].append("No breached tickets detected")
        log_event("No breached tickets detected; monitoring continues")
        st.session_state["agent_last_run"] = _now()
        st.session_state["agent_next_run"] = _now() + timedelta(minutes=30)
        log_event("Agent run completed")
        return

    for _, row in breached_df.iterrows():
        tid = row.get("ticket_id", "UNKNOWN")
        ttype = row.get("type", "UNKNOWN")
        vendor = row.get("vendor") or "Vendor"
        breach_hours = row.get("breach_hours") or (
            f"+{int(row['breach_hours_num'])}h" if pd.notna(row.get("breach_hours_num")) else "N/A"
        )

        if str(row.get("status", "")).strip().upper() == "FIXED":
            log_event(f"{tid}: Already FIXED; skipping")
            continue

        diag = row.get("root_cause") or "No explicit root cause; correlating signals"
        st.session_state["agent_progress"].append(
            f"Diagnosed {tid}: {str(diag)[:70]}{'…' if len(str(diag)) > 70 else ''}"
        )
        log_event(f"{tid}: Diagnosing — {ttype} breached by {breach_hours}; analysing context/root cause")

        matched_policy_id = _memory_match_policy(memory, row)

        if matched_policy_id is None:
            log_event(f"{tid}: Unknown scenario detected (no specific playbook match)")
            log_event(f"{tid}: Querying external system for contextual knowledge")

            learned = _external_system_lookup(row)
            if learned:
                pid = learned["policy_id"]
                log_event(f"{tid}: Learned new scenario → {pid} ({learned['description']})")
                for step in learned.get("steps", []):
                    log_event(f"{tid}: Learned step added → {step}")

                memory[pid] = learned
                st.session_state["agent_memory"] = memory

                log_event(f"{tid}: Re-running ticket evaluation using updated memory")
                matched_policy_id = pid
            else:
                log_event(f"{tid}: External system returned no matching playbook; falling back to generic policy")
                matched_policy_id = "GENERIC_SLA_BREACH"

        policy = memory.get(matched_policy_id, memory["GENERIC_SLA_BREACH"])
        action = str(policy.get("action", "ESCALATE")).upper()

        log_event(f"{tid}: Policy selected → {matched_policy_id} | action={action}")

        if action == "ESCALATE":
            log_event(f"{tid}: Preparing escalation to {vendor} with diagnostics attached")
            _attach_diagnostics(tid)

            log_event(
                f"{tid}: Escalation message sent to {vendor}: "
                f"Escalating {tid}: SLA breached ({breach_hours}). Diagnostics attached."
            )
            log_event(f"{tid}: Escalated to vendor system / ops queue for further action")

            if policy.get("vendor_response") and policy.get("fix_action"):
                log_event(f"{tid}: Awaiting NPDB vendor response (simulated wait)")
                _schedule_vendor_response(
                    ticket_id=tid,
                    vendor_response=str(policy["vendor_response"]),
                    fix_action=str(policy["fix_action"]),
                    delay_seconds=5,
                )

        elif action == "AUTO_RETRY":
            log_event(f"{tid}: Triggered auto-retry workflow")
            log_event(f"{tid}: Auto-retry did not resolve within expected window; escalating to ops queue")

        else:
            log_event(f"{tid}: Monitoring — no immediate action required")

    st.session_state["agent_last_run"] = _now()
    st.session_state["agent_next_run"] = _now() + timedelta(minutes=30)
    log_event("Agent run completed: actions logged for breached tickets")


# ============================
# Page
# ============================

st.set_page_config(page_title="Daily Ticket Dashboard", layout="wide")

st.session_state.setdefault("event_logs", [])
st.session_state.setdefault("agent_progress", [])
st.session_state.setdefault("agent_last_run", None)
st.session_state.setdefault("agent_next_run", None)
st.session_state.setdefault("data_df", None)
st.session_state.setdefault("pending_vendor_responses", [])
st.session_state.setdefault("agent_memory", _default_agent_memory())
st.session_state.setdefault("last_upload_fingerprint", None)
st.session_state.setdefault("auto_refresh_enabled", True)

# process pending vendor responses on every rerun
process_pending_vendor_responses()

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
        padding: 3px 9px;
        border-radius: 999px;
        font-size: 12px;
        border: 1px solid rgba(0,0,0,0.12);
        background: rgba(0,0,0,0.03);
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
        df_view["is_fixed"] = df_view["status"].astype(str).str.upper().eq("FIXED")

        df_view = df_view.sort_values(
            by=["is_fixed", "is_breached", "is_stuck", "breach_hours_num"],
            ascending=[True, False, False, False],
        )

        for _, row in df_view.iterrows():
            tid = row.get("ticket_id", "UNKNOWN")
            ttype = row.get("type", "UNKNOWN")
            sla_status = row.get("sla_status", "-")
            breach_hours = row.get("breach_hours") or (
                f"+{int(row['breach_hours_num'])}h" if pd.notna(row.get("breach_hours_num")) else ""
            )
            action = row.get("action", "MONITOR")
            status = row.get("status", "-")

            header_cols = st.columns([1.4, 1.0, 1.2, 1.0, 0.6])

            with header_cols[1]:
                st.markdown(f"**{ttype}**")

            with header_cols[2]:
                if str(status).upper() == "FIXED":
                    st.markdown("<span class='pill'>RESOLVED</span>", unsafe_allow_html=True)
                elif "BREACH" in str(sla_status).upper():
                    st.markdown(f"**:red[{sla_status}]**  \n{breach_hours}")
                else:
                    st.markdown(f"**{sla_status}**  \n{breach_hours}")

            with header_cols[3]:
                act = str(action).upper()
                if act == "FIXED":
                    st.markdown("**:green[FIXED]**")
                elif act == "ESCALATE":
                    st.markdown(f"**:red[{act}]**")
                elif act == "AUTO_RETRY":
                    st.markdown(f"**:green[{act}]**")
                else:
                    st.markdown(f"**{act}**")

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

    fp = _fingerprint_upload(uploaded)
    if uploaded is not None and fp != st.session_state.get("last_upload_fingerprint"):
        raw_df = _safe_read_upload(uploaded)
        df_loaded = _coerce_columns(raw_df) if raw_df is not None else None
        if isinstance(df_loaded, pd.DataFrame) and not df_loaded.empty:
            st.session_state["data_df"] = df_loaded
            st.session_state["last_upload_fingerprint"] = fp
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

    pending_ct = len(st.session_state.get("pending_vendor_responses", []))
    pending_sec = _pending_countdown_seconds()

    st.session_state["auto_refresh_enabled"] = st.toggle(
        "Auto-refresh while waiting for vendor response",
        value=st.session_state.get("auto_refresh_enabled", True),
    )

    if pending_ct > 0:
        st.info(f"Pending vendor responses: {pending_ct}" + (f" · next in ~{pending_sec}s" if pending_sec is not None else ""))

    # Auto rerun while waiting, so FIXED status appears without user clicking.
    if pending_ct > 0 and st.session_state.get("auto_refresh_enabled", True):
        try:
            st.autorefresh(interval=1000, key="vendor_wait_refresh")
        except Exception:
            pass

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
                [f"<div class='event-line'>{html.escape(str(line)).replace(chr(10), '<br/>')}</div>" for line in logs[-200:]]
            )
            + "</div>",
            unsafe_allow_html=True,
        )
