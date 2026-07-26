"""Messages API quick backend: memory context, continuity, and barge-in.

This backend has never executed on this machine (no API credentials — the
quick path auto-falls back to `claude -p`), so until now it was shipped
untested. Switching to it would have silently dropped three things the CLI
path provides: the injected memory blocks (locked decision #4), conversation
continuity, and barge-in cancel (locked decision #7).

Everything here fakes the Anthropic SDK — no network, no key.
"""

import asyncio
import os
import unittest
from unittest import mock

from dispatcher import quick, runner
from dispatcher.quick import ApiAbort, HistoryStore


class _Usage:
    input_tokens = 100
    output_tokens = 20


class _Final:
    def __init__(self, stop_reason="end_turn", model="claude-sonnet-5"):
        self.stop_reason, self.model, self.usage = stop_reason, model, _Usage()


class _Stream:
    """Stands in for client.messages.stream(...)'s async context manager."""

    def __init__(self, deltas, final, on_enter=None):
        self._deltas, self._final, self._on_enter = deltas, final, on_enter
        self.closed = False

    async def __aenter__(self):
        if self._on_enter:
            self._on_enter()
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    @property
    def text_stream(self):
        async def gen():
            for d in self._deltas:
                yield d
        return gen()

    async def get_final_message(self):
        return self._final


class _Client:
    """The client is pooled and reused across turns, so it is NOT used as an
    async context manager any more — closing it per call would throw away the
    connection pool that makes reuse worth ~300ms of TTFT."""

    def __init__(self, stream):
        self._stream = stream
        self.captured = None
        self.closed = False

    async def close(self):
        self.closed = True

    @property
    def messages(self):
        outer = self

        class _M:
            def stream(self, **kwargs):
                outer.captured = kwargs
                return outer._stream
        return _M()


def run_stream(cfg, stream_obj, **kw):
    """Drive _stream_api with a faked client; returns (deltas, meta, captured)."""
    client = _Client(stream_obj)
    deltas, meta = [], None

    async def go():
        nonlocal meta
        with mock.patch.object(quick, "api_client", return_value=client):
            async for kind, payload in quick._stream_api(cfg=cfg, **kw):
                if kind == "delta":
                    deltas.append(payload)
                else:
                    meta = payload
    asyncio.run(go())
    return deltas, meta, client.captured


class _Cfg:
    models = {"quick": "claude-sonnet-5", "fallback": "claude-opus-4-8"}
    budgets = {"quick_max_tokens": 1024}
    prices = {"claude-sonnet-5": {"input": 3.0, "output": 15.0}}

    def quick_cost(self, model, i, o):
        p = self.prices.get(model)
        return None if not p else i * p["input"] / 1e6 + o * p["output"] / 1e6


class TestMemoryContext(unittest.TestCase):
    def test_context_reaches_the_system_prompt(self):
        # locked decision #4: the memory blocks go into EVERY quick prompt.
        # _stream_api used not to accept `context` at all.
        _, _, cap = run_stream(_Cfg(), _Stream(["hi"], _Final()),
                               text="who am I?", model_override=None,
                               context="<memory_blocks>Editor is Neovim</memory_blocks>")
        self.assertIn("Neovim", cap["system"])
        self.assertIn("You are Jarvis", cap["system"])

    def test_no_context_still_sends_the_base_prompt(self):
        _, _, cap = run_stream(_Cfg(), _Stream(["hi"], _Final()),
                               text="hello", model_override=None)
        self.assertEqual(cap["system"], quick.QUICK_SYSTEM)


class TestContinuity(unittest.TestCase):
    def test_fresh_turn_sends_only_the_question_and_mints_a_session(self):
        h = HistoryStore()
        _, meta, cap = run_stream(_Cfg(), _Stream(["Neovim."], _Final()),
                                  text="which editor?", model_override=None,
                                  history=h)
        self.assertEqual(cap["messages"], [{"role": "user", "content": "which editor?"}])
        self.assertTrue(meta["session_id"])
        self.assertEqual(len(h), 1)

    def test_resumed_turn_replays_the_conversation(self):
        h = HistoryStore()
        _, meta, _ = run_stream(_Cfg(), _Stream(["Neovim."], _Final()),
                                text="which editor?", model_override=None, history=h)
        sid = meta["session_id"]
        _, meta2, cap = run_stream(_Cfg(), _Stream(["Lazy.nvim."], _Final()),
                                   text="and the plugin manager?", model_override=None,
                                   resume_session_id=sid, history=h)
        self.assertEqual(meta2["session_id"], sid)        # same thread
        self.assertEqual([m["content"] for m in cap["messages"]],
                         ["which editor?", "Neovim.", "and the plugin manager?"])

    def test_unknown_session_starts_fresh_instead_of_failing(self):
        h = HistoryStore()
        _, meta, cap = run_stream(_Cfg(), _Stream(["ok"], _Final()),
                                  text="hi", model_override=None,
                                  resume_session_id="never-existed", history=h)
        self.assertEqual(len(cap["messages"]), 1)
        self.assertNotEqual(meta["session_id"], "never-existed")

    def test_refusal_is_not_recorded_into_the_thread(self):
        # replaying a refusal would poison every follow-up in that session
        h = HistoryStore()
        _, meta, _ = run_stream(_Cfg(), _Stream(["I can't help"], _Final("refusal")),
                                text="bad thing", model_override=None, history=h)
        self.assertEqual(meta["status"], "refused")
        self.assertEqual(len(h), 0)

    def test_history_is_bounded_by_turns(self):
        h = HistoryStore(max_turns=2)
        sid = "s1"
        for i in range(5):
            h.record(sid, f"q{i}", f"a{i}")
        msgs = h.start(sid)[1]
        self.assertEqual([m["content"] for m in msgs], ["q3", "a3", "q4", "a4"])

    def test_sessions_are_bounded_lru(self):
        h = HistoryStore(max_sessions=2)
        for s in ("a", "b"):
            h.record(s, "q", "a")
        h.start("a")                       # touch 'a' so 'b' is now oldest
        h.record("c", "q", "a")
        self.assertEqual(len(h), 2)
        self.assertEqual(h.start("b")[1], [])   # evicted
        self.assertNotEqual(h.start("a")[1], [])

    def test_forget(self):
        h = HistoryStore()
        h.record("s", "q", "a")
        h.forget("s")
        self.assertEqual(len(h), 0)


class TestBargeIn(unittest.TestCase):
    def test_abort_handle_is_registered_for_cancel(self):
        procs = {}
        stream = _Stream(["a", "b"], _Final(),
                         on_enter=lambda: self.assertIn("t1", procs))
        run_stream(_Cfg(), stream, text="hi", model_override=None,
                   procs=procs, task_id="t1")
        self.assertIsInstance(procs["t1"], ApiAbort)

    def test_kill_stops_the_stream_and_closes_it(self):
        procs = {}
        # kill as soon as the turn registers: no delta should escape
        stream = _Stream(["one", "two", "three"], _Final(),
                         on_enter=lambda: procs["t1"].kill())
        deltas, meta, _ = run_stream(_Cfg(), stream, text="hi", model_override=None,
                                     procs=procs, task_id="t1")
        self.assertEqual(deltas, [])
        self.assertEqual(meta["status"], "cancelled")
        self.assertTrue(stream.closed)     # leaving `async with` closed the HTTP stream

    def test_abort_handle_duck_types_a_subprocess(self):
        # Service.cancel/shutdown only touch .returncode and .kill()
        a = ApiAbort()
        self.assertIsNone(a.returncode)
        a.kill()
        self.assertIsNotNone(a.returncode)
        self.assertTrue(a.aborted)


class TestMeta(unittest.TestCase):
    def test_cost_and_tokens_reported(self):
        _, meta, _ = run_stream(_Cfg(), _Stream(["hi"], _Final()),
                                text="hi", model_override=None)
        self.assertEqual(meta["backend"], "messages_api")
        self.assertEqual((meta["input_tokens"], meta["output_tokens"]), (100, 20))
        self.assertAlmostEqual(meta["cost_usd"], 100 * 3.0 / 1e6 + 20 * 15.0 / 1e6)
        self.assertEqual(meta["status"], "done")

    def test_model_override_wins(self):
        _, _, cap = run_stream(_Cfg(), _Stream(["hi"], _Final()),
                               text="hi", model_override="claude-haiku-4-5")
        self.assertEqual(cap["model"], "claude-haiku-4-5")


class TestClientPooling(unittest.TestCase):
    """A fresh AsyncAnthropic per turn means a fresh TLS handshake on every
    voice question — measured at ~300ms of TTFT on this box."""

    def setUp(self):
        quick._client_cache = None

    def tearDown(self):
        quick._client_cache = None

    def test_same_loop_reuses_one_client(self):
        made = []

        def factory():
            made.append(object())
            return made[-1]

        async def go():
            with mock.patch.dict("sys.modules",
                                 {"anthropic": mock.Mock(AsyncAnthropic=factory)}):
                return quick.api_client(), quick.api_client()
        a, b = asyncio.run(go())
        self.assertIs(a, b)
        self.assertEqual(len(made), 1)

    def test_a_new_loop_gets_its_own_client(self):
        # the underlying httpx client is bound to one loop
        def factory():
            return object()

        def once():
            async def go():
                with mock.patch.dict("sys.modules",
                                     {"anthropic": mock.Mock(AsyncAnthropic=factory)}):
                    return quick.api_client()
            return asyncio.run(go())
        self.assertIsNot(once(), once())

    def test_close_releases_and_is_idempotent(self):
        client = mock.Mock(close=mock.AsyncMock())

        async def go():
            with mock.patch.dict("sys.modules",
                                 {"anthropic": mock.Mock(AsyncAnthropic=lambda: client)}):
                quick.api_client()
            await quick.close_api_client()
            await quick.close_api_client()      # no client left; must not raise
        asyncio.run(go())
        client.close.assert_awaited_once()
        self.assertIsNone(quick._client_cache)

    def test_close_with_no_client_is_a_noop(self):
        asyncio.run(quick.close_api_client())


class TestUnusableKeyFallsBackToCli(unittest.TestCase):
    """`quick_backend: auto` routes to the API the instant the env var exists.
    A key that authenticates but can't serve (no credit, revoked) would
    otherwise take the entire quick path down — which is what a
    credit-exhausted key did on 2026-07-27."""

    def setUp(self):
        quick._api_disabled_until = 0.0

    tearDown = setUp

    def test_classifier(self):
        low = Exception("Your credit balance is too low to access the Anthropic API")
        self.assertTrue(quick.api_unusable(low))
        self.assertTrue(quick.api_unusable(Exception("invalid x-api-key")))
        self.assertFalse(quick.api_unusable(Exception("overloaded_error")))
        self.assertFalse(quick.api_unusable(TimeoutError("timed out")))

    def _drive(self, exc):
        cfg = mock.Mock(quick_backend="messages_api")
        out = []

        async def boom(*a, **k):
            raise exc
            yield  # pragma: no cover

        async def cli(*a, **k):
            yield ("delta", "from the cli")
            yield ("meta", {"backend": "claude_cli", "status": "done"})

        async def go():
            with mock.patch.object(quick, "_stream_api", boom), \
                 mock.patch.object(quick, "_stream_cli", cli):
                async for item in quick.stream("hi", cfg):
                    out.append(item)
        asyncio.run(go())
        return out

    def test_credit_error_transparently_uses_the_cli(self):
        out = self._drive(Exception("Your credit balance is too low"))
        self.assertEqual(out[0], ("delta", "from the cli"))
        self.assertEqual(out[-1][1]["backend"], "claude_cli")

    def test_unrelated_error_still_raises(self):
        with self.assertRaises(ValueError):
            self._drive(ValueError("something else entirely"))

    def test_partly_streamed_answer_is_not_restarted(self):
        # half the reply is already spoken; replaying it would repeat words
        cfg = mock.Mock(quick_backend="messages_api")

        async def half(*a, **k):
            yield ("delta", "the answer is ")
            raise Exception("credit balance is too low")

        async def cli(*a, **k):
            yield ("delta", "SHOULD NOT APPEAR")

        async def go():
            with mock.patch.object(quick, "_stream_api", half), \
                 mock.patch.object(quick, "_stream_cli", cli):
                async for _ in quick.stream("hi", cfg):
                    pass
        with self.assertRaises(Exception):
            asyncio.run(go())

    def test_cooldown_stops_auto_routing_to_a_dud_key(self):
        cfg = mock.Mock(quick_backend="auto")
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-x"}):
            self.assertEqual(quick.resolve_backend(cfg), "messages_api")

            async def go():
                quick.disable_api("no credit")
                return quick.resolve_backend(cfg)
            self.assertEqual(asyncio.run(go()), "claude_cli")

    def test_cooldown_does_not_affect_the_no_key_case(self):
        cfg = mock.Mock(quick_backend="auto")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(quick.resolve_backend(cfg), "claude_cli")


class TestCliEnvStripsApiKey(unittest.TestCase):
    """The CLI backend is the subscription path. The `claude` CLI prefers an
    API key over the claude.ai login when one is in the environment — so an
    exported key replaces the subscription rather than supplementing it, and an
    unfunded one breaks every CLI run (observed 2026-07-27)."""

    def test_credentials_are_removed(self):
        cfg = mock.Mock(claude_config_dir="/home/ayra/.claude-per")
        with mock.patch.dict("os.environ",
                             {"ANTHROPIC_API_KEY": "sk-x",
                              "ANTHROPIC_AUTH_TOKEN": "tok",
                              "PATH": "/usr/bin"}, clear=True):
            env = runner.cli_env(cfg)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertEqual(env["PATH"], "/usr/bin")            # rest inherited
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/ayra/.claude-per")

    def test_no_config_dir_leaves_it_unset(self):
        cfg = mock.Mock(claude_config_dir=None)
        with mock.patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            self.assertNotIn("CLAUDE_CONFIG_DIR", runner.cli_env(cfg))

    def test_os_environ_is_not_mutated(self):
        cfg = mock.Mock(claude_config_dir=None)
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-x"}, clear=True):
            runner.cli_env(cfg)
            self.assertIn("ANTHROPIC_API_KEY", os.environ)   # only the copy is filtered


class TestBackendRouting(unittest.TestCase):
    def test_tools_force_the_cli_backend(self):
        # a screenshot/file question needs Read, which the API path can't do
        cfg = mock.Mock(quick_backend="messages_api")
        with mock.patch.object(quick, "_stream_cli") as cli, \
             mock.patch.object(quick, "_stream_api") as api:
            async def drain():
                async for _ in quick.stream("q", cfg, tools=["Read"]):
                    pass
            cli.return_value = _empty()
            asyncio.run(drain())
            cli.assert_called_once()
            api.assert_not_called()


async def _empty():
    return
    yield  # pragma: no cover


if __name__ == "__main__":
    unittest.main()
