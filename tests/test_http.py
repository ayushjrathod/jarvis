"""HTTP layer tests: the routes in dispatcher/main.py.

The whole front door was untested except the ask-screen routes (tests/
test_ask_screen.py) — including POST /task, which every client in the system
goes through. This file covers the wire formats those clients parse, the CSRF
origin guard on state-changing routes, the /events SSE stream, and the
lifespan's background tasks.

HERMETIC BY CONSTRUCTION. Nothing here may spend quota or touch the developer's
machine:
  * every Config is built by hand against a TemporaryDirectory — never
    Config.load(), so the real config.yaml, vault and data/mission.db are
    unreachable;
  * the model is patched at the seams the rest of the suite uses
    (`dispatcher.service.quick.stream`, `Service.start_agentic`), so no
    `claude` subprocess is ever spawned;
  * every executor that would reach the session bus (desktop.run_intent,
    spotify.run_intent) is patched, so no screen locks and no music plays.

TestClient WITHOUT `with` skips the lifespan, which is what the rest of the
suite wants (no queue watcher, no scheduler, no startup reindex). TestLifespan
is the deliberate exception and stubs all five background jobs.
"""

import asyncio
import copy
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from dispatcher.config import Config
from dispatcher.main import create_app


# -- helpers -----------------------------------------------------------------


def _cfg(tmp: str, **overrides) -> Config:
    """A Config pointed entirely at a temp dir. Every collection-valued field
    starts empty, which is what disables media/desktop/automations/inbox/
    embeddings — i.e. the default test config can have no side effects."""
    cfg = Config(root=Path(tmp))
    cfg.db_path = Path(tmp) / "t.db"
    cfg.queue_dir = Path(tmp) / "queue"
    cfg.screenshots_dir = Path(tmp) / "data" / "screenshots"
    cfg.screenshots_dir.mkdir(parents=True)
    # A tripwire, not a setting: every path that would really invoke the model
    # is patched, so nothing should ever exec this. If a future edit misses a
    # seam, the run dies on a missing binary instead of quietly spending the
    # user's plan quota — which is the whole reason this file exists offline.
    cfg.claude_bin = str(Path(tmp) / "no-such-claude-binary")
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """[(event, data), …] from an SSE body. Deliberately hand-rolled and
    strict about the framing: the React SPA (ui/src) and the voice brain
    (jarvis/engines/brain_dispatcher.py) both parse this by hand too, so a
    frame that no longer looks like `event: X\\ndata: {…}\\n\\n` breaks two
    clients silently."""
    out = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block or block.startswith(":"):
            continue
        lines = block.split("\n")
        assert lines[0].startswith("event: "), block
        assert lines[1].startswith("data: "), block
        out.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


def fake_quick_stream(deltas=("hello",), meta=None):
    """Stand-in for dispatcher.quick.stream — same yield contract
    (('delta', str) … then ('meta', dict)), no subprocess."""
    payload = {"status": "done", "session_id": "sess-1", "cost_usd": 0.012,
               "model": "test-model", "output_tokens": 4}
    payload.update(meta or {})

    async def stream(text, cfg, model_override=None, tools=None, context="",
                     resume_session_id=None, **kwargs):
        for d in deltas:
            yield ("delta", d)
        yield ("meta", payload)
    return stream


class HTTPTestCase(unittest.TestCase):
    """A dispatcher app on a throwaway root, with the divert path off by
    default so a test that means to exercise routing isn't hijacked by one."""

    cfg_overrides: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # deep-copied: cfg_overrides is class state, and a test that mutated a
        # config block in place would leak into its siblings
        self.cfg = _cfg(self.tmp.name, **copy.deepcopy(self.cfg_overrides))
        self.app = create_app(self.cfg)
        self.svc = self.app.state.service
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def no_divert(self):
        self.svc.try_divert = AsyncMock(return_value=None)

    def divert(self, **payload):
        mock = AsyncMock(return_value=payload)
        self.svc.try_divert = mock
        return mock


# -- POST /task: the divert path ---------------------------------------------


class TestPostTaskDivertWireFormat(HTTPTestCase):
    """A deterministic divert (a command that needs no model) is rendered as a
    three-frame SSE conversation so the SPA and the voice client can consume it
    with the same parser they use for a real quick answer. These assertions are
    exact on purpose: the frames ARE the contract."""

    def test_a_divert_streams_task_delta_and_done_frames(self):
        self.divert(kind="media", speech="Playing jazz.", ok=True)
        r = self.client.post("/task", json={"text": "play jazz", "mode": "auto"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(parse_sse(r.text), [
            ("task", {"task_id": None, "kind": "media"}),
            ("delta", {"text": "Playing jazz."}),
            ("done", {"status": "done", "kind": "media"}),
        ])

    def test_a_diverts_task_frame_carries_a_null_task_id(self):
        # No task row is created over HTTP, and the clients rely on task_id
        # being present-but-null rather than absent (they read it directly).
        self.divert(kind="desktop", speech="Locked.", ok=True,
                    desktop_status="done", confirm_id=None)
        events = dict(parse_sse(
            self.client.post("/task", json={"text": "lock the screen"}).text))
        self.assertIn("task_id", events["task"])
        self.assertIsNone(events["task"]["task_id"])

    def test_a_diverts_done_frame_omits_keys_whose_value_is_none(self):
        # The comprehension in post_task filters None values deliberately: a
        # media divert has no automation_id/desktop_status/confirm_id, and the
        # clients test for key presence. A refactor that "simplifies" the dict
        # back to unconditional keys breaks that silently, hence this test.
        self.divert(kind="media", speech="Paused.", ok=True)
        done = dict(parse_sse(self.client.post("/task", json={"text": "pause"}).text))["done"]
        self.assertEqual(set(done), {"status", "kind"})
        for absent in ("automation_id", "desktop_status", "confirm_id"):
            self.assertNotIn(absent, done)

    def test_a_diverts_done_frame_always_carries_status(self):
        # `status` is exempt from the None filter (`or k == "status"`), so it
        # survives even when its value is falsy — the client branches on it.
        self.divert(kind="automation", speech="Scheduled.", ok=True,
                    automation_id=None)
        done = dict(parse_sse(
            self.client.post("/task", json={"text": "every morning, brief me"}).text))["done"]
        self.assertIn("status", done)
        self.assertNotIn("automation_id", done)

    def test_a_divert_forwards_the_ids_a_client_needs_to_follow_up(self):
        # A parked desktop confirmation is only answerable if confirm_id
        # reaches the client; automation_id is what the dashboard links to.
        self.divert(kind="desktop", speech="Shall I open github.com?", ok=False,
                    desktop_status="needs_confirmation", confirm_id="abc123")
        done = dict(parse_sse(
            self.client.post("/task", json={"text": "open github.com"}).text))["done"]
        self.assertEqual(done, {"status": "done", "kind": "desktop",
                                "desktop_status": "needs_confirmation",
                                "confirm_id": "abc123"})

    def test_a_diverted_command_leaves_no_task_row(self):
        # Documented behavior: over HTTP the caller is waiting on the answer and
        # none of these is a cancellable task, so nothing is recorded. (Through
        # Service.submit() — queue/automation — a divert IS recorded, born
        # settled; that asymmetry is intentional.)
        self.divert(kind="media", speech="Playing jazz.", ok=True)
        self.client.post("/task", json={"text": "play jazz"})
        self.assertEqual(self.client.get("/tasks").json(), [])

    def test_a_divert_that_did_not_act_reports_failed_over_http_too(self):
        # This test previously PINNED the opposite, as a reported finding: the
        # handler hardcoded "done" while Service._settle_divert (the same
        # divert arriving from the queue or a fired automation) derived the
        # status from the executor's `ok`. So the two halves of one feature
        # disagreed, and an unresolved "put on something chill" was reported to
        # an SSE client as a successful play with no field to tell it apart.
        # Fixed in dispatcher/main.py 2026-08-10; this is the deliberate flip.
        self.divert(kind="media", speech="I couldn't work out what to play.",
                    ok=False)
        done = dict(parse_sse(
            self.client.post("/task", json={"text": "put on something chill"}).text))["done"]
        self.assertEqual(done, {"status": "failed", "kind": "media"})

    def test_a_divert_that_acted_still_reports_done(self):
        self.divert(kind="media", speech="Playing jazz.", ok=True)
        done = dict(parse_sse(
            self.client.post("/task", json={"text": "play jazz"}).text))["done"]
        self.assertEqual(done, {"status": "done", "kind": "media"})

    def test_a_parked_confirmation_is_not_a_failure_over_http(self):
        # The one place the HTTP renderer deliberately diverges from
        # _settle_divert: here the caller is present and gets confirm_id, so
        # "Shall I read your clipboard?" is a live interaction, not a failure.
        # On the queue/automation path the same outcome settles 'failed'
        # because nobody is watching that source and the intent just expires.
        self.divert(kind="desktop", speech="Shall I read your clipboard?",
                    ok=False, desktop_status="needs_confirmation",
                    confirm_id="abc123")
        done = dict(parse_sse(
            self.client.post("/task", json={"text": "what's on my clipboard"}).text))["done"]
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["confirm_id"], "abc123")

    def test_an_explicit_mode_bypasses_the_divert_entirely(self):
        # mode=auto only — that gate is what keeps quick/agentic reachable for
        # text a parser would otherwise swallow.
        mock = self.divert(kind="media", speech="Playing jazz.", ok=True)
        with patch("dispatcher.service.quick.stream", fake_quick_stream()):
            r = self.client.post("/task", json={"text": "play jazz", "mode": "quick"})
        mock.assert_not_awaited()
        self.assertEqual([e for e, _ in parse_sse(r.text)],
                         ["task", "delta", "done"])


# -- POST /task: the quick path ----------------------------------------------


class TestPostTaskQuickPath(HTTPTestCase):
    def setUp(self):
        super().setUp()
        self.no_divert()

    def test_a_quick_question_streams_task_delta_and_done_as_an_event_stream(self):
        with patch("dispatcher.service.quick.stream",
                   fake_quick_stream(deltas=["Paris", " it is."])):
            r = self.client.post("/task", json={"text": "what is the capital of France?"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        events = parse_sse(r.text)
        self.assertEqual([e for e, _ in events], ["task", "delta", "delta", "done"])
        self.assertEqual("".join(d["text"] for e, d in events if e == "delta"),
                         "Paris it is.")

    def test_the_quick_task_frame_names_the_row_the_client_can_cancel(self):
        # Unlike a divert, a quick answer IS a task: the id in the first frame
        # is what POST /task/{id}/cancel (and voice barge-in) uses.
        with patch("dispatcher.service.quick.stream", fake_quick_stream()):
            events = dict(parse_sse(
                self.client.post("/task", json={"text": "hello there"}).text))
        task_id = events["task"]["task_id"]
        self.assertEqual(events["task"]["kind"], "quick")
        self.assertEqual(self.client.get(f"/task/{task_id}").json()["status"], "done")
        self.assertEqual(events["done"]["task_id"], task_id)

    def test_the_quick_done_frame_carries_status_cost_and_latency(self):
        with patch("dispatcher.service.quick.stream", fake_quick_stream()):
            done = dict(parse_sse(
                self.client.post("/task", json={"text": "hello there"}).text))["done"]
        self.assertEqual(done["status"], "done")
        self.assertAlmostEqual(done["cost_usd"], 0.012)
        self.assertEqual(done["model"], "test-model")
        for key in ("error", "speech", "ttft_ms", "tokens_per_s"):
            self.assertIn(key, done)

    def test_a_failed_quick_run_still_settles_and_reports_the_error(self):
        # The client must be able to distinguish "no answer" from "no frames";
        # a failure keeps the same three-frame shape.
        with patch("dispatcher.service.quick.stream",
                   fake_quick_stream(deltas=[],
                                     meta={"status": "failed", "error": "boom"})):
            events = dict(parse_sse(
                self.client.post("/task", json={"text": "hello there"}).text))
        self.assertEqual(events["done"]["status"], "failed")
        self.assertEqual(events["done"]["error"], "boom")
        self.assertEqual(
            self.client.get(f"/task/{events['task']['task_id']}").json()["status"],
            "failed")


# -- POST /task: the agentic path --------------------------------------------


class TestPostTaskAgenticPath(HTTPTestCase):
    def setUp(self):
        super().setUp()
        self.no_divert()

    def test_an_agentic_task_returns_202_with_a_speakable_ack(self):
        # 202 + JSON (not SSE) is how a client knows which path it got: the
        # voice brain speaks `ack` and then waits for the SSE done event.
        with patch("dispatcher.service.Service.start_agentic") as start:
            r = self.client.post("/task", json={"text": "organize my notes",
                                                "mode": "agentic"})
        self.assertEqual(r.status_code, 202)
        self.assertTrue(r.headers["content-type"].startswith("application/json"))
        body = r.json()
        self.assertEqual(set(body), {"task_id", "kind", "status", "ack"})
        self.assertEqual((body["kind"], body["status"]), ("agentic", "queued"))
        self.assertIsInstance(body["ack"], str)
        self.assertTrue(body["ack"])
        start.assert_called_once()

    def test_an_agentic_task_is_persisted_queued_before_it_runs(self):
        with patch("dispatcher.service.Service.start_agentic"):
            task_id = self.client.post(
                "/task", json={"text": "organize my notes", "mode": "agentic"}).json()["task_id"]
        row = self.client.get(f"/task/{task_id}").json()
        self.assertEqual((row["kind"], row["status"]), ("agentic", "queued"))
        self.assertEqual(row["runs"], [])   # nothing ran: start_agentic was stubbed

    def test_the_classifier_picks_the_agentic_path_on_mode_auto(self):
        # mode=auto with no divert falls through to route(); a file-writing
        # request must not be answered on the streaming quick path.
        with patch("dispatcher.service.Service.start_agentic"):
            r = self.client.post("/task", json={
                "text": "summarize the files in vault/notes into vault/briefs/x.md"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["kind"], "agentic")


class TestPostTaskTrustBoundary(HTTPTestCase):
    """H1 over the wire. POST /task is reachable from the dashboard, the phone
    over Tailscale Serve and any script — metadata arriving here may NARROW a
    task's tools/budget but never widen them. The sanitizer is unit-tested;
    what was not tested is that the HTTP route actually routes through it."""

    def setUp(self):
        super().setUp()
        self.no_divert()

    def _submit(self, metadata):
        with patch("dispatcher.service.Service.start_agentic"):
            task_id = self.client.post("/task", json={
                "text": "organize my notes", "mode": "agentic",
                "metadata": metadata}).json()["task_id"]
        row = self.client.get(f"/task/{task_id}").json()
        return json.loads(row["metadata"] or "{}")

    def test_a_caller_cannot_grant_itself_tools(self):
        meta = self._submit({"allowed_tools": ["Bash", "Edit"], "task_type": "note"})
        self.assertNotIn("allowed_tools", meta)
        self.assertEqual(meta["task_type"], "note")   # everything else survives

    def test_a_caller_cannot_inject_a_session_to_resume(self):
        self.assertNotIn("resume_session_id",
                         self._submit({"resume_session_id": "someone-elses-session"}))

    def test_a_caller_cannot_raise_the_budget_above_the_cap(self):
        self.cfg.budgets = {"max_cost_per_task_usd": 0.5}
        self.assertEqual(self._submit({"max_cost_usd": 99})["max_cost_usd"], 0.5)

    def test_a_caller_may_still_lower_the_budget(self):
        self.cfg.budgets = {"max_cost_per_task_usd": 0.5}
        self.assertEqual(self._submit({"max_cost_usd": 0.05})["max_cost_usd"], 0.05)

    def test_a_caller_cannot_force_the_refusal_fallback(self):
        # simulate_refusal doubles a run onto the fallback (an Opus); it is a
        # smoke-test seam, opt-in via security.allow_simulate_refusal
        self.assertNotIn("simulate_refusal", self._submit({"simulate_refusal": True}))


class TestPostTaskValidation(HTTPTestCase):
    def test_a_body_without_text_is_422(self):
        self.assertEqual(self.client.post("/task", json={"source": "api"}).status_code, 422)

    def test_a_non_json_body_is_422_not_a_500(self):
        self.assertEqual(
            self.client.post("/task", content=b"not json").status_code, 422)


# -- GET /task/{id}, GET /tasks, GET /task/{id}/steps ------------------------


class TestTaskLookupAndListing(HTTPTestCase):
    def seed(self, text, source="api", kind="quick", status="done"):
        task = self.svc.db.create_task(text, source, kind)
        self.svc.db.set_task_status(task["id"], status)
        return task["id"]

    def test_an_unknown_task_id_is_404_not_an_empty_body(self):
        self.assertEqual(self.client.get("/task/deadbeefcafe").status_code, 404)

    def test_a_known_task_comes_back_with_its_runs_attached(self):
        task_id = self.seed("hello there")
        run_id = self.svc.db.create_run(task_id, 1, "test-model")
        self.svc.db.finish_run(run_id, "done", cost_usd=0.01, output_text="hi")
        row = self.client.get(f"/task/{task_id}").json()
        self.assertEqual(row["id"], task_id)
        self.assertEqual([r["status"] for r in row["runs"]], ["done"])

    def test_tasks_lists_newest_first_with_the_row_shape_the_dashboard_reads(self):
        self.seed("first one")
        self.seed("second one")
        rows = self.client.get("/tasks").json()
        self.assertEqual(len(rows), 2)
        for key in ("id", "created_at", "source", "kind", "status", "text",
                    "area", "metadata"):
            self.assertIn(key, rows[0])

    def test_tasks_filters_by_status_and_by_kind(self):
        self.seed("a quick done one", kind="quick", status="done")
        self.seed("an agentic failed one", kind="agentic", status="failed")
        self.assertEqual(len(self.client.get("/tasks?status=done").json()), 1)
        self.assertEqual(len(self.client.get("/tasks?status=failed").json()), 1)
        self.assertEqual(
            self.client.get("/tasks?kind=agentic").json()[0]["text"],
            "an agentic failed one")
        # the two filters combine rather than override
        self.assertEqual(self.client.get("/tasks?kind=agentic&status=done").json(), [])

    def test_tasks_honors_limit(self):
        for i in range(5):
            self.seed(f"task {i}")
        self.assertEqual(len(self.client.get("/tasks?limit=2").json()), 2)

    def test_steps_are_404_for_an_unknown_task_and_keyed_by_run_for_a_known_one(self):
        self.assertEqual(self.client.get("/task/nope/steps").status_code, 404)
        task_id = self.seed("an agentic one", kind="agentic")
        run_id = self.svc.db.create_run(task_id, 1, "test-model")
        self.svc.db.add_run_steps(run_id, [{"type": "init", "summary": "start"},
                                           {"type": "text", "summary": "thinking"}])
        steps = self.client.get(f"/task/{task_id}/steps").json()
        self.assertEqual(list(steps), [str(run_id)])   # JSON object keys are strings
        self.assertEqual([s["step_type"] for s in steps[str(run_id)]],
                         ["init", "text"])


# -- POST /task/{id}/cancel --------------------------------------------------


class TestCancel(HTTPTestCase):
    def test_cancelling_an_unknown_task_is_409(self):
        r = self.client.post("/task/deadbeefcafe/cancel")
        self.assertEqual(r.status_code, 409)

    def test_cancelling_an_already_settled_task_is_409(self):
        # 409 rather than 200 so a client can tell "I stopped it" from "it had
        # already finished" — barge-in races with completion constantly.
        for settled in ("done", "failed", "cancelled"):
            task = self.svc.db.create_task(f"a {settled} task", "api", "quick")
            self.svc.db.set_task_status(task["id"], settled)
            r = self.client.post(f"/task/{task['id']}/cancel")
            self.assertEqual(r.status_code, 409, settled)

    def test_cancelling_a_live_task_settles_it_and_its_open_runs(self):
        task = self.svc.db.create_task("a running task", "api", "agentic")
        self.svc.db.set_task_status(task["id"], "running")
        run_id = self.svc.db.create_run(task["id"], 1, "test-model")
        r = self.client.post(f"/task/{task['id']}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"task_id": task["id"], "status": "cancelled"})
        row = self.client.get(f"/task/{task['id']}").json()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["runs"][0]["status"], "cancelled")
        self.assertEqual(row["runs"][0]["id"], run_id)

    def test_a_queued_task_can_be_cancelled_before_it_starts(self):
        task = self.svc.db.create_task("a queued task", "api", "agentic")
        self.assertEqual(self.client.post(f"/task/{task['id']}/cancel").status_code, 200)


# -- the CSRF origin guard ---------------------------------------------------


class TestOriginGuardOnTaskRoutes(HTTPTestCase):
    """tests/test_ask_screen.py pins the guard's behavior on /stt. What is not
    covered there is the route that matters — POST /task — and the fact that
    the guard runs as middleware, i.e. BEFORE the handler, so a rejected
    request must leave no trace."""

    def test_a_foreign_origin_cannot_submit_a_task(self):
        r = self.client.post("/task", json={"text": "hello there"},
                             headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("cross-origin", r.json()["detail"])

    def test_a_rejected_task_is_never_created(self):
        # the point of a middleware guard: the handler must not have run at all
        self.client.post("/task", json={"text": "rm -rf my notes"},
                         headers={"origin": "https://evil.example"})
        self.assertEqual(self.client.get("/tasks").json(), [])

    def test_a_loopback_origin_may_submit_a_task(self):
        self.no_divert()
        with patch("dispatcher.service.quick.stream", fake_quick_stream()):
            r = self.client.post("/task", json={"text": "hello there", "mode": "quick"},
                                 headers={"origin": "http://127.0.0.1:8765"})
        self.assertEqual(r.status_code, 200)

    def test_an_allowlisted_public_host_may_submit_a_task(self):
        # the phone over Tailscale Serve: the SPA's Origin is the MagicDNS name
        self.cfg.security = {"public_hosts": ["box.tail.ts.net"]}
        client = TestClient(create_app(self.cfg))
        client.app.state.service.try_divert = AsyncMock(return_value=None)
        with patch("dispatcher.service.quick.stream", fake_quick_stream()):
            r = client.post("/task", json={"text": "hello there", "mode": "quick"},
                            headers={"origin": "https://box.tail.ts.net"})
        self.assertEqual(r.status_code, 200)

    def test_the_guard_covers_delete_not_just_post(self):
        # /automations/{id} is the only DELETE the API exposes; the guarded-
        # method set must actually include it.
        r = self.client.delete("/automations/1",
                               headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_a_foreign_origin_may_still_read(self):
        # only state-changing methods are guarded — a GET is untouched, so a
        # 403 here would mean the guard had grown past its remit
        r = self.client.get("/tasks", headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 200)

    def test_origin_null_cannot_submit_a_task(self):
        # what a sandboxed iframe sends; it must not be read as "no Origin"
        r = self.client.post("/task", json={"text": "hello there"},
                             headers={"origin": "null"})
        self.assertEqual(r.status_code, 403)


# -- GET /events -------------------------------------------------------------


class TestEventsStream(unittest.IsolatedAsyncioTestCase):
    """/events loops forever with a 25s keepalive, and starlette's TestClient
    buffers a whole response body before returning — so driving this route
    through TestClient would hang the suite. The ASGI app is called directly
    instead, and `receive` reports a disconnect as soon as the frames under
    test have arrived, which is exactly what a browser closing the tab does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self.tmp.name)
        self.app = create_app(self.cfg)
        self.svc = self.app.state.service

    def tearDown(self):
        self.tmp.cleanup()

    async def _drive(self, frames_wanted: int, before_disconnect=None):
        """Run GET /events until `frames_wanted` body chunks have been sent,
        then disconnect. Returns (start_message, [body bytes])."""
        scope = {"type": "http", "asgi": {"version": "3.0"},
                 "http_version": "1.1", "method": "GET", "path": "/events",
                 "raw_path": b"/events", "query_string": b"", "root_path": "",
                 "scheme": "http", "headers": [(b"host", b"testserver")],
                 "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
        start, bodies = {}, []
        enough = asyncio.Event()

        async def receive():
            await enough.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body" and message.get("body"):
                bodies.append(message["body"])
                if len(bodies) == 1 and before_disconnect is not None:
                    before_disconnect()
                if len(bodies) >= frames_wanted:
                    enough.set()

        # the timeout is the safety net: if the endpoint ever stops honoring
        # http.disconnect this fails in 10s instead of hanging the suite
        await asyncio.wait_for(self.app(scope, receive, send), 10)
        return start, bodies

    async def test_the_event_stream_opens_with_a_comment_frame(self):
        # ": connected" is a comment, not an event: it flushes headers and
        # proxies immediately so the client knows the stream is live.
        start, bodies = await self._drive(1)
        self.assertEqual(start["status"], 200)
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        self.assertTrue(headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(bodies[0], b": connected\n\n")

    async def test_a_published_event_reaches_the_stream_as_an_sse_frame(self):
        _start, bodies = await self._drive(
            2, before_disconnect=lambda: self.svc.bus.publish(
                {"event": "notify", "task_id": "t1", "speech": "Indexed 1 file."}))
        event, data = parse_sse(bodies[1].decode())[0]
        self.assertEqual(event, "notify")
        self.assertEqual(data["speech"], "Indexed 1 file.")

    async def test_a_disconnected_client_is_unsubscribed_from_the_bus(self):
        # every open tab holds a queue; leaking one per connection would make
        # EventBus.publish fan out to dead subscribers forever
        self.assertEqual(len(self.svc.bus._subs), 0)
        await self._drive(1)
        self.assertEqual(len(self.svc.bus._subs), 0)


# -- the lifespan ------------------------------------------------------------


class TestLifespan(unittest.TestCase):
    """`with TestClient(app):` is the only thing that enters create_app's
    lifespan, and nothing in the suite did — so the queue watcher, startup
    reindex, automations scheduler, embedding refresh and inbox watcher were
    all completely unexercised. Every one of them is stubbed here: the real
    ones spawn tasks, walk the vault and load a 34MB model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.started: set[str] = set()
        self.cancelled: set[str] = set()

    def tearDown(self):
        self.tmp.cleanup()

    def _loop_stub(self, name):
        async def stub(*args, **kwargs):
            self.started.add(name)
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled.add(name)
                raise
        return stub

    def _run(self, cfg):
        def ingest_stub(db, root, dirs):
            self.started.add("reindex")
            return {"files_scanned": 0}

        def embed_stub(cfg_, db):
            self.started.add("embed")
            return 0

        with patch("dispatcher.queue_watcher.watch", self._loop_stub("queue")), \
             patch("dispatcher.automations.loop", self._loop_stub("automations")), \
             patch("dispatcher.inbox.watch", self._loop_stub("inbox")), \
             patch("dispatcher.ingest.ingest_vault", ingest_stub), \
             patch("dispatcher.embeddings.embed_missing", embed_stub):
            with TestClient(create_app(cfg)) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                # the startup tasks are scheduled, not awaited — give the
                # portal's loop a moment to actually enter them
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and len(self.started) < self.expect:
                    time.sleep(0.02)

    def test_every_enabled_background_job_starts_and_the_long_lived_ones_stop(self):
        self.expect = 5
        cfg = _cfg(self.tmp.name,
                   memory={"reindex_on_start": True},
                   automations={"enabled": True, "check_interval_s": 3600},
                   inbox={"enabled": True, "check_interval_s": 3600},
                   embeddings={"enabled": True, "refresh_minutes": 60})
        self._run(cfg)
        self.assertEqual(self.started,
                         {"queue", "reindex", "embed", "automations", "inbox"})
        # reindex/embed are one-shot and have already returned; the three
        # forever-loops must be cancelled on shutdown or a test process (and,
        # in production, a systemd stop) hangs waiting on them
        self.assertEqual(self.cancelled, {"queue", "automations", "inbox"})

    def test_config_gates_which_background_jobs_exist_at_all(self):
        # only the queue watcher is unconditional; everything else is opt-in,
        # which is what keeps a default install from walking the vault or
        # loading an embedding model at boot
        self.expect = 1
        cfg = _cfg(self.tmp.name, memory={"reindex_on_start": False},
                   automations={}, inbox={}, embeddings={})
        self._run(cfg)
        self.assertEqual(self.started, {"queue"})
        self.assertEqual(self.cancelled, {"queue"})

    def test_the_startup_reindex_runs_before_the_embedding_backfill(self):
        # embed_missing is called at the tail of _startup_reindex, so an index
        # that just gained entries gets vectors in the same pass
        self.expect = 3
        order = []
        cfg = _cfg(self.tmp.name, memory={"reindex_on_start": True},
                   embeddings={"enabled": True, "refresh_minutes": 60})
        with patch("dispatcher.queue_watcher.watch", self._loop_stub("queue")), \
             patch("dispatcher.ingest.ingest_vault",
                   lambda db, root, dirs: (order.append("reindex"), {})[1]), \
             patch("dispatcher.embeddings.embed_missing",
                   lambda cfg_, db: order.append("embed")):
            with TestClient(create_app(cfg)):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and len(order) < 2:
                    time.sleep(0.02)
        self.assertEqual(order[:2], ["reindex", "embed"])


# -- /desktop: the allow / confirm / deny safety plane -----------------------


class TestDesktopRoutes(HTTPTestCase):
    cfg_overrides = {"computer": {"enabled": True, "confirm_timeout_s": 120}}

    def test_desktop_is_503_when_the_feature_is_off(self):
        # an absent/disabled computer: block denies everything — the tier is
        # opt-in, and that default is what keeps this suite side-effect-free
        with tempfile.TemporaryDirectory() as off:
            client = TestClient(create_app(_cfg(off)))
            self.assertEqual(client.post("/desktop", json={"command": "lock"}).status_code, 503)
            self.assertEqual(client.post("/desktop/confirm",
                                         json={"confirm_id": "x"}).status_code, 503)
            self.assertEqual(client.get("/desktop/verbs").status_code, 503)

    def test_an_empty_command_is_400(self):
        self.assertEqual(self.client.post("/desktop", json={"command": "   "}).status_code, 400)

    def test_unrecognised_text_is_reported_rather_than_guessed_at(self):
        r = self.client.post("/desktop", json={"command": "make me a sandwich"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "unrecognized")

    def test_an_allowed_verb_runs_and_speaks(self):
        with patch("dispatcher.service.desktop.run_intent_ok",
                   return_value=(True, "Locked.")) as run:
            r = self.client.post("/desktop", json={"command": "lock the screen"})
        self.assertEqual(r.json(), {"status": "done", "speech": "Locked."})
        self.assertEqual(run.call_args[0][0].verb, "lock")

    def test_a_denied_verb_never_reaches_the_executor(self):
        self.cfg.computer = {"enabled": True, "policy": {"lock": "deny"}}
        with patch("dispatcher.service.desktop.run_intent_ok") as run:
            r = self.client.post("/desktop", json={"command": "lock the screen"})
        run.assert_not_called()
        self.assertEqual(r.json()["status"], "denied")

    def test_a_confirm_verb_parks_instead_of_acting(self):
        # `open` is confirm-by-default: an arbitrary URL is an exfiltration
        # channel and the obvious prompt-injection payload.
        with patch("dispatcher.service.desktop.run_intent_ok") as run:
            r = self.client.post("/desktop", json={"command": "open github.com"})
        run.assert_not_called()
        body = r.json()
        self.assertEqual(body["status"], "needs_confirmation")
        self.assertTrue(body["confirm_id"])
        self.assertIn("?", body["speech"])
        self.assertEqual(self.client.get("/desktop/verbs").json()["pending"], 1)

    def test_confirming_runs_the_parked_intent_exactly_once(self):
        cid = self.client.post("/desktop",
                               json={"command": "open github.com"}).json()["confirm_id"]
        with patch("dispatcher.service.desktop.run_intent_ok",
                   return_value=(True, "Opening github.com.")) as run:
            first = self.client.post("/desktop/confirm",
                                     json={"confirm_id": cid, "approve": True})
            replay = self.client.post("/desktop/confirm",
                                      json={"confirm_id": cid, "approve": True})
        self.assertEqual(first.json()["status"], "done")
        # single-use: a replayed id must not act a second time
        self.assertEqual(replay.json()["status"], "expired")
        self.assertEqual(run.call_count, 1)

    def test_declining_drops_the_intent_without_running_it(self):
        cid = self.client.post("/desktop",
                               json={"command": "open github.com"}).json()["confirm_id"]
        with patch("dispatcher.service.desktop.run_intent_ok") as run:
            r = self.client.post("/desktop/confirm",
                                 json={"confirm_id": cid, "approve": False})
        run.assert_not_called()
        self.assertEqual(r.json()["status"], "declined")
        self.assertEqual(self.client.get("/desktop/verbs").json()["pending"], 0)

    def test_an_expired_confirmation_is_reported_not_executed(self):
        cid = self.client.post("/desktop",
                               json={"command": "open github.com"}).json()["confirm_id"]
        self.svc.pending_desktop[cid]["expires"] = time.monotonic() - 1
        with patch("dispatcher.service.desktop.run_intent_ok") as run:
            r = self.client.post("/desktop/confirm",
                                 json={"confirm_id": cid, "approve": True})
        run.assert_not_called()
        self.assertEqual(r.json()["status"], "expired")

    def test_an_unknown_confirm_id_is_reported_not_executed(self):
        with patch("dispatcher.service.desktop.run_intent_ok") as run:
            r = self.client.post("/desktop/confirm",
                                 json={"confirm_id": "nope", "approve": True})
        run.assert_not_called()
        self.assertEqual(r.json()["status"], "expired")

    def test_desktop_verbs_publishes_the_policy_for_every_verb(self):
        # the honest surface: the dashboard renders this, and it is the answer
        # to "why did that verb refuse?"
        body = self.client.get("/desktop/verbs").json()
        self.assertIn("clipboard_get", body["verbs"])
        self.assertEqual(body["verbs"]["open"], "confirm")
        self.assertEqual(body["verbs"]["lock"], "allow")
        self.assertEqual(set(body["verbs"].values()) - {"allow", "confirm", "deny"},
                         set())


# -- /media ------------------------------------------------------------------


class TestMediaRoutes(HTTPTestCase):
    def test_media_is_503_when_the_feature_is_off(self):
        self.assertEqual(self.client.post("/media", json={"command": "pause"}).status_code, 503)
        self.assertEqual(self.client.get("/media/state").status_code, 503)

    def test_an_empty_command_is_400(self):
        self.cfg.media = {"enabled": True}
        self.assertEqual(self.client.post("/media", json={"command": ""}).status_code, 400)

    def test_a_deterministic_command_reaches_the_executor_with_no_model(self):
        self.cfg.media = {"enabled": True}
        with patch("dispatcher.service.spotify.run_intent",
                   return_value="Paused.") as run, \
             patch("dispatcher.service.quick.stream") as stream:
            r = self.client.post("/media", json={"command": "pause"})
        stream.assert_not_called()      # "pause" must never cost a token
        run.assert_called_once()
        self.assertEqual(r.json(), {"speech": "Paused.", "status": "done"})

    def test_unparseable_music_text_is_reported_as_unresolved(self):
        self.cfg.media = {"enabled": True, "llm_fallback": False}
        r = self.client.post("/media", json={"command": "xyzzy"})
        self.assertEqual(r.json()["status"], "unresolved")


# -- /automations ------------------------------------------------------------


SPEC = {"task_text": "give me the brief", "kind": "daily", "time": "07:30",
        "weekday": None, "interval_minutes": None, "once_at": None, "notify": True}


class TestAutomationRoutes(HTTPTestCase):
    cfg_overrides = {"automations": {"enabled": True}}

    def seed(self, next_run_at="2020-01-01T07:30:00+00:00"):
        return self.svc.db.create_automation("every morning, brief me", "api",
                                             SPEC, next_run_at)

    def test_an_empty_request_is_400_before_any_parse(self):
        with patch.object(type(self.svc), "create_automation_from_nl") as parse:
            r = self.client.post("/automations", json={"request": "   "})
        parse.assert_not_called()      # the LLM parse costs money; don't spend it on ""
        self.assertEqual(r.status_code, 400)

    def test_a_parsed_request_is_created_201_with_a_human_description(self):
        row = self.svc.db.create_automation("every morning, brief me", "api", SPEC, None)
        self.svc.create_automation_from_nl = AsyncMock(
            return_value=(row, "Scheduled: give me the brief — daily at 07:30."))
        r = self.client.post("/automations", json={"request": "every morning, brief me"})
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["describe"], "daily at 07:30")
        self.assertIn("Scheduled", body["speech"])
        self.assertEqual(body["task_text"], "give me the brief")

    def test_a_failed_parse_is_422_with_the_spoken_reason(self):
        self.svc.create_automation_from_nl = AsyncMock(
            return_value=(None, "I couldn't work out when to run that."))
        r = self.client.post("/automations", json={"request": "sometime, maybe"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("couldn't", r.json()["detail"])

    def test_a_disabled_block_is_409_before_any_parse(self):
        # The scheduler only polls when the block is enabled; the old code
        # returned 201 over a row nothing would ever run.
        self.cfg.automations = {}
        with patch.object(type(self.svc), "create_automation_from_nl") as parse:
            r = self.client.post("/automations",
                                 json={"request": "every morning, brief me"})
        parse.assert_not_called()
        self.assertEqual(r.status_code, 409)

    def test_listing_decorates_every_row_with_its_description(self):
        self.seed()
        rows = self.client.get("/automations").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["describe"], "daily at 07:30")
        self.assertEqual(rows[0]["enabled"], 1)

    def test_toggling_off_clears_the_schedule_and_toggling_on_recomputes_it(self):
        # the load-bearing half: re-enabling must NOT keep the stale past-due
        # next_run_at, or a long-disabled daily fires the instant it comes back
        row = self.seed(next_run_at="2020-01-01T07:30:00+00:00")
        off = self.client.post(f"/automations/{row['id']}/toggle").json()
        self.assertEqual(off["enabled"], 0)
        on = self.client.post(f"/automations/{row['id']}/toggle").json()
        self.assertEqual(on["enabled"], 1)
        self.assertIsNotNone(on["next_run_at"])
        self.assertGreater(on["next_run_at"], "2026-01-01")

    def test_toggling_an_unknown_automation_is_404(self):
        self.assertEqual(self.client.post("/automations/999/toggle").status_code, 404)

    def test_deleting_removes_the_row_and_a_second_delete_is_404(self):
        row = self.seed()
        r = self.client.delete(f"/automations/{row['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"automation_id": row["id"], "status": "deleted"})
        self.assertEqual(self.client.get("/automations").json(), [])
        self.assertEqual(self.client.delete(f"/automations/{row['id']}").status_code, 404)


# -- /memory -----------------------------------------------------------------


def _chunk(raw, heading=None, line_no=1, dates=()):
    return {"heading": heading, "line_no": line_no, "raw": raw,
            "compiled": raw, "hash": hashlib.sha256(raw.encode()).hexdigest(),
            "dates": list(dates)}


class TestMemoryRoutes(HTTPTestCase):
    """Embeddings stay disabled (cfg.embeddings is {}), so /memory/search takes
    the FTS-only path — no model download, no sqlite-vec requirement."""

    def setUp(self):
        super().setUp()
        self.svc.db.replace_file_entries("vault/notes/coffee.md", 1.0, [
            _chunk("The espresso machine needs descaling", dates=["2026-08-01"])])
        self.svc.db.replace_file_entries("vault/briefs/2026-08-02.md", 2.0, [
            _chunk("espresso beans arrived today", dates=["2026-08-02"])])

    def test_search_returns_every_scope_by_default(self):
        body = self.client.get("/memory/search?q=espresso").json()
        self.assertEqual(set(body), {"entries", "episodes", "facts", "fact_neighbors"})
        self.assertEqual(len(body["entries"]), 2)

    def test_search_can_be_narrowed_to_one_scope(self):
        body = self.client.get("/memory/search?q=espresso&scope=vault").json()
        self.assertEqual(set(body), {"entries"})

    def test_a_file_filter_restricts_the_hits_to_that_path(self):
        # a deterministic filter forces the FTS-only path (filters before
        # vectors, khoj-style) — the results must still be filtered
        body = self.client.get("/memory/search?q=espresso&file=vault/notes/*").json()
        self.assertEqual([e["file_path"] for e in body["entries"]],
                         ["vault/notes/coffee.md"])

    def test_date_filters_restrict_the_hits_to_that_window(self):
        after = self.client.get("/memory/search?q=espresso&after=2026-08-02").json()
        self.assertEqual([e["file_path"] for e in after["entries"]],
                         ["vault/briefs/2026-08-02.md"])
        before = self.client.get("/memory/search?q=espresso&before=2026-08-01").json()
        self.assertEqual([e["file_path"] for e in before["entries"]],
                         ["vault/notes/coffee.md"])

    def test_an_unknown_scope_is_400_rather_than_an_empty_answer(self):
        # silently returning {} would read as "nothing matched"
        self.assertEqual(self.client.get("/memory/search?q=x&scope=bogus").status_code, 400)

    def test_search_requires_a_query(self):
        self.assertEqual(self.client.get("/memory/search").status_code, 422)

    def test_reindex_walks_the_configured_dirs_and_reports_stats(self):
        notes = self.cfg.root / "vault" / "notes"
        notes.mkdir(parents=True)
        (notes / "new.md").write_text("# Heading\n\nA freshly written note about kayaks.\n")
        stats = self.client.post("/memory/reindex").json()
        self.assertGreaterEqual(stats["files_scanned"], 1)
        self.assertGreaterEqual(stats["added"], 1)
        hits = self.client.get("/memory/search?q=kayaks&scope=vault").json()["entries"]
        self.assertTrue(hits)

    def test_blocks_returns_the_always_injected_context(self):
        body = self.client.get("/memory/blocks").json()
        self.assertEqual(list(body), ["context"])
        self.assertIsInstance(body["context"], str)

    def test_consolidate_says_so_when_there_is_nothing_to_consolidate(self):
        # the nightly timer hits this every night on a quiet day; it must not
        # queue an agent (or spend anything) to discover that
        with patch("dispatcher.service.Service.start_agentic") as start:
            r = self.client.post("/memory/consolidate")
        start.assert_not_called()
        self.assertEqual(r.json(), {"status": "nothing_to_consolidate"})

    def test_reconcile_reports_a_disabled_graph_rather_than_failing(self):
        self.assertEqual(self.client.post("/memory/reconcile").json(),
                         {"status": "graph_disabled"})


# -- /stats, /health, /skills, /vault ----------------------------------------


class TestObservabilityRoutes(HTTPTestCase):
    def test_stats_reports_the_tiles_the_dashboard_widget_renders(self):
        task = self.svc.db.create_task("hello there", "voice", "quick")
        self.svc.db.set_task_status(task["id"], "done")
        run_id = self.svc.db.create_run(task["id"], 1, "test-model")
        self.svc.db.finish_run(run_id, "done", cost_usd=0.02, ttft_ms=2600.0,
                               tokens_per_s=31.5)
        body = self.client.get("/stats").json()
        self.assertEqual(set(body), {"days", "tasks_by_status", "by_source",
                                     "success_rate", "total_cost_usd",
                                     "quick_latency", "recent_reflections"})
        self.assertEqual(body["tasks_by_status"], {"done": 1})
        self.assertEqual(body["by_source"][0]["source"], "voice")
        self.assertEqual(body["success_rate"], 1.0)
        self.assertEqual(body["quick_latency"]["avg_ttft_ms"], 2600.0)

    def test_stats_honors_the_days_window(self):
        self.assertEqual(self.client.get("/stats?days=1").json()["days"], 1)

    def test_stats_on_an_empty_database_is_still_a_valid_shape(self):
        body = self.client.get("/stats").json()
        self.assertIsNone(body["success_rate"])
        self.assertIsNone(body["quick_latency"])
        self.assertEqual(body["total_cost_usd"], 0)

    def test_health_names_the_db_and_queue_actually_in_use(self):
        body = self.client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["db"], str(self.cfg.db_path))
        self.assertEqual(body["queue_dir"], str(self.cfg.queue_dir))
        self.assertEqual(body["running_tasks"], 0)

    def test_the_spa_routes_are_404_when_the_dashboard_is_not_built(self):
        # cfg.root has no ui/dist here, so the StaticFiles mount is absent too;
        # a missing build must read as 404, not a 500 out of FileResponse
        self.assertEqual(self.client.get("/ask").status_code, 404)
        self.assertEqual(self.client.get("/system-docs").status_code, 404)

    def test_curating_skills_reports_what_it_did_without_archiving_a_fresh_area(self):
        area = self.cfg.root / "areas" / "expenses"
        area.mkdir(parents=True)
        (area / "SKILL.md").write_text("---\nname: expenses\n---\n\nTrack spending.\n")
        report = self.client.post("/skills/curate").json()
        self.assertEqual(set(report), {"stale", "archived", "skipped"})
        self.assertEqual(report["archived"], [])
        self.assertTrue((area / "SKILL.md").is_file())   # nothing moved

    def test_skills_lists_areas_with_their_telemetry_counters(self):
        area = self.cfg.root / "areas" / "tasks"
        area.mkdir(parents=True)
        (area / "SKILL.md").write_text(
            "---\nname: tasks\ntriggers: [todo]\n---\n\nManage tasks.\n")
        self.svc.db.record_skill_use("tasks")
        rows = self.client.get("/skills").json()
        self.assertEqual([r["name"] for r in rows], ["tasks"])
        self.assertEqual(rows[0]["use_count"], 1)
        self.assertEqual(rows[0]["state"], "active")


class TestVaultRoutes(HTTPTestCase):
    def setUp(self):
        super().setUp()
        self.tasks_dir = self.cfg.root / "vault" / "tasks"
        self.tasks_dir.mkdir(parents=True)

    def test_vault_tasks_lists_parsed_task_files(self):
        (self.tasks_dir / "renew-domain.md").write_text(
            "---\ntitle: Renew the domain\nstatus: open\n---\n\nBefore September.\n")
        rows = self.client.get("/vault/tasks").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Renew the domain")
        self.assertEqual(rows[0]["status"], "open")

    def test_toggling_a_task_file_flips_its_status_on_disk(self):
        f = self.tasks_dir / "renew-domain.md"
        f.write_text("---\ntitle: Renew\nstatus: open\n---\n\nnotes\n")
        r = self.client.post("/vault/tasks/renew-domain.md/toggle")
        self.assertEqual(r.json(), {"file": "renew-domain.md", "status": "done"})
        self.assertIn("status: done", f.read_text())

    def test_toggling_a_missing_task_file_is_404(self):
        self.assertEqual(
            self.client.post("/vault/tasks/nope.md/toggle").status_code, 404)

    def test_a_traversing_filename_is_rejected_not_followed(self):
        # the toggle route takes a filename straight off the wire
        r = self.client.post("/vault/tasks/..%2F..%2Fconfig.yaml/toggle")
        self.assertIn(r.status_code, (400, 404))

    def test_the_brief_route_is_404_before_any_brief_exists(self):
        self.assertEqual(self.client.get("/vault/brief").status_code, 404)

    def test_the_brief_route_serves_the_newest_brief(self):
        briefs = self.cfg.root / "vault" / "briefs"
        briefs.mkdir(parents=True)
        (briefs / "2026-08-09.md").write_text("# Yesterday\n")
        body = self.client.get("/vault/brief").json()
        self.assertEqual(body["file"], "2026-08-09.md")
        self.assertIn("Yesterday", body["content"])


# -- /learn ------------------------------------------------------------------


class TestLearnRoute(HTTPTestCase):
    def test_learning_from_nothing_is_400(self):
        # no request text AND no recent conversation to distil: there is
        # literally nothing to author, so don't spend an agentic run finding out
        r = self.client.post("/learn", json={"request": "", "source": "api"})
        self.assertEqual(r.status_code, 400)

    def test_a_described_workflow_is_queued_202_without_running(self):
        with patch("dispatcher.service.Service.start_agentic") as start:
            r = self.client.post("/learn", json={"request": "how I do standups"})
        self.assertEqual(r.status_code, 202)
        body = r.json()
        self.assertEqual(set(body), {"task_id", "status", "resumed"})
        self.assertEqual((body["status"], body["resumed"]), ("queued", False))
        start.assert_called_once()
        row = self.client.get(f"/task/{body['task_id']}").json()
        self.assertEqual(row["area"], "learn")
        self.assertEqual(json.loads(row["metadata"])["task_type"], "learn")


class TestUICacheHeaders(HTTPTestCase):
    """Tier 3: heuristic freshness served a month-old shell with zero
    requests. Hashed assets are immutable; the shell always revalidates."""

    def setUp(self):
        self._dist_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._dist_tmp.cleanup)
        # Minimal stand-in bundle, created BEFORE the app (the static mount
        # checks is_dir at startup). Tests the headers, not the build.
        dist = Path(self._dist_tmp.name) / "ui" / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html></html>")
        (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
        self.cfg_overrides = {"root": Path(self._dist_tmp.name)}
        super().setUp()

    def test_hashed_assets_are_immutable(self):
        r = self.client.get("/assets/index-abc123.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("immutable", r.headers.get("cache-control", ""))

    def test_shell_is_never_cached(self):
        for path in ("/", "/ask?shot=x"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("cache-control"), "no-cache")

    def test_api_and_events_pass_through_untouched(self):
        r = self.client.get("/health")
        self.assertNotIn("immutable", r.headers.get("cache-control", ""))


if __name__ == "__main__":
    unittest.main()
