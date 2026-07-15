"""Classify provider usage-limit failures from claude CLI error text.

Hint-list matching rather than exact strings (pattern per openclaw
src/agents/embedded-agent-helpers/errors.ts) because these messages churn
across CLI releases. Two scopes, driven by real failures in our runs table:

- "model"   — one model's limit is hit and the message says other models
              work ("…switch models with /model"): safe to retry once on the
              configured fallback model, like a refusal.
- "session" — the plan-wide session window is exhausted ("You've hit your
              session limit · resets 2:30pm (Asia/Kolkata)"): retrying any
              model is pointless; surface the reset time instead.

Budget-cap errors are explicitly excluded: --max-budget-usd trips are
deliberate terminal outcomes and must never trigger a fallback retry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_REACHED_HINTS = (
    "hit your", "reached your", "limit reached", "exceeded your",
)
_MODEL_SCOPE_HINTS = ("switch models", "/model", "usage-credits")
_NOT_LIMIT_HINTS = ("budget",)

# "resets 2:30pm (Asia/Kolkata)", "will reset at 7pm", "resets 2:30 pm"
_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*[ap]m)\s*(?:\(([^)]+)\))?",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.IGNORECASE)


@dataclass
class LimitInfo:
    scope: str                       # "model" | "session"
    reset_phrase: str | None = None  # speakable, e.g. "2:30pm"
    resets_at: datetime | None = None  # tz-aware, when parseable


def classify_limit(error: str | None, now: datetime | None = None) -> LimitInfo | None:
    """LimitInfo when the error text is a provider usage limit, else None."""
    if not error:
        return None
    text = error.lower()
    if "limit" not in text:
        return None
    if any(h in text for h in _NOT_LIMIT_HINTS):
        return None
    if not any(h in text for h in _REACHED_HINTS):
        return None
    scope = "model" if any(h in text for h in _MODEL_SCOPE_HINTS) else "session"
    phrase, resets_at = _parse_reset(error, now)
    return LimitInfo(scope, phrase, resets_at)


def limit_speech(info: LimitInfo) -> str:
    """Short spoken/dashboard explanation for a final limit failure."""
    what = "session limit" if info.scope == "session" else "usage limit"
    when = f" It resets at {info.reset_phrase}." if info.reset_phrase else ""
    return f"Claude's {what} is hit right now.{when}"


def _parse_reset(text: str, now: datetime | None = None) -> tuple[str | None, datetime | None]:
    m = _RESET_RE.search(text)
    if not m:
        return None, None
    phrase = m.group(1).strip()
    t = _TIME_RE.match(phrase)
    if not t:
        return phrase, None
    hour, minute = int(t.group(1)) % 12, int(t.group(2) or 0)
    if t.group(3).lower() == "pm":
        hour += 12
    tz = None
    if m.group(2):
        try:
            tz = ZoneInfo(m.group(2).strip())
        except Exception:
            tz = None
    if tz is None:
        tz = (now or datetime.now().astimezone()).tzinfo or ZoneInfo("UTC")
    local_now = (now or datetime.now(tz)).astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return phrase, candidate
