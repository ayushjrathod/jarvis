"""Computer-use tier T1: deterministic desktop control.

Everything here is LLM-free and side-effect-free — the parser and the policy
plane are pure functions, and the two tests that reach an executor stub
`desktop._run` rather than touching the real session. Nothing in this file may
lock a screen, change a volume, or launch an app.
"""

import asyncio
import contextlib
import unittest
from unittest import mock

from dispatcher import desktop


class TestDetect(unittest.TestCase):
    def parse(self, text):
        return desktop.detect(text)

    def test_lock(self):
        for s in ("lock the screen", "lock", "hey jarvis, lock the computer",
                  "please lock the session", "Lock the screen."):
            i = self.parse(s)
            self.assertIsNotNone(i, s)
            self.assertEqual(i.verb, "lock", s)

    def test_system_volume_absolute(self):
        i = self.parse("set the system volume to 40")
        self.assertEqual((i.verb, i.number), ("volume", 0.4))
        self.assertEqual(self.parse("master volume 75%").number, 0.75)

    def test_system_volume_relative(self):
        i = self.parse("turn the system volume up")
        self.assertEqual(i.verb, "volume")
        self.assertGreater(i.delta, 0)
        self.assertLess(self.parse("computer volume down").delta, 0)

    def test_bare_volume_is_left_to_spotify(self):
        # spotify.detect owns "volume 40" (player volume) and its divert runs
        # first; without the system/master qualifier the two would fight.
        for s in ("volume 40", "turn it up", "louder", "volume up"):
            self.assertIsNone(self.parse(s), s)

    def test_nonsense_volume_rejected(self):
        self.assertIsNone(self.parse("system volume 400"))

    def test_mute(self):
        self.assertIs(self.parse("mute the system").on, True)
        self.assertIs(self.parse("unmute the audio").on, False)
        self.assertIs(self.parse("mute the speakers").on, True)

    def test_launch(self):
        i = self.parse("open firefox")
        self.assertEqual((i.verb, i.arg), ("launch", "firefox"))
        self.assertEqual(self.parse("launch text editor").arg, "text editor")
        self.assertEqual(self.parse("fire up spotify").arg, "spotify")

    def test_open_url(self):
        i = self.parse("open github.com")
        self.assertEqual((i.verb, i.arg), ("open", "github.com"))
        self.assertEqual(self.parse("go to https://example.org/x").verb, "open")

    def test_clipboard(self):
        self.assertEqual(self.parse("what's on my clipboard").verb, "clipboard_get")
        self.assertEqual(self.parse("read my clipboard").verb, "clipboard_get")
        i = self.parse("copy hello world to my clipboard")
        self.assertEqual((i.verb, i.arg), ("clipboard_set", "hello world"))

    def test_status(self):
        self.assertEqual(self.parse("is the screen locked").verb, "status")
        self.assertEqual(self.parse("what's the system volume").verb, "status")

    def test_open_url_needs_a_real_scheme_or_tld(self):
        # "open notes.md" used to become `xdg-open https://notes.md` — a local
        # filename turned into a web request. Bare dotted names belong to the
        # app branch, which fails reversibly.
        for s, arg in (("open notes.md", "notes.md"),
                       ("open config.yaml", "config.yaml"),
                       ("open org.gnome.Nautilus", "org.gnome.Nautilus")):
            i = self.parse(s)
            self.assertEqual((i.verb, i.arg), ("launch", arg), s)

    def test_real_urls_still_open(self):
        for s, arg in (("open news.ycombinator.com", "news.ycombinator.com"),
                       ("visit https://x.com/a/b", "https://x.com/a/b"),
                       ("go to example.com:8080/path", "example.com:8080/path"),
                       ("open youtube.com/watch?v=dQw4w9WgXcQ",
                        "youtube.com/watch?v=dQw4w9WgXcQ")):
            i = self.parse(s)
            self.assertEqual((i.verb, i.arg), ("open", arg), s)

    def test_looks_like_url(self):
        for yes in ("https://x.com/a/b", "file:///home/ayra/notes.md",
                    "example.com", "example.com:8080/path",
                    "news.ycombinator.com", "sub.domain.co.uk/x?y=1"):
            self.assertTrue(desktop.looks_like_url(yes), yes)
        for no in ("notes.md", "config.yaml", "org.gnome.Nautilus",
                   "main.py", "index.js", "backup.tar.gz"):
            self.assertFalse(desktop.looks_like_url(no), no)

    def test_veto_and_prose(self):
        for s in ("is this open source", "let's start over", "run by me again",
                  "open a discussion about the roadmap", "lock in the date",
                  "open the door for the delivery guy",
                  "what did you think of the film", ""):
            self.assertIsNone(self.parse(s), s)

    def test_a_short_article_led_phrase_is_prose_not_an_app(self):
        # The old guard was `len(target.split()) <= 3` and its comment claimed
        # it stopped "open the door" — it never did, because "the door" is two
        # words. Verified against HEAD 2026-08-08: it returned
        # Intent(launch, 'the door'). Only the LONGER phrasing was ever caught,
        # which is why the existing prose test above didn't notice.
        for s in ("open the door", "open the window", "open the fridge",
                  "open the blinds"):
            self.assertIsNone(self.parse(s), s)

    def test_real_app_names_survive_the_prose_guard(self):
        for s, arg in (("open firefox", "firefox"),
                       ("open tor browser", "tor browser"),
                       ("open toolbox", "toolbox"),
                       ("open notes.md", "notes.md"),
                       ("open Visual Studio Code", "Visual Studio Code")):
            got = self.parse(s)
            self.assertIsNotNone(got, s)
            self.assertEqual((got.verb, got.arg), ("launch", arg), s)

    def test_question_mark_vetoes_commands_but_not_reads(self):
        self.assertIsNone(self.parse("should I lock the screen?"))
        self.assertEqual(self.parse("what's on my clipboard?").verb, "clipboard_get")

    def test_detect_is_pure(self):
        # main.py gates on detect() then the service re-parses — same answer
        a, b = self.parse("lock the screen"), self.parse("lock the screen")
        self.assertEqual(a, b)


class TestWeakLaunchVerbs(unittest.TestCase):
    """`run` and `start` are ordinary English imperatives, not just launch
    verbs. Having them in the bare alternation made "run the tests" an
    Intent(launch, 'the tests') — harmless while the divert lived in the HTTP
    handler, destructive once it moved onto Service.submit(), where a queue
    file saying "run the migration" is swallowed and recorded done."""

    def test_ordinary_imperatives_fall_through(self):
        for s in ("run the tests", "start the deployment", "run the migration",
                  "start writing the report", "run the backup script",
                  "start a new branch", "run all of them again",
                  "start writing", "run it for me"):
            self.assertIsNone(desktop.detect(s), s)

    def test_unambiguous_launch_verbs_are_untouched(self):
        for s, arg in (("launch firefox", "firefox"),
                       ("open up spotify", "spotify"),
                       ("fire up gimp", "gimp"),
                       ("launch text editor", "text editor")):
            i = desktop.detect(s)
            self.assertEqual((i.verb, i.arg), ("launch", arg), s)

    def test_app_shaped_weak_verbs_still_launch(self):
        # the whole reason `run`/`start` are kept rather than deleted
        for s, arg in (("run outlook", "outlook"),
                       ("start overwatch", "overwatch"),
                       ("start Visual Studio Code", "Visual Studio Code"),
                       ("run gimp", "gimp")):
            i = desktop.detect(s)
            self.assertEqual((i.verb, i.arg), ("launch", arg), s)

    def test_looks_like_app_name(self):
        for yes in ("firefox", "tor browser", "Visual Studio Code", "gimp"):
            self.assertTrue(desktop._looks_like_app_name(yes), yes)
        for no in ("the tests", "a new branch", "writing the report",
                   "writing", "some very long thing indeed", ""):
            self.assertFalse(desktop._looks_like_app_name(no), no)


class TestVetoWordBoundaries(unittest.TestCase):
    """VETO used to match as a bare substring, so every short entry was a
    prefix of real app names — "open to" ⊂ "open toolbox", "run out" ⊂ "run
    outlook", "start over" ⊂ "start overwatch". Those commands silently
    returned None, which is the worst failure shape here: a vetoed command
    just goes to the model, so nobody sees what was dropped."""

    def test_commands_that_merely_start_with_a_veto_prefix_work(self):
        for s, arg in (("open toolbox", "toolbox"),
                       ("open tor browser", "tor browser"),
                       ("run outlook", "outlook"),
                       ("start overwatch", "overwatch"),
                       ("open sourcetree", "sourcetree"),
                       ("start upwork", "upwork")):
            i = desktop.detect(s)
            self.assertIsNotNone(i, s)
            self.assertEqual((i.verb, i.arg), ("launch", arg), s)

    def test_every_veto_entry_still_vetoes(self):
        # word boundaries must not have loosened any existing idiom
        for phrase in desktop.VETO:
            self.assertTrue(desktop._VETO_RE.search(phrase), phrase)
            self.assertIsNone(desktop.detect(phrase), phrase)

    def test_the_idioms_in_real_sentences(self):
        for s in ("is this open source", "that's an open question",
                  "are you open to a rewrite", "he wouldn't open up about it",
                  "let's start over", "start with the basics",
                  "start from scratch", "let's run through the agenda",
                  "run by me again", "don't run out of coffee",
                  "we ran into trouble on the run through",
                  "open a discussion about the roadmap",
                  "open the floor to questions", "lock in the date",
                  "the date is locked in", "start a conversation with them",
                  "open the door for the delivery guy"):
            self.assertIsNone(desktop.detect(s), s)


class TestPolicy(unittest.TestCase):
    CFG = {"enabled": True}

    def test_defaults(self):
        self.assertEqual(desktop.policy(self.CFG, "lock"), "allow")
        self.assertEqual(desktop.policy(self.CFG, "volume"), "allow")
        # the two that read user data / reach the network
        self.assertEqual(desktop.policy(self.CFG, "clipboard_get"), "confirm")
        self.assertEqual(desktop.policy(self.CFG, "open"), "confirm")

    def test_disabled_block_denies_everything(self):
        for block in ({}, None, {"enabled": False}):
            for verb in desktop.ALL_VERBS:
                self.assertEqual(desktop.policy(block, verb), "deny")

    def test_unknown_verb_and_value_fail_closed(self):
        self.assertEqual(desktop.policy(self.CFG, "rm_rf"), "deny")
        cfg = {"enabled": True, "policy": {"lock": "sure why not"}}
        self.assertEqual(desktop.policy(cfg, "lock"), "deny")

    def test_operator_override(self):
        cfg = {"enabled": True, "policy": {"open": "allow", "lock": "deny"}}
        self.assertEqual(desktop.policy(cfg, "open"), "allow")
        self.assertEqual(desktop.policy(cfg, "lock"), "deny")

    def test_malformed_policy_denies_instead_of_raising(self):
        # `policy:` written as a YAML list raised AttributeError out of .get,
        # which the callers turn into a 500 — a config typo must fail closed,
        # not fail loudly and leave the operator guessing what is live.
        for bad in (["lock"], "allow", 7, []):
            cfg = {"enabled": True, "policy": bad}
            for verb in desktop.ALL_VERBS:
                self.assertEqual(desktop.policy(cfg, verb), "deny", (bad, verb))

    def test_absent_policy_key_still_uses_the_defaults(self):
        cfg = {"enabled": True, "policy": None}      # `policy:` with no value
        self.assertEqual(desktop.policy(cfg, "lock"), "allow")
        self.assertEqual(desktop.policy(cfg, "open"), "confirm")


class TestOpenSchemeGuard(unittest.TestCase):
    """Scheme restriction is enforced at the executor, so it holds even if an
    operator sets open: allow."""

    @contextlib.contextmanager
    def opened(self):
        """Patch the two seams `open` reaches: a stubbed session (so the test
        never asks the real one) and a stubbed spawn (so nothing launches)."""
        with mock.patch.object(desktop, "session_env",
                               return_value={"DISPLAY": ":0"}), \
             mock.patch.object(desktop, "spawn_app", return_value=0) as spawn:
            yield spawn

    def test_dangerous_schemes_refused_without_running_anything(self):
        with self.opened() as spawn:
            for bad in ("javascript:alert(1)", "data:text/html,<script>",
                        "vscode://x", "ssh://box/x"):
                out = desktop.run_intent(desktop.Intent("open", arg=bad))
                self.assertIn("won't open", out, bad)
            spawn.assert_not_called()

    def test_bare_host_gets_https(self):
        with self.opened() as spawn:
            desktop.run_intent(desktop.Intent("open", arg="example.com"))
            spawn.assert_called_once()
            self.assertEqual(spawn.call_args[0][0][1], "https://example.com")

    def test_allowed_schemes_pass_through_untouched(self):
        for good in ("https://example.org/x", "http://box:8765/health",
                     "file:///home/ayra/notes.md"):
            with self.opened() as spawn:
                desktop.run_intent(desktop.Intent("open", arg=good))
                self.assertEqual(spawn.call_args[0][0][1], good, good)

    def test_host_port_is_not_read_as_a_scheme(self):
        with self.opened() as spawn:
            desktop.run_intent(desktop.Intent("open", arg="example.com:8080/x"))
            self.assertEqual(spawn.call_args[0][0][1],
                             "https://example.com:8080/x")

    def test_open_without_a_session_is_refused(self):
        with mock.patch.object(desktop, "session_env", return_value={}), \
             mock.patch.object(desktop, "spawn_app") as spawn:
            out = desktop.run_intent(desktop.Intent("open", arg="example.com"))
        self.assertIn("desktop session", out)
        spawn.assert_not_called()

    def test_scheme_of(self):
        # the regression this guard exists for: an opaque scheme has no "//"
        self.assertEqual(desktop.scheme_of("javascript:alert(1)"), "javascript")
        self.assertEqual(desktop.scheme_of("data:text/html,x"), "data")
        self.assertEqual(desktop.scheme_of("mailto:a@b.c"), "mailto")
        self.assertEqual(desktop.scheme_of("example.com"), "")
        self.assertEqual(desktop.scheme_of("/home/ayra/x.md"), "")


class TestRunIntentNeverRaises(unittest.TestCase):
    def test_desktop_error_becomes_speech(self):
        with mock.patch.object(desktop, "_run",
                               side_effect=desktop.DesktopError("wpctl isn't installed")):
            out = desktop.run_intent(desktop.Intent("volume", number=0.4))
        self.assertTrue(out.startswith("Sorry"))
        self.assertIn("wpctl", out)

    def test_unexpected_error_becomes_speech(self):
        with mock.patch.object(desktop, "_run", side_effect=RuntimeError("boom")):
            out = desktop.run_intent(desktop.Intent("lock"))
        self.assertEqual(out, "Sorry, that didn't work.")

    def test_unknown_verb(self):
        self.assertIn("don't know", desktop.run_intent(desktop.Intent("fly")))

    def test_volume_is_clamped(self):
        with mock.patch.object(desktop, "_run", return_value="") as run:
            desktop.run_intent(desktop.Intent("volume", number=5.0))
        self.assertEqual(run.call_args[0][0][3], "1.00")


class TestScreenLocked(unittest.TestCase):
    """Lock state comes from the session bus, not `loginctl show-session self`:
    the dispatcher is a systemd user service, which belongs to
    user@1000.service rather than a login session, so loginctl always answers
    "Caller does not belong to any known session" and the old code silently
    reported nothing."""

    def test_parses_both_states(self):
        with mock.patch.object(desktop, "_run", return_value="(true,)\n"):
            self.assertIs(desktop.screen_locked(), True)
        with mock.patch.object(desktop, "_run", return_value="(false,)\n"):
            self.assertIs(desktop.screen_locked(), False)

    def test_unavailable_is_none_not_false(self):
        # "we couldn't tell" must not read as "definitely unlocked"
        with mock.patch.object(desktop, "_run",
                               side_effect=desktop.DesktopError("no gdbus")):
            self.assertIsNone(desktop.screen_locked())
        with mock.patch.object(desktop, "_run", return_value="weird output"):
            self.assertIsNone(desktop.screen_locked())

    def test_asks_the_session_bus(self):
        with mock.patch.object(desktop, "_run", return_value="(false,)") as run:
            desktop.screen_locked()
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "gdbus")
        self.assertIn("--session", argv)
        self.assertNotIn("loginctl", argv)

    def test_status_reports_lock_state(self):
        with mock.patch.object(desktop, "get_volume", return_value=(0.26, False)), \
             mock.patch.object(desktop, "screen_locked", return_value=True):
            self.assertIn("screen locked", desktop.run_intent(desktop.Intent("status")))
        with mock.patch.object(desktop, "get_volume", return_value=(0.26, False)), \
             mock.patch.object(desktop, "screen_locked", return_value=None):
            out = desktop.run_intent(desktop.Intent("status"))
        self.assertNotIn("screen", out)          # unknown → say nothing about it
        self.assertIn("26%", out)


class TestResolveApp(unittest.TestCase):
    ENTRIES = {"firefox": "firefox", "files": "org.gnome.Nautilus",
               "libreoffice files demo": "lo-demo", "text editor": "org.gnome.TextEditor"}

    def resolve(self, q):
        with mock.patch.object(desktop, "_desktop_entries", return_value=self.ENTRIES):
            return desktop.resolve_app(q)

    def test_exact_match_beats_substring(self):
        # "files" must not be stolen by "libreoffice files demo"
        self.assertEqual(self.resolve("files"), "org.gnome.Nautilus")

    def test_prefix_and_substring(self):
        self.assertEqual(self.resolve("text"), "org.gnome.TextEditor")
        self.assertEqual(self.resolve("firef"), "firefox")

    def test_desktop_id_suffix(self):
        self.assertEqual(self.resolve("nautilus"), "org.gnome.Nautilus")

    def test_no_match(self):
        self.assertIsNone(self.resolve("photoshop"))
        self.assertIsNone(self.resolve(""))


class TestSessionEnv(unittest.TestCase):
    """The dispatcher is a systemd user service; started at boot it has no
    DISPLAY at all, and a GUI app spawned without one dies on startup."""

    MANAGER = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0",
               "XAUTHORITY": "/run/user/1000/.mutter-Xwaylandauth.AAA"}

    def test_missing_vars_come_from_the_manager(self):
        with mock.patch.dict(desktop.os.environ, {"HOME": "/home/ayra"}, clear=True), \
             mock.patch.object(desktop, "_manager_environment",
                               return_value=self.MANAGER) as m:
            env = desktop.session_env()
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
        self.assertEqual(env["HOME"], "/home/ayra")     # our own env survives
        m.assert_called_once()                          # asked once, not per var

    def test_manager_not_asked_when_env_is_complete(self):
        full = {v: "x" for v in desktop.SESSION_VARS}
        with mock.patch.dict(desktop.os.environ, full, clear=True), \
             mock.patch.object(desktop, "_manager_environment") as m:
            desktop.session_env()
        m.assert_not_called()

    def test_our_own_value_wins(self):
        with mock.patch.dict(desktop.os.environ, {"DISPLAY": ":9"}, clear=True), \
             mock.patch.object(desktop, "_manager_environment",
                               return_value=self.MANAGER):
            self.assertEqual(desktop.session_env()["DISPLAY"], ":9")

    def test_manager_parsing_skips_quoted_values(self):
        out = ("DISPLAY=:0\n"
               "QT_IM_MODULES=$'wayland;ibus'\n"      # systemd's own escaping
               "not a variable line\n")
        with mock.patch.object(desktop, "_run", return_value=out):
            env = desktop._manager_environment()
        self.assertEqual(env, {"DISPLAY": ":0"})

    def test_unreadable_manager_is_not_fatal(self):
        with mock.patch.object(desktop, "_run",
                               side_effect=desktop.DesktopError("nope")):
            self.assertEqual(desktop._manager_environment(), {})


class TestLaunch(unittest.TestCase):
    """`gtk-launch` exits 0 whether or not the app lived, so the executor
    checks for the process itself — the failure that hid this bug was a
    cheerful "Opening firefox." over an app that never appeared."""

    def launch(self, *, appeared, env=None, spawn_rc=0):
        with mock.patch.object(desktop, "resolve_app", return_value="firefox"), \
             mock.patch.object(desktop, "session_env",
                               return_value=env if env is not None
                               else {"WAYLAND_DISPLAY": "wayland-0"}), \
             mock.patch.object(desktop, "process_token", return_value="firefox"), \
             mock.patch.object(desktop, "spawn_app", return_value=spawn_rc) as spawn, \
             mock.patch.object(desktop, "_process_running",
                               side_effect=[False, *appeared]), \
             mock.patch.object(desktop, "LAUNCH_SETTLE_S", 0), \
             mock.patch.object(desktop, "time", mock.Mock(monotonic=lambda: 0,
                                                          sleep=lambda s: None)):
            return desktop.run_intent(desktop.Intent("launch", arg="firefox")), spawn

    def test_launch_that_starts(self):
        out, spawn = self.launch(appeared=[True])
        self.assertEqual(out, "Opening firefox.")
        self.assertEqual(spawn.call_args[0][0][:1], ["gtk-launch"])

    def test_launch_that_never_appears_is_reported(self):
        out, _ = self.launch(appeared=[False])
        self.assertIn("didn't start", out)

    def test_no_display_is_refused_before_spawning(self):
        out, spawn = self.launch(appeared=[True], env={"HOME": "/home/ayra"})
        self.assertIn("desktop session", out)
        spawn.assert_not_called()

    def test_launcher_failure_is_reported(self):
        out, _ = self.launch(appeared=[True], spawn_rc=1)
        self.assertIn("couldn't launch", out)

    def test_unknown_app(self):
        with mock.patch.object(desktop, "resolve_app", return_value=None):
            out = desktop.run_intent(desktop.Intent("launch", arg="photoshop"))
        self.assertIn("couldn't find", out)


class TestProcessToken(unittest.TestCase):
    def token(self, exec_line, ident="com.example.App"):
        with mock.patch.object(desktop, "_exec_line", return_value=exec_line):
            return desktop.process_token(ident)

    def test_plain_binary(self):
        self.assertEqual(self.token("/usr/lib/firefox/firefox %u"), "firefox")

    def test_wrapper_falls_back_to_the_desktop_id(self):
        # "flatpak" identifies nothing; the app id is what shows up in the args
        self.assertEqual(
            self.token("/usr/bin/flatpak run --branch=stable com.example.App @@u"),
            "com.example.App")

    def test_unreadable_entry_falls_back_to_the_desktop_id(self):
        self.assertEqual(self.token(""), "com.example.App")


class TestSpawnApp(unittest.TestCase):
    def spawn(self, has_systemd_run=True):
        proc = mock.Mock(wait=mock.Mock(return_value=0))
        which = (lambda c: f"/usr/bin/{c}") if has_systemd_run else \
            (lambda c: None if c == "systemd-run" else f"/usr/bin/{c}")
        with mock.patch.object(desktop.shutil, "which", side_effect=which), \
             mock.patch.object(desktop.subprocess, "Popen",
                               return_value=proc) as popen:
            rc = desktop.spawn_app(["gtk-launch", "firefox"], env={"DISPLAY": ":0"})
        return rc, popen.call_args

    def test_wrapped_in_a_transient_scope(self):
        # otherwise `systemctl --user restart mission-dispatcher` kills every
        # app the dispatcher opened — they'd sit in its cgroup
        rc, call = self.spawn()
        argv = call[0][0]
        self.assertEqual(rc, 0)
        self.assertIn("--scope", argv)
        self.assertEqual(argv[-2:], ["gtk-launch", "firefox"])

    def test_falls_back_when_systemd_run_is_absent(self):
        _, call = self.spawn(has_systemd_run=False)
        self.assertEqual(call[0][0], ["/usr/bin/gtk-launch", "firefox"])

    def test_output_is_never_piped(self):
        # a piped app holds the pipe open for its whole life, so capturing it
        # makes a *successful* launch block until the timeout
        _, call = self.spawn()
        self.assertEqual(call[1]["stdout"], desktop.subprocess.DEVNULL)
        self.assertEqual(call[1]["stderr"], desktop.subprocess.DEVNULL)


class _Svc:
    """The three Service methods under test, without a database or event bus."""

    def __init__(self, computer):
        from dispatcher.service import Service
        self.cfg = mock.Mock(computer=computer)
        self.pending_desktop = {}
        self.hooks = mock.Mock(fire=mock.AsyncMock())
        for name in ("desktop_command", "run_desktop_intent", "confirm_desktop",
                     "pending_desktop_for", "_expire_desktop_confirms"):
            setattr(self, name, getattr(Service, name).__get__(self))


class TestConfirmFlow(unittest.TestCase):
    CFG = {"enabled": True, "confirm_timeout_s": 120}

    def run_cmd(self, svc, text):
        return asyncio.run(svc.desktop_command(text, "voice"))

    def test_allowed_verb_runs_immediately(self):
        svc = _Svc(self.CFG)
        with mock.patch.object(desktop, "run_intent", return_value="Locking the screen."):
            out = self.run_cmd(svc, "lock the screen")
        self.assertEqual(out["status"], "done")
        self.assertEqual(svc.pending_desktop, {})

    def test_confirm_verb_parks_and_does_not_run(self):
        svc = _Svc(self.CFG)
        with mock.patch.object(desktop, "run_intent") as run:
            out = self.run_cmd(svc, "what's on my clipboard")
            run.assert_not_called()
        self.assertEqual(out["status"], "needs_confirmation")
        self.assertIn("Shall I read your clipboard?", out["speech"])
        self.assertEqual(len(svc.pending_desktop), 1)
        svc.hooks.fire.assert_awaited()           # the SSE `confirm` event
        payload = svc.hooks.fire.await_args[0][0]
        self.assertEqual(payload["event"], "confirm")
        self.assertEqual(payload["confirm_id"], out["confirm_id"])

        with mock.patch.object(desktop, "run_intent", return_value="Clipboard: hi"):
            done = asyncio.run(svc.confirm_desktop(out["confirm_id"], True))
        self.assertEqual(done["status"], "done")
        self.assertEqual(svc.pending_desktop, {})   # consumed, not replayable

    def test_declining_does_not_run(self):
        svc = _Svc(self.CFG)
        out = self.run_cmd(svc, "read my clipboard")
        with mock.patch.object(desktop, "run_intent") as run:
            done = asyncio.run(svc.confirm_desktop(out["confirm_id"], False))
            run.assert_not_called()
        self.assertEqual(done["status"], "declined")

    def test_unknown_or_expired_id_never_executes(self):
        svc = _Svc(self.CFG)
        with mock.patch.object(desktop, "run_intent") as run:
            self.assertEqual(asyncio.run(svc.confirm_desktop("nope", True))["status"],
                             "expired")
            out = self.run_cmd(svc, "read my clipboard")
            svc.pending_desktop[out["confirm_id"]]["expires"] = 0   # time passes
            self.assertEqual(
                asyncio.run(svc.confirm_desktop(out["confirm_id"], True))["status"],
                "expired")
            run.assert_not_called()

    def test_denied_verb_refuses(self):
        svc = _Svc({"enabled": True, "policy": {"lock": "deny"}})
        with mock.patch.object(desktop, "run_intent") as run:
            out = self.run_cmd(svc, "lock the screen")
            run.assert_not_called()
        self.assertEqual(out["status"], "denied")

    def test_feature_off_denies_every_verb(self):
        svc = _Svc({})
        with mock.patch.object(desktop, "run_intent") as run:
            self.assertEqual(self.run_cmd(svc, "lock the screen")["status"], "denied")
            run.assert_not_called()

    def test_unrecognized_text(self):
        svc = _Svc(self.CFG)
        self.assertEqual(self.run_cmd(svc, "what's the weather")["status"],
                         "unrecognized")

    def test_ordinary_imperatives_are_not_desktop_commands(self):
        # try_divert runs on Service.submit() now, so a queue file saying "run
        # the migration" that parses as a launch is answered by the desktop
        # tier and recorded done — the work silently never happens.
        svc = _Svc(self.CFG)
        for text in ("run the tests", "start the deployment",
                     "run the migration", "start writing the report"):
            with mock.patch.object(desktop, "run_intent") as run:
                out = self.run_cmd(svc, text)
                run.assert_not_called()
            self.assertEqual(out["status"], "unrecognized", text)


class TestVoiceAnswer(unittest.TestCase):
    """A parked confirmation is answerable by a bare yes/no — without this the
    confirm plane only works from the dashboard, and voice is the main UI."""

    CFG = {"enabled": True, "confirm_timeout_s": 120}

    def test_parse_answer(self):
        for yes in ("yes", "yeah", "yep", "yup", "sure", "go ahead", "do it",
                    "please do", "alright", "that's fine", "confirmed",
                    "Okay."):
            self.assertIs(desktop.parse_answer(yes), True, yes)
        for no in ("no", "nope", "nah", "don't", "cancel", "never mind",
                   "skip it", "no thanks", "not now", "leave it"):
            self.assertIs(desktop.parse_answer(no), False, no)
        for neither in ("what's the weather", "", "yesterday's brief"):
            self.assertIsNone(desktop.parse_answer(neither), neither)

    def test_an_answer_is_the_WHOLE_utterance(self):
        """The confirm window is 120s wide and try_divert runs parse_answer on
        every utterance from that source, so a `\\b` prefix match meant any
        sentence *beginning* with an affirmation approved the parked verb —
        and spoken English begins sentences that way constantly. "what's on my
        clipboard" followed by "okay so what's on my calendar" read the
        clipboard and swallowed the real question with it."""
        for s in ("okay so what is the weather today",
                  "alright I think we are done here",
                  "sure, that makes sense",
                  "yes I told him that yesterday",
                  "fine, but first show me the brief",
                  "no idea what that means",
                  "stop the dispatcher and rebuild it",
                  "cancel the 7am automation",
                  "don't forget the weekly review"):
            self.assertIsNone(desktop.parse_answer(s), s)

    def test_a_follow_up_question_does_not_approve_the_parked_verb(self):
        # the end-to-end shape of the bug above, through the service
        svc = _Svc(self.CFG)
        parked = asyncio.run(svc.desktop_command("what's on my clipboard", "voice"))
        self.assertEqual(parked["status"], "needs_confirmation")
        with mock.patch.object(desktop, "run_intent") as run:
            out = asyncio.run(
                svc.desktop_command("okay so what's on my calendar", "voice"))
            run.assert_not_called()
        self.assertEqual(out["status"], "unrecognized")
        self.assertEqual(len(svc.pending_desktop), 1)   # still parked, unanswered

    def test_yes_runs_the_parked_verb(self):
        svc = _Svc(self.CFG)
        out = asyncio.run(svc.desktop_command("read my clipboard", "voice"))
        self.assertEqual(out["status"], "needs_confirmation")
        with mock.patch.object(desktop, "run_intent", return_value="Clipboard: hi") as run:
            done = asyncio.run(svc.desktop_command("yeah", "voice"))
            run.assert_called_once()
        self.assertEqual(done["status"], "done")
        self.assertEqual(svc.pending_desktop, {})

    def test_no_declines_the_parked_verb(self):
        svc = _Svc(self.CFG)
        asyncio.run(svc.desktop_command("read my clipboard", "voice"))
        with mock.patch.object(desktop, "run_intent") as run:
            done = asyncio.run(svc.desktop_command("nope", "voice"))
            run.assert_not_called()
        self.assertEqual(done["status"], "declined")

    def test_answer_only_applies_to_the_same_source(self):
        svc = _Svc(self.CFG)
        asyncio.run(svc.desktop_command("read my clipboard", "voice"))
        with mock.patch.object(desktop, "run_intent") as run:
            other = asyncio.run(svc.desktop_command("yes", "ui"))
            run.assert_not_called()
        self.assertEqual(other["status"], "unrecognized")
        self.assertEqual(len(svc.pending_desktop), 1)   # still voice's to answer

    def test_stray_yes_without_a_pending_confirm_does_nothing(self):
        svc = _Svc(self.CFG)
        with mock.patch.object(desktop, "run_intent") as run:
            out = asyncio.run(svc.desktop_command("yes", "voice"))
            run.assert_not_called()
        self.assertEqual(out["status"], "unrecognized")

    def test_pending_lookup_ignores_expired(self):
        svc = _Svc(self.CFG)
        out = asyncio.run(svc.desktop_command("read my clipboard", "voice"))
        svc.pending_desktop[out["confirm_id"]]["expires"] = 0
        self.assertIsNone(svc.pending_desktop_for("voice"))


class TestArgumentsKeepTheirCase(unittest.TestCase):
    """Matching is case-insensitive; the extracted ARGUMENT is payload and must
    survive verbatim. Normalizing lowercased both, which silently corrupted
    clipboard text and rewrote case-sensitive URL paths into other resources."""

    def test_clipboard_text_is_not_folded(self):
        i = desktop.detect("copy Hello World ASAP to my clipboard")
        self.assertEqual(i.verb, "clipboard_set")
        self.assertEqual(i.arg, "Hello World ASAP")

    def test_url_path_case_survives(self):
        # a YouTube id differing only in case is a different video
        i = desktop.detect("open youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(i.arg, "youtube.com/watch?v=dQw4w9WgXcQ")

    def test_app_name_case_survives_prefix_stripping(self):
        for phrase, want in (("hey jarvis, open Firefox", "Firefox"),
                             ("Please launch GIMP", "GIMP"),
                             ("start Visual Studio Code", "Visual Studio Code")):
            with self.subTest(phrase=phrase):
                self.assertEqual(desktop.detect(phrase).arg, want)

    def test_matching_is_still_case_insensitive(self):
        self.assertEqual(desktop.detect("LOCK THE SCREEN").verb, "lock")
        self.assertEqual(desktop.detect("System Volume 40").number, 0.4)

    def test_vetoes_still_apply_to_mixed_case(self):
        self.assertIsNone(desktop.detect("Open Source alternatives to Slack"))


if __name__ == "__main__":
    unittest.main()
