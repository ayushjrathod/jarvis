#!/usr/bin/env python3
"""Submit an area agent's prompt to the dispatcher — what systemd timers call.

Usage: run_agent.py <area> <agent> [--date YYYY-MM-DD] [--scheduled-at HH:MM]
       (e.g. run_agent.py tasks daily-brief --scheduled-at 07:30)

Reads areas/<area>/agents/<agent>.md, substitutes {{DATE}}/{{DATE_HUMAN}}/
{{WEEK_START}}, POSTs the body to /task (mode=agentic) naming the agent, whose
allowed_tools the dispatcher then resolves from that file itself. Exits nonzero
if the dispatcher is down — systemd journals the failure.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dispatcher import brief as brief_mod  # noqa: E402
from dispatcher.config import Config  # noqa: E402

# Persistent= timers fire right at boot, often before the dispatcher has bound
# its port (this lost the 2026-07-09/11/12 briefs). Connection-level failures
# are retried for a while; an HTTP error response is not (the server got the
# request — retrying could duplicate the task).
#
# BUDGET_S caps that loop by WALL CLOCK, and it is the number the unit's
# TimeoutStartSec has to agree with. An attempt count alone is not a budget: 12
# attempts that each hang for their full socket timeout is minutes, not
# seconds. Worst case is BUDGET_S + REQUEST_TIMEOUT_S, since the last attempt
# may start just under the deadline and run its timeout out. systemd's
# DefaultTimeoutStartUSec here is 1min 30s — well under that — so a run that
# legitimately waited was SIGTERM'd and journaled as *failed* while the
# dispatcher was working fine (2026-08-10). Keep these in step with
# TimeoutStartSec in systemd/mission-daily-brief.service and
# systemd/mission-weekly-review.service.
RETRIES = 12
RETRY_DELAY_S = 5
REQUEST_TIMEOUT_S = 15
BUDGET_S = 240


def config_path() -> Path:
    """The file Config.load() will read: MC_CONFIG if set, else the repo's own.

    The timer scripts used to re-read ROOT/config.yaml by path and ignore
    MC_CONFIG entirely, so a dispatcher started on an alternate config was
    unreachable from every one of them (2026-08-10)."""
    return Path(os.environ.get("MC_CONFIG") or ROOT / "config.yaml")


def _hhmm(s: str):
    return datetime.strptime(s, "%H:%M").time()


def resolve_day(scheduled, now: datetime) -> tuple[date, str | None]:
    """The day the agent's output is ABOUT, which is not always the day it runs.

    `Persistent=true` makes a missed timer fire once at boot, for the most
    recent elapse it slept through. The box was off at 07:30 on 2026-07-29, the
    catch-up ran at 00:42 on the 30th, and `date.today()` therefore wrote the
    *30th's* brief: 07-29 got none, and the 30th's real run then found its file
    already written (CLAUDE.md records this; `ls vault/briefs/` shows the gaps).

    The missed elapse is recoverable exactly, with no guessing, given the
    timer's own OnCalendar time-of-day: a run starting BEFORE that time is a
    catch-up for yesterday's elapse, and a run at or after it is today's.
    Returns the day plus a line to print when the shift applies — a silently
    re-dated file is the failure mode this repo keeps getting bitten by, so the
    correction is always announced. `--date` overrides both."""
    today = now.date()
    if scheduled is None or now.time() >= scheduled:
        return today, None
    day = today - timedelta(days=1)
    return day, (f"catch-up: {now:%H:%M} is before the {scheduled:%H:%M} schedule, "
                 f"so this run is the missed one for {day.isoformat()} "
                 f"(pass --date to override)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("area")
    ap.add_argument("agent")
    ap.add_argument("--date", type=date.fromisoformat, metavar="YYYY-MM-DD",
                    help="the day this run is about; defaults to today (see "
                         "--scheduled-at). Use it to regenerate a missed day.")
    ap.add_argument("--scheduled-at", type=_hhmm, metavar="HH:MM",
                    help="the timer's OnCalendar time-of-day. A run starting "
                         "before it is treated as a Persistent= catch-up for "
                         "the previous day's elapse.")
    args = ap.parse_args()
    area, agent = args.area, args.agent

    path = ROOT / "areas" / area / "agents" / f"{agent}.md"
    if not path.exists():
        sys.exit(f"no such agent: {path}")

    text = path.read_text()
    meta = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = yaml.safe_load(parts[1]) or {}
            text = parts[2].strip()

    if args.date:
        today = args.date
    else:
        today, note = resolve_day(args.scheduled_at, datetime.now())
        if note:
            print(note)
    text = (
        text.replace("{{DATE}}", today.isoformat())
        .replace("{{DATE_HUMAN}}", today.strftime("%A, %B %d, %Y"))
        .replace("{{WEEK_START}}", (today - timedelta(days=6)).isoformat())
    )

    cfg = Config.load(config_path())

    # Deterministic pre-check: a brief for a vault with no open tasks and no
    # fresh notes says "clean slate" for ~$0.40 and 8-11 turns. Write it here
    # for nothing instead. Any material at all and we fall through to the agent.
    #
    # `brief:` is the one config block Config has no field for, so read it back
    # from the SAME file Config just used rather than a second, possibly
    # different, one. Follow-up: add `Config.brief` and drop this re-read.
    raw = yaml.safe_load(config_path().read_text()) or {}
    bcfg = (raw.get("dispatcher") or {}).get("brief") or {}
    if agent == "daily-brief" and bcfg.get("skip_model_when_quiet", True):
        days = int(bcfg.get("quiet_notes_days", brief_mod.DEFAULT_NOTES_DAYS))
        if brief_mod.is_quiet(ROOT, days):
            out_path = brief_mod.write_quiet(ROOT, today, days)
            print(f"vault is quiet — wrote {out_path.relative_to(ROOT)} without a model call")
            return

    url = f"http://{cfg.host}:{cfg.port}/task"
    body = {
        "text": text,
        "source": "timer",
        "mode": "agentic",
        "area": area,
        "metadata": {
            "task_type": meta.get("task_type", agent),
            # Name the agent; do NOT send its allowed_tools. We're an external
            # HTTP client, so the dispatcher's trust boundary strips tool grants
            # off the wire (that silently broke the daily brief). It reads this
            # same file's frontmatter itself instead.
            "agent": agent,
        },
    }
    req = urllib.request.Request(
        url, json.dumps(body).encode(), {"content-type": "application/json"}
    )
    deadline = time.monotonic() + BUDGET_S
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                out = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            sys.exit(f"dispatcher rejected {area}/{agent}: {e}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == RETRIES or time.monotonic() + RETRY_DELAY_S >= deadline:
                sys.exit(f"dispatcher unreachable at {url} after {attempt} attempt(s) "
                         f"/ {BUDGET_S}s: {e}")
            print(f"dispatcher not ready ({e}); retry {attempt}/{RETRIES} in {RETRY_DELAY_S}s")
            time.sleep(RETRY_DELAY_S)
    print(f"submitted {area}/{agent} as task {out['task_id']}")


if __name__ == "__main__":
    main()
