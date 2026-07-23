"""Ask-about-my-screen: portal area screenshot → chromium popup on /ask.

Launched by a GNOME custom shortcut (installed by scripts/setup_ask_screen.sh):
    .venv/bin/python -m jarvis.ask_screen

Flow: dispatcher health check → xdg-desktop-portal Screenshot(interactive)
via jeepney (pure-Python D-Bus; approved dep) → move the PNG into
data/screenshots/ + prune → open a chromium --app window at /ask?shot=<id>.
A tap-triggered flow, so no evdev and no systemd service; portal cancel is a
silent exit. grim doesn't work on this GNOME Wayland — the portal does.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from jarvis.config import JarvisConfig

log = logging.getLogger("jarvis.ask_screen")

PORTAL_TIMEOUT_S = 300  # area selection is interactive; give the user time


def fail(msg: str):
    """Shortcut runs have no terminal: surface the error as a dialog too."""
    log.error(msg)
    if shutil.which("zenity"):
        subprocess.run(["zenity", "--error", "--title", "Ask screen",
                        "--text", msg], check=False)
    sys.exit(1)


def dispatcher_up(url: str) -> bool:
    try:
        httpx.get(f"{url}/health", timeout=3).raise_for_status()
        return True
    except Exception:
        return False


def _variant(value):
    """jeepney represents a{sv} values as (signature, value) pairs."""
    return value[1] if isinstance(value, tuple) and len(value) == 2 else value


def take_screenshot() -> str | None:
    """xdg-desktop-portal Screenshot(interactive=true, modal=true).
    Returns the file:// URI, or None when the user cancelled the selection.

    The Response signal arrives on a Request object whose path is predictable
    (sender + handle_token), so we subscribe BEFORE calling Screenshot — no
    race. Pre-0.9 portals return a different request path in the reply; then
    we re-subscribe on the actual path (tiny race, accepted)."""
    from jeepney import DBusAddress, MatchRule, new_method_call
    from jeepney.bus_messages import message_bus
    from jeepney.io.blocking import Proxy, open_dbus_connection

    conn = open_dbus_connection(bus="SESSION")
    try:
        token = f"missionask{int(time.time())}"
        sender = conn.unique_name.lstrip(":").replace(".", "_")
        predicted = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        def rule_for(path):
            return MatchRule(type="signal", path=path, member="Response",
                             interface="org.freedesktop.portal.Request")

        bus = Proxy(message_bus, conn)
        rule = rule_for(predicted)
        bus.AddMatch(rule)
        with conn.filter(rule) as queue:
            portal = DBusAddress("/org/freedesktop/portal/desktop",
                                 bus_name="org.freedesktop.portal.Desktop",
                                 interface="org.freedesktop.portal.Screenshot")
            reply = conn.send_and_get_reply(new_method_call(
                portal, "Screenshot", "sa{sv}",
                ("", {"handle_token": ("s", token),
                      "interactive": ("b", True),
                      "modal": ("b", True)})))
            request_path = reply.body[0]
            if request_path != predicted:
                alt = rule_for(request_path)
                bus.AddMatch(alt)
                with conn.filter(alt) as alt_queue:
                    signal = conn.recv_until_filtered(alt_queue,
                                                      timeout=PORTAL_TIMEOUT_S)
            else:
                signal = conn.recv_until_filtered(queue, timeout=PORTAL_TIMEOUT_S)
    finally:
        conn.close()

    code, results = signal.body
    if code == 1:
        return None  # user cancelled the selection
    if code != 0:
        raise RuntimeError(f"portal screenshot failed (response code {code})")
    uri = _variant(results.get("uri"))
    if not uri:
        raise RuntimeError("portal response carried no uri")
    return uri


def store(uri: str, screenshots_dir: Path) -> Path:
    """Move the portal's PNG (usually dropped in ~/Pictures) into our dir."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise RuntimeError(f"unexpected screenshot uri scheme: {uri!r}")
    src = Path(unquote(parsed.path))
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = screenshots_dir / f"{stamp}.png"
    if dest.exists():
        dest = screenshots_dir / f"{stamp}-2.png"
    shutil.move(src, dest)
    return dest


def prune(screenshots_dir: Path, keep: int):
    if keep <= 0:
        return
    pngs = sorted(screenshots_dir.glob("*.png"))  # stamp names sort by age
    for p in pngs[:-keep]:
        try:
            p.unlink()
        except OSError:
            pass


def launch_popup(cfg: JarvisConfig, shot_id: str):
    ask = cfg.ask_screen
    w, h = ask.get("window_size", [520, 720])
    url = f"{cfg.dispatcher_url}/ask?shot={shot_id}"
    subprocess.Popen(
        [ask.get("chromium_bin", "chromium"), f"--app={url}",
         f"--window-size={w},{h}"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("popup launched: %s", url)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = JarvisConfig.load()
    if not dispatcher_up(cfg.dispatcher_url):
        fail("Mission-control dispatcher is not running — start "
             "mission-dispatcher first.")
    try:
        uri = take_screenshot()
    except Exception as e:
        fail(f"Screenshot failed: {e}")
    if uri is None:
        log.info("selection cancelled")
        sys.exit(0)
    screenshots_dir = cfg.root / "data" / "screenshots"
    dest = store(uri, screenshots_dir)
    prune(screenshots_dir, int(cfg.ask_screen.get("keep_last", 20)))
    launch_popup(cfg, dest.stem)


if __name__ == "__main__":
    main()
