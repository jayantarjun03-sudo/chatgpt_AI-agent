from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable
import re

@dataclass(frozen=True)
class TicketInsight:
    drivers: list[str]      # e.g. ["No update for 9h", "Blocked: awaiting vendor"]
    themes: list[str]       # e.g. ["dependency", "billing"]
    recommended_actions: list[str]

_KEYWORD_THEMES = [
    ("dependency", [r"\bdepend", r"\bblocked\b", r"\bawait", r"\bwaiting\b"]),
    ("provisioning", [r"\bprovision", r"\bactivate", r"\bsim\b", r"\bport\b"]),
    ("billing", [r"\bbill", r"\binvoice", r"\bcharge", r"\brefund"]),
    ("network", [r"\bnetwork", r"\boutage", r"\blatency", r"\bdown\b"]),
    ("customer_response", [r"\bcustomer\b.*\brespond", r"\bawaiting\b.*\bcustomer", r"\bno reply\b"]),
    ("internal_handoff", [r"\bhandoff", r"\bescalat", r"\btriage", r"\bqueue"]),
]

def _extract_themes(text: str) -> list[str]:
    t = (text or "").lower()
    themes = []
    for theme, pats in _KEYWORD_THEMES:
        for p in pats:
            if re.search(p, t):
                themes.append(theme)
                break
    # unique preserving order
    seen = set()
    out = []
    for x in themes:
        if x not in seen:
            out.append(x); seen.add(x)
    return out

def infer_insights(
    *,
    state: str,
    minutes_to_due: int,
    minutes_since_update: int,
    blocked_reason: str,
    latest_update: str,
    priority: str,
    owner: str
) -> TicketInsight:
    drivers = []
    actions = []

    # drivers
    if state == "breached":
        drivers.append("SLA already breached")
    elif state == "at_risk":
        drivers.append("SLA approaching deadline")

    if minutes_since_update >= 480:
        drivers.append(f"No update for {minutes_since_update//60}h+")
        actions.append(f"Request status update from owner ({owner}) and set next update time.")
    elif minutes_since_update >= 240:
        drivers.append(f"Updates are stale ({minutes_since_update//60}h+)")
        actions.append(f"Nudge owner ({owner}) for progress + blockers.")

    if blocked_reason.strip():
        drivers.append(f"Blocked: {blocked_reason.strip()}")
        actions.append("Unblock: identify dependency owner and request ETA / workaround.")

    # deterministic "next actions"
    if state in {"at_risk","breached"}:
        if priority == "P1":
            actions.append("Initiate P1 workflow: assign incident lead, confirm comms cadence.")
        elif priority == "P2":
            actions.append("Triage within team: confirm who is actively working and what’s next.")
        else:
            actions.append("Confirm scope/priority; consider deprioritizing if non-critical.")

    # themes from text fields
    themes = _extract_themes(f"{blocked_reason}\n{latest_update}")

    # tidy
    actions = list(dict.fromkeys([a for a in actions if a]))  # de-dupe preserve order
    drivers = list(dict.fromkeys([d for d in drivers if d]))

    return TicketInsight(drivers=drivers, themes=themes, recommended_actions=actions)

def top_themes(texts: Iterable[str], top_n: int = 5) -> list[tuple[str,int]]:
    counts = {}
    for tx in texts:
        for th in _extract_themes(tx or ""):
            counts[th] = counts.get(th, 0) + 1
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return ranked[:top_n]
