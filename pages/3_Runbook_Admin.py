from __future__ import annotations
import streamlit as st
import yaml
from pathlib import Path

from agent.runbook import load_runbook, validate_runbook

st.set_page_config(page_title="Runbook Admin", layout="wide")
st.title("🧭 Runbook Admin")

path = st.text_input("Runbook YAML path", value="data/sample_runbook.yaml")
p = Path(path)

if not p.exists():
    st.error(f"Runbook file not found: {path}")
    st.stop()

raw = p.read_text(encoding="utf-8")
edited = st.text_area("Edit runbook YAML", value=raw, height=520)

c1, c2, c3 = st.columns(3)
with c1:
    do_validate = st.button("Validate")
with c2:
    do_save = st.button("Save to file")
with c3:
    st.caption("Note: On Streamlit Cloud, filesystem writes may not persist across redeploys.")

if do_validate or do_save:
    try:
        rb = yaml.safe_load(edited)
        errs = validate_runbook(rb)
        if errs:
            st.error("Runbook validation failed:\n- " + "\n- ".join(errs))
        else:
            st.success("Runbook looks valid.")
            if do_save:
                p.write_text(edited, encoding="utf-8")
                st.success("Saved.")
    except Exception as e:
        st.error(f"YAML parse error: {e}")

st.subheader("Runbook structure")
st.write(
    """
Each rule has:
- `when`: match conditions (priority/state/vip and thresholds)
- `then`: escalation instruction (level/channel/reason)

Matching is **first rule wins**.
"""
)
