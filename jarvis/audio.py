"""Audio I/O: push-to-talk recorder, continuous mic stream (wake word / VAD /
barge-in monitoring), and an interruptible player.

All 16 kHz mono int16 on the capture side; the player runs at the TTS engine's
native rate. Capture pattern lifted from the original ptt_dictate.py.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from typing import Iterator

import numpy as np
import sounddevice as sd

log = logging.getLogger("jarvis.audio")


@contextlib.contextmanager
def _output_stream(sample_rate: int):
    """An sd.OutputStream that can't leak when start() fails.

    PortAudio allocates the native stream in sd.OutputStream.__init__, but
    sounddevice's own context manager start()s in __enter__ and only close()s
    in __exit__ — and __exit__ never runs if __enter__ raised. There is no
    __del__ either, so every failed start() strands a native stream. Both
    callers retry forever (play_async on each wake ack, speak_sentences on
    every sentence), so that accumulates. 2026-08-10.
    """
    stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="int16")
    try:
        stream.start()
        yield stream
    finally:
        # same order sounddevice's own __exit__ uses — stop() drains what is
        # already queued (close() alone aborts it and clips the tail); both
        # ignore a stream that never started.
        stream.stop()
        stream.close()


class Recorder:
    """Hold-to-talk: start() on key down, stop() on key up -> int16 array.

    Accumulated audio is capped at `max_seconds` (0/None = uncapped): a
    wedged or stuck trigger key (object resting on the keyboard, evdev repeat
    wedge) would otherwise grow `_frames` without bound — ~115MB/hour of int16
    — and hand faster-whisper a multi-hour clip on release. Past the cap the
    callback simply stops appending, keeping the earliest `max_seconds`."""

    def __init__(self, sample_rate: int = 16000, max_seconds: float | None = None):
        self.sample_rate = sample_rate
        self._max_samples = int(max_seconds * sample_rate) if max_seconds else 0
        self._frames: list[np.ndarray] = []
        self._total = 0
        self._stream = None
        self._lock = threading.Lock()

    def _append(self, block: np.ndarray) -> None:
        """Append one capture block, truncating at the duration cap. Runs in
        the PortAudio callback thread; kept small and lock-free like the
        original callback (stop() reads `_frames` under the lock)."""
        if self._max_samples <= 0:
            self._frames.append(block.copy())
            return
        remaining = self._max_samples - self._total
        if remaining <= 0:
            return
        if len(block) > remaining:
            block = block[:remaining]
        self._frames.append(block.copy())
        self._total += len(block)

    def start(self):
        with self._lock:
            if self._stream is not None:
                return
            self._frames = []
            self._total = 0

            def cb(indata, _frames, _time, _status):
                self._append(indata)

            self._stream = sd.InputStream(
                samplerate=self.sample_rate, channels=1, dtype="int16", callback=cb
            )
            self._stream.start()

    def stop(self) -> np.ndarray:
        with self._lock:
            if self._stream is None:
                return np.zeros(0, dtype=np.int16)
            self._stream.stop()
            self._stream.close()
            self._stream = None
            if not self._frames:
                return np.zeros(0, dtype=np.int16)
            return np.concatenate(self._frames).flatten()

    def is_recording(self) -> bool:
        """True between start() and stop().

        The wake word listens on the same microphone the PTT recorder is
        capturing from, and `busy` is only held during handle_utterance — so
        while PTT was still recording nothing marked the assistant busy.
        Holding PTT and saying "hey jarvis, …" (habit, or a false positive on
        the PTT speech itself) let capture_after_wake answer the utterance,
        and the PTT release then answered the SAME audio again: two claude -p
        runs, two spoken replies, two episode rows (2026-08-10)."""
        return self._stream is not None


class MicStream:
    """Continuous capture in fixed-size frames, consumed via read()."""

    def __init__(self, sample_rate: int = 16000, frame_samples: int = 512):
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._q: queue.Queue = queue.Queue(maxsize=256)
        self._stream = None

    def __enter__(self):
        def cb(indata, _frames, _time, _status):
            try:
                self._q.put_nowait(indata[:, 0].copy())
            except queue.Full:
                pass

        self._stream = sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="int16",
            blocksize=self.frame_samples, callback=cb,
        )
        try:
            self._stream.start()
        except BaseException:
            # PortAudio opened the stream in __init__ above; only __exit__
            # closes it, and __exit__ never runs when __enter__ raises (there
            # is no __del__ either). wake_loop catches, sleeps 5s and retries
            # forever, so a device that keeps refusing to start (busy after a
            # BT dropout) leaked one native stream per retry. 2026-08-10.
            self._stream.close()
            self._stream = None
            raise
        return self

    def __exit__(self, *exc):
        self._stream.stop()
        self._stream.close()
        self._stream = None
        return False

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self):
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break


def beep(sample_rate: int = 16000, ms: int = 120, freq: float = 880.0) -> np.ndarray:
    """Short sine tone with a 10ms fade in/out (no clicks), int16 mono."""
    n = max(1, int(sample_rate * ms / 1000))
    tone = np.sin(2 * np.pi * freq * np.arange(n) / sample_rate)
    fade = min(n // 2, int(sample_rate * 0.01))
    if fade > 0:
        tone[:fade] *= np.linspace(0.0, 1.0, fade)
        tone[-fade:] *= np.linspace(1.0, 0.0, fade)
    return (tone * 0.7 * 32767).astype(np.int16)


def play_async(samples: np.ndarray, sample_rate: int):
    """Fire-and-forget playback of a short int16 clip (wake acknowledgments).
    Runs in its own thread so it can't delay capture; a failed play (device
    busy, no sink) is never fatal."""
    # trailing silence so closing the stream can't clip the clip's tail
    data = np.concatenate([samples, np.zeros(sample_rate // 10, dtype=np.int16)])

    def run():
        try:
            with _output_stream(sample_rate) as out:
                out.write(data.reshape(-1, 1))
        except Exception:
            # This covers every wake acknowledgment and PTT beep, and the bare
            # `pass` that used to be here made a dead output sink look exactly
            # like the wake word not firing — the hardest failure to debug in
            # this pipeline. Fire-and-forget on our own thread, so a log line
            # is all we can do, but it's the difference between "the mic is
            # broken" and "the speaker is". 2026-08-10.
            log.warning("acknowledgment playback failed (%d samples @ %dHz)",
                        len(data), sample_rate, exc_info=True)

    threading.Thread(target=run, daemon=True).start()


def play_beep_async(sample_rate: int = 16000, ms: int = 120, freq: float = 880.0):
    play_async(beep(sample_rate, ms, freq), sample_rate)


def _subframes(chunk: np.ndarray, size: int) -> Iterator[np.ndarray]:
    """Yield `size`-sample slices of `chunk` (int16 mono). Piper hands the
    player one chunk per whole sentence; slicing lets play() check the
    interrupt flag mid-sentence so barge-in cuts audio within one sub-frame."""
    for start in range(0, len(chunk), size):
        yield chunk[start:start + size]


class Player:
    """Blocking chunk player with an interrupt flag checked between ~100ms
    sub-frames — this is what makes <200ms barge-in possible."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self.interrupt = threading.Event()

    def play(self, chunks) -> bool:
        """Play an iterable of int16 arrays. Returns False if interrupted.

        Each chunk (piper yields one per whole sentence) is sliced into ~100ms
        sub-frames with the interrupt flag checked before every write, so a
        long sentence still stops within ~100ms — locked decision #7."""
        sub = max(1, self.sample_rate // 10)  # ~100ms of audio
        with _output_stream(self.sample_rate) as out:
            for chunk in chunks:
                for frame in _subframes(chunk, sub):
                    if self.interrupt.is_set():
                        return False
                    out.write(frame.reshape(-1, 1))
        return not self.interrupt.is_set()

    def stop(self):
        self.interrupt.set()

    def resume(self):
        self.interrupt.clear()
