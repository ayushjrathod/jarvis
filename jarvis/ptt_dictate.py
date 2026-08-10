#!/usr/bin/env python3
"""
Push-to-talk local dictation, Wayland-friendly.

Hold TRIGGER_KEY -> records mic audio.
Release it       -> transcribes locally with faster-whisper, types the
                    result into whatever window currently has focus
                    (via ydotool, so it works regardless of compositor).

Run it, then just hold the key in any text field.

NOTE (mission-control): this is the original standalone script, kept working
as the standalone dictation mode. In Phase B its record+transcribe logic gets
refactored into jarvis/engines/ behind the STTEngine ABC; this file then
becomes a thin client of those engines. Do not add features here.
"""

import sys
import threading
import subprocess
import select
import numpy as np
import sounddevice as sd
from evdev import InputDevice, ecodes, list_devices

from jarvis.sanitize import sanitize_for_injection

# ---------------- Config you'll likely want to tweak ----------------
TRIGGER_KEY = ecodes.KEY_F9        # the held-down hotkey
SAMPLE_RATE = 16000
MODEL_SIZE = "small.en"            # tiny.en (fastest) / base.en / small.en / medium.en (most accurate)
COMPUTE_TYPE = "int8"              # int8 is the fast option on CPU
LANGUAGE = "en"                    # set to None for auto-detect / multilingual
# ----------------------------------------------------------------------

# Refuse to RUN while staying readable as the reference CLAUDE.md keeps it for
# (jarvis/hotkey.py's evdev loop is descended from main() below). Everything
# here predates the fixes in jarvis/dictate.py and would re-introduce them: a
# thread per key-release runs concurrent WhisperModel.transcribe calls and
# interleaves `ydotool type` output, `frames` is read while the PortAudio
# callback appends to it, and select()/read() are unguarded so one unplugged
# keyboard kills the loop. The guard sits above the model load on purpose —
# below it, refusing would still cost a whisper download/warm first.
# 2026-08-10.
if __name__ == "__main__":
    sys.exit(
        "jarvis/ptt_dictate.py is superseded by jarvis.dictate — run "
        "`.venv/bin/python -m jarvis.dictate` instead. This file is kept "
        "read-only as the reference for evdev hotkey capture (CLAUDE.md)."
    )

print(f"Loading faster-whisper model '{MODEL_SIZE}' (first run downloads it)...")
from faster_whisper import WhisperModel
model = WhisperModel(MODEL_SIZE, device="cpu", compute_type=COMPUTE_TYPE)
print("Model loaded. Ready — hold the trigger key to dictate.")

recording = False
frames = []
stream = None


def audio_callback(indata, frame_count, time_info, status):
    if recording:
        frames.append(indata.copy())


def start_recording():
    global recording, frames, stream
    frames = []
    recording = True
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=audio_callback
    )
    stream.start()
    print("[recording...]")


def stop_recording_and_transcribe():
    global recording, stream
    recording = False
    if stream:
        stream.stop()
        stream.close()
        stream = None
    if not frames:
        print("[no audio captured]")
        return
    audio = np.concatenate(frames, axis=0).flatten()
    duration = len(audio) / SAMPLE_RATE
    print(f"[transcribing {duration:.1f}s...]")
    segments, _ = model.transcribe(audio, language=LANGUAGE, beam_size=1)
    text = "".join(seg.text for seg in segments).strip()
    if text:
        print(f"[typing]: {text}")
        type_text(text)
    else:
        print("[empty transcription]")


def type_text(text):
    # Strip control chars before injection so a hallucinated newline can't
    # become an Enter keypress in the focused window (L9; shared helper).
    text = sanitize_for_injection(text)
    if not text:
        return
    subprocess.run(["ydotool", "type", "--", text])


def find_keyboard_devices():
    keyboards = []
    for path in list_devices():
        dev = InputDevice(path)
        caps = dev.capabilities().get(ecodes.EV_KEY, [])
        if ecodes.KEY_A in caps and ecodes.KEY_SPACE in caps:
            keyboards.append(dev)
    return keyboards


def main():
    keyboards = find_keyboard_devices()
    if not keyboards:
        print("No keyboard-like input devices found/readable.")
        print("Check that your user is in the 'input' group (and re-logged in).")
        sys.exit(1)

    print("Watching devices:")
    for kb in keyboards:
        print(f"  {kb.path}  {kb.name}")
    print(f"Trigger keycode: {TRIGGER_KEY} (default F9). Edit TRIGGER_KEY to change.")

    devices_by_fd = {dev.fd: dev for dev in keyboards}

    while True:
        ready, _, _ = select.select(devices_by_fd, [], [])
        for fd in ready:
            dev = devices_by_fd[fd]
            for event in dev.read():
                if event.type == ecodes.EV_KEY and event.code == TRIGGER_KEY:
                    if event.value == 1 and not recording:      # key down
                        start_recording()
                    elif event.value == 0 and recording:        # key up
                        threading.Thread(target=stop_recording_and_transcribe).start()


# Unreachable — the guard near the top of the file exits first. Left in place
# so the script still reads end-to-end as the original.
if __name__ == "__main__":
    main()
