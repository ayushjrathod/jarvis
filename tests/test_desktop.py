"""Computer-use tier T1: deterministic desktop control.

Everything here is LLM-free and side-effect-free — the parser and the policy
plane are pure functions, and the two tests that reach an executor stub
`desktop._run` rather than touching the real session. Nothing in this file may
lock a screen, change a volume, or launch an app.
"""

import asyncio
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

    def test_veto_and_prose(self):
        for s in ("is this open source", "let's start over", "run by me again",
                  "open a discussion about the roadmap", "lock in the date",
                  "open the door for the delivery guy",
                  "what did you think of the film", ""):
            self.assertIsNone(self.parse(s), s)

    def test_question_mark_vetoes_commands_but_not_reads(self):
        self.assertIsNone(self.parse("should I lock the screen?"))
        self.assertEqual(self.parse("what's on my clipboard?").verb, "clipboard_get")

    def test_detect_is_pure(self):
        # main.py gates on detect() then the service re-parses — same answer
        a, b = self.parse("lock the screen"), self.parse("lock the screen")
        self.assertEqual(a, b)


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


class TestOpenSchemeGuard(unittest.TestCase):
    """Scheme restriction is enforced at the executor, so it holds even if an
    operator sets open: allow."""

    def test_dangerous_schemes_refused_without_running_anything(self):
        with mock.patch.object(desktop, "_run") as run:
            for bad in ("javascript:alert(1)", "data:text/html,<script>",
                        "vscode://x", "ssh://box/x"):
                out = desktop.run_intent(desktop.Intent("open", arg=bad))
                self.assertIn("won't open", out, bad)
            run.assert_not_called()

    def test_bare_host_gets_https(self):
        with mock.patch.object(desktop, "_run") as run:
            desktop.run_intent(desktop.Intent("open", arg="example.com"))
            run.assert_called_once()
            self.assertEqual(run.call_args[0][0][1], "https://example.com")

    def test_allowed_schemes_pass_through_untouched(self):
        for good in ("https://example.org/x", "http://box:8765/health",
                     "file:///home/ayra/notes.md"):
            with mock.patch.object(desktop, "_run") as run:
                desktop.run_intent(desktop.Intent("open", arg=good))
                self.assertEqual(run.call_args[0][0][1], good, good)

    def test_host_port_is_not_read_as_a_scheme(self):
        with mock.patch.object(desktop, "_run") as run:
            desktop.run_intent(desktop.Intent("open", arg="example.com:8080/x"))
            self.assertEqual(run.call_args[0][0][1], "https://example.com:8080/x")

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


class TestVoiceAnswer(unittest.TestCase):
    """A parked confirmation is answerable by a bare yes/no — without this the
    confirm plane only works from the dashboard, and voice is the main UI."""

    CFG = {"enabled": True, "confirm_timeout_s": 120}

    def test_parse_answer(self):
        for yes in ("yes", "yeah", "yep", "sure", "go ahead", "do it", "Okay."):
            self.assertIs(desktop.parse_answer(yes), True, yes)
        for no in ("no", "nope", "don't", "cancel", "never mind", "skip it"):
            self.assertIs(desktop.parse_answer(no), False, no)
        for neither in ("what's the weather", "", "yesterday's brief"):
            self.assertIsNone(desktop.parse_answer(neither), neither)

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


if __name__ == "__main__":
    unittest.main()
