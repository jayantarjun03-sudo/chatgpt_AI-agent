# app.py
import streamlit as st
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    SGT = ZoneInfo("Asia/Singapore")
except Exception:
    SGT = None
import json
import os
import pandas as pd
from typing import Dict, Any, List

# -------------------------
# Config / file locations
# -------------------------
DAILY_BRIEF_JSON = "/mnt/data/daily_brief.json"
DAILY_BRIEF_CSV = "/mnt/data/daily_brief.csv"
EVENT_LOG_FILE = "event_log.json"  # optional local persistence

# -------------------------
# Utilities
# -------------------------
def now_ts() -> str:
    if SGT:
        return datetime.now(tz=SGT).strftime("%Y-%m-%d %H:%M:%S %Z")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def init_event_log():
    if "event_log" not in st.session_state:
        # try to load persisted log, otherwise init empty
        if os.path.exists(EVENT_LOG_FILE):
            try:
                with open(EVENT_LOG_FILE, "r", encoding="utf-8") as f:
                    st.session_state.event_log = json.load(f)
            except Exception:
                st.session_state.event_log = []
        else:
            st.session_state.event_log = []

def persist_event_log():
    try:
        with open(EVENT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(st.session_state.event_log, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def log_event(action: str, ticket_id: str = None, details: str = None, tag: str = None, metadata: Dict[str, Any] = None):
    entry: Dict[str, Any] = {
        "ts": now_ts(),
        "action": action,
        "ticket_id": ticket_id,
        "details": details or "",
        "tag": tag or "",
        "meta": metadata or {}
    }
    # newest-first
    st.session_state.event_log.insert(0, entry)
    persist_event_log()

# -------------------------
# Load daily brief (CSV/JSON) or fallback sample
# -------------------------
def load_daily_brief() -> List[Dict[str, Any]]:
    # Accepts either JSON list-of-objects or CSV with columns: id,status,breached,vendor,required_action (optional)
    if os.path.exists(DAILY_BRIEF_JSON):
        try:
            with open(DAILY_BRIEF_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            # ensure each item has expected fields
            return [_normalize_ticket(x) for x in data]
        except Exception as e:
            st.warning(f"Failed to load {DAILY_BRIEF_JSON}: {e}")

    if os.path.exists(DAILY_BRIEF_CSV):
        try:
            df = pd.read_csv(DAILY_BRIEF_CSV, dtype=str).fillna("")
            tickets = []
            for _, row in df.iterrows():
                tickets.append({
                    "id": str(row.get("id") or row.get("ticket_id") or ""),
                    "status": str(row.get("status") or "open"),
                    "breached": str(row.get("breached") or "").lower() in ("1","true","yes","y"),
                    "vendor": str(row.get("vendor") or "vendor-x"),
                    "required_action": str(row.get("required_action") or "")
                })
            return [_normalize_ticket(t) for t in tickets]
        except Exception as e:
            st.warning(f"Failed to load {DAILY_BRIEF_CSV}: {e}")

    # fallback sample data (if no daily brief provided)
    sample = [
        {"id": "TKT-1001", "status": "open", "breached": False, "vendor": "acme-corp", "required_action": "investigate"},
        {"id": "TKT-1002", "status": "open", "breached": True, "vendor": "vendor-alpha", "required_action": "escalate"},
        {"id": "TKT-1003", "status": "in_progress", "breached": False, "vendor": "vendor-beta", "required_action": "workaround"},
        {"id": "TKT-1004", "status": "open", "breached": True, "vendor": "vendor-gamma", "required_action": "escalate"},
        {"id": "TKT-1005", "status": "resolved", "breached": False, "vendor": "acme-corp", "required_action": "close"}
    ]
    return [_normalize_ticket(t) for t in sample]

def _normalize_ticket(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(raw.get("id") or raw.get("ticket_id") or raw.get("tid") or ""),
        "status": str(raw.get("status") or "open"),
        "breached": bool(raw.get("breached") is True or str(raw.get("breached") or "").lower() in ("1","true","yes","y")),
        "vendor": str(raw.get("vendor") or "vendor-x"),
        "required_action": str(raw.get("required_action") or "")
    }

# -------------------------
# Business logic: simulation & escalation
# -------------------------
def escalate_to_vendor(ticket_id: str, vendor: str, reason: str):
    # Hook: call vendor API / webhook / email here in production
    escalation_msg = f"Escalation message sent to vendor '{vendor}' for ticket {ticket_id}: {reason}"
    # Required: event log must contain that escalation message
    log_event(action="escalation_sent", ticket_id=ticket_id, details=escalation_msg, tag="escalation", metadata={"vendor": vendor})
    # Also add a compact escalated marker
    log_event(action="ticket_escalated", ticket_id=ticket_id, details=f"Ticket escalated to {vendor}", tag="escalation", metadata={"vendor": vendor})

def simulate_agent_action(ticket: Dict[str, Any]):
    tid = ticket["id"]
    # Agent assigns and takes action (simulate)
    assign_detail = f"Agent assigned to ticket {tid}"
    log_event(action="agent_assigned", ticket_id=tid, details=assign_detail, tag="info", metadata={"vendor": ticket["vendor"]})

    action_taken = ticket.get("required_action") or ("follow-up" if not ticket.get("breached") else "escalation follow-up")
    action_detail = f"Agent action for {tid}: {action_taken}"
    log_event(action="agent_action_taken", ticket_id=tid, details=action_detail, tag="info", metadata={"action": action_taken})

def process_and_simulate(tickets: List[Dict[str, Any]]):
    # Process tickets: detect breach, log, escalate, then simulate agent action for each ticket.
    # Order: oldest-first processing for readability (but logged newest-first)
    for t in tickets:
        tid = t["id"]
        status = t["status"]
        if t["breached"]:
            # log the breach event (requirement #2: must show breach + escalation message)
            log_event(action="ticket_breached", ticket_id=tid, details=f"Status={status}", tag="breach")
            escalate_to_vendor(ticket_id=tid, vendor=t.get("vendor", "vendor-x"), reason="SLA breach detected")
        else:
            log_event(action="ticket_checked", ticket_id=tid, details=f"Status={status}", tag="info")
        # simulate an agent taking the next step in every case
        simulate_agent_action(t)

# -------------------------
# UI: right panel event log (compact)
# -------------------------
def _badge_html(tag: str) -> str:
    colors = {
        "breach": "#D9534F",      # red
        "escalation": "#F0AD4E",  # orange
        "info": "#5BC0DE"         # blue
    }
    text = tag.upper() if tag else ""
    color = colors.get(tag, "#999")
    return f"<span style='background:{color};color:white;padding:2px 6px;border-radius:6px;font-size:11px'>{text}</span>"

def render_event_log_panel(panel_width_ratio: int = 1):
    left, right = st.columns([3, panel_width_ratio])
    with right:
        st.markdown("## System Event Log")
        st.markdown("<small>Concise timeline — newest first. Shows tickets and actions taken.</small>", unsafe_allow_html=True)

        # controls
        cols = st.columns([2, 2, 1, 1])
        with cols[0]:
            filter_choice = st.selectbox("Filter", ["All", "Breach", "Escalation", "Info"], index=0)
        with cols[1]:
            q = st.text_input("Search ticket id", value="")
        with cols[2]:
            if st.button("Export"):
                st.download_button("Download JSON", json.dumps(st.session_state.event_log, indent=2), file_name="event_log.json", mime="application/json")
        with cols[3]:
            if st.button("Clear"):
                st.session_state.event_log = []
                persist_event_log()
                st.experimental_rerun()

        total = len(st.session_state.event_log)
        breach_count = sum(1 for e in st.session_state.event_log if e.get("tag") == "breach")
        esc_count = sum(1 for e in st.session_state.event_log if e.get("tag") == "escalation")
        st.markdown(f"**Total:** {total} &nbsp;|&nbsp; 🔥 **Breaches:** {breach_count} &nbsp;|&nbsp; 🚨 **Escalations:** {esc_count}")

        st.write("")
        if not st.session_state.event_log:
            st.info("No events yet.")
            return

        def passes_filters(e):
            if filter_choice == "Breach" and e.get("tag") != "breach":
                return False
            if filter_choice == "Escalation" and e.get("tag") != "escalation":
                return False
            if filter_choice == "Info" and e.get("tag") != "info":
                return False
            if q:
                return (q.lower() in (e.get("ticket_id") or "").lower()) or (q.lower() in (e.get("details") or "").lower())
            return True

        displayed = 0
        for e in st.session_state.event_log:
            if not passes_filters(e):
                continue
            displayed += 1
            ts = e.get("ts", "")
            action = e.get("action", "")
            tid = e.get("ticket_id") or "-"
            details = e.get("details", "")
            tag = e.get("tag", "")

            header_html = (
                f"<div style='display:flex;align-items:center;justify-content:space-between;'>"
                f"<div style='line-height:1.1'>"
                f"<strong style='font-size:13px'>{ts}</strong><br>"
                f"<code style='font-size:12px'>{action}</code> &nbsp; <strong style='font-size:13px'>({tid})</strong>"
                f"</div>"
                f"<div>{_badge_html(tag)}</div>"
                f"</div>"
            )
            with st.expander(label="", expanded=False):
                st.markdown(header_html, unsafe_allow_html=True)
                if details:
                    st.markdown(f"<div style='color:#333;margin-top:6px'>{details}</div>", unsafe_allow_html=True)
                meta = e.get("meta") or {}
                if meta:
                    st.markdown(f"<small style='color:#666'>Meta: {json.dumps(meta)}</small>", unsafe_allow_html=True)
                st.write("---")

        if displayed == 0:
            st.info("No events match your filters/search.")

# -------------------------
# Main app UI
# -------------------------
def main():
    st.set_page_config(layout="wide")
    st.title("Ticket Monitor — Automated Daily Brief Simulation")

    init_event_log()

    # Load brief (show brief summary on the left)
    tickets = load_daily_brief()
    left, _ = st.columns([3, 1])
    with left:
        st.header("Daily Brief (auto-detected)")
        st.markdown(f"Loaded **{len(tickets)}** tickets from daily brief (fallback sample used if no file).")
        # show a small table for clarity
        if tickets:
            df = pd.DataFrame(tickets)
            st.dataframe(df)

        st.write("")
        col1, col2 = st.columns([1, 1])
        with col1:
            auto_run_if_exists = st.checkbox("Auto-run simulation when a daily brief file exists", value=False)
        with col2:
            animate = st.checkbox("Animate (slow) simulation", value=False)

        # Run control: automatic if file exists + user opted in, or manual button
        has_brief_file = os.path.exists(DAILY_BRIEF_JSON) or os.path.exists(DAILY_BRIEF_CSV)
        run_now = False
        if auto_run_if_exists and has_brief_file:
            run_now = True
        if st.button("Run simulation now"):
            run_now = True

        if run_now:
            # If animate requested, we can optionally slow it down — but avoid long sleeps in prod.
            if animate:
                # naive slow animation: we will log with progressively earlier timestamps so newest-first shows nicely.
                for t in tickets:
                    process_single_for_animation(t)
                st.experimental_rerun()
            else:
                process_and_simulate(tickets)
                st.success("Simulation completed. See system event log on the right.")

    # Show the right-hand event log
    render_event_log_panel(panel_width_ratio=1)

# A tiny helper for animate mode to inject staggered timestamps (keeps logic simple)
def process_single_for_animation(ticket: Dict[str, Any]):
    # This function mimics a small step-by-step simulation; it's fine if timestamps are very close
    tid = ticket["id"]
    status = ticket["status"]
    if ticket["breached"]:
        log_event(action="ticket_breached", ticket_id=tid, details=f"Status={status}", tag="breach")
        # small synthetic wait represented by adding another event immediately
        escalate_to_vendor(ticket_id=tid, vendor=ticket.get("vendor", "vendor-x"), reason="SLA breach detected")
    else:
        log_event(action="ticket_checked", ticket_id=tid, details=f"Status={status}", tag="info")
    simulate_agent_action(ticket)

if __name__ == "__main__":
    main()
