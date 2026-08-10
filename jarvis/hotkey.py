"""Raw evdev hotkey watcher (locked constraint: compositor hotkey APIs don't
expose keyup, breaking hold-to-talk). Adapted from the user's ptt_dictate.py.

Requires the user in the `input` group.
"""

from __future__ import annotations

import logging
import select
import threading

from evdev import InputDevice, ecodes, list_devices

log = logging.getLogger("jarvis.hotkey")


def find_keyboards() -> list[InputDevice]:
    keyboards = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        caps = dev.capabilities().get(ecodes.EV_KEY, [])
        if ecodes.KEY_A in caps and ecodes.KEY_SPACE in caps:
            keyboards.append(dev)
    return keyboards


class HotkeyWatcher(threading.Thread):
    """Calls on_press()/on_release() (from this thread) for one trigger key.

    Supervised: device loss (USB replug, BT keyboard sleep) drops that device
    and rescans; no-devices-at-start retries every 5s instead of dying. The
    thread only exits via stop() — a raised exception here would kill hotkey
    handling silently (and make mission-dictate exit 0, dodging its
    Restart=on-failure).

    That invariant is now enforced rather than assumed: run() wraps the watch
    loop, so anything it didn't catch itself (list_devices() → OSError on a
    wedged /dev/input, an evdev surprise) is logged and parked in `self.error`
    instead of vanishing with the thread. Callers still have to notice the
    thread died — see jarvis/dictate.py's join() and Jarvis._supervise_ptt.
    """

    RESCAN_S = 5.0

    def __init__(self, key_name: str, on_press, on_release):
        super().__init__(daemon=True, name="hotkey")
        self.code = ecodes.ecodes[key_name]
        self.on_press = on_press
        self.on_release = on_release
        self.error: BaseException | None = None
        self._stop = threading.Event()

    def run(self):
        try:
            self._watch()
        except BaseException as exc:  # noqa: BLE001 — a dead watcher must be visible
            # Before 2026-08-10 this killed only the thread: main.py discarded
            # the watcher, ptt_loop parked forever on an empty queue, and
            # `systemctl --user status mission-jarvis` still said
            # active (running). In --mode ptt the process became a no-op that
            # reported healthy.
            self.error = exc
            log.exception("hotkey watcher died; %s is no longer watched", self.code)

    def _watch(self):
        by_fd: dict[int, InputDevice] = {}
        # One fd per device that currently holds the key down. This was a
        # single bool shared across every watched device until 2026-08-10, so
        # a SECOND keyboard merely disappearing from select() while you held
        # PTT fired on_release and truncated the clip mid-sentence.
        pressed: set[int] = set()
        warned = False
        while not self._stop.is_set():
            if not by_fd:
                keyboards = find_keyboards()
                if not keyboards:
                    if not warned:
                        log.error(
                            "no readable keyboard devices — is the user in the `input` "
                            "group? (sudo usermod -aG input $USER, then re-login); "
                            "retrying every %ss", self.RESCAN_S,
                        )
                        warned = True
                    if self._stop.wait(self.RESCAN_S):
                        return
                    continue
                by_fd = {dev.fd: dev for dev in keyboards}
                warned = False
                log.info("watching %d keyboard(s) for %s", len(by_fd), self.code)
            try:
                ready, _, _ = select.select(by_fd, [], [], 0.5)
            except OSError:
                by_fd = {}  # some fd went stale; rescan
                continue
            for fd in ready:
                dev = by_fd.get(fd)
                try:
                    events = list(dev.read())
                except OSError:
                    log.warning("keyboard %s disappeared; dropping it", dev.path)
                    by_fd.pop(fd, None)
                    # only if THIS device was holding the key — never leave a
                    # recording stuck on, never truncate someone else's
                    self._release(fd, pressed)
                    continue
                for event in events:
                    if event.type == ecodes.EV_KEY and event.code == self.code:
                        if event.value == 1:
                            self._press(fd, pressed)
                        elif event.value == 0:
                            self._release(fd, pressed)

    def _press(self, fd: int, pressed: set[int]) -> None:
        """Key down on `fd`. on_press fires only for the first device to hold
        it, so two keyboards can't start two recordings."""
        if fd in pressed:
            return
        pressed.add(fd)
        if len(pressed) == 1:
            self._fire(self.on_press)

    def _release(self, fd: int, pressed: set[int]) -> None:
        """Key up on `fd`, or that device vanished. on_release fires only once
        no watched device still holds the key."""
        if fd not in pressed:
            return
        pressed.discard(fd)
        if not pressed:
            self._fire(self.on_release)

    def _fire(self, callback):
        try:
            callback()
        except Exception:
            log.exception("hotkey callback failed")

    def stop(self):
        self._stop.set()
