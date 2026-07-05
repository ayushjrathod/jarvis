# Notes summary (test)

Generated 2026-07-05 from `vault/notes/`.

## 2026-07-01 — Wayland audio

- PipeWire covers both PulseAudio and JACK clients; sounddevice picks up the
  default source at 16 kHz mono without issues.
- Mic level can drift after suspend — fix with
  `wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.9`.
- The /dev/uinput permission quirk recurred after a kernel update; needed
  `chmod g+rw /dev/uinput` again before ydotoold would start.

## 2026-07-03 — Project ideas

- Add a media widget (mpv + yt-dlp) to the dashboard eventually.
- Once the Gmail/Calendar MCP lands in Phase C, the weekly review agent could
  cross-reference open tasks with calendar entries.
- Try Kokoro-82M for TTS after the Piper pipeline is stable — the TTSEngine
  ABC should make it a one-line config swap.
