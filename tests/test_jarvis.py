"""Phase B unit tests: sanitizer, sentence chunker, engine registry, config,
and the Brain's SSE parsing — no audio hardware, no network.
"""

import asyncio
import json
import subprocess
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import httpx
import numpy as np

from jarvis.audio import Recorder
from jarvis.config import JarvisConfig
from jarvis.hotkey import HotkeyWatcher
from jarvis.plugins.base import BrainEvent
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


def _stream(text: str, size: int = 40):
    """Chop text into delta-sized pieces, the way the SSE stream arrives."""
    return [text[i:i + size] for i in range(0, len(text), size)]


class TestChunkerFences(unittest.TestCase):
    """2026-08-10: sanitize()'s _CODE_BLOCK only matches a COMPLETE ```-fenced
    pair, but the chunker sanitizes each chunk in isolation and force-flushes
    an oversized buffer — and code has almost no sentence boundaries. So a
    block longer than max_buffer was cut into pieces, the fence never survived
    into one chunk, "code block omitted" never fired, and Piper read the raw
    shell aloud (a 1029-char block became five chunks of `rm -rf …`)."""

    LONG_CODE = "\n".join(f"rm -rf /tmp/junk-{i}  # step {i}. done" for i in range(30))

    def test_long_code_block_is_never_read_aloud(self):
        text = f"Here is the script.\n```bash\n{self.LONG_CODE}\n```\nRun it carefully."
        self.assertGreater(len(self.LONG_CODE), 300)  # past max_buffer, the old bug
        c = SentenceChunker()
        out = []
        for delta in _stream(text):
            out.extend(c.feed(delta))
        out.extend(c.flush())
        spoken = " ".join(out)
        self.assertNotIn("rm -rf", spoken)
        self.assertIn("code block omitted", spoken.lower())
        self.assertIn("Here is the script.", spoken)
        self.assertIn("Run it carefully.", spoken)

    def test_nothing_emitted_while_a_fence_is_open(self):
        c = SentenceChunker()
        self.assertEqual(c.feed("Sure. "), ["Sure."])
        # a period-rich code line must not become a "sentence" of its own
        self.assertEqual(c.feed("```py\nx = 1. y = 2. " + "z = 3. " * 80), [])

    def test_short_code_block_still_announced(self):
        c = SentenceChunker()
        out = c.feed("Try this. ```sh\nls -l\n``` Done.")
        out.extend(c.flush())
        spoken = " ".join(out)
        self.assertNotIn("ls -l", spoken)
        self.assertIn("code block omitted", spoken.lower())

    def test_runaway_fence_announced_once_then_prose_resumes(self):
        c = SentenceChunker(max_code=60)
        out = c.feed("```\n" + "x = 1\n" * 40)
        self.assertEqual(out, ["Code block omitted."])
        # the rest of the block is discarded up to its closing fence, and the
        # announcement is NOT repeated
        out2 = c.feed("y = 2\n" * 40)
        self.assertEqual(out2, [])
        out3 = c.feed("z = 3\n```\nBack to prose. ")
        self.assertEqual(out3, ["Back to prose."])
        self.assertNotIn("x = 1", " ".join(out + out2 + out3))

    def test_unclosed_fence_at_flush_is_not_spoken(self):
        # cancelled reply / dropped SSE mid-block: the pair never completes
        c = SentenceChunker()
        c.feed("Here you go. ```py\nsecret = 1\n")
        out = c.flush()
        self.assertNotIn("secret", " ".join(out))
        self.assertIn("Code block omitted.", out)

    def test_total_spoken_length_capped(self):
        # nothing capped the whole reply before: a model dumping a directory
        # listing held the speaker (and `busy`) for as long as it kept writing
        c = SentenceChunker(max_total=100)
        out = []
        for _ in range(50):
            out.extend(c.feed("This is a sentence. "))
        out.extend(c.flush())
        spoken = " ".join(out)
        self.assertIn("too long to read out", spoken)
        self.assertLess(len(spoken), 300)


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
    """Records writes and lifecycle calls; stands in for sd.OutputStream.

    start()/stop()/close() rather than __enter__/__exit__ since 2026-08-10:
    jarvis.audio._output_stream drives the stream by hand so a failing start()
    still gets closed (sounddevice's own context manager only closes in
    __exit__, which never runs when __enter__ raised)."""

    def __init__(self, on_write=None, start_error=None):
        self.writes: list = []
        self._on_write = on_write
        self._start_error = start_error
        self.started = False
        self.closed = False

    def start(self):
        if self._start_error is not None:
            raise self._start_error
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

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


class TestStreamLeakOnFailedStart(unittest.TestCase):
    """2026-08-10: sounddevice opens the PortAudio stream in __init__, has no
    __del__, and only closes in __exit__ — which never runs if __enter__ (i.e.
    start()) raised. Both callers retry forever (wake_loop every 5s, play_async
    per acknowledgment), so each failed start leaked one native stream."""

    def test_output_stream_closes_when_start_fails(self):
        from jarvis.audio import _output_stream

        fake = _FakeOut(start_error=RuntimeError("device busy"))
        with mock.patch("jarvis.audio.sd.OutputStream", return_value=fake):
            with self.assertRaises(RuntimeError):
                with _output_stream(16000):
                    pass  # pragma: no cover — start() raised first
        self.assertTrue(fake.closed)

    def test_output_stream_closes_on_the_happy_path_too(self):
        from jarvis.audio import _output_stream

        fake = _FakeOut()
        with mock.patch("jarvis.audio.sd.OutputStream", return_value=fake):
            with _output_stream(16000) as out:
                out.write(b"")
        self.assertTrue(fake.closed)
        self.assertFalse(fake.started)  # stopped (drained) before closing

    def test_micstream_closes_when_start_fails(self):
        from jarvis.audio import MicStream

        fake = _FakeOut(start_error=RuntimeError("device busy"))
        with mock.patch("jarvis.audio.sd.InputStream", return_value=fake):
            stream = MicStream(16000, FRAME_SAMPLES)
            with self.assertRaises(RuntimeError):
                stream.__enter__()
        self.assertTrue(fake.closed)
        self.assertIsNone(stream._stream)  # and nothing left half-open on self


class TestPlayAsyncLogsFailures(unittest.TestCase):
    """A bare `except: pass` covered every wake acknowledgment and PTT beep, so
    a dead output sink looked exactly like the wake word not firing — the
    hardest failure to debug in this pipeline (2026-08-10)."""

    class _InlineThread:
        """Runs the target on start() so the fire-and-forget playback thread
        is deterministic here (no sleeping on a real thread)."""

        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    def test_failure_is_logged_not_swallowed(self):
        from jarvis.audio import play_async

        fake = _FakeOut(start_error=RuntimeError("no sink"))
        with mock.patch("jarvis.audio.sd.OutputStream", return_value=fake), \
                mock.patch("jarvis.audio.threading.Thread", self._InlineThread):
            with self.assertLogs("jarvis.audio", level="WARNING") as caught:
                play_async(np.zeros(160, dtype=np.int16), 16000)
        self.assertTrue(any("playback failed" in line for line in caught.output))


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


def _app(mode="ptt"):
    """A Jarvis with engines constructed but nothing loaded — no models, no
    devices. Same seam TestSpeakSentencesBargeRace already uses."""
    from jarvis.audio import Player
    from jarvis.main import Jarvis

    cfg = JarvisConfig.load()
    app = Jarvis(cfg, mode=mode)
    app.player = Player(cfg.sample_rate)  # hermetic: no device until play()
    return app


async def _drain(q):
    """Stand-in for speak_sentences: consumes the queue, never touches audio."""
    while await q.get() is not None:
        pass
    return True


class TestPttBargeDuringTranscription(unittest.TestCase):
    """2026-08-10: `busy` is held from the top of handle_utterance, but
    speak_sentences — whose resume() clears player.interrupt — is only created
    AFTER the blocking transcribe. So a PTT press during the STT window (1-2s,
    much longer for a long clip; max_recording_s is 300) set the interrupt,
    had it wiped by that resume(), and the STALE question was answered and
    spoken while the new one waited behind it. Nothing logged."""

    def test_press_during_stt_drops_the_superseded_utterance(self):
        app = _app()
        submitted = []

        async def fake_submit(text, source="voice"):
            submitted.append(text)
            yield BrainEvent("delta", "an answer", None)

        app.brain.submit = fake_submit
        app.speak_sentences = _drain

        def transcribe(_audio):
            app._ptt_barge()  # the user presses again to rephrase
            return "the old question"

        app.stt.transcribe = transcribe
        audio = np.zeros(app.cfg.sample_rate, dtype=np.int16)  # 1s, past the tap guard
        asyncio.run(app.handle_utterance(audio))

        self.assertEqual(submitted, [])          # the stale question never ran
        self.assertTrue(app.barge_request.is_set())

    def test_no_press_still_answers_normally(self):
        app = _app()
        submitted = []

        async def fake_submit(text, source="voice"):
            submitted.append(text)
            yield BrainEvent("delta", "an answer", None)

        app.brain.submit = fake_submit
        app.speak_sentences = _drain
        app.stt.transcribe = lambda _audio: "the only question"
        asyncio.run(app.handle_utterance(np.zeros(app.cfg.sample_rate, dtype=np.int16)))

        self.assertEqual(submitted, ["the only question"])

    def test_barge_flag_only_set_while_an_interaction_is_in_flight(self):
        app = _app()

        async def scenario():
            self.assertFalse(app._ptt_barge())          # idle: ordinary press
            self.assertFalse(app.barge_request.is_set())
            async with app.busy:
                self.assertTrue(app._ptt_barge())       # mid-interaction: barge
            return True

        asyncio.run(scenario())
        self.assertTrue(app.barge_request.is_set())
        self.assertTrue(app.player.interrupt.is_set())  # playback stopped too


class TestWakeBusyPredicate(unittest.TestCase):
    """2026-08-10: capture_after_wake's gate was `busy.locked` alone, which is
    held only during handle_utterance — not while the PTT Recorder captures on
    the SAME microphone. In --mode both (what mission-jarvis ships), holding
    PTT and saying "hey jarvis, …" got the utterance answered by the wake path
    and then again by the PTT release: two claude -p runs, two spoken replies,
    two episode rows."""

    def test_recorder_reports_capture_state(self):
        r = Recorder(sample_rate=16000)
        self.assertFalse(r.is_recording())
        r._stream = object()   # what start() sets, without opening a device
        self.assertTrue(r.is_recording())

    def test_predicate_covers_recorder_and_lock(self):
        app = _app(mode="both")
        self.assertFalse(app._busy_for_wake())

        app.recorder._stream = object()          # PTT held, nothing dispatched yet
        self.assertTrue(app._busy_for_wake())
        app.recorder._stream = None

        async def while_busy():
            async with app.busy:
                return app._busy_for_wake()

        self.assertTrue(asyncio.run(while_busy()))


class TestNoticeQueue(unittest.TestCase):
    """H-list 2026-08-10: say() discarded speak_sentences' bool, so barging in
    on notice 1 was followed immediately by notices 2 and 3; and the pending
    list was unbounded."""

    def _app_with_speaker(self, interrupted):
        app = _app()
        spoken: list[str] = []

        async def fake_speak(q):
            while True:
                s = await q.get()
                if s is None:
                    break
                spoken.append(s)
            return not interrupted

        app.speak_sentences = fake_speak
        return app, spoken

    def test_barge_on_a_notice_drops_the_backlog(self):
        app, spoken = self._app_with_speaker(interrupted=True)
        app.pending_notices = ["one.", "two.", "three."]
        asyncio.run(app._flush_notices())
        self.assertEqual(spoken, ["one."])
        self.assertEqual(app.pending_notices, [])

    def test_uninterrupted_notices_all_get_spoken(self):
        app, spoken = self._app_with_speaker(interrupted=False)
        app.pending_notices = ["one.", "two.", "three."]
        asyncio.run(app._flush_notices())
        self.assertEqual(spoken, ["one.", "two.", "three."])

    def test_pending_notices_are_bounded(self):
        from jarvis.main import MAX_PENDING_NOTICES

        app = _app()

        async def fake_notices():
            for i in range(MAX_PENDING_NOTICES + 5):
                yield BrainEvent("notice", f"n{i}", None)

        app.brain.notices = fake_notices

        async def scenario():
            async with app.busy:      # busy → every notice queues
                await app.notices_loop()

        asyncio.run(scenario())
        self.assertEqual(len(app.pending_notices), MAX_PENDING_NOTICES)
        self.assertEqual(app.pending_notices[-1], f"n{MAX_PENDING_NOTICES + 4}")


class TestHotkeyWatcherSupervision(unittest.TestCase):
    """2026-08-10: main.py discarded start_ptt()'s watcher, so a watcher thread
    that raised died silently — ptt_loop parked forever on an empty queue while
    `systemctl --user status mission-jarvis` still said active (running)."""

    def test_run_records_the_exception_instead_of_losing_it(self):
        w = HotkeyWatcher("KEY_F9", lambda: None, lambda: None)

        def boom():
            raise OSError("no such device")

        w._watch = boom
        with self.assertLogs("jarvis.hotkey", level="ERROR") as caught:
            w.run()  # called directly: no thread, no evdev
        self.assertIsInstance(w.error, OSError)
        self.assertTrue(any("watcher died" in line for line in caught.output))

    def test_supervisor_raises_when_the_watcher_stops(self):
        app = _app()
        w = HotkeyWatcher("KEY_F9", lambda: None, lambda: None)
        w._watch = lambda: None   # "crashes" immediately, hardware never touched
        w.start()

        async def scenario():
            await asyncio.wait_for(app._supervise_ptt(w), timeout=5)

        with self.assertRaises(RuntimeError):
            asyncio.run(scenario())


class TestHotkeyPerDeviceState(unittest.TestCase):
    """2026-08-10: `pressed` was one bool shared across every watched device,
    so a second keyboard merely disappearing from select() while you held PTT
    fired on_release and truncated the clip mid-sentence."""

    def _watcher(self):
        events: list[str] = []
        w = HotkeyWatcher("KEY_F9",
                          lambda: events.append("press"),
                          lambda: events.append("release"))
        return w, events

    def test_unrelated_device_loss_does_not_release(self):
        w, events = self._watcher()
        pressed = {3}                 # keyboard fd 3 is holding PTT
        w._release(9, pressed)        # a different keyboard vanished
        self.assertEqual(events, [])
        self.assertEqual(pressed, {3})

    def test_the_holding_device_vanishing_does_release(self):
        w, events = self._watcher()
        pressed = {3}
        w._release(3, pressed)        # never leave a recording stuck on
        self.assertEqual(events, ["release"])

    def test_press_and_release_fire_once_across_devices(self):
        w, events = self._watcher()
        pressed: set[int] = set()
        w._press(3, pressed)
        w._press(3, pressed)          # evdev repeat / duplicate down
        w._press(7, pressed)          # a second keyboard holds it too
        self.assertEqual(events, ["press"])
        w._release(7, pressed)        # fd 3 still holds it
        self.assertEqual(events, ["press"])
        w._release(3, pressed)
        self.assertEqual(events, ["press", "release"])
        self.assertEqual(pressed, set())


class TestDispatcherBrainCancelClose(unittest.TestCase):
    """2026-08-10: the /cancel POST was guarded but the aclose() above it was
    not, so on a broken socket — exactly when cancelling matters — the
    exception propagated and the dispatcher's claude -p kept burning quota
    against the plan cap (half of locked decision #7 not honored)."""

    def test_failing_aclose_still_posts_cancel(self):
        from jarvis.engines.brain_dispatcher import DispatcherBrain

        class _ExplodingResp(_FakeResp):
            async def aclose(self):
                raise httpx.ReadError("socket is gone")

        brain = DispatcherBrain(base_url="http://x:1")
        brain._client = _FakeClient(None)
        brain._response = _ExplodingResp({})
        brain._current_task_id = "quick-7"

        asyncio.run(brain.cancel())

        self.assertEqual(brain._client.posts, ["http://x:1/task/quick-7/cancel"])
        self.assertIsNone(brain._current_task_id)
        self.assertIsNone(brain._response)


class TestDictateTypeTimeout(unittest.TestCase):
    """2026-08-10: `ydotool type` ran with no timeout. The single serializing
    worker thread is the only consumer of `jobs`, so a wedged ydotoold (the
    /dev/uinput quirk) meant every later dictation was enqueued and never
    typed — service still active (running), nothing logged after "typing: …"."""

    def test_timeout_is_passed_and_scales_with_the_transcript(self):
        from jarvis import dictate

        seen = {}

        def fake_run(cmd, timeout=None):
            seen[len(cmd[-1])] = timeout
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch("jarvis.dictate.subprocess.run", fake_run):
            dictate.type_text("short")
            dictate.type_text("x" * 2000)
        short, long = seen[len("short")], seen[2000]
        self.assertGreater(short, 0)
        self.assertGreater(long, short)  # a long dictation isn't killed early

    def test_wedged_ydotoold_is_logged_and_the_worker_survives(self):
        from jarvis import dictate

        boom = subprocess.TimeoutExpired(cmd="ydotool", timeout=10)
        with mock.patch("jarvis.dictate.subprocess.run", side_effect=boom):
            with self.assertLogs("jarvis.dictate", level="ERROR") as caught:
                dictate.type_text("hello there")  # must NOT raise
        self.assertTrue(any("ydotoold" in line for line in caught.output))


class TestPttDictateRefusesToRun(unittest.TestCase):
    """CLAUDE.md keeps jarvis/ptt_dictate.py as the reference for evdev hotkey
    capture, but it still ran — re-introducing the races dictate.py fixed
    (thread-per-release → concurrent transcribe + interleaved ydotool type,
    `frames` read while the callback appends, unguarded select/read)."""

    def test_module_entry_point_exits_nonzero(self):
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, "-m", "jarvis.ptt_dictate"],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("superseded by jarvis.dictate", proc.stderr)
        # it refuses BEFORE the whisper load, so it costs nothing
        self.assertNotIn("Loading faster-whisper", proc.stdout)


if __name__ == "__main__":
    unittest.main()
