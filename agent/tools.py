from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass(frozen=True)
class SLAStatus:
    state: str                # "ok" | "at_risk" | "breached"
    minutes_to_due: int       # negative => breached by abs(minutes_to_due)
    minutes_since_update: int

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def compute_sla_status(
    now: datetime,
    sla_due_at: datetime,
    last_update_at: datetime,
    at_risk_minutes: int = 120
) -> SLAStatus:
    now = _ensure_utc(now)
    sla_due_at = _ensure_utc(sla_due_at)
    last_update_at = _ensure_utc(last_update_at)

    minutes_to_due = int((sla_due_at - now).total_seconds() // 60)
    minutes_since_update = int((now - last_update_at).total_seconds() // 60)

    if minutes_to_due < 0:
        state = "breached"
    elif minutes_to_due <= at_risk_minutes:
        state = "at_risk"
    else:
        state = "ok"

    return SLAStatus(
        state=state,
        minutes_to_due=minutes_to_due,
        minutes_since_update=minutes_since_update
    )

def humanize_minutes(m: int) -> str:
    # returns "2h 15m", "-1h 05m"
    sign = "-" if m < 0 else ""
    m = abs(m)
    h = m // 60
    rem = m % 60
    if h == 0:
        return f"{sign}{rem}m"
    return f"{sign}{h}h {rem:02d}m"
