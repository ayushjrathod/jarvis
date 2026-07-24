"""Standing automations (Phase I): "every morning, tell me X" → a row in the
automations table → the scheduler loop submits it as a normal task when due.

Design after khoj's automations (AGPL-3.0 — patterns re-implemented from
scratch, no code copied): one LLM call converts the natural-language request
to a structured schedule, everything after that is mechanical — validation,
next-run computation, and firing are deterministic Python. The scheduler is
an asyncio loop inside the dispatcher rather than khoj's cron thread or a
separate systemd timer: the dispatcher must be up for tasks to run anyway,
and an in-process loop gets minute precision plus catch-up (a past-due
next_run_at fires on the first check after startup, like Persistent=true).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("dispatcher.automations")

_ZONE = None


def _local_zone():
    """The machine's IANA zone, so wall-clock math is DST-correct (L4): adding
    a day to '07:30' keeps 07:30 across a DST change, and only .astimezone(UTC)
    at the end applies the per-date offset. A fixed-offset tzinfo (what
    .astimezone() yields) would freeze one offset and drift an hour twice a
    year. Falls back to the fixed local offset when the zone can't be resolved
    (harmless in a no-DST zone like IST). No new dependency: TZ env or the
    /etc/localtime symlink."""
    global _ZONE
    if _ZONE is not None:
        return _ZONE
    name = os.environ.get("TZ") or ""
    if not name:
        try:
            link = os.readlink("/etc/localtime")
            if "zoneinfo/" in link:
                name = link.split("zoneinfo/", 1)[1]
        except OSError:
            pass
    if name:
        try:
            _ZONE = ZoneInfo(name)
            return _ZONE
        except (ZoneInfoNotFoundError, ValueError):
            pass
    _ZONE = datetime.now().astimezone().tzinfo  # fixed-offset fallback
    return _ZONE


def _now_local() -> datetime:
    return datetime.now(_local_zone())

KINDS = ("daily", "weekly", "interval", "once")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")
MIN_INTERVAL_MINUTES = 5
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

# Conservative on purpose: a false divert turns a question into an unwanted
# automation, so schedule-ish phrasing is required AND question openers veto.
DETECT_RE = re.compile(
    r"\b(every|each)\s+"
    r"(morning|day|evening|night|week|weekday|hour|half.hour|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"\d+\s*(?:minutes?|hours?|days?))\b",
    re.IGNORECASE)
QUESTION_RE = re.compile(
    r"^\s*(what|when|why|how|who|where|which|did|do|does|is|are|was|were|"
    r"have|has|can|could|should|would|show|list)\b",
    re.IGNORECASE)
# A leading memory/statement verb means "write this down", never "schedule it"
# — "remember that I run every morning" must stay a memory write, not become an
# automation, even though it carries schedule-ish phrasing (L5).
STATEMENT_RE = re.compile(
    r"^\s*(remember|note|save|record|store|log|memoriz|"
    r"don'?t forget|keep in mind|fyi)\b",
    re.IGNORECASE)

PARSE_PROMPT = """\
Convert this scheduling request into JSON. Now: {now} ({tz}, weekday {weekday}).

Request: "{request}"

Reply with ONLY a JSON object — no prose, no code fences:
{{"task_text": "<the instruction to run each time, imperative, WITHOUT the schedule words>",
 "kind": "daily" | "weekly" | "interval" | "once",
 "time": "HH:MM" or null,
 "weekday": 0-6 or null,
 "interval_minutes": integer or null,
 "once_at": "YYYY-MM-DDTHH:MM" or null,
 "notify": true or false}}

Rules: time is 24-hour local; weekday 0=Monday; kind daily/weekly needs time;
weekly also needs weekday; interval needs interval_minutes (>= 5); once needs
once_at (local, in the future). Vague times: morning=07:30, afternoon=14:00,
evening=19:00, night=21:00. notify is false only if the user asked to stay
quiet about the results."""


def detect(text: str) -> bool:
    """Should this task text be diverted to automation creation?"""
    return (bool(DETECT_RE.search(text))
            and not QUESTION_RE.match(text)
            and not STATEMENT_RE.match(text))


def parse_prompt(request: str) -> str:
    now_local = _now_local()
    return PARSE_PROMPT.format(
        now=now_local.strftime("%Y-%m-%d %H:%M"),
        tz=now_local.tzname() or "local",
        weekday=WEEKDAYS[now_local.weekday()],
        request=request.replace('"', "'"),
    )


def parse_response(answer: str) -> dict:
    """Extract the JSON object from the model's reply (which may wrap it in
    prose or fences despite instructions). Raises ValueError when there is
    no parseable object."""
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in parse reply")
    try:
        obj = json.loads(answer[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"bad JSON in parse reply: {e}")
    if not isinstance(obj, dict):
        raise ValueError("parse reply is not an object")
    return obj


def validate_spec(spec: dict) -> dict:
    """Mechanical sanitizing of the LLM's schedule (khoj's crontab-sanitize
    idea): everything the scheduler will trust gets checked here."""
    task_text = str(spec.get("task_text") or "").strip()
    if not task_text or len(task_text) > 500:
        raise ValueError("task_text missing or over 500 chars")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")

    out = {"task_text": task_text, "kind": kind, "time": None, "weekday": None,
           "interval_minutes": None, "once_at": None,
           "notify": bool(spec.get("notify", True))}

    if kind in ("daily", "weekly"):
        m = TIME_RE.match(str(spec.get("time") or ""))
        if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
            raise ValueError("daily/weekly needs time HH:MM")
        out["time"] = f"{int(m.group(1)):02d}:{m.group(2)}"
    if kind == "weekly":
        wd = spec.get("weekday")
        if not isinstance(wd, int) or not 0 <= wd <= 6:
            raise ValueError("weekly needs weekday 0-6")
        out["weekday"] = wd
    if kind == "interval":
        iv = spec.get("interval_minutes")
        if not isinstance(iv, int) or iv < MIN_INTERVAL_MINUTES:
            raise ValueError(f"interval needs interval_minutes >= {MIN_INTERVAL_MINUTES}")
        out["interval_minutes"] = iv
    if kind == "once":
        try:
            dt = datetime.fromisoformat(str(spec.get("once_at") or ""))
        except ValueError:
            raise ValueError("once needs once_at as ISO local datetime")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_local_zone())
        if dt <= _now_local():
            raise ValueError("once_at is in the past")
        out["once_at"] = dt.isoformat(timespec="minutes")
    return out


def next_run_iso(spec: dict, after: datetime | None = None) -> str | None:
    """Next due moment as UTC ISO (comparable to db.now() strings), or None
    when the automation is spent (kind=once after firing). Schedules are
    local-time; catch-up policy is compute-from-now, so a machine that slept
    through three due times fires once, not three times."""
    after = after or _now_local()
    if after.tzinfo is None:
        after = after.replace(tzinfo=_local_zone())
    kind = spec["kind"]
    if kind == "once":
        if spec.get("once_at") is None:
            return None
        dt = datetime.fromisoformat(spec["once_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=after.tzinfo)
        nxt = dt if dt > after else None
        return nxt and nxt.astimezone(timezone.utc).isoformat(timespec="seconds")
    if kind == "interval":
        nxt = after + timedelta(minutes=spec["interval_minutes"])
    else:
        hh, mm = (int(x) for x in spec["time"].split(":"))
        nxt = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if kind == "daily":
            if nxt <= after:
                nxt += timedelta(days=1)
        else:  # weekly
            ahead = (spec["weekday"] - nxt.weekday()) % 7
            nxt += timedelta(days=ahead)
            if nxt <= after:
                nxt += timedelta(days=7)
    return nxt.astimezone(timezone.utc).isoformat(timespec="seconds")


def describe(spec: dict) -> str:
    kind = spec["kind"]
    if kind == "daily":
        return f"daily at {spec['time']}"
    if kind == "weekly":
        return f"weekly on {WEEKDAYS[spec['weekday']]} at {spec['time']}"
    if kind == "interval":
        return f"every {spec['interval_minutes']} minutes"
    return f"once at {str(spec.get('once_at', '')).replace('T', ' ')[:16]}"


def spec_from_row(row: dict) -> dict:
    return {"task_text": row["task_text"], "kind": row["kind"],
            "time": row["at_time"], "weekday": row["weekday"],
            "interval_minutes": row["interval_minutes"],
            "once_at": row["once_at"], "notify": bool(row["notify"])}


# -- scheduler ---------------------------------------------------------------

async def fire(svc, row: dict):
    """Submit one due automation as a normal task. The schedule is advanced
    BEFORE submitting so a failing submit can't tight-loop every check; a
    'once' row is disabled by the NULL next_run_at."""
    from .db import now as db_now  # local import: db imports nothing from here
    nxt = next_run_iso(spec_from_row(row))
    svc.db.automation_fired(row["id"], nxt)
    task = await svc.submit(
        row["task_text"], source="automation", mode="auto", trusted=True,
        metadata={"automation_id": row["id"], "notify": bool(row["notify"])})
    svc.db.automation_task_started(row["id"], task["id"])
    await svc.hooks.fire({
        "event": "automation", "automation_id": row["id"],
        "task_id": task["id"], "text": row["task_text"][:200],
        "next_run_at": nxt, "fired_at": db_now(),
    })
    log.info("automation %s fired -> task %s (next: %s)",
             row["id"], task["id"], nxt or "never")


async def loop(svc):
    """Poll due rows into the dispatch path. One bad automation or one bad
    check never kills the loop."""
    from .db import now as db_now
    interval = (getattr(svc.cfg, "automations", None) or {}).get("check_interval_s", 30)
    log.info("automations scheduler up (checking every %ss)", interval)
    while True:
        try:
            for row in svc.db.due_automations(db_now()):
                try:
                    await fire(svc, row)
                except Exception:
                    log.exception("automation %s failed to fire", row.get("id"))
        except Exception:
            log.exception("automations check failed")
        await asyncio.sleep(interval)
