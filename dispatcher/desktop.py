"""Deterministic desktop control (computer-use tier T1).

The sibling of `spotify.py`, same three-part shape: a pure `detect()` parser, a
never-raises `run_intent()` executor, and a service-layer divert — so "lock the
screen" / "open firefox" / "what's on my clipboard" cost nothing, reach no
model, and land in well under a second.

**Zero new dependencies**: every verb here is subprocess over a binary that was
already on the box (`wpctl`, `gtk-launch`, `xdg-open`, `wl-copy`, `wl-paste`,
`loginctl`). No pixels, no coordinates, no synthetic input — this tier is
*deterministic app control*, which per `future/computer-use.md` is what
"computer use" feels like ~80% of the time at a fraction of the risk.

Spike findings, 2026-07-26 (GNOME 4x / Wayland on this box) — these **narrow
the tier the design doc assumed**:

- `org.gnome.Shell.Eval` → `(false, '')`. Locked since GNOME 41; it only works
  in unsafe-mode.
- `org.gnome.Shell.Introspect.GetWindows` / `.GetRunningApplications` →
  `AccessDenied`. GNOME restricts Introspect to whitelisted callers.
  **Window list / focus / close is therefore NOT reachable zero-dep** — it
  needs a GNOME shell extension, i.e. a user-installed dependency. Deferred,
  not built.
- Screen brightness: `org.gnome.SettingsDaemon.Power` exposes only
  `.Power.Keyboard` (keyboard backlight) on this version, and
  `/sys/class/backlight/intel_backlight/brightness` is root-owned.
  **Not reachable zero-dep** either — `brightnessctl` + a udev rule would do
  it. Deferred, not built.

Safety plane (doc §5, built with the tier rather than after it): every verb
resolves through `policy()` to allow / confirm / deny, configurable per verb in
`config.yaml`'s `computer:` block. The defaults are deliberately not "all
write verbs confirm" — the verbs here are low-consequence and reversible, and
confirming "set the volume to 40" by voice every time would make the feature
worse than useless. The two that *are* sensitive default to confirm:

- `clipboard_get` — the clipboard routinely holds passwords and tokens, and
  this is the one verb that reads user data back into a model's context.
- `open` — an arbitrary URL is both an exfiltration channel (data in the query
  string) and the obvious prompt-injection payload.

`open` additionally hard-restricts schemes to http/https/file at the executor,
independent of policy: `javascript:`, `data:` and friends are never launched
even if an operator sets the verb to allow.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("dispatcher.desktop")

SINK = "@DEFAULT_AUDIO_SINK@"
RUN_TIMEOUT_S = 10.0
CLIP_MAX = 4000          # clipboard text we will read back or write
VOLUME_STEP = 0.10
MAX_VOLUME = 1.5         # wpctl allows boosting; cap it so a typo can't deafen

# Verbs that only read state. Everything else changes something.
READ_VERBS = frozenset({"status", "clipboard_get"})
ALL_VERBS = READ_VERBS | frozenset(
    {"volume", "mute", "lock", "launch", "open", "clipboard_set"})

# See the module docstring for why these are the defaults.
DEFAULT_POLICY = {
    "status": "allow",
    "volume": "allow",
    "mute": "allow",
    "lock": "allow",
    "launch": "allow",
    "clipboard_set": "allow",
    "clipboard_get": "confirm",
    "open": "confirm",
}

OPEN_SCHEMES = frozenset({"http", "https", "file", ""})


class DesktopError(Exception):
    """Anything that should reach the user as a spoken sentence, not a 500."""


@dataclass(frozen=True)
class Intent:
    verb: str
    arg: str = ""                # launch/open target, clipboard_set payload
    number: float | None = None  # volume: absolute 0.0-1.0
    delta: float = 0.0           # volume: relative step when number is None
    on: bool | None = None       # mute: True/False, None = toggle

    def describe(self) -> str:
        """Human phrasing for the confirmation prompt."""
        if self.verb == "open":
            return f"open {self.arg}"
        if self.verb == "launch":
            return f"launch {self.arg}"
        if self.verb == "clipboard_get":
            return "read your clipboard"
        if self.verb == "clipboard_set":
            return f"copy {self.arg[:40]!r} to your clipboard"
        if self.verb == "volume":
            if self.number is not None:
                return f"set the system volume to {round(self.number * 100)}%"
            return f"turn the system volume {'up' if self.delta > 0 else 'down'}"
        if self.verb == "mute":
            return {True: "mute", False: "unmute", None: "toggle mute"}[self.on]
        return self.verb.replace("_", " ")


# -- safety plane ------------------------------------------------------------

def policy(cfg_block: dict | None, verb: str) -> str:
    """"allow" | "confirm" | "deny" for this verb.

    Unknown verbs deny (fail closed — a typo in config must not silently widen
    the surface), and an unrecognised policy value denies for the same reason.
    An absent/empty `computer:` block disables the whole feature: everything
    denies, which is what keeps the unit tests free of desktop side effects.
    """
    cfg_block = cfg_block or {}
    if not cfg_block.get("enabled"):
        return "deny"
    if verb not in ALL_VERBS:
        return "deny"
    per_verb = cfg_block.get("policy") or {}
    value = per_verb.get(verb, DEFAULT_POLICY.get(verb, "deny"))
    return value if value in ("allow", "confirm", "deny") else "deny"


# -- intent parsing ----------------------------------------------------------

_PUNCT = " \t\n.!?,;:'\"-—"

# "volume 40" belongs to spotify.detect (media player volume) and the media
# divert runs first, so every pattern here demands an explicit system/master
# qualifier. Without that the two parsers would fight over the same sentence.
_SYS = r"(?:system|master|computer|desktop|laptop)"

_VOL_SET_RE = re.compile(
    rf"^(?:set (?:the )?)?{_SYS} volume (?:to |at )?(?P<n>\d{{1,3}})"
    r"(?:\s*(?:%|percent))?$")
_VOL_WORD_RE = re.compile(
    rf"^(?:turn (?:the )?{_SYS} volume (?P<dir>up|down)"
    rf"|{_SYS} volume (?P<dir2>up|down))$")
_MUTE_RE = re.compile(
    rf"^(?:(?P<un>un)?mute(?: the)? {_SYS}(?: volume| audio| sound)?"
    rf"|(?P<un2>un)?mute (?:the )?(?:audio|sound|speakers))$")
_LOCK_RE = re.compile(
    r"^(?:lock(?: the)?(?: screen| session| computer| laptop| desktop)?"
    r"|lock it)$")
_LAUNCH_RE = re.compile(
    r"^(?:launch|open up|start|fire up|run)\s+(?P<q>.+?)"
    r"(?:\s+(?:app|application))?$")
_OPEN_APP_RE = re.compile(r"^open\s+(?P<q>[a-z0-9][a-z0-9 .+_-]*)$")
_OPEN_URL_RE = re.compile(
    r"^(?:open|go to|browse to|visit)\s+(?P<q>\S+\.\S+|\S+://\S+)$")
_CLIP_GET_RE = re.compile(
    r"^(?:what(?:'?s| is) (?:on |in )?(?:my |the )?clipboard"
    r"|read (?:my |the )?clipboard|paste(?: buffer)?"
    r"|what did i copy)$")
_CLIP_SET_RE = re.compile(
    r"^(?:copy|put)\s+(?P<q>.+?)\s+(?:to|on|in)(?:to)? (?:my |the )?clipboard$")
_STATUS_RE = re.compile(
    r"^(?:(?:what(?:'?s| is) the )?{s} (?:status|state)"
    r"|is the screen locked"
    r"|what(?:'?s| is) the {s} volume)$".format(s=_SYS))

# Phrases that look like a launch/open but are conversation, not a command.
VETO = (
    "open source", "open question", "open to", "open up about",
    "start over", "start with", "start from", "run through", "run by",
    "run out", "run into", "open a discussion", "open the floor",
    "lock in", "locked in", "start a conversation",
)


# Answers to a parked confirmation ("Shall I read your clipboard?" → "yeah").
# Only consulted when that source actually has one pending, so a stray "yes" in
# ordinary conversation can never trigger a desktop verb.
_YES_RE = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|ok|okay|alright|go ahead|do it|please do"
    r"|affirmative|confirm(?:ed)?|that's fine|fine)\b")
_NO_RE = re.compile(
    r"^(?:no|nope|nah|don'?t|do not|cancel|stop|skip(?: it)?|forget it"
    r"|never ?mind|negative|leave it)\b")


def parse_answer(text: str) -> bool | None:
    """True/False for a yes/no reply, None if it isn't one."""
    t = _normalize(text)
    if not t:
        return None
    if _NO_RE.match(t):     # checked first: "don't" starts with no-ish words
        return False
    if _YES_RE.match(t):
        return True
    return None


def _normalize(text: str) -> str:
    t = (text or "").lower().strip(_PUNCT)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\bhey jarvis\b[,\s]*", "", t)
    t = re.sub(r"^(?:please|can you|could you|would you)\s+", "", t.strip(_PUNCT))
    return t.strip(_PUNCT)


def detect(text: str) -> Intent | None:
    """Intent | None. Pure — no I/O, safe to call twice (main.py gates on it,
    then the service re-parses). Returns None for anything that isn't an
    unambiguous desktop command, so normal task routing is unaffected."""
    t = _normalize(text)
    if not t or any(v in t for v in VETO):
        return None
    # a question mark means they're asking about it, not commanding it —
    # except the read verbs, which ARE questions
    asking = (text or "").strip().endswith("?")

    if _CLIP_GET_RE.match(t):
        return Intent("clipboard_get")
    if _STATUS_RE.match(t):
        return Intent("status")
    if asking:
        return None

    m = _CLIP_SET_RE.match(t)
    if m:
        return Intent("clipboard_set", arg=m.group("q").strip(_PUNCT))
    if _LOCK_RE.match(t):
        return Intent("lock")

    m = _MUTE_RE.match(t)
    if m:
        return Intent("mute", on=not (m.group("un") or m.group("un2")))
    m = _VOL_SET_RE.match(t)
    if m:
        n = int(m.group("n"))
        if n > 100:
            return None            # "system volume 400" is not a real request
        return Intent("volume", number=n / 100.0)
    m = _VOL_WORD_RE.match(t)
    if m:
        up = (m.group("dir") or m.group("dir2")) == "up"
        return Intent("volume", delta=VOLUME_STEP if up else -VOLUME_STEP)

    m = _OPEN_URL_RE.match(t)
    if m:
        return Intent("open", arg=m.group("q"))
    m = _LAUNCH_RE.match(t) or _OPEN_APP_RE.match(t)
    if m:
        target = m.group("q").strip(_PUNCT)
        # "open the door" / multi-word prose is not an app name
        if target and len(target.split()) <= 3:
            return Intent("launch", arg=target)
    return None


# -- executors ---------------------------------------------------------------

def _run(cmd: list[str], input_text: str | None = None) -> str:
    """Run a desktop helper, raising DesktopError with something speakable."""
    exe = shutil.which(cmd[0])
    if not exe:
        raise DesktopError(f"{cmd[0]} isn't installed on this machine")
    try:
        p = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True,
                           input=input_text, timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise DesktopError(f"{cmd[0]} didn't respond") from None
    except OSError as e:
        raise DesktopError(f"couldn't run {cmd[0]}: {e}") from None
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip().splitlines()
        raise DesktopError(detail[0][:160] if detail
                           else f"{cmd[0]} failed ({p.returncode})")
    return p.stdout


_VOL_RE = re.compile(r"Volume:\s*([0-9.]+)(\s*\[MUTED\])?")


def get_volume() -> tuple[float, bool]:
    """(level 0.0-1.0+, muted)."""
    m = _VOL_RE.search(_run(["wpctl", "get-volume", SINK]))
    if not m:
        raise DesktopError("couldn't read the system volume")
    return float(m.group(1)), bool(m.group(2))


def _set_volume(intent: Intent) -> str:
    if intent.number is not None:
        target = max(0.0, min(intent.number, 1.0))
    else:
        current, _ = get_volume()
        target = max(0.0, min(current + intent.delta, MAX_VOLUME))
    _run(["wpctl", "set-volume", SINK, f"{target:.2f}"])
    return f"System volume {round(target * 100)}%."


def _set_mute(intent: Intent) -> str:
    arg = "toggle" if intent.on is None else ("1" if intent.on else "0")
    _run(["wpctl", "set-mute", SINK, arg])
    if intent.on is None:
        _, muted = get_volume()
        return "Muted." if muted else "Unmuted."
    return "Muted." if intent.on else "Unmuted."


def _lock() -> str:
    _run(["loginctl", "lock-session"])
    return "Locking the screen."


def _desktop_entries() -> dict[str, str]:
    """{lowercased app name: desktop id}. Cheap enough to do per call (~190
    files here) and always current, which beats caching a stale menu."""
    dirs = [Path(p) / "applications" for p in (
        os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share"),
        *(os.environ.get("XDG_DATA_DIRS")
          or "/usr/local/share:/usr/share").split(":"),
    ) if p]
    out: dict[str, str] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.desktop")):
            name, hidden = None, False
            try:
                for line in f.read_text(errors="replace").splitlines():
                    if line.startswith("[Desktop Entry]"):
                        continue
                    if line.startswith("["):
                        break            # only the main group interests us
                    if line.startswith("Name=") and name is None:
                        name = line[5:].strip()
                    elif line.startswith(("NoDisplay=true", "Hidden=true")):
                        hidden = True
            except OSError:
                continue
            if name and not hidden:
                out.setdefault(name.lower(), f.stem)
    return out


def resolve_app(query: str) -> str | None:
    """Desktop id for a spoken app name, or None. Exact match beats prefix
    beats substring, so "files" can't be stolen by "LibreOffice Files Demo"."""
    q = (query or "").lower().strip()
    if not q:
        return None
    entries = _desktop_entries()
    if q in entries:
        return entries[q]
    for match in (lambda n: n.startswith(q), lambda n: q in n):
        hits = sorted(n for n in entries if match(n))
        if hits:
            return entries[hits[0]]
    # last resort: the desktop id itself ("org.gnome.Nautilus" ← "nautilus")
    for name, ident in entries.items():
        if ident.lower() == q or ident.lower().endswith("." + q):
            return ident
    return None


def _launch(intent: Intent) -> str:
    ident = resolve_app(intent.arg)
    if not ident:
        raise DesktopError(f"I couldn't find an app called {intent.arg}")
    _run(["gtk-launch", ident])
    return f"Opening {intent.arg}."


def scheme_of(target: str) -> str:
    """The URI scheme, or "" for a bare host/path.

    Hand-rolled because urlparse is the wrong tool here: the dangerous schemes
    are the *opaque* ones (`javascript:`, `data:`, `mailto:`) which have no
    "//", so normalizing a bare host by prepending "//" makes urlparse read
    `javascript:alert(1)` as a host with an empty scheme — i.e. it silently
    passes the exact input the guard exists to stop.
    """
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):(.*)$", target, re.S)
    if not m:
        return ""
    scheme, rest = m.group(1).lower(), m.group(2)
    if "." in scheme and rest[:1].isdigit():
        return ""              # "example.com:8080/x" is host:port, not a scheme
    return scheme


def _open(intent: Intent) -> str:
    target = (intent.arg or "").strip()
    if not target:
        raise DesktopError("nothing to open")
    scheme = scheme_of(target)
    if scheme not in OPEN_SCHEMES:
        # hard stop regardless of policy — see the module docstring
        raise DesktopError(f"I won't open a {scheme}: link")
    _run(["xdg-open", target if scheme else f"https://{target}"])
    return f"Opened {target}."


def _clipboard_get() -> str:
    text = _run(["wl-paste", "-n"]).strip()
    if not text:
        return "The clipboard is empty."
    if len(text) > CLIP_MAX:
        text = text[:CLIP_MAX] + "…"
    return f"Clipboard: {text}"


def _clipboard_set(intent: Intent) -> str:
    text = (intent.arg or "")[:CLIP_MAX]
    if not text:
        raise DesktopError("nothing to copy")
    _run(["wl-copy", "--"], input_text=text)
    return f"Copied {text[:60]}{'…' if len(text) > 60 else ''}."


def _status() -> str:
    level, muted = get_volume()
    bits = [f"system volume {round(level * 100)}%" + (" (muted)" if muted else "")]
    try:
        locked = _run(["loginctl", "show-session", "self", "-p", "LockedHint"])
        if "yes" in locked.lower():
            bits.append("screen locked")
    except DesktopError:
        pass
    return "Right now: " + ", ".join(bits) + "."


_EXECUTORS = {
    "volume": _set_volume,
    "mute": _set_mute,
    "lock": lambda i: _lock(),
    "launch": _launch,
    "open": _open,
    "clipboard_get": lambda i: _clipboard_get(),
    "clipboard_set": _clipboard_set,
    "status": lambda i: _status(),
}


def run_intent(intent: Intent) -> str:
    """Execute and return a speakable sentence. Never raises — every failure
    becomes something Jarvis can say, exactly like spotify.run_intent.

    Blocking (subprocess); the service layer calls this in asyncio.to_thread.
    Policy is checked by the *caller* — this function assumes the verb was
    already allowed, so tests can exercise executors directly.
    """
    fn = _EXECUTORS.get(intent.verb)
    if fn is None:
        return "I don't know how to do that yet."
    try:
        return fn(intent)
    except DesktopError as e:
        return f"Sorry — {e}."
    except Exception:
        log.exception("desktop verb %s crashed", intent.verb)
        return "Sorry, that didn't work."
