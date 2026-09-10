"""T3 browser control, read-mostly — over the DevTools HTTP endpoints.

No new dependency and no WebSocket framing: /json/list enumerates targets
and /json/activate/{id} switches to one, both plain HTTP (httpx is in the
stack). Reading page *text* would need Runtime.evaluate over a WS — that is
a later slice; listing and switching tabs is the honest, useful half that
needs nothing new. The browser must run with --remote-debugging-port
(default 9222); without it every call fails speakably and fast, never
hanging the divert.

Verbs live in the desktop safety plane (tabs reads, so allow;
activate_tab switches context, so confirm) — same policy, same confirm UX
as window focus, which answers the same "switch to X" shape for OS windows.
The parser keeps them apart with the tab qualifier: only sentences naming
tabs reach this tier.
"""

from __future__ import annotations

import logging

log = logging.getLogger("dispatcher.browser")

DEFAULT_DEBUG_PORT = 9222
TIMEOUT_S = 5.0


class BrowserError(Exception):
    pass


def _base_url(cfg) -> str:
    port = ((getattr(cfg, "browser", None) or {}).get("debug_port")
            or DEFAULT_DEBUG_PORT)
    return f"http://127.0.0.1:{port}"


def _get(cfg, path: str, expect_json: bool = True):
    import httpx
    try:
        r = httpx.get(_base_url(cfg) + path, timeout=TIMEOUT_S)
    except Exception as e:
        raise BrowserError(
            "no debuggable browser — launch chromium with "
            "--remote-debugging-port=9222") from e
    if r.status_code != 200:
        raise BrowserError(f"browser answered {r.status_code} for {path}")
    if not expect_json:
        return r.text
    try:
        return r.json()
    except ValueError as e:
        raise BrowserError("browser answered garbage") from e


def list_tabs(cfg) -> list[dict]:
    """[{id, title, url}] over open pages. Never raises."""
    try:
        targets = _get(cfg, "/json/list")
    except BrowserError:
        log.warning("tab list failed", exc_info=True)
        return []
    out = []
    for t in targets or []:
        if not isinstance(t, dict) or t.get("type") != "page":
            continue
        title = (t.get("title") or "").strip() or "(untitled)"
        out.append({"id": t.get("id", ""), "title": title,
                    "url": t.get("url", "")})
    return out


def activate_tab(cfg, query: str) -> str:
    """Switch to the tab whose title or URL best matches `query`."""
    q = (query or "").strip().lower()
    if not q:
        raise BrowserError("switch to which tab?")
    tabs = list_tabs(cfg)
    if not tabs:
        raise BrowserError("no browser tabs found (is it running debuggable?)")
    hits = [t for t in tabs
            if q in t["title"].lower() or q in t["url"].lower()]
    if not hits:
        raise BrowserError(f'no tab matching "{query}"')
    if len(hits) > 1:
        names = ", ".join(sorted({h["title"] for h in hits})[:5])
        raise BrowserError(f"which tab? {names}")
    _get(cfg, f"/json/activate/{hits[0]['id']}", expect_json=False)
    return f'Switched to "{hits[0]["title"]}".'
