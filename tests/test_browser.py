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


if __name__ == "__main__":
    unittest.main()
