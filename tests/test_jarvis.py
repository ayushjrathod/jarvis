"""Phase B unit tests: sanitizer, sentence chunker, engine registry, config,
and the Brain's SSE parsing — no audio hardware, no network.
"""

import asyncio
import unittest

from jarvis.config import JarvisConfig
from jarvis.sanitize import SentenceChunker, sanitize


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


if __name__ == "__main__":
    unittest.main()
