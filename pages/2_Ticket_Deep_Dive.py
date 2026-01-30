from __future__ import annotations
import streamlit as st
from datetime import datetime, timezone

from agent.dataio import load_tickets_csv
from agent.tools import compute_sla_status, humanize_minutes
from agent.runbook import load_runbook, decide_escalation
from agent.insights import infer_insights
from agent.memory import init_db, get_case, upsert_case

st.set_page_config(page_title="Ticket Deep Dive", layout="wide")
st.title("🔎 Ticket Deep Dive")

with st.sidebar:
    csv_path = st.text_input("Tickets CSV path", value="data/sample_tickets.csv")
    runbook_path = st.text_input("Runbook YAML path", value="data/sample_runbook.yaml")
    at_risk_minutes = st.number_input("At-risk threshold (minutes)", min_value=15, max_value=1440, value=120, step=15)

df = load_tickets_csv(csv_path)
rb = load_runbook(runbook_path)
now = datetime.now(timezone.utc)

ticket_id = st.selectbox("Select ticket", df["ticket_id"].astype(str).tolist())
row = df[df["ticket_id"].astype(str) == str(ticket_id)].iloc[0]

s = compute_sla_status(now, row["sla_due_at"], row["last_update_at"], at_risk_minutes=at_risk_minutes)
vip = str(row["customer_tier"]).upper() in {"VIP","PLATINUM"}
minutes_breached = abs(s.minutes_to_due) if s.state == "breached" else 0

ctx = {
    "priority": row["priority"],
    "state": s.state,
    "customer_tier": row["customer_tier"],
    "vip": vip,
    "minutes_to_due": s.minutes_to_due,
    "minutes_breached": minutes_breached,
    "minutes_since_update": s.minutes_since_update,
    "blocked_reason": row["blocked_reason"],
}
esc = decide_escalation(rb, ctx)

ins = infer_insights(
    state=s.state,
    minutes_to_due=s.minutes_to_due,
    minutes_since_update=s.minutes_since_update,
    blocked_reason=row["blocked_reason"],
    latest_update=row["latest_update"],
    priority=row["priority"],
    owner=row["owner"]
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("SLA state", s.state)
c2.metric("Time to due", humanize_minutes(s.minutes_to_due))
c3.metric("Since last update", humanize_minutes(s.minutes_since_update))
c4.metric("Escalation", esc.level)

st.subheader("Ticket facts")
st.json({
    "ticket_id": str(row["ticket_id"]),
    "customer": row["customer"],
    "customer_tier": row["customer_tier"],
    "priority": row["priority"],
    "owner": row["owner"],
    "queue": row["queue"],
    "category": row["category"],
    "status": row["status"],
    "created_at": row["created_at"].isoformat(),
    "sla_due_at": row["sla_due_at"].isoformat(),
    "last_update_at": row["last_update_at"].isoformat(),
    "blocked_reason": (row["blocked_reason"] or "").strip(),
})

st.subheader("Deterministic reasoning")
st.write("**Drivers**")
st.write("- " + "\n- ".join(ins.drivers) if ins.drivers else "- (none detected)")
st.write("**Themes**")
st.write(", ".join(ins.themes) if ins.themes else "(none)")
st.write("**Recommended actions**")
st.write("- " + "\n- ".join(ins.recommended_actions) if ins.recommended_actions else "- (none)")

st.subheader("Escalation recommendation (runbook)")
st.write(f"**Level:** `{esc.level}`  |  **Channel:** `{esc.channel}`")
st.write(f"**Reason:** {esc.reason}")

st.subheader("Draft escalation messages (LLM-free templates)")
ticket_line = f"{row['ticket_id']} [{row['priority']}/{s.state}]"
timing_line = f"SLA due in {humanize_minutes(s.minutes_to_due)} (last update {humanize_minutes(s.minutes_since_update)} ago)"
block_line = f"Blocker: {(row['blocked_reason'] or '').strip() or 'none stated'}"
ask_line = "Ask: Please confirm current status, blocker owner/ETA, and next update time."

slack_msg = "\n".join([
    f":rotating_light: {ticket_line}",
    f"- {timing_line}",
    f"- Owner: {row['owner']} | Customer: {row['customer']} ({row['customer_tier']}) | Queue: {row['queue']}",
    f"- {block_line}",
    f"- {ask_line}",
    f"- Recommended escalation: {esc.level} ({esc.reason})",
])

email_msg = "\n".join([
    f"Subject: Escalation - {ticket_line}",
    "",
    f"Hi team,",
    "",
    f"Ticket: {row['ticket_id']}",
    f"Priority/SLA state: {row['priority']} / {s.state}",
    f"{timing_line}",
    f"Owner: {row['owner']}",
    f"Customer: {row['customer']} ({row['customer_tier']})",
    f"Queue/Category: {row['queue']} / {row['category']}",
    f"{block_line}",
    "",
    "Focused insights:",
    *[f"- {d}" for d in ins.drivers],
    "",
    "Requested next steps:",
    *[f"- {a}" for a in ins.recommended_actions],
    "",
    ask_line,
    "",
    "Thanks,",
])

st.text_area("Slack draft", value=slack_msg, height=200)
st.text_area("Email draft", value=email_msg, height=260)

st.divider()
st.subheader("Casefile context (SQLite)")
con = init_db()
cf = get_case(con, str(row["ticket_id"]))
if cf:
    st.write("Existing casefile record:")
    st.json({
        "summary": cf.summary,
        "open_questions": cf.open_questions,
        "decisions": cf.decisions[-3:],  # last 3
        "updated_at": cf.updated_at
    })
else:
    st.warning("No casefile record yet for this ticket (create one below).")

if st.button("Write/update casefile for this ticket"):
    facts = {
        "customer": row["customer"],
        "tier": row["customer_tier"],
        "priority": row["priority"],
        "owner": row["owner"],
        "queue": row["queue"],
        "status": row["status"],
        "sla_due_at": row["sla_due_at"].isoformat(),
        "last_update_at": row["last_update_at"].isoformat(),
        "blocked_reason": row["blocked_reason"],
    }
    summary = f"{row['priority']} / {s.state}. Owner {row['owner']}. Escalation {esc.level}. Blocked: {(row['blocked_reason'] or '').strip() or 'none'}."
    open_questions = []
    if not (row["latest_update"] or "").strip():
        open_questions.append("What is the latest progress update?")
    if (row["blocked_reason"] or "").strip():
        open_questions.append("Who owns the dependency and what is the ETA?")
    decisions = [{
        "ts": now.isoformat(),
        "note": "Deep dive saved draft escalation + actions.",
        "sla_state": s.state,
        "escalation": esc.level,
    }]
    upsert_case(con, str(row["ticket_id"]), facts, summary, open_questions, decisions)
    st.success("Casefile updated.")
