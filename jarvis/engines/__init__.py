"""Engine registry: one config line (`stt.engine: faster_whisper`) picks the
implementation. Modules import lazily so unused engines cost nothing.
"""

from __future__ import annotations

import importlib

_REGISTRY = {
    "stt": {
        "faster_whisper": ("jarvis.engines.stt_faster_whisper", "FasterWhisperSTT"),
    },
    "wake": {
        "openwakeword": ("jarvis.engines.wake_openwakeword", "OpenWakeWordEngine"),
    },
    "tts": {
        "piper": ("jarvis.engines.tts_piper", "PiperTTS"),
        # "kokoro": drop-in later per locked decision #6
    },
    "brain": {
        "dispatcher": ("jarvis.engines.brain_dispatcher", "DispatcherBrain"),
    },
}


def create(kind: str, name: str, **kwargs):
    try:
        module_name, cls_name = _REGISTRY[kind][name]
    except KeyError:
        raise ValueError(f"no {kind} engine named {name!r}; known: {list(_REGISTRY.get(kind, {}))}")
    module = importlib.import_module(module_name)
    return getattr(module, cls_name)(**kwargs)
