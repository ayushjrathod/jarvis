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

_CODE_BLOCK = re.compile(r"```.*?```|~~~.*?~~~", re.S)
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

_FENCES = ("```", "~~~")  # GFM allows both; models emit both


def _fence_count(text: str, start: int = 0, end: int | None = None) -> int:
    """Fence markers in text[start:end]. Mixed ```/~~~ nesting miscounts —
    accepted: real replies don't nest one marker inside the other, and the
    alternative (style tracking) breaks the day they do it anyway."""
    stop = len(text) if end is None else end
    return sum(text.count(m, start, stop) for m in _FENCES)


def _find_fence(text: str, start: int = 0) -> int:
    """Earliest index of either marker at/after start, -1 when absent."""
    hits = [i for m in _FENCES for i in [text.find(m, start)] if i >= 0]
    return min(hits) if hits else -1


def _rfind_fence(text: str) -> int:
    """Latest index of either marker, -1 when absent."""
    return max([text.rfind(m) for m in _FENCES])
# spoken in place of a fenced block the chunker refuses to buffer to its close,
# and when a single reply outruns the whole-reply cap
_CODE_OMITTED = "Code block omitted."
_TRUNCATED = "The rest of the answer is too long to read out."


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
    """Feed streaming deltas in, get complete speakable sentences out.

    Fence-aware since 2026-08-10. `sanitize()` can only swallow a COMPLETE
    ```-fenced pair, but this chunker sanitizes each chunk in isolation and
    force-flushes an oversized buffer — and code has almost no sentence
    boundaries, so a block longer than `max_buffer` was cut into pieces, the
    fence never survived into a single chunk, "code block omitted" never fired
    and Piper read the raw shell aloud (verified: a 1029-char block became five
    chunks of `rm -rf …` text). Two rules fix it: nothing is emitted while a
    fence is open, and no chunk boundary is ever placed inside a pair.
    """

    def __init__(self, max_buffer: int = 300, max_code: int = 4000,
                 max_total: int = 3000):
        self._buf = ""
        self._max = max_buffer
        # Ceiling on how much unterminated code we'll hold waiting for the
        # closing fence. Past it we say so once and discard to the closer —
        # buffering a model that decided to print a whole file is worse than
        # losing it, and a stream that dies mid-fence would never flush.
        self._max_code = max_code
        # Nothing capped the total spoken length anywhere (2026-08-10): a
        # reply that dumps a directory listing holds the speaker — and `busy`
        # — for as long as the model keeps writing, with barge-in the only
        # way out. ~3000 chars is a few minutes of Piper.
        self._max_total = max_total
        self._spoken = 0
        self._dropping = False  # inside a runaway block, discarding to its closer
        # Trailing "`"s of a fenceless dropping delta. Token boundaries split
        # inside the 3-char closer routinely (`` `` `` then `` ` ``), so the
        # partition below must see the carry + the new delta together.
        self._carry = ""

    # -- fence bookkeeping ---------------------------------------------------
    # A position sits inside a ``` pair iff an odd number of fences precede it;
    # the buffer as a whole has an open fence iff its fence count is odd.

    def _fence_open(self) -> bool:
        return _fence_count(self._buf) % 2 == 1

    def _inside_fence(self, index: int) -> bool:
        return _fence_count(self._buf, 0, index) % 2 == 1

    def _next_boundary(self) -> int | None:
        """End offset of the next sentence boundary that is NOT inside a
        fenced pair — code is full of periods, and splitting there would
        strand the two fences in different chunks."""
        for m in _SENTENCE_END.finditer(self._buf):
            if not self._inside_fence(m.start()):
                return m.end()
        return None

    def _emit(self, out: list[str], text: str) -> None:
        """Append one speakable chunk, enforcing the whole-reply cap."""
        if not text or self._spoken >= self._max_total:
            return
        self._spoken += len(text)
        out.append(text)
        if self._spoken >= self._max_total:
            out.append(_TRUNCATED)  # once: further chunks return above

    def feed(self, delta: str) -> list[str]:
        out: list[str] = []
        if self._dropping:
            # mid-runaway-block: everything up to the closing fence is code we
            # already announced as omitted, so it never reaches the buffer —
            # but the closer may be SPLIT across deltas, so search the
            # carry + delta together. A full 3-char marker would have matched
            # already, so at most 2 of one kind carry forward.
            s = self._carry + delta
            i = _find_fence(s)
            if i < 0:
                m = re.search(r"(`{1,2}|~{1,2})$", delta)
                self._carry = m.group(0) if m else ""
                return out
            self._dropping, self._carry, delta = False, "", s[i + 3:]
        self._buf += delta
        while True:
            if self._fence_open():
                # Hold everything while a fence is open: emitting now would
                # speak raw code, because the closing fence sanitize() needs
                # would land in a later chunk.
                if len(self._buf) <= self._max_code:
                    return out
                cut = _rfind_fence(self._buf)  # the last fence is the open one
                prefix, self._buf, self._dropping = self._buf[:cut], "", True
                self._emit(out, sanitize(prefix))
                self._emit(out, _CODE_OMITTED)
                return out
            end = self._next_boundary()
            if end is not None:
                sentence, self._buf = self._buf[:end], self._buf[end:]
                self._emit(out, sanitize(sentence))
            elif len(self._buf) > self._max:
                # no boundary in an oversized buffer — flush at last space
                cut = self._buf.rfind(" ", 0, self._max)
                cut = cut if cut > 0 else self._max
                if self._inside_fence(cut):
                    # the fence count is even here, so the next fence at/after
                    # the cut closes the pair the cut fell into: take the whole
                    # block as one chunk and let sanitize() swallow it
                    close = _find_fence(self._buf, cut)
                    cut = close + 3 if close >= 0 else len(self._buf)
                chunk, self._buf = self._buf[:cut], self._buf[cut:]
                self._emit(out, sanitize(chunk))
            else:
                return out

    def flush(self) -> list[str]:
        out: list[str] = []
        rest, self._buf, self._dropping, self._carry = self._buf, "", False, ""
        if _fence_count(rest) % 2 == 1:
            # the stream ended mid-block (cancelled reply, dropped SSE): the
            # unpaired fence means sanitize() would hand the code to the TTS
            cut = _rfind_fence(rest)
            self._emit(out, sanitize(rest[:cut]))
            self._emit(out, _CODE_OMITTED)
            return out
        self._emit(out, sanitize(rest))
        return out
