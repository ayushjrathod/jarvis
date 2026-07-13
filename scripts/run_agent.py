#!/usr/bin/env python3
"""Submit an area agent's prompt to the dispatcher — what systemd timers call.

Usage: run_agent.py <area> <agent>            (e.g. run_agent.py tasks daily-brief)

Reads areas/<area>/agents/<agent>.md, substitutes {{DATE}}/{{DATE_HUMAN}}/
{{WEEK_START}}, POSTs the body to /task (mode=agentic) with the frontmatter's
allowed_tools as the tool override. Exits nonzero if the dispatcher is down —
systemd journals the failure.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Persistent= timers fire right at boot, often before the dispatcher has bound
# its port (this lost the 2026-07-09/11/12 briefs). Connection-level failures
# are retried for a while; an HTTP error response is not (the server got the
# request — retrying could duplicate the task).
RETRIES = 12
RETRY_DELAY_S = 5


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    area, agent = sys.argv[1], sys.argv[2]
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

    today = date.today()
    text = (
        text.replace("{{DATE}}", today.isoformat())
        .replace("{{DATE_HUMAN}}", today.strftime("%A, %B %d, %Y"))
        .replace("{{WEEK_START}}", (today - timedelta(days=6)).isoformat())
    )

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())["dispatcher"]
    url = f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8765)}/task"
    body = {
        "text": text,
        "source": "timer",
        "mode": "agentic",
        "area": area,
        "metadata": {
            "task_type": meta.get("task_type", agent),
            "allowed_tools": meta.get("allowed_tools"),
        },
    }
    req = urllib.request.Request(
        url, json.dumps(body).encode(), {"content-type": "application/json"}
    )
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                out = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            sys.exit(f"dispatcher rejected {area}/{agent}: {e}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == RETRIES:
                sys.exit(f"dispatcher unreachable at {url} after {RETRIES} attempts: {e}")
            print(f"dispatcher not ready ({e}); retry {attempt}/{RETRIES} in {RETRY_DELAY_S}s")
            time.sleep(RETRY_DELAY_S)
    print(f"submitted {area}/{agent} as task {out['task_id']}")


if __name__ == "__main__":
    main()
