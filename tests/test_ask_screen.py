"""Ask-about-my-screen tests: /stt + /screenshots + /ask endpoints, the
resolve_screenshot traversal guard, and the screenshot-aware quick path.
TestClient WITHOUT `with` skips lifespan on purpose — no queue watcher,
scheduler, or startup reindex in tests."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from dispatcher.config import Config
from dispatcher.main import create_app
from dispatcher.service import Service, resolve_screenshot

from jarvis import ask_screen

PNG = bytes.fromhex("89504e470d0a1a0a") + b"not-a-real-png-but-bytes"


def _cfg(tmp: str) -> Config:
    cfg = Config(root=Path(tmp))
    cfg.db_path = Path(tmp) / "t.db"
    cfg.queue_dir = Path(tmp) / "queue"
    cfg.screenshots_dir = Path(tmp) / "data" / "screenshots"
    cfg.screenshots_dir.mkdir(parents=True)
    return cfg


class TestResolveScreenshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self.tmp.name)
        (self.cfg.screenshots_dir / "ok.png").write_bytes(PNG)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid(self):
        p = resolve_screenshot(self.cfg, "ok.png")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "ok.png")

    def test_rejects_missing_wrong_suffix_and_traversal(self):
        (Path(self.tmp.name) / "data" / "evil.png").write_bytes(PNG)
        (Path(self.tmp.name) / "config.yaml").write_text("x")
        for name in ("missing.png", "ok.jpg", "", "../evil.png",
                     "../config.yaml", "sub/ok.png"):
            self.assertIsNone(resolve_screenshot(self.cfg, name), name)


class _FakeSTT:
    def __init__(self, reply="hello world", boom=False):
        self.reply, self.boom = reply, boom

    def transcribe_bytes(self, data):
        if self.boom:
            raise ValueError("bad container")
        return self.reply


class TestEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self.tmp.name)
        (self.cfg.screenshots_dir / "ok.png").write_bytes(PNG)
        ui_dist = Path(self.tmp.name) / "ui" / "dist"
        ui_dist.mkdir(parents=True)
        (ui_dist / "index.html").write_text("<!doctype html><div id=root></div>")
        self.client = TestClient(create_app(self.cfg))

    def tearDown(self):
        self.tmp.cleanup()

    def test_screenshot_roundtrip_and_404(self):
        r = self.client.get("/screenshots/ok.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, PNG)
        self.assertEqual(self.client.get("/screenshots/nope.png").status_code, 404)

    def test_ask_serves_html(self):
        r = self.client.get("/ask")
        self.assertEqual(r.status_code, 200)
        self.assertIn("doctype html", r.text)

    def test_system_docs_serves_html(self):
        r = self.client.get("/system-docs")
        self.assertEqual(r.status_code, 200)
        self.assertIn("doctype html", r.text)

    def test_swagger_still_owns_docs(self):
        # the docs page deliberately lives at /system-docs so FastAPI's
        # auto-mounted Swagger UI keeps /docs
        r = self.client.get("/docs")
        self.assertEqual(r.status_code, 200)
        self.assertIn("swagger", r.text.lower())

    def test_stt_ok(self):
        with patch("dispatcher.stt.get_stt", return_value=_FakeSTT()):
            r = self.client.post("/stt", content=b"x" * 500,
                                 headers={"content-type": "audio/webm"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"text": "hello world"})

    def test_stt_empty_body_400(self):
        self.assertEqual(self.client.post("/stt", content=b"").status_code, 400)

    def test_stt_decode_failure_422(self):
        with patch("dispatcher.stt.get_stt", return_value=_FakeSTT(boom=True)):
            r = self.client.post("/stt", content=b"x" * 500)
        self.assertEqual(r.status_code, 422)

    def test_stt_over_cap_413(self):
        # M1: an oversized audio body is rejected before it reaches STT
        with patch("dispatcher.main.MAX_STT_BYTES", 200):
            r = self.client.post("/stt", content=b"x" * 500)
        self.assertEqual(r.status_code, 413)

    def test_cross_origin_post_rejected(self):
        # M1: a state-changing request from another site's page is refused
        r = self.client.post("/stt", content=b"x" * 500,
                             headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_loopback_origin_post_allowed(self):
        with patch("dispatcher.stt.get_stt", return_value=_FakeSTT()):
            r = self.client.post("/stt", content=b"x" * 500,
                                 headers={"origin": "http://localhost:8765"})
        self.assertEqual(r.status_code, 200)

    def test_cross_origin_get_allowed(self):
        # only state-changing methods are guarded; a GET is untouched
        r = self.client.get("/screenshots/ok.png",
                            headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 200)

    def test_public_host_origin_allowed(self):
        # phone access: the SPA loaded over Tailscale Serve POSTs with that
        # Origin; security.public_hosts must let it through the CSRF guard.
        self.cfg.security = {"public_hosts": ["box.tail.ts.net"]}
        client = TestClient(create_app(self.cfg))
        with patch("dispatcher.stt.get_stt", return_value=_FakeSTT()):
            r = client.post("/stt", content=b"x" * 500,
                            headers={"origin": "https://box.tail.ts.net"})
        self.assertEqual(r.status_code, 200)

    def test_public_host_does_not_open_others(self):
        # allowlisting one host must not admit a different cross-origin site
        self.cfg.security = {"public_hosts": ["box.tail.ts.net"]}
        client = TestClient(create_app(self.cfg))
        r = client.post("/stt", content=b"x" * 500,
                        headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)


class TestScreenshotQuickPath(unittest.IsolatedAsyncioTestCase):
    """Step-4 behavior: first turn wraps the prompt + grants Read; resumed
    follow-ups send the raw question; a missing file degrades cleanly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self.tmp.name)
        self.cfg.quick_session_idle_minutes = 10
        (self.cfg.screenshots_dir / "shot1.png").write_bytes(PNG)
        self.svc = Service(self.cfg)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_stream(self):
        calls = self.calls

        async def stream(text, cfg, model_override=None, tools=None, context="",
                         resume_session_id=None, **kwargs):
            calls.append({"text": text, "tools": tools, "context": context,
                          "resume": resume_session_id})
            yield ("delta", "an answer")
            yield ("meta", {"status": "done", "session_id": "sess1"})
        return stream

    async def _run(self, text, metadata):
        task = await self.svc.create_task(text, "screen:shot1", "quick",
                                          None, metadata)
        with patch("dispatcher.service.quick.stream", self._fake_stream()):
            async for _ in self.svc.stream_quick(task):
                pass
        return task

    async def test_first_turn_wraps_and_grants_read(self):
        task = await self._run("what app is this?", {"screenshot": "shot1.png"})
        call = self.calls[0]
        self.assertIn("Read tool", call["text"])
        self.assertIn("shot1.png", call["text"])
        self.assertIn("what app is this?", call["text"])
        self.assertIn("Read", call["tools"])
        self.assertIn("screenshot", call["context"])
        # the DB keeps the raw question, not the wrapped prompt
        self.assertEqual(self.svc.db.get_task(task["id"])["text"],
                         "what app is this?")

    async def test_resumed_followup_sends_raw_question(self):
        await self._run("what app is this?", {"screenshot": "shot1.png"})
        await self._run("what about the sidebar?", {"screenshot": "shot1.png"})
        second = self.calls[1]
        self.assertEqual(second["resume"], "sess1")
        self.assertEqual(second["text"], "what about the sidebar?")
        self.assertIn("Read", second["tools"])  # tools persist for fresh retries

    async def test_missing_screenshot_degrades_to_plain_answer(self):
        await self._run("what is this?", {"screenshot": "gone.png"})
        call = self.calls[0]
        self.assertEqual(call["text"], "what is this?")
        self.assertIsNone(call["tools"])


class TestLaunchPopupPrecheck(unittest.TestCase):
    """M9: the module runs from a terminal-less GUI shortcut, so a missing
    chromium (Popen → FileNotFoundError) was a silent failure. launch_popup
    must shutil.which-precheck and fail() with the exact binary name."""

    def _cfg(self, chromium="chromium"):
        return SimpleNamespace(
            ask_screen={"chromium_bin": chromium, "window_size": [520, 720]},
            dispatcher_url="http://127.0.0.1:8765")

    def test_missing_chromium_fails_before_popen(self):
        with patch("jarvis.ask_screen.shutil.which", return_value=None), \
             patch("jarvis.ask_screen.subprocess.Popen") as popen, \
             patch("jarvis.ask_screen.fail", side_effect=SystemExit) as failed:
            with self.assertRaises(SystemExit):
                ask_screen.launch_popup(self._cfg("no-such-browser"), "shot1")
        failed.assert_called_once()
        self.assertIn("no-such-browser", failed.call_args[0][0])
        popen.assert_not_called()

    def test_present_chromium_launches(self):
        with patch("jarvis.ask_screen.shutil.which", return_value="/usr/bin/chromium"), \
             patch("jarvis.ask_screen.subprocess.Popen") as popen, \
             patch("jarvis.ask_screen.fail", side_effect=SystemExit) as failed:
            ask_screen.launch_popup(self._cfg(), "shot1")
        failed.assert_not_called()
        popen.assert_called_once()
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], "chromium")
        self.assertIn("--app=http://127.0.0.1:8765/ask?shot=shot1", argv)


class TestRequestPathErrorReply(unittest.TestCase):
    """L10: jeepney returns D-Bus ERROR messages without raising; the old code
    treated body[0] of an error as an object path → misleading MatchRuleInvalid.
    _request_path must detect the ERROR message type and surface the real text."""

    def _reply(self, message_type, body):
        return SimpleNamespace(header=SimpleNamespace(message_type=message_type), body=body)

    def test_error_reply_calls_fail_with_portal_text(self):
        from jeepney import MessageType

        reply = self._reply(MessageType.error,
                            ["org.freedesktop.DBus.Error.Failed: portal exploded"])
        with patch("jarvis.ask_screen.fail", side_effect=SystemExit) as failed:
            with self.assertRaises(SystemExit):
                ask_screen._request_path(reply)
        failed.assert_called_once()
        self.assertIn("portal exploded", failed.call_args[0][0])

    def test_error_reply_empty_body_no_indexerror(self):
        from jeepney import MessageType

        reply = self._reply(MessageType.error, [])
        with patch("jarvis.ask_screen.fail", side_effect=SystemExit) as failed:
            with self.assertRaises(SystemExit):
                ask_screen._request_path(reply)
        self.assertIn("unknown D-Bus error", failed.call_args[0][0])

    def test_method_return_yields_request_path(self):
        from jeepney import MessageType

        path = "/org/freedesktop/portal/desktop/request/x/y"
        reply = self._reply(MessageType.method_return, [path])
        with patch("jarvis.ask_screen.fail", side_effect=SystemExit) as failed:
            self.assertEqual(ask_screen._request_path(reply), path)
        failed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
