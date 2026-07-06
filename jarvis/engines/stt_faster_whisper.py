"""STTEngine on faster-whisper (CPU int8). Record+transcribe logic lifted from
the original ptt_dictate.py.
"""

from __future__ import annotations

import numpy as np

from jarvis.plugins.base import Frame, STTEngine


class FasterWhisperSTT(STTEngine):
    def __init__(self, model: str = "small.en", compute: str = "int8", language: str = "en"):
        self.model_name = model
        self.compute = compute
        self.language = language
        self._model = None

    def load(self) -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(self.model_name, device="cpu", compute_type=self.compute)

    def transcribe(self, audio: Frame) -> str:
        if self._model is None:
            raise RuntimeError("call load() first")
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        audio = np.ascontiguousarray(audio.flatten(), dtype=np.float32)
        segments, _info = self._model.transcribe(audio, language=self.language, beam_size=1)
        return "".join(seg.text for seg in segments).strip()
