"""Server-side STT for browser audio uploads (the ask-screen popup mic).

Deliberately NOT the jarvis FasterWhisperSTT engine: that ABC takes int16
numpy frames from the mic pipeline; browser uploads are webm/opus (or wav)
blobs, and faster-whisper's av backend decodes those straight from a
file-like. Lazy singleton — the model (~200-300MB) loads on the first /stt
call, then stays warm like the voice models do.
"""

from __future__ import annotations

import io
import logging
import threading

from .config import Config

log = logging.getLogger("dispatcher.stt")


class SpeechToText:
    def __init__(self, model: str = "small.en", compute: str = "int8",
                 language: str = "en"):
        self.model_name = model
        self.compute = compute
        self.language = language
        self._model = None
        self._lock = threading.Lock()  # serializes load AND transcribe (CPU-bound)

    def transcribe_bytes(self, data: bytes) -> str:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                log.info("loading STT model %s (%s)…", self.model_name, self.compute)
                self._model = WhisperModel(self.model_name, device="cpu",
                                           compute_type=self.compute)
            segments, _info = self._model.transcribe(io.BytesIO(data),
                                                     language=self.language)
            return " ".join(s.text.strip() for s in segments).strip()


_stt: SpeechToText | None = None


def get_stt(cfg: Config) -> SpeechToText:
    global _stt
    if _stt is None:
        s = cfg.stt or {}
        _stt = SpeechToText(s.get("model", "small.en"), s.get("compute", "int8"),
                            s.get("language", "en"))
    return _stt
