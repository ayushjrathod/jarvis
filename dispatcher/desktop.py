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
READ_VERBS = frozenset({"status", "clipboard_get", "windows"})
ALL_VERBS = READ_VERBS | frozenset(
    {"volume", "mute", "lock", "launch", "open", "clipboard_set", "focus"})

# See the module docstring for why these are the defaults.
DEFAULT_POLICY = {
    "status": "allow",
    "volume": "allow",
    "mute": "allow",
    "lock": "allow",
    "launch": "allow",
    "clipboard_set": "allow",
    "windows": "allow",
    "clipboard_get": "confirm",
    "open": "confirm",
    "focus": "confirm",
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
        if self.verb == "windows":
            return "list open windows"
        if self.verb == "focus":
            return f'focus "{self.arg}"'
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
    per_verb = cfg_block.get("policy")
    if per_verb is None:
        per_verb = {}                    # key absent → the defaults above
    if not isinstance(per_verb, dict):
        # A `policy:` written as a YAML list (or a string) used to raise
        # AttributeError out of `.get`, which the callers turn into a 500 —
        # i.e. a config typo failed OPEN of the fail-closed contract, since an
        # operator seeing a crash rather than a denial learns nothing about
        # which verbs are live. Malformed means deny (2026-08-08).
        log.warning("desktop: computer.policy is %s, not a mapping — denying "
                    "every verb until it is fixed", type(per_verb).__name__)
        return "deny"
    value = per_verb.get(verb, DEFAULT_POLICY.get(verb, "deny"))
    return value if value in ("allow", "confirm", "deny") else "deny"


# -- intent parsing ----------------------------------------------------------

_PUNCT = " \t\n.!?,;:'\"-—"

# "volume 40" belongs to spotify.detect (media player volume) and the media
# divert runs first, so every pattern here demands an explicit system/master
# qualifier. Without that the two parsers would fight over the same sentence.
#
# That invariant is why the widening on 2026-08-10 stopped where it did. The
# phrasings that failed were the qualified ones — "turn up the system volume",
# "raise the system volume", "volume up on my computer" all fell through to a
# model — so those are now covered. The *un*qualified ones ("max volume",
# "half volume", "make it louder") are deliberately still left alone: claiming
# them here while bare "volume 40" goes to Spotify would split one sentence
# shape across two subsystems, which is the exact confusion this rule prevents.
# The narrow set, and the wider one. They are separate because `machine`, `pc`
# and `speaker(s)` are safe only next to an audio noun: the bare
# "<qualifier> status|state" shape in `_STATUS_RE` reads "machine state" and
# "pc status" as a request for the volume/lock report, and "what is the machine
# state" is an ordinary question about a state machine, a VM or a CI box. It
# would be answered "system volume 16%, screen unlocked", filed done, and never
# reach the model — the silent-swallow failure this module keeps re-learning
# ("open the door" → launch an app called "the door"). So the bare shape keeps
# the words that were always there, and the new ones only qualify a noun.
_SYS_CORE = r"(?:system|master|computer|desktop|laptop)"
_SYS = rf"(?:{_SYS_CORE}|machine|pc|speakers?)"
# A determiner in front of the qualifier: "turn up MY computer volume".
_DET = r"(?:the |my |this )?"
# The qualifier as it appears directly before a noun, where English allows a
# possessive: "turn up my computer's volume".
_SYSP = rf"{_SYS}(?:'s)?"
# The noun a volume phrase hangs off. `audio`/`sound` are system-side words on
# their own (the shipped mute patterns have always taken them unqualified);
# `volume` is the one Spotify also answers to, so it never appears without a
# system qualifier next to it.
_AUDIO = r"(?:audio|sound)"
_NOUN = r"(?:volume|audio|sound)"

# Levels that can be named instead of numbered. Absolute, so `_set_volume`
# clamps them to 1.0 — "max" is 100%, not the 150% boost wpctl would allow.
NAMED_LEVELS = {
    "max": 1.0, "maximum": 1.0, "full": 1.0, "full blast": 1.0,
    "all the way up": 1.0,
    "half": 0.5, "halfway": 0.5, "quarter": 0.25, "a quarter": 0.25,
    "min": 0.0, "minimum": 0.0, "zero": 0.0, "silent": 0.0,
    "all the way down": 0.0,
}
# Longest alternative first, so "max" can't shadow "maximum".
_NAMED = "(?P<name>%s)" % "|".join(
    re.escape(w) for w in sorted(NAMED_LEVELS, key=len, reverse=True))
_NUM = r"(?P<n>\d{1,3})\s*(?:%|percent)?"
_VALUE = rf"(?:{_NUM}|{_NAMED})"

# Absolute: "system volume 40", "set my computer volume to half",
# "change the master volume to 20%".
_VOL_SET_RE = re.compile(
    rf"^(?:(?:set|put|change|turn) )?{_DET}{_SYSP} {_NOUN} (?:to |at |on )?"
    rf"{_VALUE}$")
# The same thing with the qualifier trailing: "set the volume to 40 on my
# computer".
_VOL_SET_ON_RE = re.compile(
    rf"^(?:set|put|change|turn) {_DET}{_NOUN} (?:to |at )?{_VALUE}"
    rf" (?:on|for) {_DET}{_SYS}$")
_VOL_MAX_RE = re.compile(rf"^max(?:imum)? out {_DET}{_SYSP} {_NOUN}$")

# Relative. Split by sentence shape rather than crammed into one alternation,
# because each shape wants a different slice of the direction vocabulary: a
# leading word is a verb ("raise the …"), a trailing one is an adverb
# ("… louder").
_ADV = r"up|down|louder|quieter|softer"
_VERB = r"raise|increase|boost|lower|decrease|reduce|drop"
# "turn up" and its synonyms — a verb that needs a direction word after it.
_HOIST = r"turn|crank|bump|kick|dial"
_VOL_REL_RES = (
    # "turn up the system volume", "crank up my laptop sound"
    re.compile(rf"^(?:{_HOIST}) (?P<dir>up|down) {_DET}{_SYSP} {_NOUN}$"),
    # "turn the system volume up", "system volume down", "master volume louder"
    re.compile(rf"^(?:turn |put )?{_DET}{_SYSP} {_NOUN} (?P<dir>{_ADV})$"),
    # "raise the system volume", "lower my computer volume"
    re.compile(rf"^(?P<dir>{_VERB}) {_DET}{_SYSP} {_NOUN}$"),
    # "make the computer louder", "turn my laptop down"
    re.compile(rf"^(?:make|turn) {_DET}{_SYS} (?P<dir>{_ADV})$"),
    # "volume up on my computer", "turn the volume up on my laptop",
    # "make it louder on this machine"
    re.compile(rf"^(?:turn |make )?(?:it |{_DET}{_NOUN} )?(?P<dir>{_ADV})"
               rf" (?:on|for) {_DET}{_SYS}$"),
    # The same trailing qualifier, but with the direction word LEADING —
    # "turn up the volume on my computer", "raise the volume on my pc". The
    # two shapes above can't reach these: one wants the noun before the
    # direction, the other only takes an absolute value.
    re.compile(rf"^(?:{_HOIST}) (?P<dir>up|down) {_DET}{_NOUN}"
               rf" (?:on|for) {_DET}{_SYS}$"),
    re.compile(rf"^(?P<dir>{_VERB}) {_DET}{_NOUN} (?:on|for) {_DET}{_SYS}$"),
)
# Every direction word that means quieter; anything else in `dir` means louder.
#
# A denylist, so a word added to `_VERB`/`_ADV` and forgotten here would
# silently turn the volume UP on a verb whose policy is `allow` — no
# confirmation, no way to notice. `tests/test_desktop.py` pins the partition
# rather than trusting review to catch it.
_DOWN_WORDS = frozenset(
    {"down", "quieter", "softer", "lower", "decrease", "reduce", "drop"})
_UP_WORDS = frozenset(
    {"up", "louder", "raise", "increase", "boost"})

_MUTE_RE = re.compile(
    rf"^(?:(?P<un>un)?mute {_DET}{_SYS}(?: {_NOUN})?"
    rf"|(?P<un2>un)?mute {_DET}{_AUDIO})$")
_SILENCE_RE = re.compile(rf"^silence {_DET}(?:{_SYS}|{_AUDIO})$")
# "turn the sound off" / "turn off the system audio". Restricted to
# audio/sound: "turn the volume off" would be a bare-volume phrasing, which
# belongs to the media player by the rule at the top of this section.
_SOUND_TOGGLE_RE = re.compile(
    rf"^turn (?:(?P<off>off|on) {_DET}(?:{_SYS} )?{_AUDIO}"
    rf"|{_DET}(?:{_SYS} )?{_AUDIO} (?P<off2>off|on))$")


def _level_of(m: re.Match) -> float | None:
    """The absolute level a `_VALUE` match asks for, or None if it isn't a real
    request. Above 100 returns None rather than clamping: "system volume 400"
    is far likelier to be a misheard sentence than an intent to deafen you, so
    it goes back to normal routing instead of being executed."""
    if m.groupdict().get("name"):
        return NAMED_LEVELS[m.group("name")]
    n = int(m.group("n"))
    return n / 100.0 if n <= 100 else None


_LOCK_RE = re.compile(
    r"^(?:lock(?: the)?(?: screen| session| computer| laptop| desktop)?"
    r"|lock it)$")
# The five arg-bearing patterns carry re.IGNORECASE because they are matched
# against the CASE-PRESERVING normalization, not the folded one — see detect().
#
# The launch verbs are split in two (2026-08-08). "launch", "fire up" and
# "open up" are unambiguous — nobody fires up a deployment — but `run` and
# `start` are ordinary English imperatives, and having them in the bare
# alternation meant "run the tests" parsed as Intent(launch, 'the tests'). That
# was merely wrong while the divert lived in the HTTP handler; once it moved
# onto Service.submit() it became destructive, because a *queue file* saying
# "run the migration" is now swallowed by the desktop divert and recorded done
# without anything happening. So the weak verbs must additionally look like
# they are naming an app — see _looks_like_app_name.
_LAUNCH_RE = re.compile(
    r"^(?:launch|open up|fire up)\s+(?P<q>.+?)"
    r"(?:\s+(?:app|application))?$", re.I)
_LAUNCH_WEAK_RE = re.compile(
    r"^(?:run|start)\s+(?P<q>.+?)"
    r"(?:\s+(?:app|application))?$", re.I)
_OPEN_APP_RE = re.compile(r"^open\s+(?P<q>[a-z0-9][a-z0-9 .+_-]*)$", re.I)
_OPEN_URL_RE = re.compile(
    r"^(?:open|go to|browse to|visit)\s+(?P<q>\S+\.\S+|\S+://\S+)$", re.I)
_CLIP_GET_RE = re.compile(
    r"^(?:what(?:'?s| is) (?:on |in )?(?:my |the )?clipboard"
    r"|read (?:my |the )?clipboard|paste(?: buffer)?"
    r"|what did i copy)$")
_CLIP_SET_RE = re.compile(
    r"^(?:copy|put)\s+(?P<q>.+?)\s+(?:to|on|in)(?:to)? (?:my |the )?clipboard$",
    re.I)
# `_SYS_CORE`, not `_SYS`, on the bare status/state shape — see the comment
# there. Every other alternative names an audio noun or the lock, so the wider
# qualifier set is unambiguous in them.
_STATUS_RE = re.compile(
    rf"^(?:(?:what(?:'?s| is) )?{_DET}{_SYS_CORE} (?:status|state)"
    rf"|is the screen locked"
    rf"|what(?:'?s| is) {_DET}{_SYSP} {_NOUN}"
    rf"|what {_NOUN} is {_DET}{_SYS} (?:at|on)"
    rf"|how loud is {_DET}{_SYS}"
    rf"|is {_DET}(?:{_SYS}|{_AUDIO}) muted)$")


# Window verbs (K2 T2, 2026-09-08). The list shapes all name windows beside a
# listing verb or an open-state question — "open the window", "close the
# window", "look out the window" and "windows update" match none of them, by
# the same qualifier discipline that keeps the volume parser honest. Focus
# takes any argument EXCEPT a bare generic ("focus the window" is prose, not
# a target); misses fail honestly at the executor, which names no match.
_WINDOW_LIST_RE = re.compile(
    r"^(?:(?:what|which) windows are open"
    r"|(?:list|show)(?: me)?(?: my| the| all| open)? windows"
    r"|are (?:there )?any windows open"
    r"|what(?:'?s| is) open(?: right now)?)$")
_WINDOW_FOCUS_RES = (
    # No "raise": it collides with volume phrasing ("raise the volume" has no
    # system qualifier, so it reaches this branch) — three verbs suffice.
    re.compile(r"^(?:focus|switch to)\s+(?P<q>.+)$"),
    re.compile(r"^bring\s+(?P<q>.+?)\s+to\s+(?:the\s+)?(?:front|foreground|forward)$"),
)
_WINDOW_GENERIC_ARGS = frozenset({
    "the window", "a window", "this window", "that window", "it", "that",
    "this", "them", "windows",
})


# -- shape tests for the two ambiguous branches ------------------------------
#
# Both are pure and lexical, because detect() may not do I/O: it cannot ask
# resolve_app whether an app exists or DNS whether a host does. They decide
# which BRANCH a sentence belongs to, and both are written to fail toward the
# cheaper mistake.

# Words that never appear in an app's name but are everywhere in an ordinary
# imperative. One of these anywhere in a `run`/`start` argument is enough to
# hand the sentence back to normal routing.
_NOT_APP_WORDS = frozenset({
    "the", "a", "an", "my", "our", "your", "this", "that", "these", "those",
    "all", "some", "another", "again", "it", "them", "up", "for",
    "with", "from", "on", "in", "of", "and", "to", "please",
})


def _looks_like_app_name(target: str) -> bool:
    """Whether a `run …`/`start …` argument is plausibly an application.

    The test is "does this read like a name rather than a sentence": at most
    three words, no article or preposition anywhere ("run the tests", "start
    the deployment"), and no leading gerund ("start writing the report").

    What it cannot catch is a bare imperative whose object happens to be
    name-shaped — "run tests" is still read as an app called "tests". That is
    unavoidable without a lookup, and it is the same shape as the commands this
    must keep working ("run outlook", "start overwatch"); the executor's
    "I couldn't find an app called tests" is the backstop.
    """
    words = target.split()
    if not 1 <= len(words) <= 3:
        return False
    low = [w.lower() for w in words]
    if any(w in _NOT_APP_WORDS for w in low):
        return False
    return not low[0].endswith("ing")


# A dotted token is only a web address if it carries a real scheme or ends in a
# plausible TLD (2026-08-08). `_OPEN_URL_RE`'s `\S+\.\S+` matched ANY dotted
# token, so "open notes.md" became `xdg-open https://notes.md` — a request to
# read a local file turned into a web navigation, which is exactly the
# exfiltration shape the `open` verb defaults to confirm over. Anything not on
# this list falls through to the app branch, and that asymmetry is the point:
# guessing "name" costs a sentence ("I couldn't find an app called
# config.yaml"), guessing "URL" costs a request to a host nobody named. The
# list is deliberately short of the two-letter TLDs that are also common file
# extensions (md, py, sh, js, rs, pl); a site on one of those still opens with
# an explicit scheme.
_TLDS = frozenset("""
com org net edu gov mil int io co dev app ai me tv cc xyz info biz online
site tech blog news wiki cloud page shop store live fm gg to ly
uk de fr jp cn ru br au ca nl se es it ch be at dk fi ie nz mx kr sg za us eu
""".split())


def looks_like_url(target: str) -> bool:
    """Whether an `open <thing>` argument is a web address, not a name."""
    if re.match(r"^\S+://\S+$", target):
        return True
    # strip path/query/fragment, then a :port — "example.com:8080/path"
    host = re.split(r"[/?#]", target, maxsplit=1)[0].split(":", 1)[0]
    return "." in host and host.rsplit(".", 1)[-1].lower() in _TLDS


# Phrases that look like a launch/open but are conversation, not a command.
VETO = (
    "open source", "open question", "open to", "open up about",
    "start over", "start with", "start from", "run through", "run by",
    "run out", "run into", "open a discussion", "open the floor",
    "lock in", "locked in", "start a conversation",
)

# Matched on WORD BOUNDARIES, not as bare substrings (fixed 2026-08-08). Every
# short entry here is a prefix of real app names: "open to" ⊂ "open toolbox",
# "run out" ⊂ "run outlook", "start over" ⊂ "start overwatch", "open to" ⊂
# "open tor browser". All four were silently returning None, and a vetoed
# command is invisible — it just goes to the model as if it had never been a
# command, so nobody notices which sentences the desktop tier quietly dropped.
_VETO_RE = re.compile("|".join(rf"\b{re.escape(v)}\b" for v in VETO))


# Answers to a parked confirmation ("Shall I read your clipboard?" → "yeah").
# Only consulted when that source actually has one pending, so a stray "yes" in
# ordinary conversation can never trigger a desktop verb.
#
# Anchored to the WHOLE utterance, `^…$` (fixed 2026-08-08). With `\b` these
# approved on any sentence that merely *started* with an affirmation, and
# spoken English starts sentences that way constantly: while a confirm was
# parked, "okay so what's on my calendar" read as "yes" — so asking one
# question read the clipboard and swallowed the next question with it. The
# window is 120s wide and the confirm verbs are the two sensitive ones, which
# makes a false yes the worst outcome this module can produce.
#
# _normalize has already stripped a leading "please"/"can you", which is why
# "please do" isn't spelled out: it arrives here as "do".
_YES_RE = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|ok|okay|alright|go ahead|do it|do|do that"
    r"|affirmative|confirm(?:ed)?|that'?s fine|fine)$")
# The no side may be a little more generous than the yes side: a false decline
# only cancels a parked verb, a false approval runs one.
_NO_RE = re.compile(
    r"^(?:no|nope|nah|don'?t(?: do it| bother)?|do not|cancel|stop"
    r"|skip(?: it)?|forget it|never ?mind|negative|leave it"
    r"|no thanks?|no thank you|not now)$")


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


def _normalize(text: str, fold: bool = True) -> str:
    """Trim the noise around a command. `fold=False` runs the identical pipeline
    without lowercasing, so an extracted argument keeps the case the user typed
    or said — see detect() for why that matters."""
    t = (text or "").strip(_PUNCT)
    if fold:
        t = t.lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\bhey jarvis\b[,\s]*", "", t, flags=re.I)
    t = re.sub(r"^(?:please|can you|could you|would you)\s+", "",
               t.strip(_PUNCT), flags=re.I)
    # …and a TRAILING "please", which only the leading form was stripping —
    # "mute the computer please" is the same command and was reaching a model.
    t = re.sub(r"[,\s]+please$", "", t.strip(_PUNCT), flags=re.I)
    return t.strip(_PUNCT)


def detect(text: str) -> Intent | None:
    """Intent | None. Pure — no I/O, safe to call twice (`Service.try_divert`
    gates on it, then `run_desktop` re-parses). Returns None for anything that
    isn't an unambiguous desktop command, so normal task routing is
    unaffected."""
    t = _normalize(text)
    # Same string with the user's capitalization intact. Matching decisions are
    # made on `t`; every EXTRACTED argument comes from `raw`, because the arg is
    # payload, not syntax: lowercasing it silently corrupted "copy Hello World
    # to my clipboard" into "hello world", and turned a URL with a case-
    # sensitive path (a YouTube video id) into a link to something else.
    raw = _normalize(text, fold=False)
    if not t or _VETO_RE.search(t):
        return None
    # a question mark means they're asking about it, not commanding it —
    # except the read verbs, which ARE questions
    asking = (text or "").strip().endswith("?")

    if _CLIP_GET_RE.match(t):
        return Intent("clipboard_get")
    if _STATUS_RE.match(t):
        return Intent("status")
    if _WINDOW_LIST_RE.match(t):
        return Intent("windows")
    if asking:
        return None

    m = _CLIP_SET_RE.match(raw)
    if m:
        return Intent("clipboard_set", arg=m.group("q").strip(_PUNCT))
    if _LOCK_RE.match(t):
        return Intent("lock")

    m = _MUTE_RE.match(t)
    if m:
        return Intent("mute", on=not (m.group("un") or m.group("un2")))
    if _SILENCE_RE.match(t):
        return Intent("mute", on=True)
    m = _SOUND_TOGGLE_RE.match(t)
    if m:
        return Intent("mute", on=(m.group("off") or m.group("off2")) == "off")

    for rx in (_VOL_SET_RE, _VOL_SET_ON_RE):
        m = rx.match(t)
        if m:
            level = _level_of(m)
            if level is None:
                return None        # "system volume 400" is not a real request
            return Intent("volume", number=level)
    if _VOL_MAX_RE.match(t):
        return Intent("volume", number=1.0)
    for rx in _VOL_REL_RES:
        m = rx.match(t)
        if m:
            down = m.group("dir") in _DOWN_WORDS
            return Intent("volume", delta=-VOLUME_STEP if down else VOLUME_STEP)

    for rx in _WINDOW_FOCUS_RES:
        m = rx.match(t)
        if m:
            target = m.group("q").strip(_PUNCT)
            # A bare generic is prose, not a target ("focus the window" about
            # a state machine, "switch to it" mid-conversation). Misses with a
            # REAL name fail honestly at the executor instead of here.
            if target and target not in _WINDOW_GENERIC_ARGS:
                return Intent("focus", arg=target)
            return None

    # A dotted token that isn't a plausible web address ("notes.md",
    # "org.gnome.Nautilus") falls through to the app branch rather than being
    # navigated to — see looks_like_url.
    m = _OPEN_URL_RE.match(raw)
    if m and looks_like_url(m.group("q")):
        return Intent("open", arg=m.group("q"))
    m = _LAUNCH_RE.match(raw) or _OPEN_APP_RE.match(raw)
    if m:
        target = m.group("q").strip(_PUNCT)
        # The old guard here was `len(target.split()) <= 3`, and its comment
        # claimed it stopped "open the door" — it never did: "the door" is two
        # words (verified against HEAD 2026-08-08, which returns
        # Intent(launch, 'the door'), likewise "open the window"/"open the
        # fridge"). An article-led phrase is prose, not an app name, so the
        # strong verbs now use the same lexical test the weak ones do.
        if target and _looks_like_app_name(target):
            return Intent("launch", arg=target)
    m = _LAUNCH_WEAK_RE.match(raw)
    if m and _looks_like_app_name(m.group("q").strip(_PUNCT)):
        return Intent("launch", arg=m.group("q").strip(_PUNCT))
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
    "windows": lambda i: _list_windows(),
    "focus": lambda i: _focus_window(i),
}


def _list_windows() -> str:
    from . import windows as _windows
    wins = _windows.list_windows()
    if not wins:
        return "No windows found."
    return "Open windows: " + "; ".join(
        f'{w["title"]} ({w["app"]})' for w in wins)


def _focus_window(intent: Intent) -> str:
    from . import windows as _windows
    try:
        return _windows.focus_window(intent.arg)
    except _windows.WindowError as e:
        raise DesktopError(str(e)) from None


def run_intent_ok(intent: Intent) -> tuple[bool, str]:
    """(acted, speakable sentence). run_intent() never raises, so a failed
    launch used to come back as a sorry-sentence the service filed 'done' —
    every parser miss ("open tasks", "run migrations") looked like success
    and was never seen by a model. The bool is what the service settles on;
    the sentence is what Jarvis speaks."""
    fn = _EXECUTORS.get(intent.verb)
    if fn is None:
        return False, "I don't know how to do that yet."
    try:
        return True, fn(intent)
    except DesktopError as e:
        return False, f"Sorry — {e}."
    except Exception:
        log.exception("desktop verb %s crashed", intent.verb)
        return False, "Sorry, that didn't work."


def run_intent(intent: Intent) -> str:
    """Execute and return a speakable sentence. Never raises — every failure
    becomes something Jarvis can say, exactly like spotify.run_intent.

    Blocking (subprocess); the service layer calls this in asyncio.to_thread.
    Policy is checked by the *caller* — this function assumes the verb was
    already allowed, so tests can exercise executors directly.
    """
    return run_intent_ok(intent)[1]
