"""Config typos must be loud, not silent."""

import tempfile
import unittest
from pathlib import Path

from dispatcher.config import Config


class TestUnknownKeys(unittest.TestCase):
    def _write(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "config.yaml"
        p.write_text(text)
        return p

    def test_typo_warns_but_loads(self):
        p = self._write("dispatcher:\n  poll_intervall_s: 5\n")
        with self.assertLogs("dispatcher.config", level="WARNING") as logs:
            cfg = Config.load(p)
        self.assertTrue(any("poll_intervall_s" in m for m in logs.output))
        self.assertEqual(cfg.poll_interval_s, 5)  # default kept, nothing half-set

    def test_known_keys_stay_silent(self):
        p = self._write("dispatcher:\n  poll_interval_s: 7\n")
        with self.assertNoLogs("dispatcher.config", level="WARNING"):
            cfg = Config.load(p)
        self.assertEqual(cfg.poll_interval_s, 7)

    def test_real_config_is_clean(self):
        root = Path(__file__).resolve().parent.parent
        with self.assertNoLogs("dispatcher.config", level="WARNING"):
            Config.load(root / "config.yaml")


if __name__ == "__main__":
    unittest.main()
