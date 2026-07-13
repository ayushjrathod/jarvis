"""Jarvis main loop: PTT (hold F9) and/or wake word ("hey jarvis") → VAD
endpointing → STT → dispatcher Brain → sentence-streamed TTS with barge-in.

State per interaction: capture → transcribe → submit → speak-as-it-streams.
Barge-in (locked decision #7): while Jarvis speaks, Silero VAD watches the mic;
`barge_in_frames` consecutive 32ms speech frames (default 6 ≈ 192ms) stop
playback AND cancel the in-flight dispatcher call.

Run: .venv/bin/python -m jarvis.main [--mode ptt|wake|both]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
import threading

import httpx
import numpy as np

from jarvis import engines
from jarvis.audio import MicStream, Player, Recorder, play_async, play_beep_async
from jarvis.config import JarvisConfig
from jarvis.hotkey import HotkeyWatcher
from jarvis.sanitize import SentenceChunker, sanitize
from jarvis.vad import FRAME_SAMPLES, SileroVAD
from jarvis.wake_capture import capture_after_wake

log = logging.getLogger("jarvis")


class Jarvis:
    def __init__(self, cfg: JarvisConfig, mode: str = "both"):
        self.cfg = cfg
        self.mode = mode
        self.stt = engines.create(
            "stt", cfg.stt.get("engine", "faster_whisper"),
            model=cfg.stt.get("model", "small.en"), compute=cfg.stt.get("compute", "int8"),
        )
        self.tts = engines.create(
            "tts", cfg.tts.get("engine", "piper"),
            voice=cfg.tts.get("voice", "en_US-lessac-medium"),
            models_dir=cfg.models_dir / "piper",
        )
        self.brain = engines.create("brain", "dispatcher", base_url=cfg.dispatcher_url)
        self.wake = None
        if mode in ("wake", "both"):
            self.wake = engines.create(
                "wake", "openwakeword",
                model=cfg.wake_word, threshold=cfg.wake_threshold,
            )
        self.vad = SileroVAD(cfg.models_dir / "silero_vad.onnx", cfg.vad_threshold)
        self.recorder = Recorder(cfg.sample_rate)
        self.player: Player | None = None
        self.wake_acks: list[np.ndarray] = []

        self.loop: asyncio.AbstractEventLoop | None = None
        self.busy = asyncio.Lock()          # one interaction at a time
        self.ptt_audio: asyncio.Queue = asyncio.Queue()
        self.pending_notices: list[str] = []

    # -- startup ------------------------------------------------------------

    def load_models(self):
        log.info("loading STT (%s)…", self.cfg.stt.get("model"))
        self.stt.load()
        log.info("loading TTS (%s)…", self.cfg.tts.get("voice"))
        self.tts.load()
        self.player = Player(self.tts.sample_rate)
        # pre-synthesize wake acknowledgments so on_wake can play one instantly
        self.wake_acks = []
        for phrase in self.cfg.wake_ack_phrases:
            chunks = list(self.tts.synthesize(phrase))
            if chunks:
                self.wake_acks.append(np.concatenate(chunks))
        log.info("loading VAD…")
        self.vad.load()
        if self.wake:
            log.info("loading wake word (%s)…", self.cfg.wake_word)
            self.wake.load()
        log.info("all models warm")

    # -- speaking with barge-in ----------------------------------------------

    async def speak_sentences(self, sentence_queue: asyncio.Queue) -> bool:
        """Consume sentences until None sentinel; True if finished unbarged.
        Playback/TTS errors (sink vanished mid-reply) drop the utterance and
        return False — they must never propagate and kill the main loop."""
        self.player.resume()
        barge = asyncio.Event()
        monitor_stop = threading.Event()
        monitor = asyncio.create_task(self._barge_monitor(barge, monitor_stop))
        interrupted = False
        try:
            while True:
                sentence = await sentence_queue.get()
                if sentence is None:
                    break
                if barge.is_set():
                    interrupted = True
                    break
                try:
                    ok = await asyncio.to_thread(
                        self.player.play, self.tts.synthesize(sentence)
                    )
                except Exception:
                    log.exception("playback failed (audio device gone?); dropping reply")
                    interrupted = True
                    break
                if not ok:
                    interrupted = True
                    break
        finally:
            monitor_stop.set()  # watcher thread exits within one mic-read timeout
            monitor.cancel()
        if interrupted:
            await self.brain.cancel()
        return not interrupted

    async def _barge_monitor(self, barge: asyncio.Event, stop: threading.Event):
        """VAD on the mic while TTS plays; sets `barge` and stops the player.

        Known limitation (documented): without echo cancellation the mic hears
        Jarvis itself. Run PipeWire's echo-cancel module or use headphones.

        `stop` is this monitor's own exit flag. The thread must not key off
        player.interrupt alone: the next utterance's resume() clears that flag,
        which would leave a stale thread (and its late player.stop()) racing
        the new playback.
        """
        def watch():
            consecutive = 0
            self.vad.reset()
            with MicStream(self.cfg.sample_rate, FRAME_SAMPLES) as mic:
                while not stop.is_set() and not self.player.interrupt.is_set():
                    frame = mic.read(timeout=0.2)
                    if frame is None:
                        continue
                    if len(frame) == FRAME_SAMPLES and self.vad.is_speech(frame):
                        consecutive += 1
                        if consecutive >= self.cfg.barge_in_frames:
                            return True
                    else:
                        consecutive = 0
            return False

        try:
            if await asyncio.to_thread(watch):
                log.info("barge-in detected")
                barge.set()
                self.player.stop()
        except Exception:
            # mic unavailable: this utterance plays without barge-in, that's all
            log.warning("barge-in monitor failed; playback continues", exc_info=True)

    async def say(self, text: str):
        q = asyncio.Queue()
        for s in [x for x in [sanitize(text)] if x]:
            q.put_nowait(s)
        q.put_nowait(None)
        await self.speak_sentences(q)

    # -- one interaction ------------------------------------------------------

    async def handle_utterance(self, audio: np.ndarray):
        if len(audio) < self.cfg.sample_rate // 4:  # <250ms: key tap, ignore
            return
        async with self.busy:
            text = await asyncio.to_thread(self.stt.transcribe, audio)
            if not text:
                log.info("empty transcription")
                return
            log.info("heard: %s", text)

            chunker = SentenceChunker()
            q: asyncio.Queue = asyncio.Queue()
            speaker = asyncio.create_task(self.speak_sentences(q))
            try:
                async for ev in self.brain.submit(text):
                    if ev.type in ("ack", "error"):
                        for s in chunker.feed(ev.text + " "):
                            q.put_nowait(s)
                    elif ev.type == "delta":
                        for s in chunker.feed(ev.text):
                            q.put_nowait(s)
                for s in chunker.flush():
                    q.put_nowait(s)
            except (httpx.HTTPError, httpx.StreamError) as e:
                # Expected when a barge-in cancels the task server-side and the
                # SSE stream dies under us; never let one interaction kill the
                # loop. StreamError is NOT an HTTPError (it's a RuntimeError):
                # it's what iterating the response raises when cancel() closed
                # it between reads rather than during one.
                log.warning("brain stream dropped (%s)", e)
            finally:
                q.put_nowait(None)
                await speaker
            await self._flush_notices()

    async def _flush_notices(self):
        while self.pending_notices:
            await self.say(self.pending_notices.pop(0))

    # -- input sources ---------------------------------------------------------

    def start_ptt(self):
        def on_press():
            self.recorder.start()

        def on_release():
            audio = self.recorder.stop()
            self.loop.call_soon_threadsafe(self.ptt_audio.put_nowait, audio)

        watcher = HotkeyWatcher(self.cfg.trigger_key, on_press, on_release)
        watcher.start()
        log.info("PTT ready: hold %s to talk", self.cfg.trigger_key)
        return watcher

    async def _handle_safely(self, audio: np.ndarray):
        """One interaction, fully contained: STT/dispatcher/audio failures are
        logged and dropped — a single bad exchange must not kill the service."""
        try:
            await self.handle_utterance(audio)
        except Exception:
            log.exception("interaction failed; recovering")

    async def ptt_loop(self):
        while True:
            audio = await self.ptt_audio.get()
            await self._handle_safely(audio)

    async def wake_loop(self):
        """Wake word → VAD-endpointed capture → same handler as PTT."""
        log.info("wake word ready: say '%s'", self.cfg.wake_word.replace("_", " "))
        while True:
            try:
                audio = await asyncio.to_thread(self._wake_capture_once)
            except Exception:
                # mic device lost (BT dropout, replug): keep retrying until it's back
                log.exception("wake capture failed; retrying in 5s")
                await asyncio.sleep(5)
                continue
            if audio is not None:
                await self._handle_safely(audio)

    def _wake_capture_once(self) -> np.ndarray | None:
        sr = self.cfg.sample_rate
        prebuffer_frames = max(0, self.cfg.wake_prebuffer_ms // 32)
        with MicStream(sr, FRAME_SAMPLES) as mic:
            def on_busy():  # don't trigger while speaking/thinking
                mic.drain()
                threading.Event().wait(0.2)

            def on_wake():
                log.info("wake word detected, listening…")
                if self.wake_acks:
                    play_async(random.choice(self.wake_acks), self.tts.sample_rate)
                elif self.cfg.wake_beep_ms > 0:
                    play_beep_async(ms=self.cfg.wake_beep_ms)

            return capture_after_wake(
                mic.read, self.wake, self.vad, self.cfg,
                prebuffer_frames=prebuffer_frames,
                is_busy=self.busy.locked, on_busy=on_busy,
                on_wake=on_wake,
            )

    async def notices_loop(self):
        async for ev in self.brain.notices():
            try:
                if self.busy.locked():
                    self.pending_notices.append(ev.text)
                else:
                    async with self.busy:
                        await self.say(ev.text)
            except Exception:
                log.exception("failed to speak notice; dropping it")

    # -- entry ------------------------------------------------------------------

    async def run(self):
        self.loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(self.notices_loop())]
        if self.mode in ("ptt", "both"):
            self.start_ptt()
            tasks.append(asyncio.create_task(self.ptt_loop()))
        if self.mode in ("wake", "both"):
            tasks.append(asyncio.create_task(self.wake_loop()))
        log.info("jarvis is up (mode=%s, dispatcher=%s)", self.mode, self.cfg.dispatcher_url)
        await asyncio.gather(*tasks)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Jarvis voice assistant")
    ap.add_argument("--mode", choices=["ptt", "wake", "both"], default="both")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = JarvisConfig.load(args.config)
    app = Jarvis(cfg, mode=args.mode)
    app.load_models()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("bye")
        sys.exit(0)


if __name__ == "__main__":
    main()
