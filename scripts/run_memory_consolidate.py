#!/usr/bin/env python3
"""Trigger the nightly memory consolidation — what the systemd timer calls.

POSTs to the dispatcher's /memory/consolidate, which exports unconsolidated
episodes and queues the consolidation agent (areas/memory/agents/
consolidate.md). Same boot-race retry loop as run_agent.py: connection-level
failures retry for a while; an HTTP error response does not (the server got
the request — retrying could duplicate the run).
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RETRIES = 12
RETRY_DELAY_S = 5


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())["dispatcher"]
    url = f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8765)}/memory/consolidate"
    req = urllib.request.Request(url, b"", {"content-type": "application/json"})
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                out = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            sys.exit(f"dispatcher rejected consolidation: {e}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == RETRIES:
                sys.exit(f"dispatcher unreachable at {url} after {RETRIES} attempts: {e}")
            print(f"dispatcher not ready ({e}); retry {attempt}/{RETRIES} in {RETRY_DELAY_S}s")
            time.sleep(RETRY_DELAY_S)
    if out.get("status") == "nothing_to_consolidate":
        print("no unconsolidated episodes — nothing to do")
    else:
        print(f"consolidation queued as task {out['task_id']}"
              f" ({out['episodes']} episode(s), export {out['export']})")


if __name__ == "__main__":
    main()
