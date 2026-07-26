#!/usr/bin/env python
"""Live acceptance for the messages_api quick backend.

The dispatcher has always run the CLI backend here (subscription OAuth, no
API key), so this path shipped untested. This exercises it for real against
the Anthropic API and checks the three things that make it a drop-in
replacement rather than a downgrade:

  1. the memory blocks reach the system prompt (locked decision #4)
  2. a resumed session actually remembers the previous turn
  3. an in-flight turn can be aborted by the barge-in handle (decision #7)

It also prints TTFT so the CLI-vs-API latency claim stops being anecdotal.

Needs ANTHROPIC_API_KEY in the environment:
    set -a; . ./.env; set +a; .venv/bin/python scripts/smoke_messages_api.py

Costs a fraction of a cent. It never touches the running dispatcher, the
database, or config.yaml.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dispatcher import quick               # noqa: E402
from dispatcher.config import Config       # noqa: E402

CODEWORD = "thornfield-42"
MEMORY = ("<memory_blocks>\n- The user's project codeword is "
          f"{CODEWORD}.\n</memory_blocks>")

ok = True


def check(label, passed, detail=""):
    global ok
    ok = ok and passed
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}{' — ' + detail if detail else ''}")


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — run with:  set -a; . ./.env; set +a")
        return 2

    cfg = Config.load()
    cfg.quick_backend = "messages_api"          # in-memory only; config.yaml untouched
    print(f"backend: {quick.resolve_backend(cfg)}   model: {cfg.models.get('quick')}")
    assert quick.resolve_backend(cfg) == "messages_api"

    history = quick.HistoryStore(max_turns=cfg.budgets.get("quick_history_turns", 6))
    procs: dict = {}

    # --- 1. memory blocks injected -----------------------------------------
    print("\n1. memory blocks reach the model")
    t0, ttft, out, meta = time.monotonic(), None, [], None
    async for kind, payload in quick.stream(
            "What is my project codeword? Answer with just the word.",
            cfg, context=MEMORY, history=history, procs=procs, task_id="t1"):
        if kind == "delta":
            ttft = ttft or (time.monotonic() - t0)
            out.append(payload)
        else:
            meta = payload
    answer = "".join(out)
    check("answered from the injected block", CODEWORD in answer.lower(), repr(answer.strip()))
    check("status done", meta["status"] == "done")
    check("session id minted", bool(meta.get("session_id")))
    print(f"       TTFT {ttft * 1000:.0f} ms   cost ${meta['cost_usd']:.5f}"
          f"   in/out {meta['input_tokens']}/{meta['output_tokens']}")
    api_ttft, sid = ttft, meta["session_id"]

    # --- 2. continuity ------------------------------------------------------
    print("\n2. resumed session remembers the previous turn")
    out2, meta2 = [], None
    async for kind, payload in quick.stream(
            "Reverse the word you just told me. Answer with just the reversed word.",
            cfg, resume_session_id=sid, history=history, procs=procs, task_id="t2"):
        if kind == "delta":
            out2.append(payload)
        else:
            meta2 = payload
    ans2 = "".join(out2).lower()
    check("same session id", meta2["session_id"] == sid)
    check("used prior context", CODEWORD[::-1] in ans2 or "thornfield" in ans2,
          repr("".join(out2).strip()))

    # --- 3. barge-in --------------------------------------------------------
    print("\n3. barge-in aborts an in-flight turn")
    got, meta3 = [], None
    agen = quick.stream("Count slowly from 1 to 40, one number per line.",
                        cfg, history=history, procs=procs, task_id="t3")
    async for kind, payload in agen:
        if kind == "delta":
            got.append(payload)
            if len(got) == 2 and "t3" in procs:
                procs["t3"].kill()          # exactly what Service.cancel does
        else:
            meta3 = payload
    check("stopped early", meta3["status"] == "cancelled",
          f"{len(got)} delta(s) before abort")
    check("aborted turn not recorded", history.start(meta3["session_id"])[1] == [],
          "a cancelled turn must not pollute the thread")

    # --- comparison ---------------------------------------------------------
    print("\n4. latency vs the CLI backend")
    cfg_cli = Config.load()
    cfg_cli.quick_backend = "claude_cli"
    t0, cli_ttft, meta4 = time.monotonic(), None, None
    async for kind, payload in quick.stream(
            "What is my project codeword? Answer with just the word.",
            cfg_cli, context=MEMORY, task_id="t4"):
        if kind == "delta":
            cli_ttft = cli_ttft or (time.monotonic() - t0)
        else:
            meta4 = payload
    if cli_ttft:
        print(f"       api {api_ttft * 1000:.0f} ms   cli {cli_ttft * 1000:.0f} ms"
              f"   ({cli_ttft / api_ttft:.1f}x)")
        print(f"       api ${meta['cost_usd']:.5f}   cli ${(meta4 or {}).get('cost_usd') or 0:.5f}")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
