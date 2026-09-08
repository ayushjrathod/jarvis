"""T2 window control over AT-SPI — dependency-free (jeepney is in the stack).

The session-20 spike declared this unreachable (GNOME Shell D-Bus is locked
down); session 24 measured otherwise: the AT-SPI registry answers, window
titles enumerate, Component.grabFocus focuses. No pyatspi, no extension,
no new dependency — the same blocking-jeepney shape as the MPRIS code in
spotify.py (jeepney returns D-Bus errors as replies, not exceptions).

Read verbs (list) are allow; focus is confirm: switching windows is visible
and potentially disruptive, in the same seat as open and clipboard_get.
"""

from __future__ import annotations

import logging

log = logging.getLogger("dispatcher.windows")

REGISTRY_BUS = "org.a11y.atspi.Registry"
REGISTRY_PATH = "/org/a11y/atspi/accessible/root"
ACCESSIBLE_IFACE = "org.a11y.atspi.Accessible"
COMPONENT_IFACE = "org.a11y.atspi.Component"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

WINDOW_ROLES = {"window", "frame", "dialog"}


class WindowError(Exception):
    pass


def _a11y_connection():
    from jeepney.io.blocking import open_dbus_connection
    from jeepney import DBusAddress, new_method_call
    conn = open_dbus_connection(bus="SESSION")
    addr = DBusAddress("/org/a11y/bus", bus_name="org.a11y.Bus",
                       interface="org.a11y.Bus")
    reply = conn.send_and_get_reply(new_method_call(addr, "GetAddress"))
    _check(reply)
    return open_dbus_connection(bus=reply.body[0])


def _check(reply):
    from jeepney import MessageType
    if reply.header.message_type == MessageType.error:
        raise WindowError(str(reply.body[0])[:200] if reply.body else "D-Bus error")
    return reply


def _call(conn, bus, path, iface, method, sig=None, body=None):
    from jeepney import DBusAddress, new_method_call
    addr = DBusAddress(path, bus_name=bus, interface=iface)
    msg = (new_method_call(addr, method, sig, body) if sig
           else new_method_call(addr, method))
    return _check(conn.send_and_get_reply(msg)).body


def _name(conn, bus, path) -> str:
    try:
        return _call(conn, bus, path, PROPS_IFACE, "Get", "ss",
                     (ACCESSIBLE_IFACE, "Name"))[0][1] or ""
    except WindowError:
        return ""


def _children(conn, bus, path) -> list:
    try:
        return _call(conn, bus, path, ACCESSIBLE_IFACE, "GetChildren")[0]
    except WindowError:
        return []


def list_windows(conn=None) -> list[dict]:
    """[{app, title}] over every AT-SPI application. Read-only, never raises
    (the service settles what it can from an empty list instead)."""
    own = conn is None
    try:
        conn = conn or _a11y_connection()
        out, seen = [], set()
        for bus, path in _children(conn, *conn_root()):
            try:
                app = _name(conn, bus, path) or bus.split(".")[-1]
            except WindowError:
                continue
            for cb, cp in _children(conn, bus, path):
                try:
                    role = _call(conn, cb, cp, ACCESSIBLE_IFACE,
                                 "GetRoleName")[0]
                except WindowError:
                    continue
                if role not in WINDOW_ROLES:
                    continue
                title = _name(conn, cb, cp)
                if not title or (app, title) in seen:
                    continue
                seen.add((app, title))
                out.append({"app": app, "title": title})
        return out
    except WindowError:
        log.warning("window list failed", exc_info=True)
        return []
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def conn_root() -> tuple[str, str]:
    return REGISTRY_BUS, REGISTRY_PATH


def focus_window(query: str, conn=None) -> str:
    """Focus the window whose title (or app) best matches `query`.
    Raises WindowError when nothing matches (the service files it failed,
    speakably) — and names candidates when several do."""
    wins = [w for w in list_windows(conn) if w["title"] or w["app"]]
    q = (query or "").strip().lower()
    if not q:
        raise WindowError("focus what?")
    hits = [w for w in wins
            if q in w["title"].lower() or q in w["app"].lower()]
    if not hits:
        raise WindowError(f'no window matching "{query}"')
    if len(hits) > 1:
        names = ", ".join(sorted({h["title"] for h in hits})[:5])
        raise WindowError(f'which one? {names}')
    return _grab(hits[0], conn)


def _grab(win: dict, conn=None) -> str:
    own = conn is None
    try:
        conn = conn or _a11y_connection()
        for bus, path in _children(conn, *conn_root()):
            for cb, cp in _children(conn, bus, path):
                try:
                    role = _call(conn, cb, cp, ACCESSIBLE_IFACE,
                                 "GetRoleName")[0]
                except WindowError:
                    continue
                if role not in WINDOW_ROLES:
                    continue
                if _name(conn, cb, cp) == win["title"]:
                    _call(conn, cb, cp, COMPONENT_IFACE, "GrabFocus")
                    return f'Focused "{win["title"]}".'
        raise WindowError(f'"{win["title"]}" went away before focus landed')
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
