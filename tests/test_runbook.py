from agent.runbook import load_runbook, decide_escalation

def test_default_rule():
    rb = load_runbook("data/sample_runbook.yaml")
    ctx = {
        "priority": "P3",
        "state": "ok",
        "customer_tier": "STANDARD",
        "vip": False,
        "minutes_to_due": 999,
        "minutes_breached": 0,
        "minutes_since_update": 10,
        "blocked_reason": "",
    }
    d = decide_escalation(rb, ctx)
    assert d.level in {"none","owner","team","manager","incident"}
