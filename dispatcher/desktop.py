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
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("dispatcher.desktop")

SINK = "@DEFAULT_AUDIO_SINK@"
RUN_TIMEOUT_S = 10.0
CLIP_MAX = 4000          # clipboard text we will read back or write
VOLUME_STEP = 0.10
MAX_VOLUME = 1.5         # wpctl allows boosting; cap it so a typo can't deafen
LAUNCH_SETTLE_S = 2.0    # how long we wait to see a launched app show up
LAUNCH_POLL_S = 0.1

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

def _run(cmd: list[str], input_text: str | None = None,
         env: dict[str, str] | None = None) -> str:
    """Run a desktop helper, raising DesktopError with something speakable."""
    exe = shutil.which(cmd[0])
    if not exe:
        raise DesktopError(f"{cmd[0]} isn't installed on this machine")
    try:
        p = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True,
                           input=input_text, timeout=RUN_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        raise DesktopError(f"{cmd[0]} didn't respond") from None
    except OSError as e:
        raise DesktopError(f"couldn't run {cmd[0]}: {e}") from None
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip().splitlines()
        raise DesktopError(detail[0][:160] if detail
                           else f"{cmd[0]} failed ({p.returncode})")
    return p.stdout


def spawn_app(cmd: list[str], env: dict[str, str] | None = None,
              wait_s: float = 5.0) -> int | None:
    """Start a GUI app detached from this service. Returns the launcher's exit
    status, or None if it hadn't exited within `wait_s`.

    Not `_run`, for two reasons — both measured on 2026-07-27, and both of
    which only appear once an app actually *survives* being launched:

    - `capture_output` waits for EOF on the pipes, and the launched app
      inherits them and holds them open for its whole lifetime. A successful
      launch therefore blocks until the timeout while a failed one returns at
      once: the exact inverse of what you want.
    - A child inherits our cgroup, so everything the dispatcher opened would
      be killed by `systemctl --user restart mission-dispatcher` — which this
      project does routinely. A transient scope (the same mechanism behind
      GNOME's own `app-*.scope` units) moves the app out from under us.
    """
    argv = list(cmd)
    if shutil.which("systemd-run"):
        argv = ["systemd-run", "--user", "--scope", "--collect", "--quiet",
                "--slice=app.slice", "--", *argv]
    exe = shutil.which(argv[0])
    if not exe:
        raise DesktopError(f"{argv[0]} isn't installed on this machine")
    try:
        p = subprocess.Popen([exe, *argv[1:]], env=env, start_new_session=True,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except OSError as e:
        raise DesktopError(f"couldn't run {cmd[0]}: {e}") from None
    try:
        return p.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        return None          # still going: the caller decides what that means


# -- session environment -----------------------------------------------------

# What a GUI process needs to find the display and the session bus.
#
# The dispatcher is a systemd *user* service, and a user service only carries
# these if it was started **after** GNOME ran `systemctl --user
# import-environment` — at boot it isn't, so its environment has no DISPLAY and
# no WAYLAND_DISPLAY at all. That failed silently and expensively (2026-07-27):
# `gtk-launch firefox` exits **0** regardless, firefox printed "no DISPLAY
# environment variable specified" to a pipe nobody read and died, and the verb
# happily answered "Opening firefox." over an app that never appeared.
#
# The systemd user manager itself always holds the real values, so ask it
# rather than trusting our own snapshot — that also survives a reboot, which
# putting the variables in the unit file would not.
SESSION_VARS = (
    "WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_TYPE", "XDG_SESSION_DESKTOP", "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
)


def _manager_environment() -> dict[str, str]:
    """The systemd user manager's environment block, {} if it can't be read."""
    try:
        out = _run(["systemctl", "--user", "show-environment"])
    except DesktopError:
        return {}
    env: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        # `$'…'` is systemd's escaping for values that need quoting. Nothing we
        # want is ever quoted, so skipping those beats mis-unescaping them.
        if sep and key.isidentifier() and not value.startswith("$'"):
            env[key] = value
    return env


def session_env() -> dict[str, str]:
    """Our environment, plus any session variables it is missing.

    Costs nothing when the dispatcher was started inside a graphical session
    (every variable is already present, so the manager is never asked).
    """
    env = dict(os.environ)
    manager: dict[str, str] | None = None
    for var in SESSION_VARS:
        if env.get(var):
            continue
        if manager is None:
            manager = _manager_environment()
        if manager.get(var):
            env[var] = manager[var]
            log.info("desktop: took %s from the systemd user manager", var)
    return env


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


def _app_dirs() -> list[Path]:
    """Every directory that can hold a .desktop file, in XDG precedence."""
    return [Path(p) / "applications" for p in (
        os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share"),
        *(os.environ.get("XDG_DATA_DIRS")
          or "/usr/local/share:/usr/share").split(":"),
    ) if p]


def _desktop_entries() -> dict[str, str]:
    """{lowercased app name: desktop id}. Cheap enough to do per call (~190
    files here) and always current, which beats caching a stale menu."""
    out: dict[str, str] = {}
    for d in _app_dirs():
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


# Wrappers whose own name says nothing about what is running: for these the
# desktop id (`com.spotify.Client`) identifies the process, not the binary.
_WRAPPER_BINARIES = frozenset({"flatpak", "snap", "env", "sh", "bash", "gio"})


def _exec_line(ident: str) -> str:
    """The entry's `Exec=` line, or "" when the file can't be read."""
    for d in _app_dirs():
        f = d / f"{ident}.desktop"
        if not f.is_file():
            continue
        try:
            for line in f.read_text(errors="replace").splitlines():
                if line.startswith("[Desktop Entry]"):
                    continue
                if line.startswith("["):
                    break                    # only the main group interests us
                if line.startswith("Exec="):
                    return line[5:].strip()
        except OSError:
            return ""
    return ""


def process_token(ident: str) -> str:
    """A substring that identifies this app in a process command line."""
    parts = _exec_line(ident).split()
    binary = os.path.basename(parts[0]) if parts else ""
    if not binary or binary in _WRAPPER_BINARIES:
        return ident
    return binary


def _process_running(token: str, argv0_only: bool = True) -> bool:
    """Whether a running process looks like this app.

    `argv0_only` matches the executable path alone, because matching the whole
    command line finds far too much — a terminal running `xdg-open firefox` or
    a script with the word in it both count as "firefox is up" otherwise (which
    is exactly what fooled the first live test of this check). Wrapper-launched
    apps are the exception: `flatpak run … com.spotify.Client` only carries its
    identity in the later arguments.

    Errs toward True: a false positive just means we go back to trusting
    gtk-launch, which is what we did before this check existed.
    """
    needle = token.encode()
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            raw = (p / "cmdline").read_bytes()
        except OSError:
            continue                          # the process exited under us
        if needle in (raw.split(b"\0", 1)[0] if argv0_only else raw):
            return True
    return False


def _launch(intent: Intent) -> str:
    ident = resolve_app(intent.arg)
    if not ident:
        raise DesktopError(f"I couldn't find an app called {intent.arg}")
    env = session_env()
    if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
        raise DesktopError("I can't reach your desktop session")

    # gtk-launch's exit code says only that it forked, so check the app is
    # actually there afterwards — unless something matching it already was, in
    # which case launching only raises an existing window and there is nothing
    # new to see.
    token = process_token(ident)
    whole_cmdline = token == ident            # wrapper: identity is in the args

    def running() -> bool:
        return _process_running(token, argv0_only=not whole_cmdline)

    verify = not running()
    if spawn_app(["gtk-launch", ident], env):
        raise DesktopError(f"couldn't launch {intent.arg}")
    if verify:
        deadline = time.monotonic() + LAUNCH_SETTLE_S
        while not running():
            if time.monotonic() >= deadline:
                raise DesktopError(f"{intent.arg} didn't start")
            time.sleep(LAUNCH_POLL_S)
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
    env = session_env()
    if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
        raise DesktopError("I can't reach your desktop session")
    # spawn_app, not _run: xdg-open hands off to a browser that inherits our
    # pipes and cgroup, so the same two traps apply as for `launch`.
    if spawn_app(["xdg-open", target if scheme else f"https://{target}"], env):
        raise DesktopError(f"couldn't open {target}")
    return f"Opened {target}."


def _clipboard_get() -> str:
    # wl-paste falls back to the "wayland-0" socket when WAYLAND_DISPLAY is
    # unset, which is why the clipboard worked while launching didn't — luck,
    # not design. Hand it the real value.
    text = _run(["wl-paste", "-n"], env=session_env()).strip()
    if not text:
        return "The clipboard is empty."
    if len(text) > CLIP_MAX:
        text = text[:CLIP_MAX] + "…"
    return f"Clipboard: {text}"


def _clipboard_set(intent: Intent) -> str:
    text = (intent.arg or "")[:CLIP_MAX]
    if not text:
        raise DesktopError("nothing to copy")
    _run(["wl-copy", "--"], input_text=text, env=session_env())
    return f"Copied {text[:60]}{'…' if len(text) > 60 else ''}."


def screen_locked() -> bool | None:
    """True/False, or None when the lock state can't be determined.

    Asks the **session bus**, not `loginctl show-session self`. The dispatcher
    is a systemd *user* service, and a user service belongs to
    `user@1000.service`, not to a login session — so loginctl answers
    "Caller does not belong to any known session" every time. The original
    `_status` swallowed that as a DesktopError, which meant the lock state was
    silently never reported (found 2026-07-26 while verifying the lock verb).
    """
    try:
        out = _run(["gdbus", "call", "--session",
                    "-d", "org.gnome.ScreenSaver",
                    "-o", "/org/gnome/ScreenSaver",
                    "-m", "org.gnome.ScreenSaver.GetActive"])
    except DesktopError:
        return None
    low = out.lower()
    if "true" in low:
        return True
    if "false" in low:
        return False
    return None


def _status() -> str:
    level, muted = get_volume()
    bits = [f"system volume {round(level * 100)}%" + (" (muted)" if muted else "")]
    locked = screen_locked()
    if locked is not None:
        bits.append("screen locked" if locked else "screen unlocked")
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
