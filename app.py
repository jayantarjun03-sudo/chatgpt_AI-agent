import streamlit as st

st.set_page_config(
    page_title="SLA Ops Agent (LLM-free)",
    page_icon="⏱️",
    layout="wide"
)

st.title("⏱️ SLA Ops Agent (LLM-free)")
st.write(
    """
This app is a deterministic ops agent for testing:
- context building (casefile in SQLite)
- SLA delay reasoning (rules + staleness + blockers)
- daily operational escalation (YAML runbook)
- focused insights (keyword + driver summaries)

Use the pages in the left sidebar.
"""
)

st.info("Start with **Daily Brief** → then drill into **Ticket Deep Dive**. Runbook rules are editable in **Runbook Admin**.")
