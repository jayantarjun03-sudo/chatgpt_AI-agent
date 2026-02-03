import streamlit as st
import pandas as pd
import json
import time
from datetime import datetime

# ----------------------------
# UI helpers
# ----------------------------
def now_stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def add_log(message: str, level: str = "INFO"):
    st.session_state.event_logs.insert(
        0,  # newest on top
        {"ts": now_stamp(), "level": level, "msg": message},
    )

def render_event_log(logs):
    # Simple console-like log panel (black bg, green text)
    st.markdown(
        """
        <style>
        .logbox {
            background: #0b0f0c;
            border-radius: 12px;
            padding: 14px 14px;
            border: 1px solid rgba(255,255,255,0.08);
            height: 320px;
            overflow-y: auto;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 13px;
            line-height: 1.45;
            color: #7CFC90;
        }
        .logline { margin-bottom: 6px; white-space: pre-wrap; }
        .lvl-WARN { color: #FFD166; }
        .lvl-ERROR { color: #FF5C5C; }
        .lvl-INFO { color: #7CFC90; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    lines = []
    for e in logs:
        cls = f"lvl-{e['level']}"
        lines.append(f"<div class='logline {cls}'>{e['ts']} — {e['msg']}</div>")
    st.markdown(f"<div class='logbox'>{''.join(lines) if lines else 'No events yet.'}</div>", unsafe_allow_html=True)

# ----------------------------
# Data model assumptions
# ----------------------------
REQUIRED_COLUMNS = [
    "ticket_id",
    "type",
    "vendor",
    "sla_status",     # e.g. "OK", "BREACHED"
    "breach_by_hours",# numeric, >0 indicates how late
    "status",
    "context",
    "root_cause",
    "impact",
    "recommended_next_steps",
]

def sample_data() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticket_id": "ORD-10008",
                "type": "DEVICE",
                "vendor": "VendorA",
                "sla_status": "BREACHED",
                "breach_by_hours": 24,
                "status": "Stuck",
                "context": "Order breached 48h SLA by 24h; no state transition observed.",
                "root_cause": "Payment authorised but not captured (confidence 91%).",
                "impact": "Activation delayed; higher inbound contacts and churn risk.",
                "recommended_next_steps": "Escalate to vendor with full diagnostic context and notify on-call.",
            },
            {
                "ticket_id": "ORD-10012",
                "type": "SIM",
                "vendor": "VendorB",
                "sla_status": "BREACHED",
                "breach_by_hours": 9,
                "status": "Stuck",
                "context": "SIM order pending provisioning; downstream queue backlog suspected.",
                "root_cause": "Downstream provisioning queue saturation.",
                "impact": "Customer waiting for service activation.",
                "recommended_next_steps": "Attempt auto-retry once; if still stuck, escalate to vendor.",
            },
            {
                "ticket_id": "ORD-10019",
                "type": "PLAN_CHANGE",
                "vendor": "VendorC",
                "sla_status": "OK",
                "breach_by_hours": 0,
                "status": "In progress",
                "context": "Plan change in progress; within SLA.",
                "root_cause": "N/A",
                "impact": "None",
                "recommended_next_steps": "Monitor.",
            },
        ]
    )

def load_uploaded_data(upload) -> pd.DataFrame:
    if upload is None:
        return sample_data()

    name = upload.name.lower()
    if name.endswith(".csv"):
        df = pd.read_csv(upload)
    elif name.endswith(".json"):
        raw = json.load(upload)
        # Accept either list-of-dicts or dict with "tickets" key
        if isinstance(raw, dict) and "tickets" in raw:
            raw = raw["tickets"]
        df = pd.DataFrame(raw)
    else:
        st.error("Please upload a CSV or JSON file.")
        return sample_data()

    # Gentle fallback: add missing columns as blanks (so UI still works)
    for c in REQUIRED_COLUMNS:
        if c not in df.columns:
            df[c] = "" if c not in ["breach_by_hours"] else 0

    # Normalize types
    df["breach_by_hours"] = pd.to_numeric(df["breach_by_hours"], errors="coerce").fillna(0)
    df["sla_status"] = df["sla_status"].fillna("OK").astype(str)
    return df

# ----------------------------
# Breach detection + action decision
# ----------------------------
def detect_breached(df: pd.DataFrame) -> pd.DataFrame:
    # Breached if explicitly marked, OR breach_by_hours > 0
    breached = df[(df["sla_status"].str.upper() == "BREACHED") | (df["breach_by_hours"] > 0)].copy()
    return breached

def decide_action(ticket_row: pd.Series) -> str:
    """
    Simple heuristic:
      - If recommended_next_steps mentions 'auto-retry' => AUTO_RETRY
      - Else => ESCALATE
    """
    steps = str(ticket_row.get("recommended_next_steps", "")).lower()
    if "auto-retry" in steps or "retry" in steps:
        return "AUTO_RETRY"
    return "ESCALATE"

def simulate_agent_run(df: pd.DataFrame):
    add_log("Agent run started: Checked SLA deadlines for all tickets")

    breached_df = detect_breached(df)
    if breached_df.empty:
        add_log("No breached tickets detected. Agent run completed.")
        return

    add_log(f"Detected {len(breached_df)} breached ticket(s)")
    time.sleep(0.15)

    for _, t in breached_df.iterrows():
        tid = t["ticket_id"]
        vendor = t["vendor"]
        breach_h = t["breach_by_hours"]

        add_log(f"Diagnosing {tid}: SLA breached by +{breach_h:.0f}h; analysing context/root cause")
        time.sleep(0.15)

        action = decide_action(t)
        add_log(f"Agent decision for {tid} → {action}")

        # Required: for each breached ticket, show escalation message sent + escalated
        if action == "ESCALATE":
            add_log(f"{tid}: Escalation message sent to vendor {vendor}", level="WARN")
            time.sleep(0.10)
            add_log(f"{tid}: Escalated to vendor system / ops queue for further action", level="WARN")
        else:
            add_log(f"{tid}: Triggered AUTO_RETRY with vendor {vendor}")
            time.sleep(0.10)
            # If still breached after retry (simulation), escalate anyway
            add_log(f"{tid}: Auto-retry did not resolve within expected window → fallback to ESCALATE", level="WARN")
            add_log(f"{tid}: Escalation message sent to vendor {vendor}", level="WARN")
            add_log(f"{tid}: Escalated to vendor system / ops queue for further action", level="WARN")

        time.sleep(0.15)

    add_log("Agent run completed: actions logged for breached tickets")

# ----------------------------
# Streamlit App
# ----------------------------
st.set_page_config(page_title="Ticket Ops Dashboard", layout="wide")

if "event_logs" not in st.session_state:
    st.session_state.event_logs = []
    add_log("App loaded")

# Top-level layout: main area + right panel
main_col, right_col = st.columns([2.4, 1.0], gap="large")

with right_col:
    st.subheader("AI Agent Progress")
    st.caption("Uploads drive the run. No manual ticket processing.")

    upload = st.file_uploader("Upload test data (CSV/JSON)", type=["csv", "json"])

    st.divider()
    run = st.button("Run agent on current data", use_container_width=True)

    st.subheader("Agent Event Logs")
    render_event_log(st.session_state.event_logs)

with main_col:
    df = load_uploaded_data(upload)

    st.title("Daily Ticket Dashboard")

    # Dashboard metrics (top cards)
    total = len(df)
    breached = len(detect_breached(df))
    stuck = int((df["status"].astype(str).str.lower() == "stuck").sum())
    by_type = df["type"].value_counts().to_dict()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Tickets", total)
    c2.metric("Breached Tickets", breached)
    c3.metric("Stuck Tickets", stuck)
    c4.metric("Ticket Types", ", ".join([f"{k}:{v}" for k, v in list(by_type.items())[:3]]) or "-")

    st.markdown("### Ticket Queue")

    # Queue table-ish view
    queue_cols = st.columns([1.2, 1.0, 1.2, 1.0, 1.0])
    queue_cols[0].markdown("**Ticket**")
    queue_cols[1].markdown("**Type**")
    queue_cols[2].markdown("**SLA Status**")
    queue_cols[3].markdown("**Action**")
    queue_cols[4].markdown("**Details**")

    for _, t in df.iterrows():
        tid = t["ticket_id"]
        ttype = t["type"]
        sla = str(t["sla_status"]).upper()
        breach_h = float(t["breach_by_hours"]) if pd.notna(t["breach_by_hours"]) else 0
        action = decide_action(t) if (sla == "BREACHED" or breach_h > 0) else "MONITOR"

        row = st.columns([1.2, 1.0, 1.2, 1.0, 1.0])

        row[0].write(tid)
        row[1].write(ttype)

        if sla == "BREACHED" or breach_h > 0:
            row[2].markdown(f"**:red[SLA BREACHED]**  \n+{breach_h:.0f}h")
        else:
            row[2].markdown("**:green[OK]**")

        row[3].markdown(f"**{action}**")

        with row[4]:
            with st.expander("View"):
                st.markdown("**Status**")
                st.write(t.get("status", ""))

                st.markdown("**Context**")
                st.write(t.get("context", ""))

                st.markdown("**Root cause**")
                st.write(t.get("root_cause", ""))

                st.markdown("**Impact**")
                st.write(t.get("impact", ""))

                st.markdown("**Recommended next steps**")
                st.write(t.get("recommended_next_steps", ""))

    if run:
        simulate_agent_run(df)
        st.rerun()
