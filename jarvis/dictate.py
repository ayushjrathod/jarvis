"""Standalone dictation mode: hold F9 → transcribe → type at cursor via
ydotool. The original ptt_dictate.py behavior, rebuilt on the engine ABCs
(models shared with the assistant, loaded once).

Run: .venv/bin/python -m jarvis.dictate
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading

from jarvis import engines
from jarvis.audio import Recorder
from jarvis.config import JarvisConfig
from jarvis.hotkey import HotkeyWatcher

log = logging.getLogger("jarvis.dictate")


def type_text(text: str):
    try:
        res = subprocess.run(["ydotool", "type", "--", text])
    except FileNotFoundError:
        log.error("ydotool not installed — sudo pacman -S ydotool")
        return
    if res.returncode != 0:
        log.error("ydotool exited %d — is ydotoold up? (systemctl --user status ydotool)",
                  res.returncode)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = JarvisConfig.load()
    stt = engines.create(
        "stt", cfg.stt.get("engine", "faster_whisper"),
        model=cfg.stt.get("model", "small.en"), compute=cfg.stt.get("compute", "int8"),
    )
    log.info("loading STT model…")
    stt.load()
    recorder = Recorder(cfg.sample_rate)

    def on_release():
        audio = recorder.stop()
        if len(audio) < cfg.sample_rate // 4:
            return

        def work():
            text = stt.transcribe(audio)
            if text:
                log.info("typing: %s", text)
                type_text(text)
            else:
                log.info("empty transcription")

        threading.Thread(target=work, daemon=True).start()

    watcher = HotkeyWatcher(cfg.trigger_key, recorder.start, on_release)
    watcher.start()
    log.info("dictation ready — hold %s and speak; ctrl-c to quit", cfg.trigger_key)
    try:
        watcher.join()
    except KeyboardInterrupt:
        print("bye")
    else:
        # the watcher only returns via stop(); anything else is a crash — exit
        # nonzero so systemd's Restart=on-failure actually fires
        sys.exit("hotkey watcher exited unexpectedly")


if __name__ == "__main__":
    main()
