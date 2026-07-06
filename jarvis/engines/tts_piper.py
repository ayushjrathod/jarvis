"""TTSEngine on Piper. Pure converter: sentence in, int16 chunks out at
`self.sample_rate` — playback and barge-in live in the player.

Handles both piper-tts APIs (synthesize_stream_raw on older releases,
AudioChunk-yielding synthesize on piper >= 1.3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np

from jarvis.plugins.base import Frame, TTSEngine


class PiperTTS(TTSEngine):
    def __init__(self, voice: str = "en_US-lessac-medium", models_dir: str | Path = "data/models"):
        self.voice_name = voice
        self.models_dir = Path(models_dir)
        self._voice = None
        self.sample_rate = 22050

    def load(self) -> None:
        try:
            from piper import PiperVoice
        except ImportError:
            from piper.voice import PiperVoice

        onnx = self.models_dir / f"{self.voice_name}.onnx"
        if not onnx.exists():
            raise FileNotFoundError(
                f"piper voice missing: {onnx}\n"
                f"run: .venv/bin/python -m piper.download_voices {self.voice_name} "
                f"--data-dir {onnx.parent}"
            )
        self._voice = PiperVoice.load(str(onnx))
        self.sample_rate = self._voice.config.sample_rate

    def synthesize(self, sentence: str) -> Iterator[Frame]:
        if self._voice is None:
            raise RuntimeError("call load() first")
        if hasattr(self._voice, "synthesize_stream_raw"):
            for raw in self._voice.synthesize_stream_raw(sentence):
                yield np.frombuffer(raw, dtype=np.int16)
        else:
            for chunk in self._voice.synthesize(sentence):
                arr = getattr(chunk, "audio_int16_array", None)
                if arr is None:
                    arr = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
                yield arr
