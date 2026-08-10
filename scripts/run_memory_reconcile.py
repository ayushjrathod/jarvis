#!/usr/bin/env python3
"""Trigger the weekly knowledge-graph reconciliation — what the systemd
timer calls. Same boot-race retry loop as run_memory_consolidate.py."""

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

# This is the one endpoint here that BLOCKS on a model call: /memory/reconcile
# runs the graph-reconcile quick task inline, so the server-side bound is
# budgets.quick_timeout_s (120s) plus overhead. 180s leaves headroom without
# the old 300s, which was 3.3x systemd's DefaultTimeoutStartUSec (1min 30s) on
# a single attempt — the unit had no TimeoutStartSec, so a reconcile that ran
# past 90s was SIGTERM'd and journaled as *failed* while the dispatcher
# happily finished the work (2026-08-10). Worst case is now
# BUDGET_S + REQUEST_TIMEOUT_S = 420s; keep
# systemd/mission-memory-reconcile.service's TimeoutStartSec above it.
RETRIES = 12
RETRY_DELAY_S = 5
REQUEST_TIMEOUT_S = 180
BUDGET_S = 240


def config_path() -> Path:
    """The file Config.load() will read: MC_CONFIG if set, else the repo's own.
    This script used to re-read ROOT/config.yaml by path and ignore MC_CONFIG,
    so a dispatcher on an alternate config was unreachable (2026-08-10)."""
    return Path(os.environ.get("MC_CONFIG") or ROOT / "config.yaml")


def main():
    cfg = Config.load(config_path())
    url = f"http://{cfg.host}:{cfg.port}/memory/reconcile"
    req = urllib.request.Request(url, b"", {"content-type": "application/json"})
    deadline = time.monotonic() + BUDGET_S
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                out = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            sys.exit(f"dispatcher rejected reconciliation: {e}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == RETRIES or time.monotonic() + RETRY_DELAY_S >= deadline:
                sys.exit(f"dispatcher unreachable at {url} after {attempt} attempt(s) "
                         f"/ {BUDGET_S}s: {e}")
            print(f"dispatcher not ready ({e}); retry {attempt}/{RETRIES} in {RETRY_DELAY_S}s")
            time.sleep(RETRY_DELAY_S)
    print(f"reconciliation: {out}")


if __name__ == "__main__":
    main()
