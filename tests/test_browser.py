"""T3 browser tabs over DevTools HTTP (zero new deps).

httpx is faked at dispatcher.browser level: target lists, activation,
unreachable browsers. The live registry was verified by hand (list +
activate + miss against headless chromium) before these were written.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from dispatcher import browser


def _cfg(port=9222):
    return SimpleNamespace(browser={"debug_port": port})


TARGETS = [
    {"id": "a1", "type": "page", "title": "Inbox — Gmail", "url": "https://mail.google.com/x"},
    {"id": "b2", "type": "page", "title": "", "url": "about:blank"},
    {"id": "c3", "type": "background_page", "title": "Hangouts", "url": "chrome-extension://x"},
]


class TestListTabs(unittest.TestCase):
    def test_lists_pages_only(self):
        with mock.patch("httpx.get", return_value=mock.Mock(
                status_code=200, json=lambda: TARGETS)):
            out = browser.list_tabs(_cfg())
        self.assertEqual(out, [
            {"id": "a1", "title": "Inbox — Gmail", "url": "https://mail.google.com/x"},
            {"id": "b2", "title": "(untitled)", "url": "about:blank"},
        ])

    def test_dead_browser_means_empty_not_exception(self):
        import httpx
        with mock.patch("httpx.get", side_effect=httpx.ConnectError("nope")):
            self.assertEqual(browser.list_tabs(_cfg()), [])


class TestActivateTab(unittest.TestCase):
    def _get(self, calls, targets=None):
        def fake(url, timeout=None):
            calls.append(url)
            if url.endswith("/json/list"):
                return mock.Mock(status_code=200, json=lambda: targets or TARGETS)
            return mock.Mock(status_code=200, text="Target activated")
        return fake

    def test_activate_matches_title_or_url(self):
        calls = []
        with mock.patch("httpx.get", side_effect=self._get(calls)):
            speech = browser.activate_tab(_cfg(), "gmail")
        self.assertIn("Gmail", speech)
        self.assertTrue(any("/json/activate/a1" in u for u in calls))

    def test_miss_and_ambiguity_are_speakable(self):
        with mock.patch("httpx.get", side_effect=self._get([])):
            with self.assertRaises(browser.BrowserError) as ctx:
                browser.activate_tab(_cfg(), "nope")
            self.assertIn("nope", str(ctx.exception))
        dupes = [dict(TARGETS[0], id="z9")]
        with mock.patch("httpx.get", side_effect=self._get([], TARGETS + dupes)):
            with self.assertRaises(browser.BrowserError) as ctx:
                browser.activate_tab(_cfg(), "gmail")
            self.assertIn("which tab", str(ctx.exception).lower())


    def test_empty_query_refuses(self):
        with self.assertRaises(browser.BrowserError):
            browser.activate_tab(_cfg(), "   ")


class TestReadTab(unittest.TestCase):
    def _targets(self, extra=None):
        base = [
            {"id": "a1", "type": "page", "title": "Docs",
             "url": "https://x/y",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/a1"},
        ]
        return base + (extra or [])

    def test_reads_visible_text_clipped(self):
        import dispatcher.cdp as cdp_mod
        long_text = "word " * 2000
        with mock.patch("httpx.get") as hg, \
                mock.patch.object(cdp_mod.CDPClient, "connect") as conn, \
                mock.patch.object(cdp_mod.CDPClient, "evaluate",
                                  return_value=long_text):
            hg.return_value = mock.Mock(status_code=200,
                                        json=lambda: self._targets())
            out = browser.read_tab(_cfg(), "docs")
        self.assertIn("Docs", out)
        self.assertIn("omitted", out)
        self.assertLess(len(out), 4500)

    def test_unmatched_and_gone_tabs_speak(self):
        with mock.patch("httpx.get") as hg:
            hg.return_value = mock.Mock(status_code=200,
                                        json=lambda: self._targets())
            with self.assertRaises(browser.BrowserError):
                browser.read_tab(_cfg(), "nope")
        with mock.patch("httpx.get") as hg:
            hg.return_value = mock.Mock(status_code=200, json=lambda: [])
            with self.assertRaises(browser.BrowserError):
                browser.read_tab(_cfg(), "docs")


if __name__ == "__main__":
    unittest.main()
