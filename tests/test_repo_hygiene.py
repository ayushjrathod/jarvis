"""Repo hygiene — the vault privacy cordon, pinned.

Session 30 found the repo public with 42 personal vault files tracked, and
`vault/inbox/` inviting bank statements that `git add -A` would sweep in.
The fix (2026-08-03) ignores the personal subtrees while keeping the READMEs
and dev notes tracked. These tests fail if anyone re-tracks a personal path
or drops the ignore rules — `git check-ignore` is the same mechanism git
itself uses, so there is no drift between the test and the tool.
"""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Personal paths that must stay ignored even though they exist on disk.
IGNORED = [
    "vault/briefs/2026-08-11.md",
    "vault/memory/USER.md",
    "vault/memory/MEMORY.md",
    "vault/tasks/renew-the-domain.md",
    "vault/inbox/statement.pdf",  # hypothetical — must be ignored before it exists
    "queue/drop.md",  # hypothetical live queue file
]

# Docs that must stay tracked.
TRACKED = [
    "vault/inbox/README.md",
    "vault/notes/2026-07-01-wayland-audio.md",
]


def _tracked(path):
    out = subprocess.run(
        ["git", "ls-files", "--error-unmatch", path],
        cwd=ROOT, capture_output=True, text=True,
    )
    return out.returncode == 0


class TestVaultCordon(unittest.TestCase):
    def test_personal_paths_are_ignored(self):
        out = subprocess.run(
            ["git", "check-ignore", *IGNORED],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(out.returncode, 0, f"not all ignored:\n{out.stdout}")
        ignored = set(out.stdout.split())
        for p in IGNORED:
            self.assertIn(p, ignored, f"{p} is not ignored")

    def test_readmes_and_notes_stay_tracked(self):
        for p in TRACKED:
            self.assertTrue(_tracked(p), f"{p} should stay tracked")

    def test_no_personal_files_tracked(self):
        out = subprocess.run(
            ["git", "ls-files", "vault/briefs", "vault/memory", "vault/tasks"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(out.stdout.strip(), "")
