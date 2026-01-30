from __future__ import annotations
import streamlit as st
import pandas as pd
from datetime import datetime, timezone

from agent.dataio import load_tickets_csv
from agent.tools import compute_sla_status, humanize_minutes
from agent.runbook import load_runbook, decide_escalation
from agent.insights import top_themes, infer_insights
from agent.memory import init_db, upsert_case, get_case

st.set_page_config(page_title="Daily Brief", layout="wide")

st.title("📋 Daily Ops Brief")

with st.sidebar:
    st.header("Data")
    csv_path = st.text_input("Tickets CSV path", value="data/sample_tickets.csv")
    runbook_path = st.text_input("Runbook YAML path", value="data/sample_runbook.yaml")
    at_risk_minutes = st.number_input("At-risk threshold (minutes)", min_value=15, max_value=1440, value=120, step=15)

df = load_tickets_csv(csv_path)
rb = load_runbook(runbook_path)
now = datetime.now(timezone.utc)

# filters
c1, c2, c3, c4 = st.columns(4)
with c1:
    queue = st.selectbox("Queue", ["(all)"] + sorted(df["queue"].unique().tolist()))
with c2:
    owner = st.selectbox("Owner", ["(all)"] + sorted(df["owner"].unique().tolist()))
with c3:
    priority = st.selectbox("Priority", ["(all)"] + sorted(df["priority"].unique().tolist()))
with c4:
    tier = st.selectbox("Customer tier", ["(all)"] + sorted(df["customer_tier"].unique().tolist()))

f = df.copy()
if queue != "(all)":
    f = f[f["queue"] == queue]
if owner != "(all)":
    f = f[f["owner"] == owner]
if priority != "(all)":
    f = f[f["priority"] == priority]
if tier != "(all)":
    f = f[f["customer_tier"] == tier]

rows = []
for _, r in f.iterrows():
    s = compute_sla_status(now, r["sla_due_at"], r["last_update_at"], at_risk_minutes=at_risk_minutes)
    vip = str(r["customer_tier"]).upper() in {"VIP","PLATINUM"}
    minutes_breached = abs(s.minutes_to_due) if s.state == "breached" else 0
    ctx = {
        "priority": r["priority"],
        "state": s.state,
        "customer_tier": r["customer_tier"],
        "vip": vip,
        "minutes_to_due": s.minutes_to_due,
        "minutes_breached": minutes_breached,
        "minutes_since_update": s.minutes_since_update,
        "blocked_reason": r["blocked_reason"],
    }
    esc = decide_escalation(rb, ctx)

    insight = infer_insights(
        state=s.state,
        minutes_to_due=s.minutes_to_due,
        minutes_since_update=s.minutes_since_update,
        blocked_reason=r["blocked_reason"],
        latest_update=r["latest_update"],
        priority=r["priority"],
        owner=r["owner"],
    )

    rows.append({
        "ticket_id": r["ticket_id"],
        "customer": r["customer"],
        "tier": r["customer_tier"],
        "priority": r["priority"],
        "owner": r["owner"],
        "queue": r["queue"],
        "status": r["status"],
        "sla_state": s.state,
        "to_due": humanize_minutes(s.minutes_to_due),
        "since_update": humanize_minutes(s.minutes_since_update),
        "blocked": (r["blocked_reason"] or "").strip()[:60],
        "escalation": esc.level,
        "esc_reason": esc.reason,
        "themes": ", ".join(insight.themes),
    })

out = pd.DataFrame(rows)

breached = out[out["sla_state"] == "breached"]
at_risk = out[out["sla_state"] == "at_risk"]

k1, k2, k3, k4 = st.columns(4)
k1.metric("Breached", int(len(breached)))
k2.metric("At risk", int(len(at_risk)))
k3.metric("Total in view", int(len(out)))
stale_8h = (out["since_update"].str.contains("h") & out["since_update"].str.contains("-") == False)  # crude but fine for UI
k4.metric("Stale (heuristic)", int(stale_8h.sum()))

st.subheader("Priority list (breached first, then at-risk)")
def _rank(row):
    # breached first, then minutes_to_due ascending
    if row["sla_state"] == "breached":
        return (0, 0)
    if row["sla_state"] == "at_risk":
        return (1, 0)
    return (2, 0)

# for sorting, rebuild using numeric mins
# (use original f to avoid parsing human strings)
rank_rows = []
for _, r in f.iterrows():
    s = compute_sla_status(now, r["sla_due_at"], r["last_update_at"], at_risk_minutes=at_risk_minutes)
    rank_rows.append((r["ticket_id"], s.state, s.minutes_to_due))
rank_map = {tid: (state, m) for tid, state, m in rank_rows}

out2 = out.copy()
out2["minutes_to_due"] = out2["ticket_id"].apply(lambda x: rank_map[x][1])
out2["state_rank"] = out2["ticket_id"].apply(lambda x: 0 if rank_map[x][0]=="breached" else (1 if rank_map[x][0]=="at_risk" else 2))
out2 = out2.sort_values(["state_rank","minutes_to_due"]).drop(columns=["minutes_to_due","state_rank"])

st.dataframe(out2, use_container_width=True, height=420)

st.subheader("Focused insights")
themes = top_themes((f["blocked_reason"].fillna("") + "\n" + f["latest_update"].fillna("")).tolist(), top_n=8)
if themes:
    st.write("Top themes driving risk/breaches:")
    st.write(", ".join([f"{t} ({c})" for t, c in themes]))
else:
    st.write("No themes detected yet (try adding text in blocked_reason/latest_update).")

st.subheader("Generate Daily Brief (deterministic)")
brief_scope = st.radio("Include", ["Breached + At risk", "Breached only", "At risk only"], horizontal=True)

scope_df = out2.copy()
if brief_scope == "Breached only":
    scope_df = scope_df[scope_df["sla_state"] == "breached"]
elif brief_scope == "At risk only":
    scope_df = scope_df[scope_df["sla_state"] == "at_risk"]
else:
    scope_df = scope_df[scope_df["sla_state"].isin(["breached","at_risk"])]

if st.button("Generate brief text"):
    lines = []
    lines.append(f"Daily SLA Brief (UTC {now.strftime('%Y-%m-%d %H:%M')})")
    lines.append(f"- Breached: {len(breached)} | At risk: {len(at_risk)} | Total in view: {len(out)}")
    if themes:
        lines.append(f"- Top themes: " + ", ".join([f"{t}({c})" for t, c in themes[:5]]))
    lines.append("")
    lines.append("Key tickets:")
    for _, r in scope_df.head(15).iterrows():
        lines.append(
            f"- {r['ticket_id']} [{r['priority']}/{r['sla_state']}] due {r['to_due']} | "
            f"owner {r['owner']} | esc {r['escalation']} | {r['blocked']}"
        )
    st.code("\n".join(lines))

st.divider()
st.subheader("Update casefile context (optional)")
st.write("This writes a compact case summary per ticket into SQLite (storage/casefile.sqlite). Useful to test context building without an LLM.")
if st.button("Write/refresh casefile for tickets in view"):
    con = init_db()
    wrote = 0
    for _, r in f.iterrows():
        s = compute_sla_status(now, r["sla_due_at"], r["last_update_at"], at_risk_minutes=at_risk_minutes)
        facts = {
            "customer": r["customer"],
            "tier": r["customer_tier"],
            "priority": r["priority"],
            "owner": r["owner"],
            "queue": r["queue"],
            "status": r["status"],
            "sla_due_at": r["sla_due_at"].isoformat(),
            "last_update_at": r["last_update_at"].isoformat(),
            "blocked_reason": r["blocked_reason"],
        }
        summary = f"{r['priority']} / {s.state}. Owner {r['owner']}. Blocked: {(r['blocked_reason'] or '').strip() or 'none'}."
        open_questions = []
        if not (r["latest_update"] or "").strip():
            open_questions.append("What is the latest progress update?")
        if (r["blocked_reason"] or "").strip():
            open_questions.append("Who owns the dependency and what is the ETA?")
        decisions = [{
            "ts": now.isoformat(),
            "note": "Refreshed from Daily Brief.",
            "sla_state": s.state
        }]
        upsert_case(con, str(r["ticket_id"]), facts, summary, open_questions, decisions)
        wrote += 1
    st.success(f"Wrote/refreshed casefile entries: {wrote}")
    sample_id = str(f.iloc[0]["ticket_id"]) if len(f) else None
    if sample_id:
        cf = get_case(con, sample_id)
        if cf:
            st.write("Example casefile record:")
            st.json({
                "ticket_id": cf.ticket_id,
                "summary": cf.summary,
                "open_questions": cf.open_questions,
                "updated_at": cf.updated_at
            })
