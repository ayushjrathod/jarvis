"""Phase B unit tests: sanitizer, sentence chunker, engine registry, config,
and the Brain's SSE parsing — no audio hardware, no network.
"""

import asyncio
import unittest
from dataclasses import dataclass

import numpy as np

from jarvis.config import JarvisConfig
from jarvis.sanitize import SentenceChunker, sanitize
from jarvis.vad import FRAME_SAMPLES
from jarvis.wake_capture import capture_after_wake


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
    def test_loads_repo_config(self):
        cfg = JarvisConfig.load()
        self.assertEqual(cfg.trigger_key, "KEY_F9")
        self.assertEqual(cfg.sample_rate, 16000)
        self.assertEqual(cfg.dispatcher_url, "http://127.0.0.1:8765")
        self.assertEqual(cfg.stt["model"], "small.en")
        self.assertGreaterEqual(cfg.barge_in_frames, 1)
        self.assertGreaterEqual(cfg.wake_beep_ms, 0)


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


if __name__ == "__main__":
    unittest.main()
