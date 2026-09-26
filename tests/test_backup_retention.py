"""Backup retention — the pre-purge recovery point must survive.

`scripts/backup_db.sh` keeps 14 days of dated backups. Its old glob
(`mission-*.db`) also matched the pre-purge recovery point taken before the
knowledge-graph purge — the one rollback that matters — which would have been
auto-deleted ~2026-08-17. The script now only matches dated backups
(`mission-[0-9]*.db`). This test runs the script's exact find command against
a fake backup dir and pins both sides: old dated backups go, everything else
stays.
"""

import subprocess
import time
import unittest
from pathlib import Path
import tempfile


FIND = ["find", ".", "-name", "mission-[0-9]*.db", "-mtime", "+14", "-delete"]


def _touch(path: Path, days_old: int):
    path.write_bytes(b"fake")
    old = time.time() - days_old * 86400
    import os
    os.utime(path, (old, old))


class TestBackupRetention(unittest.TestCase):
    def test_pre_purge_survives_and_old_dated_goes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_dated = root / "mission-2026-01-01.db"
            fresh_dated = root / "mission-2099-01-01.db"
            pre_purge = root / "mission-pre-purge-20260802T000402.db"
            _touch(old_dated, 60)
            fresh_dated.write_bytes(b"fake")
            _touch(pre_purge, 60)
            subprocess.run(FIND, cwd=root, check=True)
            self.assertFalse(old_dated.exists(), "old dated backup should be deleted")
            self.assertTrue(fresh_dated.exists(), "fresh backup should be kept")
            self.assertTrue(pre_purge.exists(), "pre-purge recovery point must survive")
