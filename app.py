import streamlit as st
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    SGT = ZoneInfo("Asia/Singapore")
except Exception:
    SGT = None
import json
import os
from typing import Dict, Any, List

# -------------------------
# Config
# -------------------------
EVENT_LOG_FILE = "event_log.json"  # optional; ephemeral on Streamlit Cloud

# -------------------------
# Helpers
# -------------------------
def now_ts() -> str:
    if SGT:
        return datetime.now(tz=SGT).strftime("%Y-%m-%d %H:%M:%S %Z")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def init_event_log(persist: bool = False):
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
    st.session_state.event_log.insert(0, entry)  # newest-first
    persist_event_log()

# -------------------------
# Escalation & processing
# -------------------------
def escalate_to_vendor(ticket_id: str, vendor: str, reason: str):
    # Real integration point: POST to webhook / call vendor API / send email
    escalation_msg = f"Escalation message sent to vendor '{vendor}' for ticket {ticket_id}: {reason}"
    # record the message (visible)
    log_event(action="escalation_sent", ticket_id=ticket_id, details=escalation_msg, tag="escalation", metadata={"vendor": vendor})
    # record a compact escalation marker for analytics or counts
    log_event(action="ticket_escalated", ticket_id=ticket_id, details=f"Ticket escalated to {vendor}", tag="escalation", metadata={"vendor": vendor})

def process_ticket(ticket: Dict[str, Any]):
    tid = ticket.get("id")
    status = ticket.get("status")
    breached = ticket.get("breached", False)
    vendor = ticket.get("vendor", "vendor-x")
    if breached:
        # log breach then escalate
        log_event(action="ticket_breached", ticket_id=tid, details=f"Status={status}", tag="breach")
        escalate_to_vendor(ticket_id=tid, vendor=vendor, reason="SLA breach detected")
    else:
        log_event(action="ticket_checked", ticket_id=tid, details=f"Status={status}", tag="info")

# -------------------------
# UI rendering: right panel
# -------------------------
def _badge_html(tag: str) -> str:
    colors = {
        "breach": "#D9534F",      # red-ish
        "escalation": "#F0AD4E",  # orange
        "info": "#5BC0DE"         # blue
    }
    text = tag.upper() if tag else ""
    color = colors.get(tag, "#999")
    return f"<span style='background:{color};color:white;padding:2px 6px;border-radius:6px;font-size:11px'>{text}</span>"

def render_event_log_panel(panel_width_ratio: int = 1):
    # Layout: place as a narrower right column
    left, right = st.columns([3, panel_width_ratio])
    with right:
        # panel header
        st.markdown("## System Event Log")
        st.markdown("<small>Concise timeline — newest first. Click an entry to expand details.</small>", unsafe_allow_html=True)

        # controls: filter, search, export, clear
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

        # helper counts
        total = len(st.session_state.event_log)
        breach_count = sum(1 for e in st.session_state.event_log if e.get("tag") == "breach")
        esc_count = sum(1 for e in st.session_state.event_log if e.get("tag") == "escalation")
        st.markdown(f"**Total:** {total} &nbsp;|&nbsp; 🔥 **Breaches:** {breach_count} &nbsp;|&nbsp; 🚨 **Escalations:** {esc_count}")

        st.write("")  # spacing

        # Empty state
        if not st.session_state.event_log:
            st.info("No events yet.")
            return

        # Filter + search
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

        # Render entries (newest-first already)
        # We'll render as compact rows with a clickable expander for details
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

            # compact header line
            header_html = (
                f"<div style='display:flex;align-items:center;justify-content:space-between;'>"
                f"<div style='line-height:1.1'>"
                f"<strong style='font-size:13px'>{ts}</strong><br>"
                f"<code style='font-size:12px'>{action}</code> &nbsp; <strong style='font-size:13px'>({tid})</strong>"
                f"</div>"
                f"<div>{_badge_html(tag)}</div>"
                f"</div>"
            )
            # use expander for details (keeps compact)
            with st.expander(label="", expanded=False):
                st.markdown(header_html, unsafe_allow_html=True)
                if details:
                    st.markdown(f"<div style='color:#333;margin-top:6px'>{details}</div>", unsafe_allow_html=True)
                # show metadata if present
                meta = e.get("meta") or {}
                if meta:
                    st.markdown(f"<small style='color:#666'>Meta: {json.dumps(meta)}</small>", unsafe_allow_html=True)
                st.write("---")

        if displayed == 0:
            st.info("No events match your filters/search.")

# -------------------------
# Demo / integration area
# -------------------------
def main():
    st.set_page_config(layout="wide")
    st.title("Ticket Monitor — Event Log (polished)")

    # init log
    init_event_log(persist=False)

    # Left area: demo ticket controls
    left, _ = st.columns([3, 1])
    with left:
        st.header("Tickets (demo)")
        tid = st.text_input("Ticket ID", value="TKT-1001")
        status = st.selectbox("Status", ["open", "in_progress", "resolved"])
        breached = st.checkbox("SLA breached?", value=False)
        vendor = st.text_input("Vendor", value="vendor-acme")
        if st.button("Process ticket"):
            ticket = {"id": tid, "status": status, "breached": breached, "vendor": vendor}
            process_ticket(ticket)
            st.success(f"Processed {tid}")

        st.markdown("---")
        # quick actions to demo multiple logs
        if st.button("Add sample breach"):
            process_ticket({"id": f"TKT-{datetime.utcnow().strftime('%H%M%S')}", "status": "open", "breached": True, "vendor": "vendor-x"})
        if st.button("Add sample info"):
            process_ticket({"id": f"TKT-{datetime.utcnow().strftime('%H%M%S')}", "status": "in_progress", "breached": False, "vendor": "vendor-x"})

    # Render right panel
    render_event_log_panel(panel_width_ratio=1)

if __name__ == "__main__":
    main()
