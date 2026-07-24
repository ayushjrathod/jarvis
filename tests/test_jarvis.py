"""Phase B unit tests: sanitizer, sentence chunker, engine registry, config,
and the Brain's SSE parsing — no audio hardware, no network.
"""

import asyncio
import json
import unittest
from dataclasses import dataclass
from unittest import mock

import httpx
import numpy as np

from jarvis.audio import Recorder
from jarvis.config import JarvisConfig
from jarvis.sanitize import SentenceChunker, sanitize
from jarvis.vad import FRAME_SAMPLES
from jarvis.wake_capture import MicLostError, MicWatchdog, capture_after_wake


class TestSanitize(unittest.TestCase):
    def test_markdown_stripped(self):
        self.assertEqual(sanitize("**bold** and *italic* and `code`"), "bold and italic and code")
        self.assertEqual(sanitize("# Header\nbody"), "Header\nbody")
        self.assertEqual(sanitize("- one\n- two"), "one\ntwo")
        self.assertEqual(sanitize("1. first\n2. second"), "first\nsecond")

    def test_urls_and_links(self):
        self.assertEqual(sanitize("see [the docs](https://x.com/a?b=1)"), "see the docs")
        self.assertEqual(sanitize("go to https://example.com/x now"), "go to a link now")

    def test_blockquote_and_hrule(self):
        self.assertEqual(sanitize("> quoted\n---\nafter"), "quoted\n\nafter")

    def test_code_block(self):
        out = sanitize("before\n```py\nx = 1\n```\nafter")
        self.assertIn("code block omitted", out)
        self.assertNotIn("x = 1", out)

    def test_identifiers_keep_underscores(self):
        # boundary-guarded per openclaw strip-markdown.ts: snake_case names
        # survive to the TTS, real _emphasis_ is still stripped
        self.assertEqual(sanitize("run backup_db.sh now"), "run backup_db.sh now")
        self.assertEqual(sanitize("wake_prebuffer_ms controls it"),
                         "wake_prebuffer_ms controls it")
        self.assertEqual(sanitize("a _really_ good idea"), "a really good idea")
        self.assertEqual(sanitize("the __init__ method"), "the init method")
        self.assertEqual(sanitize("set wake_prebuffer_ms to _400_"),
                         "set wake_prebuffer_ms to 400")

    def test_plain_prose_untouched(self):
        self.assertEqual(sanitize("The capital of France is Paris."),
                         "The capital of France is Paris.")


class TestSentenceChunker(unittest.TestCase):
    def test_stream_chunks_to_sentences(self):
        c = SentenceChunker()
        out = []
        for delta in ["The capital", " of France is Paris. It has", " a population. And more"]:
            out.extend(c.feed(delta))
        out.extend(c.flush())
        self.assertEqual(out, ["The capital of France is Paris.", "It has a population.", "And more"])

    def test_abbreviation_not_split_midword(self):
        c = SentenceChunker()
        out = c.feed("It costs 3.50 dollars total. Next sentence starts.")
        self.assertEqual(out[0], "It costs 3.50 dollars total.")

    def test_oversized_buffer_flushes_at_space(self):
        c = SentenceChunker(max_buffer=40)
        out = c.feed("word " * 20)  # no sentence boundary anywhere
        self.assertTrue(out)
        self.assertTrue(all(len(s) <= 45 for s in out))

    def test_markdown_sanitized_per_sentence(self):
        c = SentenceChunker()
        out = c.feed("**Paris** is the capital. ")
        self.assertEqual(out, ["Paris is the capital."])


class TestEngineRegistry(unittest.TestCase):
    def test_unknown_engine_raises(self):
        from jarvis import engines
        with self.assertRaises(ValueError):
            engines.create("stt", "nonexistent")

    def test_known_engines_instantiate_without_loading(self):
        from jarvis import engines
        stt = engines.create("stt", "faster_whisper", model="tiny.en")
        self.assertEqual(stt.model_name, "tiny.en")
        tts = engines.create("tts", "piper", voice="en_US-lessac-medium")
        self.assertEqual(tts.voice_name, "en_US-lessac-medium")
        wake = engines.create("wake", "openwakeword")
        self.assertEqual(wake.model_name, "hey_jarvis")
        brain = engines.create("brain", "dispatcher", base_url="http://x:1")
        self.assertEqual(brain.base_url, "http://x:1")


class TestConfig(unittest.TestCase):
    # Load from a fixture, not the live config.yaml (L13): the test should pin
    # the parse/merge/default logic, not the maintainer's current settings.
    FIXTURE = (
        "dispatcher:\n  host: 127.0.0.1\n  port: 9999\n"
        "jarvis:\n  trigger_key: KEY_F8\n  ptt_key: KEY_F7\n  sample_rate: 22050\n"
        "  stt:\n    model: base.en\n  barge_in_frames: 9\n  wake_beep_ms: 50\n"
    )

    def _load(self, text):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "config.yaml"
        p.write_text(text)
        return JarvisConfig.load(p)

    def test_parses_fixture_values(self):
        cfg = self._load(self.FIXTURE)
        self.assertEqual(cfg.trigger_key, "KEY_F8")
        self.assertEqual(cfg.ptt_key, "KEY_F7")
        self.assertEqual(cfg.sample_rate, 22050)
        self.assertEqual(cfg.stt["model"], "base.en")
        self.assertEqual(cfg.barge_in_frames, 9)
        self.assertEqual(cfg.wake_beep_ms, 50)
        self.assertEqual(cfg.dispatcher_url, "http://127.0.0.1:9999")

    def test_defaults_when_sections_empty(self):
        cfg = self._load("dispatcher: {}\njarvis: {}\n")
        self.assertEqual(cfg.trigger_key, "KEY_F9")   # dataclass default
        self.assertEqual(cfg.ptt_key, "KEY_RIGHTCTRL")  # never equal to trigger_key
        self.assertEqual(cfg.sample_rate, 16000)
        self.assertEqual(cfg.dispatcher_url, "http://127.0.0.1:8765")


class TestBeep(unittest.TestCase):
    def test_tone_shape_and_fades(self):
        from jarvis.audio import beep

        tone = beep(sample_rate=16000, ms=120)
        self.assertEqual(tone.dtype.name, "int16")
        self.assertEqual(len(tone), 16000 * 120 // 1000)
        # fade in/out: endpoints silent, middle loud
        self.assertEqual(tone[0], 0)
        self.assertEqual(tone[-1], 0)
        self.assertGreater(abs(int(tone[len(tone) // 2])), 1000)

    def test_zero_ms_still_returns_audio(self):
        from jarvis.audio import beep

        self.assertGreaterEqual(len(beep(ms=0)), 1)


class TestBrainSSEParsing(unittest.TestCase):
    def test_sse_event_parsing(self):
        from jarvis.engines.brain_dispatcher import _sse_events

        class FakeResponse:
            async def aiter_lines(self):
                for line in [
                    "event: task",
                    'data: {"task_id": "abc", "kind": "quick"}',
                    "",
                    "event: delta",
                    'data: {"text": "Paris."}',
                    "",
                    ": ping",
                    "event: done",
                    'data: {"task_id": "abc", "status": "done"}',
                    "",
                ]:
                    yield line

        async def collect():
            return [ev async for ev in _sse_events(FakeResponse())]

        events = asyncio.run(collect())
        self.assertEqual([e[0] for e in events], ["task", "delta", "done"])
        self.assertEqual(events[1][1]["text"], "Paris.")


class _FakeWake:
    def __init__(self, fire_at: int):
        self.fire_at = fire_at
        self.count = 0

    def process(self, frame) -> bool:
        self.count += 1
        return self.count == self.fire_at

    def reset(self):
        pass


class _FakeVAD:
    def is_speech(self, frame) -> bool:
        return bool(frame[0])

    def reset(self):
        pass


@dataclass
class _FakeCfg:
    sample_rate: int = 16000
    endpoint_silence_ms: int = 320  # 10 frames of 32ms
    max_utterance_s: int = 5


SPEECH = np.full(FRAME_SAMPLES, 1000, dtype=np.int16)
SILENCE = np.zeros(FRAME_SAMPLES, dtype=np.int16)


class TestWakeCapturePrebuffer(unittest.TestCase):
    """The pre-wake-word ring buffer (item 4 of the adaptation audit): frames
    seen before the wake word fires must survive onto the front of the
    captured utterance, since the first word can land in the same frame
    that triggers detection."""

    def test_prebuffer_frames_spliced_onto_capture(self):
        cfg = _FakeCfg()
        pre = [SPEECH] * 5          # audio before the wake word "fires"
        post_speech = [SPEECH] * 3  # rest of the utterance
        post_silence = [SILENCE] * 15
        frames = iter(pre + post_speech + post_silence)
        wake = _FakeWake(fire_at=5)
        vad = _FakeVAD()

        audio = capture_after_wake(lambda: next(frames), wake, vad, cfg, prebuffer_frames=5)

        self.assertIsNotNone(audio)
        silence_limit_frames = cfg.endpoint_silence_ms // 32
        expected_frames = 5 + 3 + silence_limit_frames
        self.assertEqual(len(audio), expected_frames * FRAME_SAMPLES)
        self.assertTrue(np.all(audio[:FRAME_SAMPLES] == 1000),
                         "prebuffered speech should be at the front of the capture")

    def test_zero_prebuffer_drops_pre_wake_audio(self):
        cfg = _FakeCfg()
        pre = [SPEECH] * 5
        post_speech = [SPEECH] * 3
        post_silence = [SILENCE] * 15
        frames = iter(pre + post_speech + post_silence)
        wake = _FakeWake(fire_at=5)
        vad = _FakeVAD()

        audio = capture_after_wake(lambda: next(frames), wake, vad, cfg, prebuffer_frames=0)

        silence_limit_frames = cfg.endpoint_silence_ms // 32
        expected_frames = 3 + silence_limit_frames
        self.assertEqual(len(audio), expected_frames * FRAME_SAMPLES)

    def test_busy_hook_invoked_before_wake_check(self):
        cfg = _FakeCfg()
        frames = iter([SPEECH] * 4 + [SILENCE] * 15)
        wake = _FakeWake(fire_at=1)
        vad = _FakeVAD()
        busy_calls = []
        state = {"busy": True}

        def is_busy():
            return state["busy"]

        def on_busy():
            busy_calls.append(1)
            state["busy"] = False  # unblock after one busy tick

        audio = capture_after_wake(
            lambda: next(frames), wake, vad, cfg,
            prebuffer_frames=0, is_busy=is_busy, on_busy=on_busy,
        )
        self.assertEqual(len(busy_calls), 1)
        self.assertIsNotNone(audio)


class TestMicWatchdog(unittest.TestCase):
    """H6: a capture device that dies after the stream opened makes read()
    return None on every timeout forever; the watchdog turns a sustained None
    streak into a raise so wake_loop's retry re-opens the mic. A real frame —
    and silence is still real audio frames, not None — must reset the streak so
    ordinary quiet never trips it."""

    def test_consecutive_nones_raise_at_limit(self):
        w = MicWatchdog(none_limit=3)
        w.observe(None)  # 1
        w.observe(None)  # 2 — still under the limit
        self.assertEqual(w.none_streak, 2)
        with self.assertRaises(MicLostError):
            w.observe(None)  # 3 — device declared lost

    def test_good_frame_resets_streak(self):
        w = MicWatchdog(none_limit=3)
        w.observe(None)
        w.observe(None)     # 2 consecutive, one short of the limit
        w.observe(SILENCE)  # a real (silent) frame resets the streak
        self.assertEqual(w.none_streak, 0)
        w.observe(None)
        w.observe(None)     # 2 again — must not raise (reset worked)
        with self.assertRaises(MicLostError):
            w.observe(None)  # only the third *consecutive* None trips it

    def test_limit_floored_at_one(self):
        w = MicWatchdog(none_limit=0)
        with self.assertRaises(MicLostError):
            w.observe(None)


class TestCaptureRaisesOnMicLoss(unittest.TestCase):
    """The watchdog is wired into both capture phases, so a dead mic
    propagates out of capture_after_wake to wake_loop's open-time retry
    instead of spinning on None forever (finding H6)."""

    def test_phase1_mic_loss_raises(self):
        cfg = _FakeCfg()
        wake = _FakeWake(fire_at=999)  # never fires; mic feeds None forever
        vad = _FakeVAD()
        with self.assertRaises(MicLostError):
            capture_after_wake(lambda: None, wake, vad, cfg,
                               prebuffer_frames=0, none_limit=4)

    def test_phase2_mic_loss_raises(self):
        cfg = _FakeCfg()
        seq = iter([SPEECH])  # one frame fires the wake word, then the mic dies
        wake = _FakeWake(fire_at=1)
        vad = _FakeVAD()
        with self.assertRaises(MicLostError):
            capture_after_wake(lambda: next(seq, None), wake, vad, cfg,
                               prebuffer_frames=0, none_limit=4)

    def test_transient_none_does_not_raise(self):
        # a lone None between good frames must not abort a normal capture
        cfg = _FakeCfg()
        frames = iter([SPEECH, None, SPEECH, SPEECH] + [SILENCE] * 15)
        wake = _FakeWake(fire_at=1)
        vad = _FakeVAD()
        audio = capture_after_wake(lambda: next(frames), wake, vad, cfg,
                                   prebuffer_frames=0, none_limit=4)
        self.assertIsNotNone(audio)


class TestRecorderDurationCap(unittest.TestCase):
    """L11: a wedged/stuck trigger key must not grow the PTT buffer without
    bound. `_append` is the callback's accumulation path, exercised here
    directly so the cap is tested without audio hardware."""

    @staticmethod
    def _block(n):
        return np.zeros((n, 1), dtype=np.int16)  # (frames, channels), as sd delivers

    def test_cap_bounds_accumulated_samples(self):
        cap_s = 2
        r = Recorder(sample_rate=16000, max_seconds=cap_s)  # cap = 32000 samples
        for _ in range(200):  # 200*512 = 102400 samples, far past the cap
            r._append(self._block(512))
        cap_samples = 16000 * cap_s
        self.assertEqual(sum(len(f) for f in r._frames), cap_samples)
        self.assertEqual(len(np.concatenate(r._frames)), cap_samples)

    def test_short_recording_keeps_all_frames(self):
        r = Recorder(sample_rate=16000, max_seconds=30)
        for _ in range(10):  # 10*512 = 5120 samples, well under a 30s cap
            r._append(self._block(512))
        self.assertEqual(sum(len(f) for f in r._frames), 5120)

    def test_uncapped_when_max_seconds_none(self):
        r = Recorder(sample_rate=16000, max_seconds=None)
        for _ in range(50):
            r._append(self._block(1000))
        self.assertEqual(sum(len(f) for f in r._frames), 50000)


class _FakeOut:
    """Records writes; stands in for sd.OutputStream in Player.play tests."""

    def __init__(self, on_write=None):
        self.writes: list = []
        self._on_write = on_write

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, data):
        self.writes.append(data)
        if self._on_write is not None:
            self._on_write(len(self.writes))


class TestPlayerSubframes(unittest.TestCase):
    """H4: a whole TTS sentence is one piper chunk; slicing it into ~100ms
    sub-frames is what lets a barge-in cut audio mid-sentence."""

    def test_subframes_slices_and_preserves_audio(self):
        from jarvis.audio import _subframes

        chunk = np.arange(2500, dtype=np.int16)
        frames = list(_subframes(chunk, 1000))
        self.assertEqual([len(f) for f in frames], [1000, 1000, 500])
        self.assertTrue(np.array_equal(np.concatenate(frames), chunk))

    def test_subframes_empty_chunk_yields_nothing(self):
        from jarvis.audio import _subframes

        self.assertEqual(list(_subframes(np.zeros(0, dtype=np.int16), 100)), [])

    def test_play_stops_writing_after_interrupt(self):
        from jarvis.audio import Player

        player = Player(sample_rate=1000)  # sub-frame = 100 samples
        chunk = np.ones(1000, dtype=np.int16)  # 10 sub-frames
        fake = _FakeOut(on_write=lambda n: player.interrupt.set() if n == 2 else None)

        with mock.patch("jarvis.audio.sd.OutputStream", return_value=fake):
            ok = player.play([chunk])

        self.assertFalse(ok)                 # reported interrupted
        self.assertEqual(len(fake.writes), 2)  # cut after ~200ms, not all 1000ms

    def test_play_completes_without_interrupt(self):
        from jarvis.audio import Player

        player = Player(sample_rate=1000)
        chunk = np.ones(350, dtype=np.int16)  # 100 + 100 + 100 + 50
        fake = _FakeOut()

        with mock.patch("jarvis.audio.sd.OutputStream", return_value=fake):
            ok = player.play([chunk])

        self.assertTrue(ok)
        self.assertEqual(len(fake.writes), 4)


class TestSpeakSentencesBargeRace(unittest.TestCase):
    """H5: a barge-in fired while parked on the sentence queue (brain stalled
    between sentences) must still break the loop and cancel the dispatcher
    call, rather than holding `busy` until the 300s read timeout."""

    def test_barge_during_stall_breaks_and_cancels(self):
        from jarvis.audio import Player
        from jarvis.main import Jarvis

        cfg = JarvisConfig.load()
        app = Jarvis(cfg, mode="ptt")
        app.player = Player(cfg.sample_rate)  # hermetic: no device until play()

        cancelled = []

        async def fake_cancel():
            cancelled.append(True)

        app.brain.cancel = fake_cancel

        async def fake_monitor(barge, stop):  # fires instead of opening a mic
            barge.set()
            app.player.stop()

        app._barge_monitor = fake_monitor

        class NeverQueue:
            async def get(self):
                await asyncio.Event().wait()  # a stalled brain: never yields

        async def run():
            return await asyncio.wait_for(app.speak_sentences(NeverQueue()), timeout=5)

        result = asyncio.run(run())
        self.assertFalse(result)             # reported as interrupted
        self.assertEqual(cancelled, [True])  # in-flight dispatcher call cancelled


class TestBargeVadIsolation(unittest.TestCase):
    """M8: the barge monitor and wake-capture run on different threads; sharing
    one SileroVAD would trample its read-modify-write state."""

    def test_barge_monitor_uses_distinct_vad(self):
        from jarvis.main import Jarvis
        from jarvis.vad import SileroVAD

        cfg = JarvisConfig.load()
        app = Jarvis(cfg, mode="ptt")
        self.assertIsInstance(app.barge_vad, SileroVAD)
        self.assertIsNot(app.barge_vad, app.vad)


class _FakeResp:
    def __init__(self, headers, body=b"", lines=None, status_code=200):
        self.headers = headers
        self._body = body
        self._lines = lines or []
        self.closed = False
        self.status_code = status_code

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aclose(self):
        self.closed = True


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.posts: list = []

    def build_request(self, method, url, json=None):
        return (method, url, json)

    async def send(self, req, stream=False):
        return self._resp

    async def post(self, url):
        self.posts.append(url)
        return _FakeResp({})


class TestDispatcherBrainCancelScope(unittest.TestCase):
    """H7: `_current_task_id` must only be barge-cancellable while the response
    is actively streaming (or its agentic ack is in flight). A finished task's
    id lingering would let an unrelated barge-in cancel a live background run."""

    def _brain_with(self, resp):
        from jarvis.engines.brain_dispatcher import DispatcherBrain

        brain = DispatcherBrain(base_url="http://x:1")
        brain._client = _FakeClient(resp)
        return brain

    def test_agentic_id_not_cancellable_after_submit(self):
        body = json.dumps({"task_id": "agentic-1", "ack": "On it."}).encode()
        brain = self._brain_with(_FakeResp({"content-type": "application/json"}, body=body))

        async def run():
            events = [ev async for ev in brain.submit("kick off a big job")]
            await brain.cancel()  # a later, unrelated barge-in
            return events

        events = asyncio.run(run())
        self.assertEqual(events[0].type, "ack")
        self.assertIsNone(brain._current_task_id)
        self.assertEqual(brain._client.posts, [])  # background agentic left alone

    def test_quick_id_not_cancellable_after_stream(self):
        resp = _FakeResp({"content-type": "text/event-stream"}, lines=[
            "event: task", 'data: {"task_id": "quick-1", "kind": "quick"}', "",
            "event: delta", 'data: {"text": "Paris."}', "",
            "event: done", 'data: {"task_id": "quick-1", "status": "done"}', "",
        ])
        brain = self._brain_with(resp)

        async def run():
            events = [ev async for ev in brain.submit("capital of france?")]
            await brain.cancel()
            return events

        events = asyncio.run(run())
        self.assertIn("delta", [e.type for e in events])
        self.assertIsNone(brain._current_task_id)
        self.assertEqual(brain._client.posts, [])

    def test_cancel_during_active_quick_stream_posts(self):
        resp = _FakeResp({"content-type": "text/event-stream"}, lines=[
            "event: task", 'data: {"task_id": "quick-9", "kind": "quick"}', "",
            "event: delta", 'data: {"text": "one "}', "",
            "event: delta", 'data: {"text": "two"}', "",
            "event: done", 'data: {"task_id": "quick-9", "status": "done"}', "",
        ])
        brain = self._brain_with(resp)

        async def run():
            gen = brain.submit("count")
            await gen.__anext__()   # first delta → the id is live
            await brain.cancel()    # barge-in mid-stream
            await gen.aclose()

        asyncio.run(run())
        self.assertEqual(brain._client.posts, ["http://x:1/task/quick-9/cancel"])
        self.assertIsNone(brain._current_task_id)


class TestSubmitErrorStatus(unittest.TestCase):
    """M7: FastAPI errors (500 {"detail":…}, 422) are application/json too, so
    branching on content-type alone spoke "On it." to a failure (and a non-JSON
    error body yielded dead silence). submit() must check status first and yield
    a speakable `error` event — which main.py chunks and speaks like an ack."""

    def _brain_with(self, resp):
        from jarvis.engines.brain_dispatcher import DispatcherBrain

        brain = DispatcherBrain(base_url="http://x:1")
        brain._client = _FakeClient(resp)
        return brain

    def test_500_json_yields_error_not_ack(self):
        body = json.dumps({"detail": "boom"}).encode()
        resp = _FakeResp({"content-type": "application/json"}, body=body, status_code=500)
        brain = self._brain_with(resp)

        async def run():
            return [ev async for ev in brain.submit("do a thing")]

        events = asyncio.run(run())
        self.assertEqual([e.type for e in events], ["error"])
        self.assertTrue(events[0].text)             # a speakable message
        self.assertIsNone(brain._current_task_id)   # never adopted an id
        self.assertTrue(resp.closed)                # stream closed in finally

    def test_non2xx_nonjson_body_still_yields_error(self):
        # the old SSE branch would have parsed zero events → dead silence
        resp = _FakeResp({"content-type": "text/plain"}, body=b"nope", status_code=422)
        brain = self._brain_with(resp)

        async def run():
            return [ev async for ev in brain.submit("do a thing")]

        events = asyncio.run(run())
        self.assertEqual([e.type for e in events], ["error"])


class TestSSEScalarPayload(unittest.TestCase):
    """L8: a valid-but-scalar SSE payload (`data: null` / `data: 42`) parses
    fine but has no .get(); left unnormalized it AttributeErrors outside the
    reconnect catch and kills notices_loop (→ the whole process exits)."""

    def test_scalar_payloads_normalize_to_empty_dict(self):
        from jarvis.engines.brain_dispatcher import _sse_events

        class FakeResponse:
            async def aiter_lines(self):
                for line in [
                    "event: notify", "data: null", "",
                    "event: done", "data: 42", "",
                    "event: done", 'data: "hello"', "",
                ]:
                    yield line

        async def collect():
            return [ev async for ev in _sse_events(FakeResponse())]

        events = asyncio.run(collect())
        self.assertEqual(len(events), 3)
        for _event, data in events:
            self.assertIsInstance(data, dict)
            self.assertEqual(data, {})
            data.get("anything")  # must not raise


class TestNoticesReconnectBackoff(unittest.TestCase):
    """L7: notices() must back off before reconnecting on BOTH a finite non-2xx
    (raise_for_status → exception) and a clean EOF (stream ends, no exception) —
    the old code only slept in the except branch, so a 404 from a mismatched
    dispatcher tight-looped the socket."""

    def test_404_and_clean_eof_both_sleep_before_reconnect(self):
        from jarvis.engines import brain_dispatcher
        from jarvis.engines.brain_dispatcher import DispatcherBrain

        class FakeStreamResp:
            def __init__(self, status_code, lines):
                self.status_code = status_code
                self._lines = lines

            def raise_for_status(self):
                if not (200 <= self.status_code < 300):
                    raise httpx.HTTPStatusError("bad status", request=None, response=None)

            async def aiter_lines(self):
                for line in self._lines:
                    yield line

        class FakeStreamCtx:
            def __init__(self, resp):
                self._resp = resp

            async def __aenter__(self):
                return self._resp

            async def __aexit__(self, *exc):
                return False

        responses = [FakeStreamResp(404, []), FakeStreamResp(200, [])]  # 404 then clean EOF

        class FakeClient:
            def stream(self, method, url, timeout=None):
                return FakeStreamCtx(responses.pop(0))

        brain = DispatcherBrain(base_url="http://x:1")
        brain._client = FakeClient()

        sleeps = []

        async def fake_sleep(secs):
            sleeps.append(secs)
            if len(sleeps) >= 2:
                raise asyncio.CancelledError  # break the otherwise-infinite loop

        async def run():
            with mock.patch.object(brain_dispatcher.asyncio, "sleep", fake_sleep):
                try:
                    async for _ in brain.notices():
                        pass
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())
        self.assertEqual(sleeps, [3, 3])  # backed off after the 404 AND the EOF


class TestSanitizeForInjection(unittest.TestCase):
    """L9: transcripts are typed at the cursor via ydotool; a hallucinated
    newline is otherwise an Enter keypress (a terminal runs the line). Strip
    control/non-printing chars, keep ordinary printable text incl. unicode."""

    def test_strips_newlines_tabs_and_controls(self):
        from jarvis.sanitize import sanitize_for_injection

        self.assertEqual(sanitize_for_injection("rm -rf /\n"), "rm -rf /")
        self.assertEqual(sanitize_for_injection("hello\nworld"), "hello world")
        self.assertEqual(sanitize_for_injection("hello\r\nworld"), "hello world")
        self.assertEqual(sanitize_for_injection("tab\there"), "tab here")
        self.assertEqual(sanitize_for_injection("a\x00b\x07c"), "abc")

    def test_preserves_normal_and_unicode_text(self):
        from jarvis.sanitize import sanitize_for_injection

        self.assertEqual(sanitize_for_injection("The capital of France is Paris."),
                         "The capital of France is Paris.")
        self.assertEqual(sanitize_for_injection("café résumé naïve"), "café résumé naïve")
        self.assertEqual(sanitize_for_injection("emoji 😀 ok"), "emoji 😀 ok")
        self.assertEqual(sanitize_for_injection("run backup_db.sh, it's 3.50"),
                         "run backup_db.sh, it's 3.50")

    def test_all_control_or_empty_yields_empty(self):
        from jarvis.sanitize import sanitize_for_injection

        self.assertEqual(sanitize_for_injection("\n\r\t\x00"), "")
        self.assertEqual(sanitize_for_injection(""), "")


if __name__ == "__main__":
    unittest.main()
