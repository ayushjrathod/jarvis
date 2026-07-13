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
    """

    RESCAN_S = 5.0

    def __init__(self, key_name: str, on_press, on_release):
        super().__init__(daemon=True, name="hotkey")
        self.code = ecodes.ecodes[key_name]
        self.on_press = on_press
        self.on_release = on_release
        self._stop = threading.Event()

    def run(self):
        by_fd: dict[int, InputDevice] = {}
        pressed = False
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
                    if pressed:
                        pressed = False
                        self._fire(self.on_release)  # never leave a recording stuck on
                    continue
                for event in events:
                    if event.type == ecodes.EV_KEY and event.code == self.code:
                        if event.value == 1 and not pressed:
                            pressed = True
                            self._fire(self.on_press)
                        elif event.value == 0 and pressed:
                            pressed = False
                            self._fire(self.on_release)

    def _fire(self, callback):
        try:
            callback()
        except Exception:
            log.exception("hotkey callback failed")

    def stop(self):
        self._stop.set()
