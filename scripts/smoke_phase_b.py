#!/usr/bin/env python3
"""Phase B acceptance smoke — headless (no mic/speaker needed).

Closes the loop synthetically: Piper TTS *generates* the speech that the wake
word, VAD, and STT stages must then recognize, and the Brain talks to a live
dispatcher. Covers every pipeline stage except physical audio I/O and the F9
key (hardware; user-tested).

Needs the dispatcher running. Run: .venv/bin/python scripts/smoke_phase_b.py
"""

import asyncio
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from jarvis import engines
from jarvis.config import JarvisConfig
from jarvis.sanitize import SentenceChunker
from jarvis.vad import FRAME_SAMPLES, SileroVAD

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" ({detail})" if detail else ""))
    PASS, FAIL = PASS + (1 if ok else 0), FAIL + (0 if ok else 1)


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    n = int(len(audio) * dst / src)
    x = np.linspace(0, len(audio) - 1, n)
    return np.interp(x, np.arange(len(audio)), audio.astype(np.float32)).astype(np.int16)


def tts_say(tts, text: str) -> np.ndarray:
    return np.concatenate(list(tts.synthesize(text)))


def main():
    cfg = JarvisConfig.load()

    print("== loading engines (once, warm) ==")
    t0 = time.time()
    tts = engines.create("tts", "piper", voice=cfg.tts["voice"], models_dir=cfg.models_dir / "piper")
    tts.load()
    stt = engines.create("stt", "faster_whisper", model=cfg.stt["model"], compute=cfg.stt["compute"])
    stt.load()
    wake = engines.create("wake", "openwakeword", model=cfg.wake_word, threshold=cfg.wake_threshold)
    wake.load()
    vad = SileroVAD(cfg.models_dir / "silero_vad.onnx", cfg.vad_threshold)
    vad.load()
    print(f"all models loaded in {time.time() - t0:.1f}s")

    # -- TTS -> STT roundtrip -------------------------------------------------
    phrase = "What is the capital of France?"
    audio_tts = tts_say(tts, phrase)
    check("TTS synthesizes audio", len(audio_tts) > tts.sample_rate // 2,
          f"{len(audio_tts)/tts.sample_rate:.1f}s @ {tts.sample_rate}Hz")
    audio_16k = resample(audio_tts, tts.sample_rate, cfg.sample_rate)
    t0 = time.time()
    text = stt.transcribe(audio_16k)
    check("STT transcribes TTS output", "capital of france" in text.lower(),
          f"{text!r} in {time.time() - t0:.1f}s")

    # -- wake word on synthesized "hey jarvis" --------------------------------
    wake_audio = resample(tts_say(tts, "Hey Jarvis!"), tts.sample_rate, cfg.sample_rate)
    pad = np.zeros(cfg.sample_rate // 2, dtype=np.int16)
    wake_audio = np.concatenate([pad, wake_audio, pad])
    detected = False
    for i in range(0, len(wake_audio) - 1280, 1280):
        if wake.process(wake_audio[i:i + 1280]):
            detected = True
            break
    check("wake word fires on synthesized 'hey jarvis'", detected)
    wake.reset()
    silence = np.zeros(cfg.sample_rate * 3, dtype=np.int16)
    false_fire = any(
        wake.process(silence[i:i + 1280]) for i in range(0, len(silence) - 1280, 1280)
    )
    check("wake word silent on silence", not false_fire)

    # -- VAD: speech vs silence ------------------------------------------------
    vad.reset()
    speech_frames = sum(
        vad.is_speech(audio_16k[i:i + FRAME_SAMPLES])
        for i in range(0, len(audio_16k) - FRAME_SAMPLES, FRAME_SAMPLES)
    )
    total = len(audio_16k) // FRAME_SAMPLES
    check("VAD detects speech in TTS audio", speech_frames > total * 0.4,
          f"{speech_frames}/{total} frames")
    vad.reset()
    quiet = sum(
        vad.is_speech(silence[i:i + FRAME_SAMPLES])
        for i in range(0, cfg.sample_rate * 2, FRAME_SAMPLES)
    )
    check("VAD quiet on silence", quiet == 0, f"{quiet} false frames")

    # barge-in latency budget: barge_in_frames consecutive 32ms frames + VAD compute
    vad.reset()
    frame = audio_16k[: FRAME_SAMPLES]
    t0 = time.time()
    for _ in range(cfg.barge_in_frames):
        vad.is_speech(frame)
    compute_ms = (time.time() - t0) * 1000
    budget_ms = cfg.barge_in_frames * 32 + compute_ms
    check("barge-in budget < 200ms", budget_ms < 200, f"{budget_ms:.0f}ms")

    # -- Brain against the live dispatcher --------------------------------------
    async def brain_roundtrip():
        brain = engines.create("brain", "dispatcher", base_url=cfg.dispatcher_url)
        chunker = SentenceChunker()
        sentences, first_delta_s = [], None
        t0 = time.time()
        async for ev in brain.submit("what is the capital of France?"):
            if ev.type == "delta":
                if first_delta_s is None:
                    first_delta_s = time.time() - t0
                sentences.extend(chunker.feed(ev.text))
        sentences.extend(chunker.flush())
        await brain.aclose()
        return sentences, first_delta_s

    try:
        sentences, first_delta_s = asyncio.run(brain_roundtrip())
        joined = " ".join(sentences)
        check("brain quick answer mentions Paris", "paris" in joined.lower(), joined[:80])
        check("first delta timing recorded", first_delta_s is not None,
              f"{first_delta_s:.1f}s (CLI path; Messages API path will be faster)")
        if sentences:
            spoken = tts_say(tts, sentences[0])
            check("answer synthesizes to audio", len(spoken) > 0,
                  f"{len(spoken)/tts.sample_rate:.1f}s")
    except Exception as e:
        check("brain roundtrip", False, f"{type(e).__name__}: {e} — is the dispatcher running?")

    print(f"\n== {PASS} passed, {FAIL} failed ==")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
