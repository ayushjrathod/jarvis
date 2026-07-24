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


def _request_path(reply) -> str:
    """Pull the Request object path out of the portal's Screenshot reply.

    jeepney's blocking send_and_get_reply returns D-Bus ERROR messages without
    raising; the old code took body[0] as an object path regardless, so an
    error reply's text got fed to MatchRule → a misleading MatchRuleInvalid
    dialog. Detect the ERROR message type first and surface the portal's actual
    error text, guarding an empty body against IndexError (L10)."""
    from jeepney import MessageType

    if reply.header.message_type == MessageType.error:
        detail = reply.body[0] if reply.body else "unknown D-Bus error"
        fail(f"Screenshot portal error: {detail}")
    return reply.body[0]


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
            request_path = _request_path(reply)
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
    chromium = ask.get("chromium_bin", "chromium")
    # Precheck: a missing binary makes Popen raise FileNotFoundError, which is
    # invisible from a GUI shortcut with no terminal — surface it as a dialog
    # with the exact binary name instead (M9).
    if shutil.which(chromium) is None:
        fail(f"chromium not found: {chromium} — set jarvis.ask_screen.chromium_bin")
    w, h = ask.get("window_size", [520, 720])
    url = f"{cfg.dispatcher_url}/ask?shot={shot_id}"
    subprocess.Popen(
        [chromium, f"--app={url}", f"--window-size={w},{h}"],
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
    # store/prune/launch_popup all ran uncaught from a terminal-less GUI
    # shortcut, so any failure (bad uri, unwritable dir, chromium missing) was
    # silent. Wrap them in the same fail() dialog path as the screenshot (M9).
    # launch_popup's own fail() raises SystemExit (a BaseException), so its
    # precise "chromium not found" message passes through this `except Exception`.
    try:
        screenshots_dir = cfg.root / "data" / "screenshots"
        dest = store(uri, screenshots_dir)
        prune(screenshots_dir, int(cfg.ask_screen.get("keep_last", 20)))
        launch_popup(cfg, dest.stem)
    except Exception as e:
        fail(f"Could not open the ask window: {e}")


if __name__ == "__main__":
    main()
