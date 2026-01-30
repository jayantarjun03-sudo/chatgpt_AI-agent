from datetime import datetime, timezone, timedelta
from agent.tools import compute_sla_status

def test_breached():
    now = datetime(2026,1,30,0,0,0,tzinfo=timezone.utc)
    due = now - timedelta(minutes=5)
    upd = now - timedelta(minutes=60)
    s = compute_sla_status(now, due, upd, at_risk_minutes=120)
    assert s.state == "breached"
    assert s.minutes_to_due < 0

def test_at_risk():
    now = datetime(2026,1,30,0,0,0,tzinfo=timezone.utc)
    due = now + timedelta(minutes=90)
    upd = now - timedelta(minutes=30)
    s = compute_sla_status(now, due, upd, at_risk_minutes=120)
    assert s.state == "at_risk"

def test_ok():
    now = datetime(2026,1,30,0,0,0,tzinfo=timezone.utc)
    due = now + timedelta(minutes=500)
    upd = now - timedelta(minutes=30)
    s = compute_sla_status(now, due, upd, at_risk_minutes=120)
    assert s.state == "ok"
