import streamlit as st
from datetime import datetime
try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
    SGT = ZoneInfo("Asia/Singapore")
except Exception:
    # fallback - use local time
    SGT = None
import json
import os
from typing import Dict, Any

# ----------------------------
# Event logging utilities
# ----------------------------
EVENT_LOG_FILE = "event_log.json"  # optional persistence file in app repo (works in dev; streamlit cloud ephemeral)

def now_ts() -> str:
    """Return current datetime string in Asia/Singapore (fallback to local)."""
    if SGT:
        return datetime.now(tz=SGT).strftime("%Y-%m-%d %H:%M:%S %Z")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def init_event_log(persist: bool = False):
    """Ensure event log exists in session state. If persist True and file exists, load it."""
    if "event_log" not in st.session_state:
        if persist and os.path.exists(EVENT_LOG_FILE):
            try:
                with open(EVENT_LOG_FILE, "r", encoding="utf-8") as f:
                    st.session_state.event_log = json.load(f)
            except Exception:
                st.session_state.event_log = []
        else:
            st.session_state.event_log = []

def persist_event_log():
    """Persist event log to disk (optional)."""
    try:
        with open(EVENT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(st.session_state.event_log, f, ensure_ascii=False, indent=2)
    except Exception:
        # ignore persistence errors (Streamlit Cloud filesystem ephemeral)
        pass

def log_event(action: str, ticket_id: str = None, details: str = None, tag: str = None):
    """Append a concise event to session event log (newest at top)."""
    entry: Dict[str, Any] = {
        "ts": now_ts(),
        "action": action,
        "ticket_id": ticket_id,
        "details": details,
        "tag": tag
    }
    # Prepend for newest-first display
    st.session_state.event_log.insert(0, entry)
    # Optional: persist
    persist_event_log()

# ----------------------------
# Example: ticket processing helpers (call from your app logic)
# ----------------------------
def escalate_to_vendor(ticket_id: str, vendor: str, reason: str):
    """Simulate sending escalation message to vendor and log it."""
    # your real escalation code goes here (API call, email, webhooks, etc.)
    msg = f"Escalation sent to vendor '{vendor}' for ticket {ticket_id}: {reason}"
    log_event(action="escalation_sent", ticket_id=ticket_id, details=msg, tag="escalation")
    # record that ticket was escalated
    log_event(action="ticket_escalated", ticket_id=ticket_id, details=f"Ticket escalated to {vendor}", tag="escalation")

def process_ticket(ticket: dict):
    """
    Example processing:
      - determine if ticket breached SLA
      - if breached, log and escalate
      - otherwise, log user action
    """
    tid = ticket.get("id")
    status = ticket.get("status")
    breached = ticket.get("breached", False)
    vendor = ticket.get("vendor", "vendor-x")
    if breached:
        log_event(action="ticket_breached", ticket_id=tid, details=f"Status={status}", tag="breach")
        # auto-escalate for breached tickets
        escalate_to_vendor(ticket_id=tid, vendor=vendor, reason="SLA breach detected")
    else:
        log_event(action="ticket_checked", ticket_id=tid, details=f"Status={status}", tag="info")

# ----------------------------
# UI layout: main area + right-hand event log panel
# ----------------------------
def render_event_log_panel(width_ratio: int = 1):
    """
    Render the right panel: concise timeline showing time, action, ticket id and a short detail.
    The panel is sorted newest-first.
    """
    # Right column is narrower; main content should use the left column.
    # In your actual app, adjust the ratios (e.g., st.columns([3,1])).
    _, right = st.columns([3, width_ratio])
    with right:
        st.markdown("### System Event Log")
        st.markdown("_Concise audit of tickets & escalations (newest first)_")
        # compact controls
        c1, c2 = st.columns([3, 1])
        with c1:
            if st.button("Clear log", key="clear_log"):
                st.session_state.event_log = []
                persist_event_log()
                st.experimental_rerun()
        with c2:
            if st.button("Export JSON", key="export_log"):
                st.download_button("Download log", json.dumps(st.session_state.event_log, indent=2), file_name="event_log.json", mime="application/json")

        # Render log entries
        if not st.session_state.event_log:
            st.info("No events yet.")
            return

        # show compressed list
        for e in st.session_state.event_log:
            ts = e.get("ts", "")
            action = e.get("action", "")
            tid = e.get("ticket_id") or "-"
            details = e.get("details") or ""
            tag = e.get("tag") or ""
            # colorize simple tags (very minimal)
            tag_label = f" [{tag}]" if tag else ""
            # compact line: timestamp — ACTION (ticket) — details
            st.markdown(f"**{ts}** — `{action}` (ticket: **{tid}**){tag_label}")
            if details:
                st.markdown(f"<small style='color:#555'>{details}</small>", unsafe_allow_html=True)
            st.write("---")  # separator

# ----------------------------
# Example main for demo + integration points
# ----------------------------
def main():
    st.set_page_config(layout="wide")
    st.title("Ticket Monitor + Event Log (demo)")

    # initialize log (try loading from disk; set persist=True if you want)
    init_event_log(persist=False)

    # Left area: simple ticket controls for demo
    left, _ = st.columns([3, 1])
    with left:
        st.header("Tickets (demo controls)")
        # demo: create a fake ticket
        tid = st.text_input("Ticket ID", value="TKT-1001")
        status = st.selectbox("Status", ["open", "in_progress", "resolved"])
        breached = st.checkbox("SLA breached?", value=False)
        vendor = st.text_input("Vendor", value="vendor-acme")
        if st.button("Process ticket"):
            ticket = {"id": tid, "status": status, "breached": breached, "vendor": vendor}
            process_ticket(ticket)
            st.success(f"Processed {tid}")

    # Right panel: show event log
    render_event_log_panel(width_ratio=1)

if __name__ == "__main__":
    main()
