"""Silero VAD run directly on onnxruntime — a fixed utility, deliberately not
a plugin ABC (the spec names exactly four plugin points, and barge-in timing
depends on this staying predictable).

Model: silero_vad.onnx from https://github.com/snakers4/silero-vad (MIT
license, (c) Silero Team) — downloaded by scripts/setup_voice.sh; the ~2MB
ONNX file avoids the 2GB torch dependency the pip package would pull in.

Frames: 512 samples (32ms) @ 16kHz, int16 or float32.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

FRAME_SAMPLES = 512
# silero v5 expects each 512-sample chunk prepended with the previous chunk's
# last 64 samples (context) — feeding a bare 512 yields near-zero probs.
CONTEXT_SAMPLES = 64
DOWNLOAD_URL = (
    "https://github.com/snakers4/silero-vad/raw/master/"
    "src/silero_vad/data/silero_vad.onnx"
)


class SileroVAD:
    def __init__(self, model_path: str | Path, threshold: float = 0.5, sample_rate: int = 16000):
        self.model_path = Path(model_path)
        self.threshold = threshold
        self.sample_rate = sample_rate
        self._session = None
        self._state = None

    def load(self) -> None:
        import onnxruntime as ort

        if not self.model_path.exists():
            raise FileNotFoundError(
                f"silero VAD model missing: {self.model_path}\n"
                f"run scripts/setup_voice.sh (downloads from {DOWNLOAD_URL})"
            )
        self._session = ort.InferenceSession(
            str(self.model_path), providers=["CPUExecutionProvider"]
        )
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        if frame.dtype == np.int16:
            frame = frame.astype(np.float32) / 32768.0
        frame = frame.flatten().astype(np.float32)
        x = np.concatenate([self._context, frame])[np.newaxis, :]
        self._context = frame[-CONTEXT_SAMPLES:]
        out, self._state = self._session.run(
            None,
            {"input": x, "state": self._state, "sr": np.array(self.sample_rate, dtype=np.int64)},
        )
        return float(out[0][0])

    def is_speech(self, frame: np.ndarray) -> bool:
        return self.prob(frame) >= self.threshold
