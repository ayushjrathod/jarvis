"""Jarvis main loop: PTT (hold `ptt_key`, default Right Ctrl) and/or wake word
("hey jarvis") → VAD
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

# How long speak_sentences waits for its barge-in watcher THREAD to finish
# before giving up on it (one mic-read timeout plus slack) — see _barge_monitor.
MONITOR_JOIN_S = 2.0
# Ceiling on notices queued behind an in-flight interaction. The list was
# unbounded: a burst of agentic completions during one long conversation would
# queue minutes of speech to be read out the moment the user stopped talking.
MAX_PENDING_NOTICES = 10


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
        # The barge monitor needs its own VAD: capture_after_wake (wake thread)
        # and _barge_monitor (playback thread) both reset()/prob() their VAD,
        # which read-modify-write internal state — a notice spoken during a
        # phase-2 capture would otherwise corrupt endpointing (M8). State is
        # ~65KB and construction is cheap, so a second instance is free.
        self.barge_vad = SileroVAD(cfg.models_dir / "silero_vad.onnx", cfg.vad_threshold)
        self.recorder = Recorder(cfg.sample_rate, max_seconds=cfg.max_recording_s)
        self.player: Player | None = None
        self.wake_acks: list[np.ndarray] = []

        self.loop: asyncio.AbstractEventLoop | None = None
        self.busy = asyncio.Lock()          # one interaction at a time
        # Set by the PTT key going down mid-interaction; separate from
        # player.interrupt because speak_sentences' resume() clears that flag
        # unconditionally (see _ptt_barge / handle_utterance).
        self.barge_request = threading.Event()
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
        self.barge_vad.load()
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
                # Race the next sentence against a barge-in: if the brain stream
                # stalls between sentences, a barge fired while parked on get()
                # must still wake us (H5) — otherwise `busy` is held until the
                # next sentence or the 300s read timeout.
                get_task = asyncio.ensure_future(sentence_queue.get())
                barge_task = asyncio.ensure_future(barge.wait())
                _, pending = await asyncio.wait(
                    {get_task, barge_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:  # cancel the loser cleanly (no destroyed-pending warns)
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                if barge.is_set():  # barge wins even if a sentence arrived same tick
                    interrupted = True
                    break
                sentence = get_task.result()
                if sentence is None:
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
            # …and we have to WAIT for it, which monitor.cancel() did not do:
            # cancelling only detaches the asyncio.to_thread future, while the
            # worker thread runs on and pushes one more frame through the
            # shared self.barge_vad (the stop check sits at the TOP of the
            # loop, before the blocking read). SileroVAD.prob is an unlocked
            # read-modify-write of _state/_context, so monitor N+1 — started
            # immediately when _flush_notices calls say() — would reset() the
            # VAD while N's thread was still inside is_speech(), interleaving
            # the state and yielding garbage probabilities on the first frames
            # of the next utterance: a spurious or a missed barge-in.
            # 2026-08-10.
            try:
                await asyncio.wait_for(monitor, MONITOR_JOIN_S)
            except asyncio.TimeoutError:
                log.warning("barge monitor still running after %.0fs; abandoning it",
                            MONITOR_JOIN_S)
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
            self.barge_vad.reset()
            with MicStream(self.cfg.sample_rate, FRAME_SAMPLES) as mic:
                while not stop.is_set() and not self.player.interrupt.is_set():
                    frame = mic.read(timeout=0.2)
                    if frame is None:
                        continue
                    if len(frame) == FRAME_SAMPLES and self.barge_vad.is_speech(frame):
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

    async def say(self, text: str) -> bool:
        """Speak one short line. Returns False if the user barged in — the
        caller has to act on that (see _flush_notices); discarding it meant a
        barge-in on notice 1 was immediately followed by notices 2 and 3."""
        q = asyncio.Queue()
        for s in [x for x in [sanitize(text)] if x]:
            q.put_nowait(s)
        q.put_nowait(None)
        return await self.speak_sentences(q)

    # -- one interaction ------------------------------------------------------

    async def handle_utterance(self, audio: np.ndarray):
        if len(audio) < self.cfg.sample_rate // 4:  # <250ms: key tap, ignore
            return
        async with self.busy:
            # `busy` is held from here, but speak_sentences (and its
            # resume(), which clears player.interrupt) doesn't exist until
            # after the blocking transcribe below. A PTT press in between —
            # the user rephrasing while whisper chews on a long clip — set
            # player.interrupt and had it wiped by that resume(), so the STALE
            # question was answered and spoken while the new one waited behind
            # it, with nothing logged. The request is tracked separately now:
            # cleared here as this interaction begins (a press aimed at the
            # PREVIOUS reply is already spent), honored right after transcribe.
            # 2026-08-10.
            self.barge_request.clear()
            text = await asyncio.to_thread(self.stt.transcribe, audio)
            if self.barge_request.is_set():
                log.info("barged in during transcription; dropping the superseded "
                         "utterance (%s)", text or "empty")
                return
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
            if not await self.say(self.pending_notices.pop(0)):
                # Barging in on notice 1 used to be followed instantly by
                # notices 2 and 3, because say()'s result was discarded. An
                # interruption means the user wants the floor, not the rest of
                # the backlog — drop it (every notice is also on the dashboard).
                dropped, self.pending_notices = len(self.pending_notices), []
                log.info("barge-in during a notice; dropped %d queued notice(s)", dropped)
                return

    # -- input sources ---------------------------------------------------------

    def _ptt_barge(self) -> bool:
        """PTT pressed while an interaction is in flight = explicit barge-in.

        Stops playback (speak_sentences then cancels the in-flight dispatcher
        call) so the new utterance isn't queued behind a long reply, AND
        records the request in `barge_request` — player.interrupt alone is
        erased by the next resume(), which is how a press during the STT
        window used to be swallowed. Kept out of the on_press closure so it is
        testable without a mic or a hotkey device.
        """
        if not self.busy.locked():
            return False
        self.barge_request.set()
        if self.player is not None:
            self.player.stop()
        return True

    def _busy_for_wake(self) -> bool:
        """Is the assistant occupied, as far as the wake word is concerned?

        `busy` alone was wrong: it is held only during handle_utterance, not
        while the PTT Recorder is capturing — and the wake MicStream listens on
        the same microphone. So in --mode both (what mission-jarvis ships)
        holding PTT and saying "hey jarvis, …" had capture_after_wake answer
        the utterance, and the PTT release answered the same audio again.
        2026-08-10.
        """
        return self.busy.locked() or self.recorder.is_recording()

    def start_ptt(self):
        """Hold-to-talk on `ptt_key`: press = listen, release = ask.

        Same downstream path as the wake word (STT → dispatcher → streamed TTS),
        minus the VAD endpointing — the key edge IS the endpoint.
        """
        def on_press():
            self._ptt_barge()
            if self.cfg.ptt_beep_ms > 0:
                play_beep_async(ms=self.cfg.ptt_beep_ms)
            self.recorder.start()

        def on_release():
            audio = self.recorder.stop()
            if self.cfg.ptt_beep_ms > 0:  # lower tone = "got it, thinking"
                play_beep_async(ms=self.cfg.ptt_beep_ms, freq=660.0)
            self.loop.call_soon_threadsafe(self.ptt_audio.put_nowait, audio)

        if self.cfg.ptt_key == self.cfg.trigger_key:
            log.warning(
                "ptt_key == trigger_key (%s): mission-dictate will type the same "
                "speech it answers — set a different jarvis.ptt_key in config.yaml",
                self.cfg.ptt_key,
            )
        watcher = HotkeyWatcher(self.cfg.ptt_key, on_press, on_release)
        watcher.start()
        log.info("PTT ready: hold %s to talk", self.cfg.ptt_key)
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
                is_busy=self._busy_for_wake, on_busy=on_busy,
                on_wake=on_wake,
                # mic.read uses a ~1s timeout, so seconds-of-silence ≈ None count
                none_limit=self.cfg.mic_lost_after_s,
            )

    async def notices_loop(self):
        async for ev in self.brain.notices():
            try:
                if self.busy.locked():
                    if len(self.pending_notices) >= MAX_PENDING_NOTICES:
                        # bounded on purpose: the newest completion is the one
                        # worth hearing, and the dashboard keeps them all
                        log.warning("notice backlog full; dropping the oldest")
                        self.pending_notices.pop(0)
                    self.pending_notices.append(ev.text)
                else:
                    async with self.busy:
                        await self.say(ev.text)
            except Exception:
                log.exception("failed to speak notice; dropping it")

    # -- entry ------------------------------------------------------------------

    async def _supervise_ptt(self, watcher: HotkeyWatcher):
        """Fail the process if the hotkey thread stops.

        start_ptt()'s return value used to be discarded (compare dictate.py,
        which join()s the watcher and sys.exit()s precisely so a dead one trips
        Restart=on-failure). A watcher that died left ptt_loop parked forever
        on an empty queue while `systemctl --user status mission-jarvis` still
        reported active (running) — in --mode ptt, a no-op that looks healthy.
        join() blocks in a worker thread, so this task simply never completes
        while PTT is alive; it costs one default-executor thread for the life
        of the process, which is cheap next to a silently dead hotkey.
        2026-08-10.
        """
        await asyncio.to_thread(watcher.join)
        raise RuntimeError(
            f"PTT hotkey watcher exited ({watcher.error or 'no error recorded'}); "
            "exiting so systemd restarts us"
        )

    async def run(self):
        self.loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(self.notices_loop())]
        if self.mode in ("ptt", "both"):
            watcher = self.start_ptt()
            tasks.append(asyncio.create_task(self.ptt_loop()))
            tasks.append(asyncio.create_task(self._supervise_ptt(watcher)))
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
