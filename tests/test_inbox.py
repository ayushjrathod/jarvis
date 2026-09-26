"""Inbox arrival watcher — the first event-driven (not clock-driven) trigger.

`InboxWatcher.step` is the whole policy and is a pure function over successive
scans, so none of this needs a filesystem, a clock, or a dispatcher.
"""

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dispatcher import inbox
from dispatcher.inbox import InboxWatcher, scan


class TestStep(unittest.TestCase):
    def setUp(self):
        self.w = InboxWatcher()

    def test_first_pass_seeds_without_firing(self):
        # a restart must not re-announce everything already sitting there
        self.assertEqual(self.w.step({"old.pdf": (1.0, 10)}), ([], []))
        self.assertEqual(self.w.step({"old.pdf": (1.0, 10)}), ([], []))

    def test_new_file_fires_only_after_it_settles(self):
        self.w.step({})
        self.assertEqual(self.w.step({"a.pdf": (1.0, 100)})[0], [])     # seen once
        self.assertEqual(self.w.step({"a.pdf": (1.0, 100)})[0], ["a.pdf"])
        self.assertEqual(self.w.step({"a.pdf": (1.0, 100)})[0], [])     # not again

    def test_file_still_being_written_does_not_fire(self):
        self.w.step({})
        for size in (100, 5000, 20000):          # a big PDF landing in chunks
            self.assertEqual(self.w.step({"big.pdf": (1.0, size)})[0], [])
        self.assertEqual(self.w.step({"big.pdf": (1.0, 20000)})[0], ["big.pdf"])

    def test_modified_file_fires_again(self):
        self.w.step({"a.md": (1.0, 10)})
        self.assertEqual(self.w.step({"a.md": (2.0, 20)})[0], [])
        self.assertEqual(self.w.step({"a.md": (2.0, 20)})[0], ["a.md"])

    def test_several_files_fire_together_sorted(self):
        self.w.step({})
        self.w.step({"b.md": (1.0, 1), "a.md": (1.0, 1)})
        self.assertEqual(self.w.step({"b.md": (1.0, 1), "a.md": (1.0, 1)})[0],
                         ["a.md", "b.md"])

    def test_file_removed_mid_settle_is_forgotten(self):
        self.w.step({})
        self.w.step({"gone.md": (1.0, 1)})
        self.assertEqual(self.w.step({}), ([], []))
        self.assertEqual(self.w.pending, {})

    def test_removals_are_reported_separately_from_arrivals(self):
        # a deletion needs a reindex (else the file stays searchable after it's
        # gone) but must not produce an "indexed a new file" notification
        self.w.step({"a.md": (1.0, 1), "b.md": (1.0, 1)})
        arrived, removed = self.w.step({"a.md": (1.0, 1)})
        self.assertEqual((arrived, removed), ([], ["b.md"]))

    def test_deleted_file_returning_fires_again(self):
        self.w.step({})
        self.w.step({"a.md": (1.0, 1)})
        self.w.step({"a.md": (1.0, 1)})           # fired
        self.assertEqual(self.w.step({}), ([], ["a.md"]))  # deleted -> reindex
        self.w.step({"a.md": (1.0, 1)})
        self.assertEqual(self.w.step({"a.md": (1.0, 1)})[0], ["a.md"])


class TestScan(unittest.TestCase):
    def test_skips_readme_dotfiles_and_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "README.md").write_text("the convention doc")
            (root / ".hidden").write_text("x")
            (root / "sub").mkdir()
            (root / "sub" / "note.md").write_text("real content")
            (root / "top.txt").write_text("also real")
            names = set(scan(root))
        self.assertEqual(names, {"top.txt", "sub/note.md"})

    def test_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(scan(Path("/nonexistent/inbox")), {})

    def test_reports_mtime_and_size(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.txt"
            p.write_text("hello")
            (mtime, size) = scan(Path(d))["a.txt"]
        self.assertEqual(size, 5)
        self.assertGreater(mtime, 0)


def _fake_svc(**inbox_over):
    """A dispatcher stand-in. `create_task` and `_collect_quick` are the two
    seams `_summarize` reaches through — no DB, no model, no subprocess."""
    icfg = {"enabled": True, "dir": "vault/inbox", "notify": True}
    icfg.update(inbox_over)
    svc = mock.Mock(db=mock.Mock(), hooks=mock.Mock(fire=mock.AsyncMock()))
    svc.cfg = SimpleNamespace(inbox=icfg, root=Path("/nonexistent"),
                              memory={"index_dirs": ["vault"]})
    svc.created = []
    svc.sleeps = []

    async def create_task(text, source, kind, area=None, metadata=None,
                          trusted=False):
        task = {"id": f"t{len(svc.created)}", "text": text, "source": source}
        svc.created.append(task)
        return task

    svc.create_task = create_task
    svc._collect_quick = mock.AsyncMock(return_value=("A tax invoice.", "done"))
    return svc


async def _drive(svc, scans):
    """Run inbox.watch through exactly len(scans) polls, then stop it.
    Returns the patched notify.send_desktop."""
    pending = list(scans)

    async def stop_after_the_scripted_polls(interval):
        svc.sleeps.append(interval)
        if len(svc.sleeps) >= len(scans):
            raise asyncio.CancelledError

    with mock.patch.object(inbox, "scan", lambda d: pending.pop(0)), \
         mock.patch("dispatcher.ingest.ingest_vault",
                    return_value={"files_scanned": 1, "files_changed": 1,
                                  "added": 1, "deleted": 0, "dep_gated": 0,
                                  "unsupported": 0, "unparseable": 0}), \
         mock.patch("dispatcher.embeddings.embed_missing", return_value={}), \
         mock.patch("dispatcher.notify.send_desktop",
                    new=mock.AsyncMock()) as send, \
         mock.patch.object(inbox.asyncio, "sleep",
                           stop_after_the_scripted_polls):
        with contextlib.suppress(asyncio.CancelledError):
            await inbox.watch(svc)
    return send


def _spoken(send):
    return [c.args[0] for c in send.await_args_list]


def _notices(svc):
    return [c.args[0] for c in svc.hooks.fire.await_args_list
            if c.args[0].get("event") == "notify"]


class TestWatchNotifies(unittest.IsolatedAsyncioTestCase):
    """`step` returns (arrived, removed) and the loop acts on either, so the
    notify branch has to read `arrived` specifically — it didn't, and deleting
    a file announced "Indexed 0 files from your inbox — searchable now."
    """

    async def _drive(self, scans):
        svc = _fake_svc()
        return await _drive(svc, scans), svc

    async def test_removal_reindexes_but_says_nothing(self):
        send, svc = await self._drive([{"a.pdf": (1.0, 10)}, {}])
        send.assert_not_called()
        svc.hooks.fire.assert_not_awaited()

    async def test_arrival_still_announces_the_file_by_name(self):
        send, svc = await self._drive(
            [{}, {"report.pdf": (1.0, 99)}, {"report.pdf": (1.0, 99)}])
        send.assert_awaited_once()
        self.assertIn("report.pdf", send.await_args.args[0])
        svc.hooks.fire.assert_awaited_once()


class TestSummarizeDelivery(unittest.IsolatedAsyncioTestCase):
    """The defect was delivery, not creation. The summarize task ran fine, but
    source "inbox" isn't in automations.notify_sources, so notify.surfacing
    returns "surface" rather than "gate"; the answer left as a kind="quick"
    `done` event, and the voice brain only speaks kind == "agentic". You paid
    a cold `claude -p` per file and heard nothing. Asserting that a task was
    created would not have caught that — so these assert the notification.
    """

    ARRIVES = [{}, {"rent.pdf": (1.0, 9)}, {"rent.pdf": (1.0, 9)}]

    async def test_the_summary_reaches_the_desktop_and_the_voice_client(self):
        svc = _fake_svc(summarize=True)
        svc._collect_quick = mock.AsyncMock(
            return_value=("  A rent receipt\nfor July.  ", "done"))
        send = await _drive(svc, self.ARRIVES)
        self.assertIn("A rent receipt for July.", _spoken(send))
        spoken = [n for n in _notices(svc)
                  if n["summary"] == "A rent receipt for July."]
        self.assertEqual(len(spoken), 1)
        # `speech` is the field the voice brain reads; `event` is what makes it
        # look at the payload at all
        self.assertEqual(spoken[0]["speech"], "A rent receipt for July.")
        self.assertEqual(spoken[0]["source"], "inbox")

    async def test_summarizing_no_longer_replaces_the_free_notice(self):
        # it used to sit in an `elif`: switching summaries on switched off the
        # one announcement that costs nothing
        svc = _fake_svc(summarize=True)
        send = await _drive(svc, self.ARRIVES)
        self.assertTrue(any("searchable now" in s for s in _spoken(send)),
                        _spoken(send))
        self.assertEqual(len(svc.created), 1)

    async def test_a_failed_summary_stays_quiet(self):
        svc = _fake_svc(summarize=True, notify=False)
        svc._collect_quick = mock.AsyncMock(return_value=("", "failed"))
        with self.assertLogs("dispatcher.inbox", level="WARNING"):
            send = await _drive(svc, self.ARRIVES)
        send.assert_not_awaited()
        self.assertEqual(_notices(svc), [])

    async def test_a_crash_in_one_summary_never_kills_the_watcher(self):
        svc = _fake_svc(summarize=True)
        svc._collect_quick = mock.AsyncMock(side_effect=RuntimeError("boom"))
        with self.assertLogs("dispatcher.inbox", level="ERROR"):
            send = await _drive(svc, self.ARRIVES)
        self.assertTrue(any("searchable now" in s for s in _spoken(send)))


class TestSummarizeCap(unittest.IsolatedAsyncioTestCase):
    """vault/inbox is a directory the user *syncs* into. Uncapped, a 50-file
    sync was 50 serialized cold `claude -p` runs at the ~$0.10-0.14 floor, and
    the plan cap has taken the assistant out twice. Capped — and loudly, since
    this codebase does not do silent caps.
    """

    @staticmethod
    def _bulk(n):
        return {f"f{i}.pdf": (1.0, i + 1) for i in range(n)}

    async def test_bulk_arrival_is_capped_logged_and_announced(self):
        svc = _fake_svc(summarize=True, summarize_max_per_poll=2)
        files = self._bulk(6)
        with self.assertLogs("dispatcher.inbox", level="WARNING") as logs:
            send = await _drive(svc, [{}, files, files])
        self.assertEqual(len(svc.created), 2)
        self.assertTrue(any("skipped" in line for line in logs.output),
                        logs.output)
        notice = next(s for s in _spoken(send) if "searchable now" in s)
        self.assertIn("4 skipped by the per-poll cap", notice)

    async def test_the_default_cap_applies_with_no_config_key_at_all(self):
        svc = _fake_svc(summarize=True)
        files = self._bulk(inbox.DEFAULT_SUMMARIZE_MAX + 4)
        with self.assertLogs("dispatcher.inbox", level="WARNING"):
            await _drive(svc, [{}, files, files])
        self.assertEqual(len(svc.created), inbox.DEFAULT_SUMMARIZE_MAX)

    async def test_under_the_cap_says_nothing_about_it(self):
        svc = _fake_svc(summarize=True, summarize_max_per_poll=5)
        files = self._bulk(2)
        send = await _drive(svc, [{}, files, files])
        self.assertEqual(len(svc.created), 2)
        self.assertNotIn("cap", next(s for s in _spoken(send)
                                     if "searchable now" in s))


class TestConfigParsing(unittest.TestCase):
    def test_cap_never_throws_on_a_junk_config_value(self):
        for value in (None, "", "junk", [], {}):
            self.assertEqual(inbox._summarize_cap(value),
                             inbox.DEFAULT_SUMMARIZE_MAX, value)
        self.assertEqual(inbox._summarize_cap(3), 3)
        self.assertEqual(inbox._summarize_cap("7"), 7)
        self.assertEqual(inbox._summarize_cap(-1), 0)


class TestWatchSurvivesAwkwardConfig(unittest.IsolatedAsyncioTestCase):
    """A bare "check_interval_s:" in config.yaml parses as None, and
    max(5, None) raised a TypeError *before* the loop — on a fire-and-forget
    task nobody awaits, so the watcher just silently never started.
    """

    async def test_blank_interval_falls_back_to_the_default(self):
        svc = _fake_svc(check_interval_s=None)
        send = await _drive(svc, [{}, {"a.md": (1.0, 1)}, {"a.md": (1.0, 1)}])
        send.assert_awaited_once()          # i.e. the loop ran at all
        self.assertEqual(svc.sleeps[0], inbox.DEFAULT_INTERVAL_S)

    async def test_an_explicit_interval_still_wins_and_is_floored(self):
        svc = _fake_svc(check_interval_s=1)
        await _drive(svc, [{}, {}])
        self.assertEqual(svc.sleeps[0], 5)


class TestArrivalSummary(unittest.TestCase):
    """Finding 2.8: the notice was derived from the arrival list, never the
    reindex — a contract.docx/csv/eml that indexed nothing was still
    celebrated, and a raised reindex still notified success."""

    def test_success_names_the_file(self):
        s = inbox.arrival_summary(["report.pdf"], {"added": 3})
        self.assertIn("report.pdf", s)
        self.assertIn("searchable now", s)

    def test_success_counts_many(self):
        s = inbox.arrival_summary(["a.pdf", "b.pdf"], {"added": 5})
        self.assertIn("2 files", s)

    def test_unsupported_formats_are_named_not_celebrated(self):
        s = inbox.arrival_summary(
            ["contract.docx", "expenses.csv", "thread.eml"],
            {"added": 0, "unsupported": 2, "dep_gated": 1, "unparseable": 0})
        self.assertNotIn("searchable now", s)
        self.assertIn("unsupported", s)

    def test_raised_reindex_reports_failure(self):
        s = inbox.arrival_summary(["report.pdf"], None)
        self.assertIn("Couldn't index", s)
        self.assertIn("retry", s)


if __name__ == "__main__":
    unittest.main()
