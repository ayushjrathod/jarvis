"""Wake-word-triggered, VAD-endpointed utterance capture.

Split out of jarvis/main.py so the capture logic can run against a fake
frame source in tests (no mic hardware, no wake/VAD models needed — those
are duck-typed).

Pulled from the wake-word detection window into a small ring buffer: frames
seen *before* the wake word fires are kept and spliced onto the front of the
capture, since the first word of an utterance can otherwise land in the same
~32ms frame that triggers detection and get clipped.
"""

from __future__ import annotations

from collections import deque
from typing import Callable

import numpy as np

from jarvis.vad import FRAME_SAMPLES


class MicLostError(RuntimeError):
    """The capture device stopped delivering audio *after* the stream opened
    (BT dropout, unplug, PipeWire node death). Raised out of
    capture_after_wake so wake_loop's open-time retry re-opens the MicStream
    — without this the read loop just spins on None forever and the wake word
    goes silently dead until the process restarts (finding H6).
    """


class MicWatchdog:
    """Detects a capture device that died mid-stream by counting consecutive
    None reads.

    A live mic delivers a frame every ~32ms, so read() returns immediately and
    never times out — even during silence, which is still audio frames, not
    None. Once the device is gone the PortAudio callback stops firing and
    read() returns None on every read timeout (~1s). A single None is a normal
    transient; many *consecutive* Nones mean the device is gone. Any good frame
    resets the streak, so ordinary silence never trips it.
    """

    def __init__(self, none_limit: int):
        self.none_limit = max(1, none_limit)
        self.none_streak = 0

    def observe(self, frame) -> None:
        """Feed each read() result. Raises MicLostError once none_limit
        consecutive Nones have been seen; any non-None frame resets the streak."""
        if frame is not None:
            self.none_streak = 0
            return
        self.none_streak += 1
        if self.none_streak >= self.none_limit:
            raise MicLostError(
                f"no audio for {self.none_streak} consecutive reads; device lost?"
            )


def capture_after_wake(
    read_frame: Callable[[], np.ndarray | None],
    wake,
    vad,
    cfg,
    prebuffer_frames: int = 0,
    is_busy: Callable[[], bool] = lambda: False,
    on_busy: Callable[[], None] = lambda: None,
    on_wake: Callable[[], None] = lambda: None,
    none_limit: int = 5,
) -> np.ndarray | None:
    """Block on `read_frame()` until the wake word fires, then VAD-endpoint
    an utterance. Returns the captured audio (prebuffer + utterance), or
    None if nothing was heard within the silence budget.
    """
    sr = cfg.sample_rate
    ring: deque = deque(maxlen=prebuffer_frames) if prebuffer_frames > 0 else deque(maxlen=0)
    watch = MicWatchdog(none_limit)

    # phase 1: wait for the wake word
    while True:
        if is_busy():
            on_busy()
            continue
        frame = read_frame()
        watch.observe(frame)  # raises MicLostError if the device died mid-stream
        if frame is None or len(frame) != FRAME_SAMPLES:
            continue
        ring.append(frame)
        if wake.process(frame):
            break
    wake.reset()
    on_wake()

    # phase 2: VAD-endpointed capture, prebuffer spliced onto the front
    vad.reset()
    frames: list[np.ndarray] = []
    silence_frames = 0
    silence_limit = cfg.endpoint_silence_ms // 32
    max_frames = cfg.max_utterance_s * sr // FRAME_SAMPLES
    heard_speech = False
    waited = 0

    def consume(frame):
        nonlocal heard_speech, silence_frames, waited
        frames.append(frame)
        if vad.is_speech(frame):
            heard_speech = True
            silence_frames = 0
        elif heard_speech:
            silence_frames += 1
        else:
            waited += 1

    for frame in ring:
        consume(frame)

    while len(frames) < max_frames:
        if heard_speech and silence_frames >= silence_limit:
            break
        if not heard_speech and waited > 5 * sr // FRAME_SAMPLES:
            return None
        frame = read_frame()
        watch.observe(frame)  # raises MicLostError if the device died mid-stream
        if frame is None:
            continue
        consume(frame)

    return np.concatenate(frames) if heard_speech else None
