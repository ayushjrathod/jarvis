"""Jarvis-side view of config.yaml (the `jarvis:` section + dispatcher URL)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent


@dataclass
class JarvisConfig:
    root: Path
    trigger_key: str = "KEY_F9"
    sample_rate: int = 16000
    wake_word: str = "hey_jarvis"
    wake_threshold: float = 0.5
    # rolling audio kept before wake-word detection fires, spliced onto the
    # front of the capture so the first word isn't clipped
    wake_prebuffer_ms: int = 400
    # acknowledgment beep when the wake word fires; 0 disables
    wake_beep_ms: int = 120
    # spoken wake acknowledgments (random pick, pre-synthesized at startup);
    # empty list falls back to the beep
    wake_ack_phrases: list = field(default_factory=lambda: ["Hmm?", "Yes?", "Mm-hmm?"])
    stt: dict = field(default_factory=lambda: {"engine": "faster_whisper", "model": "small.en", "compute": "int8"})
    tts: dict = field(default_factory=lambda: {"engine": "piper", "voice": "en_US-lessac-medium"})
    dispatcher_url: str = "http://127.0.0.1:8765"
    models_dir: Path = None
    # VAD endpointing: stop recording after this much trailing silence
    vad_threshold: float = 0.5
    endpoint_silence_ms: int = 900
    max_utterance_s: int = 30
    # barge-in: consecutive speech frames (32ms each) before cutting TTS
    barge_in_frames: int = 6
    # ask-about-my-screen popup (jarvis.ask_screen)
    ask_screen: dict = field(default_factory=lambda: {
        "shortcut": "<Super><Alt>a", "window_size": [520, 720],
        "keep_last": 20, "chromium_bin": "chromium"})

    @classmethod
    def load(cls, path: str | Path | None = None) -> "JarvisConfig":
        path = Path(path or os.environ.get("MC_CONFIG", ROOT / "config.yaml"))
        raw = yaml.safe_load(path.read_text())
        j = raw.get("jarvis", {}) or {}
        d = raw.get("dispatcher", {}) or {}
        root = path.parent.resolve()
        cfg = cls(root=root)
        cfg.trigger_key = j.get("trigger_key", cfg.trigger_key)
        cfg.sample_rate = j.get("sample_rate", cfg.sample_rate)
        cfg.wake_word = j.get("wake_word", cfg.wake_word)
        cfg.wake_threshold = j.get("wake_threshold", cfg.wake_threshold)
        cfg.wake_prebuffer_ms = j.get("wake_prebuffer_ms", cfg.wake_prebuffer_ms)
        cfg.wake_beep_ms = j.get("wake_beep_ms", cfg.wake_beep_ms)
        cfg.wake_ack_phrases = j.get("wake_ack_phrases", cfg.wake_ack_phrases)
        cfg.stt = {**cfg.stt, **(j.get("stt") or {})}
        cfg.tts = {**cfg.tts, **(j.get("tts") or {})}
        cfg.vad_threshold = j.get("vad_threshold", cfg.vad_threshold)
        cfg.endpoint_silence_ms = j.get("endpoint_silence_ms", cfg.endpoint_silence_ms)
        cfg.max_utterance_s = j.get("max_utterance_s", cfg.max_utterance_s)
        cfg.barge_in_frames = j.get("barge_in_frames", cfg.barge_in_frames)
        cfg.ask_screen = {**cfg.ask_screen, **(j.get("ask_screen") or {})}
        cfg.dispatcher_url = j.get(
            "dispatcher_url", f"http://{d.get('host', '127.0.0.1')}:{d.get('port', 8765)}"
        )
        cfg.models_dir = root / "data" / "models"
        cfg.models_dir.mkdir(parents=True, exist_ok=True)
        return cfg
