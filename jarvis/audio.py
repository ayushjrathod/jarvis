"""Audio I/O: push-to-talk recorder, continuous mic stream (wake word / VAD /
barge-in monitoring), and an interruptible player.

All 16 kHz mono int16 on the capture side; the player runs at the TTS engine's
native rate. Capture pattern lifted from the original ptt_dictate.py.
"""

from __future__ import annotations

import queue
import threading

import numpy as np
import sounddevice as sd


class Recorder:
    """Hold-to-talk: start() on key down, stop() on key up -> int16 array."""

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._frames: list[np.ndarray] = []
        self._stream = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._stream is not None:
                return
            self._frames = []

            def cb(indata, _frames, _time, _status):
                self._frames.append(indata.copy())

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
        self._stream.start()
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
            with sd.OutputStream(samplerate=sample_rate, channels=1, dtype="int16") as out:
                out.write(data.reshape(-1, 1))
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def play_beep_async(sample_rate: int = 16000, ms: int = 120, freq: float = 880.0):
    play_async(beep(sample_rate, ms, freq), sample_rate)


class Player:
    """Blocking chunk player with an interrupt flag checked between chunks —
    this is what makes <200ms barge-in possible."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self.interrupt = threading.Event()

    def play(self, chunks) -> bool:
        """Play an iterable of int16 arrays. Returns False if interrupted."""
        with sd.OutputStream(samplerate=self.sample_rate, channels=1, dtype="int16") as out:
            for chunk in chunks:
                if self.interrupt.is_set():
                    return False
                out.write(chunk.reshape(-1, 1))
        return not self.interrupt.is_set()

    def stop(self):
        self.interrupt.set()

    def resume(self):
        self.interrupt.clear()
