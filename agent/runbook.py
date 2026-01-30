from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import yaml

DEFAULT_RUNBOOK_PATH = Path("data/sample_runbook.yaml")

@dataclass(frozen=True)
class EscalationDecision:
    level: str   # none | owner | team | manager | incident
    channel: str # slack | email | both
    reason: str  # short explanation

def load_runbook(path: str | None = None) -> dict:
    p = Path(path) if path else DEFAULT_RUNBOOK_PATH
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "rules" not in data:
        raise ValueError("Runbook YAML must be a mapping with a top-level 'rules' key.")
    return data

def validate_runbook(rb: dict) -> list[str]:
    errs = []
    if "rules" not in rb or not isinstance(rb["rules"], list):
        return ["Runbook must contain 'rules' as a list."]
    for i, r in enumerate(rb["rules"]):
        if not isinstance(r, dict):
            errs.append(f"rules[{i}] must be a mapping.")
            continue
        for k in ["name","when","then"]:
            if k not in r:
                errs.append(f"rules[{i}] missing '{k}'.")
        if "then" in r and isinstance(r["then"], dict):
            for k in ["level","channel","reason"]:
                if k not in r["then"]:
                    errs.append(f"rules[{i}].then missing '{k}'.")
    return errs

def _match_when(when: dict, ctx: dict) -> bool:
    # supports simple conditions:
    # - equals: {"priority": ["P1","P2"]} or scalar
    # - booleans: {"vip": True}
    # - thresholds: {"minutes_to_due_lte": 120, "minutes_breached_gte": 240, "minutes_since_update_gte": 480}
    # - nonempty: {"blocked_nonempty": True}
    for k, v in when.items():
        if k in {"priority","state","customer_tier"}:
            cv = ctx.get(k)
            if isinstance(v, list):
                if cv not in v: return False
            else:
                if cv != v: return False

        elif k == "vip":
            if bool(ctx.get("vip")) != bool(v): return False

        elif k == "minutes_to_due_lte":
            if ctx.get("minutes_to_due", 10**9) > int(v): return False
        elif k == "minutes_to_due_gte":
            if ctx.get("minutes_to_due", -10**9) < int(v): return False

        elif k == "minutes_breached_gte":
            # minutes_breached only meaningful if breached; else 0
            if ctx.get("minutes_breached", 0) < int(v): return False

        elif k == "minutes_since_update_gte":
            if ctx.get("minutes_since_update", 0) < int(v): return False

        elif k == "blocked_nonempty":
            if bool(ctx.get("blocked_reason", "").strip()) != bool(v): return False

        else:
            # unknown predicate -> fail safe
            return False

    return True

def decide_escalation(runbook: dict, ctx: dict) -> EscalationDecision:
    # first-match wins
    for r in runbook.get("rules", []):
        when = r.get("when", {})
        then = r.get("then", {})
        if _match_when(when, ctx):
            return EscalationDecision(
                level=str(then.get("level","none")),
                channel=str(then.get("channel","both")),
                reason=str(then.get("reason","runbook match")),
            )
    return EscalationDecision(level="none", channel="both", reason="no rule matched")
