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
import unicodedata

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_HEADER = re.compile(r"^#{1,6}\s*", re.M)
_EMPHASIS = re.compile(r"(\*{1,3}|~~|__)(?=\S)(.+?)(?<=\S)\1")
# Single-underscore italics only at word edges ([^\W_] = letter/digit, the
# re equivalent of \p{L}\p{N} in openclaw's strip-markdown.ts): _really_ is
# emphasis, but the underscores in backup_db.sh are part of the name and must
# reach the TTS intact.
_UNDERSCORE_EMPHASIS = re.compile(r"(?<![^\W_])_(?!_)(?=\S)(.+?)(?<=\S)(?<!_)_(?![^\W_])")
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
    text = _UNDERSCORE_EMPHASIS.sub(r"\1", text)
    text = _BULLET.sub("", text)
    text = _NUMBERED.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _HRULE.sub("", text)
    text = _HASHTAG.sub(r"\1", text)
    text = text.replace("|", " ")
    text = _WS.sub(" ", text)
    return text.strip()


# whitespace controls that must not reach ydotool as literal keypresses; they
# collapse to a plain space so words stay separated (a bare newline would be an
# Enter keypress — executing whatever sits in the focused terminal)
_INJECT_WS = {"\n", "\r", "\t", "\f", "\v"}


def sanitize_for_injection(text: str) -> str:
    """Make a transcript safe to type at the cursor via `ydotool type`.

    Whisper occasionally hallucinates a newline; injected verbatim that is an
    Enter keypress in the focused window (a terminal would run the line). Other
    control / non-printing characters can fire unintended key events too. So:
    newlines/tabs become a single space, every other control char (Unicode
    C* category) is dropped, and ordinary printable text — spaces, punctuation,
    unicode letters and symbols — passes through unchanged. Pure function."""
    out = []
    for ch in text:
        if ch in _INJECT_WS:
            out.append(" ")
        elif unicodedata.category(ch).startswith("C"):
            continue  # Cc/Cf/Cs/Co/Cn — control or non-printing
        else:
            out.append(ch)
    # split()/join collapses the runs the substitutions created and trims ends
    return " ".join("".join(out).split())


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
