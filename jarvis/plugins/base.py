"""The four voice-pipeline ABCs (locked decision #6). Engines are pure
converters: playback, the TTS sanitizer, and barge-in cancellation live in the
player/main loop so swapping Piper for Kokoro stays a one-line config change.
Silero VAD is a fixed utility, not a plugin point.

load() is called once at startup; models stay warm — never reload per request.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Iterator, Literal

import numpy as np

# 16 kHz mono int16 audio chunk
Frame = np.ndarray


class WakeWordEngine(ABC):
    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def process(self, frame: Frame) -> bool:
        """Feed one ~80ms frame; True on wake-word detection."""

    @abstractmethod
    def reset(self) -> None:
        """Clear internal buffers after a detection."""


class STTEngine(ABC):
    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def transcribe(self, audio: Frame) -> str:
        """One full utterance in, text out."""


@dataclass
class BrainEvent:
    type: Literal["ack", "delta", "done", "notice", "error"]
    text: str = ""
    task_id: str | None = None


class Brain(ABC):
    @abstractmethod
    def submit(self, text: str, source: str = "voice") -> AsyncIterator[BrainEvent]:
        """Send text to the dispatcher; yields ack/delta/done/error events."""

    @abstractmethod
    async def cancel(self) -> None:
        """Abort the in-flight request — the barge-in path (<200ms)."""


class TTSEngine(ABC):
    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def synthesize(self, sentence: str) -> Iterator[Frame]:
        """One sentence in, audio chunks out (streaming)."""
