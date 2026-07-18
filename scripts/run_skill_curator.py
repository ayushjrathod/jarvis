#!/usr/bin/env python3
"""Run the monthly deterministic skill curator — what the systemd timer calls.

POSTs to the dispatcher's /skills/curate (lifecycle pass over skill_usage:
active -> stale -> archived; archive = move to areas/.archive/, recoverable).
Same boot-race retry loop as run_agent.py.
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
    url = f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8765)}/skills/curate"
    req = urllib.request.Request(url, b"", {"content-type": "application/json"})
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                report = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            sys.exit(f"dispatcher rejected curator run: {e}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == RETRIES:
                sys.exit(f"dispatcher unreachable at {url} after {RETRIES} attempts: {e}")
            print(f"dispatcher not ready ({e}); retry {attempt}/{RETRIES} in {RETRY_DELAY_S}s")
            time.sleep(RETRY_DELAY_S)
    print(f"curator: {report}")


if __name__ == "__main__":
    main()
