"""Spotify media control: deterministic intent → local desktop client.

Two halves, both dependency-free (jeepney and httpx are already in the stack):

1. **Intent parsing** — `detect()` turns "play bohemian rhapsody" / "pause" /
   "skip this song" / "what's playing" / "volume 40" into an `Intent`. It is a
   pure function, the sibling of `automations.detect`: no LLM, no I/O, so the
   common commands cost nothing and land in well under a second. Music-shaped
   text it can't parse cleanly returns the `MAYBE` sentinel, which arms the
   one-call `media-parse` fallback in the service layer. Anything else returns
   None and falls through to normal task routing.

2. **Execution** — playback happens in the user's own Spotify desktop client
   over MPRIS (`org.mpris.MediaPlayer2.spotify`), driven with jeepney. Finding
   a song by name is the one thing MPRIS can't do, so `search()` asks the
   Spotify Web API under the **client-credentials** flow (app-only: no user
   OAuth, no redirect URI, no Premium requirement) and hands the resulting
   `spotify:track:…` URI to the client via `OpenUri`.

Everything here is blocking; the service layer calls `run_intent` inside
asyncio.to_thread so the event loop never stalls on D-Bus or HTTP.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import desktop

log = logging.getLogger("dispatcher.spotify")

BUS_NAME = "org.mpris.MediaPlayer2.spotify"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"

# how long to wait for the MPRIS name after launching the client, and how long
# to let the client settle before reading back what actually started playing
LAUNCH_TIMEOUT_S = 20.0
SETTLE_S = 0.6

VOLUME_STEP = 0.15


class MediaError(Exception):
    """Anything that should reach the user as a spoken sentence, not a 500."""


# -- intents -----------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    action: str                 # play|pause|resume|next|previous|now_playing|volume
    query: str = ""             # play only: what to search for
    type: str = "track"         # play only: track|album|artist|playlist|show
    arg: float | None = None    # volume only: 0.0-1.0, or None for a relative step
    delta: float = 0.0          # volume only: relative change when arg is None


class _Maybe:
    """Sentinel: music-shaped, but not parseable without a model."""

    def __repr__(self):  # pragma: no cover - debug aid
        return "MAYBE"

    def __bool__(self):
        return True


MAYBE = _Maybe()

SEARCH_TYPES = ("track", "album", "artist", "playlist", "show")

# "play devil's advocate" is not a music request. These win over every pattern.
VETO = (
    "devil's advocate", "devils advocate", "play along", "play it safe",
    "play a role", "playing field", "play out", "play down", "play games",
    "foul play", "play on youtube", "play the video", "play a video",
    "play on netflix", "play tennis", "play football", "play cricket",
    "play chess", "play a game", "play games",
)

# words that make a sentence music-ish for the MAYBE fallback
MUSIC_WORDS = ("music", "song", "songs", "track", "album", "artist", "band",
               "playlist", "spotify", "tune", "tunes")

# a play query built only from these can't be searched literally — ask the model
VAGUE_STARTS = ("something", "anything", "some music", "some tunes", "music",
                "a song", "songs", "some songs", "whatever")
VAGUE_MARKERS = ("that song", "the one", "the song from", "that thing",
                 "similar to", "sounds like", "kind of", "my liked", "my playlist",
                 "my discover", "my daily")

_PUNCT = " \t\n.!?,;:'\"-—"

_PLAY_RE = re.compile(
    r"^(?:please\s+)?(?:can you\s+|could you\s+)?"
    r"(?:play|put on|start playing|throw on|queue up)\s+(?P<q>.+)$")
_TYPE_RES = (
    ("album", re.compile(r"^(?:the\s+)?album\s+(?P<q>.+)$")),
    ("artist", re.compile(r"^(?:the\s+)?(?:artist|band)\s+(?P<q>.+)$")),
    ("playlist", re.compile(r"^(?:the\s+)?playlist\s+(?P<q>.+)$")),
    ("show", re.compile(r"^(?:the\s+)?(?:podcast|show)\s+(?P<q>.+)$")),
)
_PAUSE_RE = re.compile(
    r"^(?:pause|pause (?:the )?(?:music|song|track|playback|spotify)"
    r"|stop (?:the )?(?:music|song|playback|spotify))$")
_RESUME_RE = re.compile(
    r"^(?:resume|unpause|play|keep playing"
    r"|(?:resume|continue|unpause) (?:the )?(?:music|song|track|playback|spotify))$")
_NEXT_RE = re.compile(
    r"^(?:next|skip|skip it|next one"
    r"|(?:skip|next) (?:this )?(?:song|track|one)"
    r"|skip this|play the next (?:song|track))$")
_PREV_RE = re.compile(
    r"^(?:previous|go back|back a (?:song|track)"
    r"|(?:previous|last) (?:song|track)"
    r"|(?:play|go to) (?:the )?(?:previous|last) (?:song|track))$")
_NOW_RE = re.compile(
    r"^(?:what(?:'?s| is) (?:this|playing|this song|the song|currently playing)"
    r"|what song is (?:this|playing)|what(?:'?s| is) playing (?:right )?now"
    r"|who sings this|who(?:'?s| is) this|what am i listening to"
    r"|name this song)$")
_VOL_SET_RE = re.compile(
    r"^(?:set (?:the )?)?volume (?:to |at )?(?P<n>\d{1,3})(?:\s*(?:%|percent))?$")
_VOL_WORD_RE = re.compile(
    r"^(?:turn (?:it |the (?:music|volume|song) )?(?P<dir>up|down)"
    r"|(?:volume|music) (?P<dir2>up|down)|louder|quieter|softer)$")
_MUTE_RE = re.compile(r"^(?:mute|mute (?:the )?(?:music|spotify)|unmute)$")


def _normalize(text: str) -> str:
    t = (text or "").lower().strip(_PUNCT)
    t = re.sub(r"\s+", " ", t)
    return re.sub(r"\bhey jarvis\b[,\s]*", "", t).strip(_PUNCT)


def _looks_musical(text: str) -> bool:
    return any(w in text.split() or w in text for w in MUSIC_WORDS)


def _classify_query(q: str) -> tuple[str, str]:
    """(search_type, cleaned_query) — "album abbey road" → ("album", "abbey road")."""
    for type_, rx in _TYPE_RES:
        m = rx.match(q)
        if m:
            return type_, m.group("q").strip(_PUNCT)
    return "track", q


def _is_vague(q: str) -> bool:
    if any(m in q for m in VAGUE_MARKERS):
        return True
    return any(q == s or q.startswith(s + " ") for s in VAGUE_STARTS)


def detect(text: str):
    """Intent | MAYBE | None. Pure — safe to call twice (main.py gates on it,
    then the service re-parses)."""
    t = _normalize(text)
    if not t or any(v in t for v in VETO):
        return None

    if _NOW_RE.match(t):
        return Intent("now_playing")
    # a question that isn't "what's playing" belongs to Claude, not the player
    if (text or "").strip().endswith("?"):
        return None
    if _PAUSE_RE.match(t):
        return Intent("pause")
    if _NEXT_RE.match(t):
        return Intent("next")
    if _PREV_RE.match(t):
        return Intent("previous")
    if _RESUME_RE.match(t):
        return Intent("resume")
    if _MUTE_RE.match(t):
        return Intent("volume", arg=0.0 if not t.startswith("un") else 0.6)

    m = _VOL_SET_RE.match(t)
    if m:
        return Intent("volume", arg=min(100, int(m.group("n"))) / 100.0)
    m = _VOL_WORD_RE.match(t)
    if m:
        down = (m.group("dir") or m.group("dir2")) == "down" or t in ("quieter", "softer")
        return Intent("volume", delta=-VOLUME_STEP if down else VOLUME_STEP)

    m = _PLAY_RE.match(t)
    if m:
        q = m.group("q").strip(_PUNCT)
        q = re.sub(r"\s+(?:on|in|through|via)\s+spotify$", "", q).strip(_PUNCT)
        q = re.sub(r"^(?:some|a bit of|a little)\s+", "", q).strip(_PUNCT)
        if not q:
            return Intent("resume")
        if _is_vague(q):
            return MAYBE
        type_, q = _classify_query(q)
        return Intent("play", query=q, type=type_)

    # music-shaped but unparsed ("put on something for focusing")
    if _looks_musical(t) and re.search(r"\b(play|put on|listen to|hear)\b", t):
        return MAYBE
    return None


# -- LLM fallback (media-parse) ----------------------------------------------

PARSE_PROMPT = """\
Convert the user's music request into a Spotify search.

Request: {request}

Reply with ONE JSON object and nothing else:
{{"action": "play", "search": "<search words for the Spotify catalog>", \
"type": "track|album|artist|playlist|show"}}

Rules:
- "search" must be words that exist in the Spotify catalog: a song title, an \
artist, an album, or a genre/mood word that names real playlists ("lo-fi \
beats", "jazz classics"). Never a sentence, never a description.
- Guess the most likely well-known match for a vague or indirect reference.
- If the request is not about playing music, reply exactly {{"action": "none"}}."""


def parse_prompt(request: str) -> str:
    return PARSE_PROMPT.format(request=(request or "")[:300])


def parse_response(reply: str) -> dict:
    """Pull the JSON object out of a model reply (it may be fenced or padded)."""
    text = (reply or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"bad JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("reply JSON is not an object")
    return data


def validate_parsed(data: dict) -> Intent | None:
    """Mechanical validation, like automations.validate_spec: the model chooses
    words, never behavior. None = "not a music request"."""
    if data.get("action") != "play":
        return None
    query = data.get("search")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("missing search text")
    type_ = data.get("type") if data.get("type") in SEARCH_TYPES else "track"
    return Intent("play", query=query.strip()[:200], type=type_)


# -- MPRIS -------------------------------------------------------------------


def _variant(value):
    """jeepney gives a{sv} values as (signature, value) pairs."""
    return value[1] if isinstance(value, tuple) and len(value) == 2 else value


def _check(reply):
    """jeepney's blocking send_and_get_reply returns D-Bus errors as replies
    rather than raising (same trap as jarvis/ask_screen.py)."""
    from jeepney import MessageType

    if reply.header.message_type == MessageType.error:
        detail = reply.body[0] if reply.body else "unknown D-Bus error"
        raise MediaError(str(detail))
    return reply


def _open_connection():
    from jeepney.io.blocking import open_dbus_connection

    return open_dbus_connection(bus="SESSION")


def is_running(conn=None) -> bool:
    from jeepney.bus_messages import message_bus
    from jeepney.io.blocking import Proxy

    own = conn is None
    conn = conn or _open_connection()
    try:
        return bool(Proxy(message_bus, conn).NameHasOwner(BUS_NAME)[0])
    finally:
        if own:
            conn.close()


def _player_call(conn, method: str, signature: str | None = None, body=None):
    from jeepney import DBusAddress, new_method_call

    addr = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=PLAYER_IFACE)
    msg = (new_method_call(addr, method, signature, body) if signature
           else new_method_call(addr, method))
    return _check(conn.send_and_get_reply(msg))


def _prop_get(conn, name: str):
    from jeepney import DBusAddress, new_method_call

    addr = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=PROPS_IFACE)
    reply = _check(conn.send_and_get_reply(
        new_method_call(addr, "Get", "ss", (PLAYER_IFACE, name))))
    return _variant(reply.body[0])


def _prop_set(conn, name: str, signature: str, value):
    from jeepney import DBusAddress, new_method_call

    addr = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=PROPS_IFACE)
    return _check(conn.send_and_get_reply(new_method_call(
        addr, "Set", "ssv", (PLAYER_IFACE, name, (signature, value)))))


def now_playing(conn) -> dict:
    """{'title','artist','album','status'} — empty strings when nothing is loaded."""
    try:
        meta = _prop_get(conn, "Metadata") or {}
        status = _prop_get(conn, "PlaybackStatus") or ""
    except MediaError:
        return {"title": "", "artist": "", "album": "", "status": ""}
    artists = _variant(meta.get("xesam:artist")) or []
    if isinstance(artists, str):
        artists = [artists]
    return {
        "title": _variant(meta.get("xesam:title")) or "",
        "artist": ", ".join(a for a in artists if a),
        "album": _variant(meta.get("xesam:album")) or "",
        "status": str(status),
    }


def ensure_running(cfg) -> None:
    """Launch the desktop client if its MPRIS name is absent, then wait for it."""
    if is_running():
        return
    cmd = (cfg.media or {}).get("launch_cmd") or ["flatpak", "run", "com.spotify.Client"]
    if isinstance(cmd, str):
        cmd = cmd.split()
    log.info("spotify not running; launching %s", " ".join(cmd))
    try:
        # desktop.spawn_app, not a bare Popen: it supplies the session
        # environment a GUI client needs (a dispatcher started before GNOME
        # imported it has no DISPLAY at all) and detaches the client into its
        # own scope, so restarting the dispatcher doesn't kill the music.
        # wait_s=0 because `flatpak run` stays alive as the client's parent —
        # the MPRIS poll below is what tells us it came up.
        desktop.spawn_app(cmd, env=desktop.session_env(), wait_s=0)
    except (OSError, desktop.DesktopError) as e:
        raise MediaError(f"couldn't launch Spotify: {e}") from e
    deadline = time.monotonic() + LAUNCH_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if is_running():
            time.sleep(1.0)   # the client accepts OpenUri a beat after claiming the name
            return
    raise MediaError("Spotify didn't start in time")


# -- Web API search ----------------------------------------------------------

_token_cache: tuple[str, float] | None = None


def credentials(cfg) -> tuple[str, str] | None:
    """(client_id, client_secret) from the environment or the gitignored
    credentials file, or None when the user hasn't run setup_spotify.sh."""
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    rel = (cfg.media or {}).get("credentials", "data/spotify.json")
    path = Path(rel)
    if not path.is_absolute():
        path = cfg.root / path
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    cid, secret = data.get("client_id"), data.get("client_secret")
    return (cid, secret) if cid and secret else None


def _access_token(cfg) -> str:
    """Client-credentials token, cached in-process until it expires. App-only
    auth: it can search the public catalog but touches no user account."""
    global _token_cache
    if _token_cache and _token_cache[1] > time.time() + 30:
        return _token_cache[0]
    creds = credentials(cfg)
    if not creds:
        raise MediaError("no-credentials")
    resp = httpx.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                      auth=creds, timeout=10)
    if resp.status_code != 200:
        raise MediaError(f"Spotify rejected the credentials ({resp.status_code})")
    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise MediaError("Spotify returned no access token")
    _token_cache = (token, time.time() + float(payload.get("expires_in", 3600)))
    return token


def search(cfg, query: str, type_: str = "track") -> dict | None:
    """Best catalog match: {'uri','name','artist','type'} or None."""
    mcfg = cfg.media or {}
    params = {"q": query, "type": type_,
              "limit": int(mcfg.get("search_limit", 5))}
    if mcfg.get("market"):
        params["market"] = mcfg["market"]
    resp = httpx.get(SEARCH_URL, params=params, timeout=10,
                     headers={"Authorization": f"Bearer {_access_token(cfg)}"})
    if resp.status_code != 200:
        raise MediaError(f"Spotify search failed ({resp.status_code})")
    items = ((resp.json().get(f"{type_}s") or {}).get("items") or [])
    items = [i for i in items if i]          # the API pads playlist results with nulls
    if not items:
        return None
    top = items[0]
    artists = ", ".join(a.get("name", "") for a in (top.get("artists") or []))
    if type_ == "show":
        artists = top.get("publisher", "")
    return {"uri": top.get("uri"), "name": top.get("name", ""),
            "artist": artists, "type": type_}


# -- execution ---------------------------------------------------------------


def _spoken_track(hit: dict) -> str:
    name, artist = hit.get("name", ""), hit.get("artist", "")
    type_ = hit.get("type", "track")
    if type_ == "artist":
        return f"Playing {name}."
    label = {"album": "the album", "playlist": "the playlist",
             "show": "the podcast"}.get(type_, "")
    who = f" by {artist}" if artist and type_ != "playlist" else ""
    return f"Playing {label} {name}{who}.".replace("  ", " ")


def _do_play(cfg, conn, intent: Intent) -> str:
    hit = search(cfg, intent.query, intent.type)
    if not hit or not hit.get("uri"):
        return f"I couldn't find {intent.query} on Spotify."
    _player_call(conn, "OpenUri", "s", (hit["uri"],))
    time.sleep(SETTLE_S)
    # OpenUri sometimes loads without starting; Play is a harmless no-op when
    # it already started.
    try:
        _player_call(conn, "Play")
    except MediaError:
        pass
    return _spoken_track(hit)


def _do_volume(conn, intent: Intent) -> str:
    if intent.arg is not None:
        level = intent.arg
    else:
        try:
            current = float(_prop_get(conn, "Volume") or 0.0)
        except (MediaError, TypeError, ValueError):
            current = 0.5
        level = current + intent.delta
    level = max(0.0, min(1.0, level))
    _prop_set(conn, "Volume", "d", level)
    return f"Volume {round(level * 100)} percent."


def _do_now_playing(conn) -> str:
    state = now_playing(conn)
    if not state["title"]:
        return "Nothing is playing."
    who = f" by {state['artist']}" if state["artist"] else ""
    if state["status"] == "Paused":
        return f"Paused on {state['title']}{who}."
    return f"{state['title']}{who}."


def _after_skip(conn, verb: str) -> str:
    time.sleep(SETTLE_S)
    state = now_playing(conn)
    if not state["title"]:
        return f"{verb}."
    who = f" by {state['artist']}" if state["artist"] else ""
    return f"{verb} to {state['title']}{who}."


def run_intent(cfg, intent: Intent) -> str:
    """Execute an intent, returning the one sentence to speak. Blocking —
    call it from asyncio.to_thread. Never raises: every failure becomes a
    sentence the user can act on."""
    try:
        # a transport command against a dead player is a no-op worth reporting;
        # only "play something" is worth launching the client for
        if intent.action == "play":
            ensure_running(cfg)
        elif not is_running():
            return "Spotify isn't running."
        conn = _open_connection()
        try:
            if intent.action == "play":
                return _do_play(cfg, conn, intent)
            if intent.action == "pause":
                _player_call(conn, "Pause")
                return "Paused."
            if intent.action == "resume":
                _player_call(conn, "Play")
                return "Playing."
            if intent.action == "next":
                _player_call(conn, "Next")
                return _after_skip(conn, "Skipped")
            if intent.action == "previous":
                _player_call(conn, "Previous")
                return _after_skip(conn, "Went back")
            if intent.action == "now_playing":
                return _do_now_playing(conn)
            if intent.action == "volume":
                return _do_volume(conn, intent)
            return "I don't know how to do that with Spotify."
        finally:
            conn.close()
    except MediaError as e:
        if str(e) == "no-credentials":
            return ("I need Spotify credentials for that — "
                    "run scripts/setup_spotify.sh.")
        log.warning("media command failed: %s", e)
        return f"Spotify said: {e}"
    except Exception as e:
        log.exception("media command crashed")
        return f"Sorry, the music control failed: {type(e).__name__}."


def state(cfg) -> dict:
    """Player snapshot for GET /media/state (never raises)."""
    try:
        if not is_running():
            return {"running": False}
        conn = _open_connection()
        try:
            snapshot = now_playing(conn)
            try:
                snapshot["volume"] = float(_prop_get(conn, "Volume") or 0.0)
            except (MediaError, TypeError, ValueError):
                snapshot["volume"] = None
            return {"running": True, **snapshot}
        finally:
            conn.close()
    except Exception as e:
        log.warning("media state failed: %s", e)
        return {"running": False, "error": str(e)}
