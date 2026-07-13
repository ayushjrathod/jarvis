"""WakeWordEngine on openWakeWord (ONNX, stock hey_jarvis model).

Written against openwakeword 0.4.0 (newer releases hard-require tflite-runtime,
which has no Python 3.14 wheels). 0.4.0 bundles the pretrained ONNX models in
the package, so there is nothing to download, and onnxruntime is the only
inference backend. Feed int16 frames; scores update as internal buffers fill —
~80ms (1280-sample) frames are the sweet spot but any size works.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from jarvis.plugins.base import Frame, WakeWordEngine

log = logging.getLogger("jarvis.wake")

FRAME_SAMPLES = 1280


class OpenWakeWordEngine(WakeWordEngine):
    def __init__(self, model: str = "hey_jarvis", threshold: float = 0.5):
        self.model_name = model
        self.threshold = threshold
        self._model = None
        self._key = None
        self._last_miss_log = 0.0

    def load(self) -> None:
        from openwakeword import get_pretrained_model_paths
        from openwakeword.model import Model

        path = self.model_name
        if not Path(path).exists():
            matches = [
                p for p in get_pretrained_model_paths()
                if Path(p).stem.startswith(self.model_name)
            ]
            if not matches:
                raise FileNotFoundError(
                    f"no pretrained wake model matching {self.model_name!r}; "
                    f"available: {[Path(p).stem for p in get_pretrained_model_paths()]}"
                )
            path = matches[0]
        self._model = Model(wakeword_model_paths=[path])
        self._key = next(iter(self._model.models))
        log.info("wake model %s loaded (key=%s)", Path(path).name, self._key)

    def process(self, frame: Frame) -> bool:
        score = self._model.predict(frame)[self._key]
        if self.threshold * 0.5 <= score < self.threshold:
            # near-miss: the phrase registered but didn't clear the bar.
            # Logged (rate-limited) so wake_threshold can be tuned from data.
            # Band scales with the threshold — a fixed floor stops producing
            # tuning data once the threshold is lowered to meet it.
            now = time.monotonic()
            if now - self._last_miss_log > 1.0:
                self._last_miss_log = now
                log.info("wake near-miss: score %.2f < threshold %.2f", score, self.threshold)
        return score >= self.threshold

    def reset(self) -> None:
        # 0.4.0's reset() clears the prediction buffer but NOT the mel-spec
        # feature buffer — stale hot audio would re-fire on the next frames.
        # Flushing 2s of zeros through predict() scrubs both, version-proof.
        import numpy as np

        self._model.reset()
        zeros = np.zeros(FRAME_SAMPLES, dtype=np.int16)
        for _ in range(25):
            self._model.predict(zeros)
        self._model.reset()
