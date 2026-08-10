#!/usr/bin/env python3
"""Trigger the nightly memory consolidation — what the systemd timer calls.

POSTs to the dispatcher's /memory/consolidate, which exports unconsolidated
episodes and queues the consolidation agent (areas/memory/agents/
consolidate.md). Same boot-race retry loop as run_agent.py: connection-level
failures retry for a while; an HTTP error response does not (the server got
the request — retrying could duplicate the run).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dispatcher.config import Config  # noqa: E402

# See run_agent.py for why the loop is capped by wall clock as well as by
# attempt count: worst case is BUDGET_S + REQUEST_TIMEOUT_S, and
# systemd/mission-memory-consolidate.service must carry a TimeoutStartSec
# above that or a slow-but-working run is SIGTERM'd and journaled as failed
# (2026-08-10). The endpoint returns 202 as soon as the agent is queued — it
# does not wait for the run — so 30s is generous for the request itself.
RETRIES = 12
RETRY_DELAY_S = 5
REQUEST_TIMEOUT_S = 30
BUDGET_S = 240


def config_path() -> Path:
    """The file Config.load() will read: MC_CONFIG if set, else the repo's own.
    This script used to re-read ROOT/config.yaml by path and ignore MC_CONFIG,
    so a dispatcher on an alternate config was unreachable (2026-08-10)."""
    return Path(os.environ.get("MC_CONFIG") or ROOT / "config.yaml")


def main():
    cfg = Config.load(config_path())
    url = f"http://{cfg.host}:{cfg.port}/memory/consolidate"
    req = urllib.request.Request(url, b"", {"content-type": "application/json"})
    deadline = time.monotonic() + BUDGET_S
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                out = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            sys.exit(f"dispatcher rejected consolidation: {e}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == RETRIES or time.monotonic() + RETRY_DELAY_S >= deadline:
                sys.exit(f"dispatcher unreachable at {url} after {attempt} attempt(s) "
                         f"/ {BUDGET_S}s: {e}")
            print(f"dispatcher not ready ({e}); retry {attempt}/{RETRIES} in {RETRY_DELAY_S}s")
            time.sleep(RETRY_DELAY_S)
    if out.get("status") == "nothing_to_consolidate":
        print("no unconsolidated episodes — nothing to do")
    else:
        print(f"consolidation queued as task {out['task_id']}"
              f" ({out['episodes']} episode(s), export {out['export']})")


if __name__ == "__main__":
    main()
