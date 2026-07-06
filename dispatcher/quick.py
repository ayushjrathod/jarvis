"""Quick-path streaming (locked decision #2: quick Q&A streams immediately).

Two backends behind one async-generator interface that yields ("delta", str)
chunks followed by exactly one ("meta", dict):

- messages_api: Anthropic Messages API stream. On Fable 5 the server-side
  fallback beta retries refusals on the fallback model inside the same call,
  so meta["retry_on_refusal"] is False.
- claude_cli: `claude -p --output-format stream-json` — works with the
  subscription OAuth login when no API credentials exist on the machine.
  Refusal retry is the dispatcher's job here (meta["retry_on_refusal"] True).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .config import Config

QUICK_SYSTEM = (
    "You are Jarvis, a concise personal voice assistant. Answer directly in "
    "one to three short sentences suitable for being read aloud. Plain prose "
    "only: no markdown, no lists, no preamble."
)

FALLBACK_BETA = "server-side-fallback-2026-06-01"


def resolve_backend(cfg: Config) -> str:
    if cfg.quick_backend in ("messages_api", "claude_cli"):
        return cfg.quick_backend
    has_creds = bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or (Path.home() / ".config" / "anthropic" / "credentials").exists()
    )
    return "messages_api" if has_creds else "claude_cli"


async def stream(text: str, cfg: Config, model_override: str | None = None,
                 tools: list[str] | None = None, context: str = ""):
    backend = resolve_backend(cfg)
    if tools and backend == "messages_api":
        backend = "claude_cli"  # file-reading quick queries need CLI tool access
    if backend == "messages_api":
        agen = _stream_api(text, cfg, model_override)
    else:
        agen = _stream_cli(text, cfg, model_override, tools, context)
    async for item in agen:
        yield item


async def _stream_api(text: str, cfg: Config, model_override: str | None):
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    model = model_override or cfg.models.get("quick", "claude-fable-5")
    kwargs = dict(
        model=model,
        max_tokens=cfg.budgets.get("quick_max_tokens", 1024),
        system=QUICK_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    if model.startswith("claude-fable"):
        ctx = client.beta.messages.stream(
            **kwargs,
            betas=[FALLBACK_BETA],
            fallbacks=[{"model": cfg.models.get("fallback", "claude-opus-4-8")}],
        )
        retry_on_refusal = False  # server already tried the fallback chain
    else:
        ctx = client.messages.stream(**kwargs)
        retry_on_refusal = True
    async with ctx as s:
        async for delta in s.text_stream:
            yield ("delta", delta)
        final = await s.get_final_message()
    usage = final.usage
    refused = final.stop_reason == "refusal"
    yield ("meta", {
        "backend": "messages_api",
        "model": final.model,
        "status": "refused" if refused else "done",
        "stop_reason": final.stop_reason,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": cfg.quick_cost(final.model, usage.input_tokens, usage.output_tokens),
        "retry_on_refusal": retry_on_refusal and refused,
    })


async def _stream_cli(text: str, cfg: Config, model_override: str | None,
                      tools: list[str] | None = None, context: str = ""):
    system = QUICK_SYSTEM + ("\n\n" + context if context else "")
    cmd = [
        cfg.claude_bin, "-p", text,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--max-budget-usd", str(cfg.budgets.get("quick_max_cost_usd", 0.10)),
        "--system-prompt", system,
    ]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    if model_override:
        cmd += ["--model", model_override]
    env = dict(os.environ)
    if cfg.claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = cfg.claude_config_dir

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cfg.root, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    meta = {"backend": "claude_cli", "model": model_override or "cli-default",
            "status": "failed", "retry_on_refusal": False}
    saw_delta = False
    timeout = cfg.budgets.get("quick_timeout_s", 120)
    try:
        async with asyncio.timeout(timeout):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "stream_event":
                    ev = obj.get("event", {})
                    delta = ev.get("delta", {})
                    if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        saw_delta = True
                        yield ("delta", delta.get("text", ""))
                elif obj.get("type") == "result":
                    refused = _cli_refused(obj)
                    meta.update({
                        "status": "refused" if refused
                        else ("failed" if obj.get("is_error") else "done"),
                        "stop_reason": obj.get("stop_reason") or obj.get("subtype"),
                        "cost_usd": obj.get("total_cost_usd"),
                        "input_tokens": (obj.get("usage") or {}).get("input_tokens"),
                        "output_tokens": (obj.get("usage") or {}).get("output_tokens"),
                        "session_id": obj.get("session_id"),
                        "retry_on_refusal": refused,
                        "error": obj.get("result") if obj.get("is_error") else None,
                    })
                    if not saw_delta and not obj.get("is_error") and obj.get("result"):
                        yield ("delta", obj["result"])
            await proc.wait()
    except TimeoutError:
        proc.kill()
        meta.update({"status": "failed", "error": f"quick timeout after {timeout}s"})
    finally:
        if proc.returncode is None:
            proc.kill()
    if proc.returncode not in (0, None) and meta["status"] == "failed" and "error" not in meta:
        stderr = (await proc.stderr.read())[:500].decode(errors="replace")
        meta["error"] = f"claude exited {proc.returncode}: {stderr}"
    yield ("meta", meta)


def _cli_refused(result: dict) -> bool:
    if result.get("stop_reason") == "refusal":
        return True
    return "refusal" in str(result.get("subtype", ""))
