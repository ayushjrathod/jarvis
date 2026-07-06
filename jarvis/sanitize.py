"""Text-to-speech preprocessing: strip markdown/URLs so Piper never reads
asterisks aloud, and chunk streaming text into sentences so playback starts
before the model finishes writing.

Pattern per the reference table (OpenClaw): sanitize before TTS, speak
sentence-by-sentence as text streams. Blockquote/horizontal-rule handling
adapted from openclaw/openclaw src/shared/text/strip-markdown.ts (MIT,
(c) 2026 OpenClaw Foundation).
"""

from __future__ import annotations

import re

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_HEADER = re.compile(r"^#{1,6}\s*", re.M)
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3}|~~)(?=\S)(.+?)(?<=\S)\1")
_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)
_NUMBERED = re.compile(r"^\s*\d+\.\s+", re.M)
_BLOCKQUOTE = re.compile(r"^>\s?", re.M)
_HRULE = re.compile(r"^[-*_]{3,}\s*$", re.M)
_HASHTAG = re.compile(r"(?<!\w)#(\w+)")
_WS = re.compile(r"[ \t]+")

# sentence boundary: ./!/? followed by whitespace (avoids most mid-number splits)
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+")


def sanitize(text: str) -> str:
    """Make model output speakable. Idempotent, safe on plain prose."""
    text = _CODE_BLOCK.sub(" code block omitted. ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" a link ", text)
    text = _HEADER.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _BULLET.sub("", text)
    text = _NUMBERED.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _HRULE.sub("", text)
    text = _HASHTAG.sub(r"\1", text)
    text = text.replace("|", " ")
    text = _WS.sub(" ", text)
    return text.strip()


class SentenceChunker:
    """Feed streaming deltas in, get complete speakable sentences out."""

    def __init__(self, max_buffer: int = 300):
        self._buf = ""
        self._max = max_buffer

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out = []
        while True:
            m = _SENTENCE_END.search(self._buf)
            if m:
                sentence, self._buf = self._buf[: m.end()], self._buf[m.end():]
                sentence = sanitize(sentence)
                if sentence:
                    out.append(sentence)
            elif len(self._buf) > self._max:
                # no boundary in an oversized buffer — flush at last space
                cut = self._buf.rfind(" ", 0, self._max)
                cut = cut if cut > 0 else self._max
                chunk, self._buf = self._buf[:cut], self._buf[cut:]
                chunk = sanitize(chunk)
                if chunk:
                    out.append(chunk)
            else:
                return out

    def flush(self) -> list[str]:
        rest, self._buf = sanitize(self._buf), ""
        return [rest] if rest else []
