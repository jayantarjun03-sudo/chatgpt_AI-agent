# SLA Ops Agent (LLM-free) — Streamlit

Deterministic test agent to validate:
- context building (SQLite casefile)
- reasoning for delayed/at-risk SLA (rules + staleness + blockers)
- daily operational escalation (YAML runbook)
- deployability on Streamlit via GitHub

## Local run

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
