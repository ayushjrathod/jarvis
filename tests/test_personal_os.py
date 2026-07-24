"""Phase I tests: ingest format expansion, automation parse/validate/schedule,
scheduler firing, notify-or-not gate policy. stdlib unittest, no network."""

import asyncio
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dispatcher import automations, notify
from dispatcher.db import Database
from dispatcher.ingest import (chunk_html, chunk_markdown, chunk_plaintext,
                               html_to_markdown, ingest_vault)

IST = timezone(timedelta(hours=5, minutes=30))


def _installed(*mods) -> bool:
    """True only if every optional dependency is importable — lets the PDF/OCR
    tests skip cleanly on a machine that never approved those deps (L14)."""
    import importlib.util
    return all(importlib.util.find_spec(m) is not None for m in mods)


class TestChunkPlaintext(unittest.TestCase):
    def test_basic(self):
        chunks = chunk_plaintext("vault/inbox/note.txt", "hello world\n\nsecond para")
        self.assertEqual(len(chunks), 1)
        self.assertIn("vault/inbox/note.txt", chunks[0]["compiled"])
        self.assertIn("hello world", chunks[0]["raw"])
        self.assertIsNone(chunks[0]["heading"])

    def test_empty(self):
        self.assertEqual(chunk_plaintext("x.txt", "  \n "), [])

    def test_oversize_splits(self):
        text = "\n\n".join("word " * 100 for _ in range(5))  # ~500 words
        chunks = chunk_plaintext("big.txt", text)
        self.assertGreater(len(chunks), 1)


class TestJunkWordFilter(unittest.TestCase):
    """M5: the >500-char junk filter must tokenize on all whitespace, not just
    single spaces — else newline-separated content (a bookmark-per-line .txt)
    is one giant pseudo-word, gets dropped wholesale, and the file indexes as a
    single empty entry."""

    def test_url_per_line_txt_is_searchable(self):
        urls = "\n".join(f"https://example.com/bookmark-{i}" for i in range(30))
        chunks = chunk_plaintext("vault/inbox/bookmarks.txt", urls)
        self.assertTrue(chunks)
        self.assertTrue(any("http" in c["raw"] for c in chunks))
        # regression for the empty-entry bug: no chunk with empty raw
        self.assertFalse(any(c["raw"].strip() == "" for c in chunks))

    def test_url_per_line_markdown_section_is_searchable(self):
        body = "# Links\n" + "\n".join(
            f"https://example.com/bookmark-{i}" for i in range(30))
        chunks = chunk_markdown("vault/inbox/links.md", body)
        self.assertTrue(chunks)
        self.assertTrue(any("http" in c["raw"] for c in chunks))
        self.assertFalse(any(c["raw"].strip() == "" for c in chunks))

    def test_single_long_junk_token_still_dropped(self):
        # a genuine 5000-char blob with no whitespace stays dropped/empty
        self.assertEqual(chunk_plaintext("x.txt", "A" * 5000), [])
        self.assertEqual(chunk_markdown("x.md", "# H\n" + "B" * 5000), [])

    def test_normal_prose_keeps_paragraphs(self):
        chunks = chunk_plaintext("p.txt", "first para\n\nsecond para")
        self.assertEqual(len(chunks), 1)
        self.assertIn("\n\n", chunks[0]["raw"])


class TestHtml(unittest.TestCase):
    HTML = ("<html><head><title>Bookmarks</title><script>var x=1;</script>"
            "</head><body><h2>Dev</h2>"
            '<a href="https://example.com/page">Example Page</a>'
            "<p>notes from 2026-07-19</p></body></html>")

    def test_title_and_headings_become_markdown(self):
        md = html_to_markdown(self.HTML)
        self.assertTrue(md.startswith("# Bookmarks"))
        self.assertIn("## Dev", md)
        self.assertNotIn("var x=1", md)

    def test_links_keep_urls(self):
        md = html_to_markdown(self.HTML)
        self.assertIn("Example Page (https://example.com/page)", md)

    def test_chunks_carry_ancestry_and_dates(self):
        chunks = chunk_html("vault/inbox/bm.html", self.HTML)
        hit = [c for c in chunks if "Example Page" in c["raw"]]
        self.assertTrue(hit)
        self.assertIn("Bookmarks / Dev", hit[0]["compiled"])
        self.assertIn("2026-07-19", hit[0]["dates"])


class TestIngestWalk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "t.db")
        inbox = self.root / "vault" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "note.txt").write_text("quarterly insurance premium is due")
        (inbox / "bm.html").write_text(TestHtml.HTML)
        (inbox / "old.docx").write_bytes(b"PK\x03\x04fake")
        (self.root / "vault" / "plain.md").write_text("# Plan\ntoday's plan text")

    def tearDown(self):
        self.tmp.cleanup()

    def test_walk_indexes_new_formats_and_counts_gated(self):
        stats = ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(stats["dep_gated"], 1)
        self.assertEqual(stats["files_scanned"], 3)
        self.assertTrue(self.db.search_entries("insurance premium"))
        self.assertTrue(self.db.search_entries("Example Page"))

    def test_deleted_file_loses_entries(self):
        ingest_vault(self.db, self.root, ["vault"])
        (self.root / "vault" / "inbox" / "note.txt").unlink()
        stats = ingest_vault(self.db, self.root, ["vault"])
        self.assertGreater(stats["deleted"], 0)
        self.assertFalse(self.db.search_entries("insurance premium"))


class TestIngestStaleDir(unittest.TestCase):
    """L6: a briefly-absent index_dir must not purge its whole index; a
    present dir must still prune files that were genuinely deleted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "t.db")
        (self.root / "vault").mkdir()
        (self.root / "vault" / "note.txt").write_text(
            "quarterly insurance premium is due")
        (self.root / "notes").mkdir()
        (self.root / "notes" / "memo.txt").write_text(
            "standalone journal memo content")
        self.dirs = ["vault", "notes"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_dir_keeps_its_entries(self):
        ingest_vault(self.db, self.root, self.dirs)
        self.assertTrue(self.db.search_entries("journal memo"))
        # 'notes' vanishes (unmounted / mid-sync); reindex must not purge it
        shutil.rmtree(self.root / "notes")
        stats = ingest_vault(self.db, self.root, self.dirs)
        self.assertEqual(stats["deleted"], 0)
        self.assertTrue(self.db.search_entries("journal memo"))
        self.assertTrue(self.db.search_entries("insurance premium"))

    def test_present_dir_still_prunes_deleted_file(self):
        ingest_vault(self.db, self.root, self.dirs)
        (self.root / "notes" / "memo.txt").unlink()  # dir present, file gone
        stats = ingest_vault(self.db, self.root, self.dirs)
        self.assertGreater(stats["deleted"], 0)
        self.assertFalse(self.db.search_entries("journal memo"))
        self.assertTrue(self.db.search_entries("insurance premium"))


class TestIngestDocs(unittest.TestCase):
    """PDF + image OCR processors (deps approved + installed 2026-07-19)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "t.db")
        (self.root / "vault" / "inbox").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(_installed("pymupdf"), "pymupdf not installed")
    def test_pdf_text_extraction(self):
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "quarterly cashflow report for the vineyard")
        doc.save(self.root / "vault" / "inbox" / "report.pdf")
        doc.close()
        stats = ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(stats["dep_gated"], 0)
        hits = self.db.search_entries("cashflow vineyard")
        self.assertTrue(hits)
        self.assertIn("page 1", hits[0]["heading"])

    @unittest.skipUnless(_installed("cv2", "rapidocr_onnxruntime"),
                         "cv2/rapidocr not installed")
    def test_image_ocr(self):
        import cv2
        import numpy as np
        img = np.full((90, 700, 3), 255, np.uint8)
        cv2.putText(img, "WARRANTY EXPIRES DECEMBER", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
        cv2.imwrite(str(self.root / "vault" / "inbox" / "scan.png"), img)
        ingest_vault(self.db, self.root, ["vault"])
        self.assertTrue(self.db.search_entries("WARRANTY"))

    def test_docx_still_gated(self):
        (self.root / "vault" / "inbox" / "x.docx").write_bytes(b"PK\x03\x04junk")
        stats = ingest_vault(self.db, self.root, ["vault"])
        self.assertEqual(stats["dep_gated"], 1)


class TestDetect(unittest.TestCase):
    def test_schedule_phrases_divert(self):
        for text in ("every morning, tell me my schedule",
                     "remind me every 30 minutes to stretch",
                     "each week summarize my notes",
                     "every sunday plan the coming week"):
            self.assertTrue(automations.detect(text), text)

    def test_questions_and_plain_tasks_do_not(self):
        for text in ("what did I do every morning last week",
                     "did I run every day this month?",
                     "should I water the plants every day",
                     "add a task: buy milk",
                     "tell me my schedule"):
            self.assertFalse(automations.detect(text), text)

    def test_memory_statements_do_not_divert(self):
        # L5: "remember/note/save …" is a memory write, not a schedule, even
        # with schedule-ish phrasing
        for text in ("remember that I run every morning",
                     "note that I take meds every day",
                     "save this: standup is every weekday"):
            self.assertFalse(automations.detect(text), text)


class TestValidateSpec(unittest.TestCase):
    def test_daily_normalizes_time(self):
        spec = automations.validate_spec(
            {"task_text": "tell me my schedule", "kind": "daily", "time": "7:30"})
        self.assertEqual(spec["time"], "07:30")
        self.assertTrue(spec["notify"])

    def test_daily_needs_time(self):
        with self.assertRaises(ValueError):
            automations.validate_spec({"task_text": "x", "kind": "daily"})

    def test_weekly_needs_weekday(self):
        with self.assertRaises(ValueError):
            automations.validate_spec(
                {"task_text": "x", "kind": "weekly", "time": "09:00", "weekday": 7})

    def test_interval_minimum(self):
        with self.assertRaises(ValueError):
            automations.validate_spec(
                {"task_text": "x", "kind": "interval", "interval_minutes": 1})

    def test_once_must_be_future(self):
        with self.assertRaises(ValueError):
            automations.validate_spec(
                {"task_text": "x", "kind": "once", "once_at": "2020-01-01T10:00"})
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        spec = automations.validate_spec(
            {"task_text": "x", "kind": "once", "once_at": future})
        self.assertIsNotNone(spec["once_at"])

    def test_kind_and_text_required(self):
        with self.assertRaises(ValueError):
            automations.validate_spec({"task_text": "x", "kind": "hourly"})
        with self.assertRaises(ValueError):
            automations.validate_spec({"task_text": "", "kind": "daily", "time": "07:00"})


class TestNextRun(unittest.TestCase):
    def test_daily_before_and_after_time(self):
        spec = {"kind": "daily", "time": "07:30"}
        early = datetime(2026, 7, 19, 6, 0, tzinfo=IST)
        late = datetime(2026, 7, 19, 8, 0, tzinfo=IST)
        self.assertEqual(
            automations.next_run_iso(spec, early),
            datetime(2026, 7, 19, 7, 30, tzinfo=IST)
            .astimezone(timezone.utc).isoformat(timespec="seconds"))
        self.assertEqual(
            automations.next_run_iso(spec, late),
            datetime(2026, 7, 20, 7, 30, tzinfo=IST)
            .astimezone(timezone.utc).isoformat(timespec="seconds"))

    def test_weekly_wraps(self):
        # 2026-07-19 is a Sunday (weekday 6)
        spec = {"kind": "weekly", "time": "09:00", "weekday": 6}
        after = datetime(2026, 7, 19, 10, 0, tzinfo=IST)  # past 09:00 Sunday
        self.assertEqual(
            automations.next_run_iso(spec, after),
            datetime(2026, 7, 26, 9, 0, tzinfo=IST)
            .astimezone(timezone.utc).isoformat(timespec="seconds"))

    def test_interval(self):
        spec = {"kind": "interval", "interval_minutes": 45}
        after = datetime(2026, 7, 19, 6, 0, tzinfo=IST)
        self.assertEqual(
            automations.next_run_iso(spec, after),
            datetime(2026, 7, 19, 6, 45, tzinfo=IST)
            .astimezone(timezone.utc).isoformat(timespec="seconds"))

    def test_once_spent_is_none(self):
        spec = {"kind": "once", "once_at": "2026-07-19T10:00"}
        after = datetime(2026, 7, 19, 11, 0, tzinfo=IST)
        self.assertIsNone(automations.next_run_iso(spec, after))
        self.assertIsNone(automations.next_run_iso({"kind": "once", "once_at": None}))

    def test_daily_default_after_is_dst_correct(self):
        # L4: the default 'now' rides the real IANA zone, so a daily 09:30 keeps
        # its wall-clock hour across a DST transition (a frozen fixed offset
        # would drift an hour). Simulate a DST zone the day before spring-forward.
        from unittest.mock import patch
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        now = datetime(2026, 3, 7, 10, 0, tzinfo=ny)  # EST, before spring-forward
        with patch("dispatcher.automations._local_zone", return_value=ny), \
             patch("dispatcher.automations._now_local", return_value=now):
            iso = automations.next_run_iso({"kind": "daily", "time": "09:30"})
        got = datetime.fromisoformat(iso).astimezone(ny)
        self.assertEqual((got.month, got.day, got.hour, got.minute), (3, 8, 9, 30))


class TestParseResponse(unittest.TestCase):
    GOOD = '{"task_text": "tell me the weather", "kind": "daily", "time": "07:30"}'

    def test_plain_and_fenced_and_prose(self):
        for wrap in ("{}", "```json\n{}\n```", "Sure! Here it is:\n{}\nDone."):
            obj = automations.parse_response(wrap.format(self.GOOD))
            self.assertEqual(obj["kind"], "daily")

    def test_garbage_raises(self):
        for bad in ("no json here", "{broken", "[1, 2]"):
            with self.assertRaises(ValueError):
                automations.parse_response(bad)


class TestAutomationsDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.spec = {"task_text": "tell me the weather", "kind": "daily",
                     "time": "07:30", "notify": True}

    def tearDown(self):
        self.tmp.cleanup()

    def test_crud_and_due(self):
        row = self.db.create_automation("every morning weather", "voice",
                                        self.spec, "2026-07-19T02:00:00+00:00")
        self.assertEqual(row["task_text"], "tell me the weather")
        self.assertTrue(self.db.due_automations("2026-07-19T02:30:00+00:00"))
        self.assertFalse(self.db.due_automations("2026-07-19T01:00:00+00:00"))

        self.db.automation_fired(row["id"], "2026-07-20T02:00:00+00:00")
        self.db.automation_task_started(row["id"], "task123")
        got = self.db.get_automation(row["id"])
        self.assertEqual(got["last_task_id"], "task123")
        self.assertIsNotNone(got["last_run_at"])
        self.assertFalse(self.db.due_automations("2026-07-19T03:00:00+00:00"))

    def test_spent_and_disabled_never_due(self):
        row = self.db.create_automation("once", "api",
                                        {**self.spec, "kind": "once",
                                         "time": None,
                                         "once_at": "2026-07-19T07:30"},
                                        "2026-07-19T02:00:00+00:00")
        self.db.automation_fired(row["id"], None)  # spent
        self.assertFalse(self.db.due_automations("2027-01-01T00:00:00+00:00"))

        row2 = self.db.create_automation("r", "api", self.spec,
                                         "2026-07-19T02:00:00+00:00")
        self.db.set_automation_enabled(row2["id"], False)
        self.assertFalse(self.db.due_automations("2027-01-01T00:00:00+00:00"))
        self.db.set_automation_enabled(row2["id"], True, "2027-06-01T00:00:00+00:00")
        self.assertEqual(self.db.get_automation(row2["id"])["next_run_at"],
                         "2027-06-01T00:00:00+00:00")

    def test_delete(self):
        row = self.db.create_automation("r", "api", self.spec, None)
        self.assertTrue(self.db.delete_automation(row["id"]))
        self.assertFalse(self.db.delete_automation(row["id"]))
        self.assertEqual(self.db.list_automations(), [])


class _StubSvc:
    """Just enough of Service for automations.fire()."""

    def __init__(self, db):
        self.db = db
        self.submitted = []
        self.events = []
        self.cfg = type("C", (), {"automations": {"enabled": True}})()

        class Hooks:
            def __init__(self, events):
                self.events = events

            async def fire(self, payload):
                self.events.append(payload)

        self.hooks = Hooks(self.events)

    async def submit(self, text, source=None, mode=None, metadata=None, **kw):
        self.submitted.append({"text": text, "source": source,
                               "metadata": metadata})
        return {"id": f"task{len(self.submitted)}"}


class TestFireAutomation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.svc = _StubSvc(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fire_advances_schedule_and_submits(self):
        row = self.db.create_automation(
            "every morning weather", "voice",
            {"task_text": "tell me the weather", "kind": "daily",
             "time": "07:30", "notify": True},
            "2020-01-01T02:00:00+00:00")
        asyncio.run(automations.fire(self.svc, self.db.get_automation(row["id"])))

        got = self.db.get_automation(row["id"])
        self.assertGreater(got["next_run_at"], "2020-01-01T02:00:00+00:00")
        self.assertEqual(got["last_task_id"], "task1")
        self.assertEqual(self.svc.submitted[0]["source"], "automation")
        self.assertEqual(self.svc.submitted[0]["metadata"]["automation_id"], row["id"])
        self.assertEqual(self.svc.events[0]["event"], "automation")

    def test_fire_once_spends_the_row(self):
        row = self.db.create_automation(
            "once", "api",
            {"task_text": "x", "kind": "once", "once_at": "2020-01-01T10:00",
             "notify": True},
            "2020-01-01T04:30:00+00:00")
        asyncio.run(automations.fire(self.svc, self.db.get_automation(row["id"])))
        self.assertIsNone(self.db.get_automation(row["id"])["next_run_at"])
        self.assertFalse(self.db.due_automations("2027-01-01T00:00:00+00:00"))


class TestNotifyGatePieces(unittest.TestCase):
    def test_parse_gate(self):
        self.assertEqual(notify.parse_gate("NOTIFY: rain expected at 5pm"),
                         ("notify", "rain expected at 5pm"))
        self.assertEqual(notify.parse_gate("SKIP: routine, nothing new"),
                         ("skip", "routine, nothing new"))
        self.assertEqual(notify.parse_gate("thinking...\nnotify: check email"),
                         ("notify", "check email"))

    def test_parse_gate_fails_open(self):
        self.assertEqual(notify.parse_gate("I think this is interesting")[0], "notify")
        self.assertEqual(notify.parse_gate("")[0], "notify")

    def test_gate_prompt_clips_result(self):
        p = notify.gate_prompt("check the feeds", "x" * 5000)
        self.assertLess(len(p), 2500)
        p2 = notify.gate_prompt("check", None)
        self.assertIn("(no text output)", p2)


class TestSurfacingPolicy(unittest.TestCase):
    ACFG = {"enabled": True, "notify_gate": True,
            "notify_sources": ["automation", "timer"]}

    def surf(self, acfg=None, source="automation", meta=None, final="done"):
        return notify.surfacing(self.ACFG if acfg is None else acfg,
                                {"source": source}, meta or {}, final)

    def test_meta_types_silent_on_success(self):
        for tt in ("reflection", "memory-consolidate", "notify-gate",
                   "automation-parse"):
            self.assertEqual(self.surf(meta={"task_type": tt}), "silent")

    def test_plumbing_meta_types_silent_even_on_failure(self):
        # gate/parse/reflection failures are internal noise — stay silent
        for tt in ("reflection", "notify-gate", "automation-parse"):
            self.assertEqual(self.surf(acfg={}, meta={"task_type": tt},
                                       final="failed"), "silent")

    def test_failed_consolidation_surfaces(self):
        # M6: a broken nightly consolidation must be visible, not swallowed
        self.assertEqual(self.surf(acfg={}, meta={"task_type": "memory-consolidate"},
                                   final="failed"), "surface")
        self.assertEqual(self.surf(meta={"task_type": "memory-consolidate"},
                                   final="done"), "silent")

    def test_feature_off_or_wrong_source_surfaces(self):
        self.assertEqual(self.surf(acfg={}), "surface")
        self.assertEqual(self.surf(source="voice"), "surface")

    def test_failures_surface(self):
        self.assertEqual(self.surf(final="failed"), "surface")

    def test_notify_false_is_silent(self):
        self.assertEqual(self.surf(meta={"notify": False}), "silent")

    def test_gate_when_eligible_or_surface_when_gate_off(self):
        self.assertEqual(self.surf(), "gate")
        self.assertEqual(self.surf(source="timer"), "gate")
        self.assertEqual(
            self.surf(acfg={**self.ACFG, "notify_gate": False}), "surface")


class TestDescribe(unittest.TestCase):
    def test_all_kinds(self):
        self.assertEqual(
            automations.describe({"kind": "daily", "time": "07:30"}),
            "daily at 07:30")
        self.assertEqual(
            automations.describe({"kind": "weekly", "time": "09:00", "weekday": 6}),
            "weekly on Sunday at 09:00")
        self.assertEqual(
            automations.describe({"kind": "interval", "interval_minutes": 45}),
            "every 45 minutes")
        self.assertIn("once at 2026-07-20 06:00",
                      automations.describe({"kind": "once",
                                            "once_at": "2026-07-20T06:00"}))


if __name__ == "__main__":
    unittest.main()
