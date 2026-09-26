"""T2 window control over AT-SPI (jeepney, zero new deps).

Faked at windows._call (one step above the wire): applications with window
children, roles and names. No D-Bus, no display, no pyatspi — the live
registry was verified by hand (4 real windows) before these were written.
"""

import unittest
from unittest import mock

from dispatcher import windows

APPS = "/org/a11y/atspi/accessible/root"

TREE = {
    (APPS, "children"): [("bus.a", "/appA"), ("bus.b", "/appB")],
    ("/appA", "name"): "org.gnome.Nautilus",
    ("/appB", "name"): "vlc",
    ("/appA", "children"): [("bus.a", "/win1"), ("bus.a", "/win2")],
    ("/appB", "children"): [("bus.b", "/win3")],
    ("/win1", "role"): "window",
    ("/win1", "name"): "Data3",
    ("/win2", "role"): "filler",
    ("/win2", "name"): "",
    ("/win3", "role"): "frame",
    ("/win3", "name"): "Concert",
}


class FakeBus:
    def __init__(self, tree):
        self.tree = tree
        self.focused = []
        self.closed = False

    def fake_call(self, conn, bus, path, iface, method, sig=None, body=None):
        if method == "GetChildren":
            return [self.tree.get((path, "children"), [])]
        if method == "GetRoleName":
            return [self.tree.get((path, "role"), "window")]
        if method == "Get":
            return [(("", self.tree.get((path, "name"), "")))]
        if method == "GrabFocus":
            self.focused.append(path)
            return [None]
        raise AssertionError(f"unexpected call {method} on {path}")

    def close(self):
        self.closed = True


class TestListWindows(unittest.TestCase):
    def test_lists_titled_windows_only(self):
        bus = FakeBus(dict(TREE))
        with mock.patch.object(windows, "_a11y_connection", return_value=bus), \
                mock.patch.object(windows, "_call", bus.fake_call):
            out = windows.list_windows()
        self.assertEqual(out, [
            {"app": "org.gnome.Nautilus", "title": "Data3"},
            {"app": "vlc", "title": "Concert"},
        ])
        self.assertTrue(bus.closed)

    def test_dedupes_and_never_raises(self):
        tree = dict(TREE)
        tree[("/appB", "children")] = [("bus.b", "/win3"), ("bus.b", "/win3")]
        bus = FakeBus(tree)
        with mock.patch.object(windows, "_a11y_connection", return_value=bus), \
                mock.patch.object(windows, "_call", bus.fake_call):
            out = windows.list_windows()
        self.assertEqual(len(out), 2)

    def test_bus_error_means_empty_not_exception(self):
        with mock.patch.object(windows, "_a11y_connection",
                               side_effect=windows.WindowError("no bus")):
            self.assertEqual(windows.list_windows(), [])


class TestFocusWindow(unittest.TestCase):
    def _bus(self, tree=None):
        return FakeBus(dict(tree or TREE))

    def _patched(self, bus):
        return (mock.patch.object(windows, "_a11y_connection", return_value=bus),
                mock.patch.object(windows, "_call", bus.fake_call))

    def test_focus_matches_title_case_insensitively(self):
        bus = self._bus()
        pa, pc = self._patched(bus)
        with pa, pc:
            speech = windows.focus_window("data")
        self.assertIn("Data3", speech)
        self.assertEqual(bus.focused, ["/win1"])

    def test_no_match_is_speakable(self):
        bus = self._bus()
        pa, pc = self._patched(bus)
        with pa, pc:
            with self.assertRaises(windows.WindowError) as ctx:
                windows.focus_window("photoshop")
        self.assertIn("photoshop", str(ctx.exception))

    def test_ambiguity_lists_candidates(self):
        tree = dict(TREE)
        tree[("/win3", "name")] = "Data3 Live"
        bus = self._bus(tree)
        pa, pc = self._patched(bus)
        with pa, pc:
            with self.assertRaises(windows.WindowError) as ctx:
                windows.focus_window("data")
        self.assertIn("which one", str(ctx.exception).lower())
        self.assertEqual(bus.focused, [])

    def test_same_title_twice_focuses_without_asking(self):
        # X11 clients appear under their own name AND mutter-x11-frames with
        # one title: asking "which one?" with a single name would be theater.
        tree = dict(TREE)
        tree[("/win-extra", "role")] = "window"
        tree[("/win-extra", "name")] = "Data3"
        tree[("/appA", "children")] = [("bus.a", "/win1"), ("bus.a", "/win-extra")]
        bus = FakeBus(tree)
        with mock.patch.object(windows, "_a11y_connection", return_value=bus), \
                mock.patch.object(windows, "_call", bus.fake_call):
            speech = windows.focus_window("data3")
        self.assertIn("Data3", speech)
        self.assertEqual(len(bus.focused), 1)


if __name__ == "__main__":
    unittest.main()
