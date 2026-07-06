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
    """Calls on_press()/on_release() (from this thread) for one trigger key."""

    def __init__(self, key_name: str, on_press, on_release):
        super().__init__(daemon=True, name="hotkey")
        self.code = ecodes.ecodes[key_name]
        self.on_press = on_press
        self.on_release = on_release
        self._stop = threading.Event()

    def run(self):
        keyboards = find_keyboards()
        if not keyboards:
            raise RuntimeError(
                "no readable keyboard devices — is the user in the `input` group? "
                "(sudo usermod -aG input $USER, then re-login)"
            )
        log.info("watching %d keyboard(s) for %s", len(keyboards), self.code)
        by_fd = {dev.fd: dev for dev in keyboards}
        pressed = False
        while not self._stop.is_set():
            ready, _, _ = select.select(by_fd, [], [], 0.5)
            for fd in ready:
                for event in by_fd[fd].read():
                    if event.type == ecodes.EV_KEY and event.code == self.code:
                        if event.value == 1 and not pressed:
                            pressed = True
                            self.on_press()
                        elif event.value == 0 and pressed:
                            pressed = False
                            self.on_release()

    def stop(self):
        self._stop.set()
