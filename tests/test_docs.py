"""API docs coverage — every route is described in the system docs.

The dashboard ships its own reference (ui/src/docs/content.js, rendered at
/system-docs). Routes kept arriving without entries — the deferred list
carried "2 undocumented routes" for weeks — so this pins the mapping both
ways: every @app route path appears in the docs text, and every documented
path matches a route, with {params} normalized.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _routes() -> set[str]:
    src = (ROOT / "dispatcher" / "main.py").read_text()
    return set(re.findall(r'@app\.(?:get|post|put|patch|delete)\("([^"]*)"', src))


def _documented() -> set[str]:
    docs = (ROOT / "ui" / "src" / "docs" / "content.js").read_text()
    return set(re.findall(r'path: "([^"]*)"', docs))


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


class TestDocsCoverage(unittest.TestCase):
    def test_every_route_is_documented(self):
        documented = {_norm(d) for d in _documented()}
        missing = [p for p in sorted(_routes()) if _norm(p) not in documented]
        self.assertEqual(missing, [])

    def test_every_documented_path_exists(self):
        routes = {_norm(p) for p in _routes()}
        stale = [d for d in sorted(_documented()) if _norm(d) not in routes]
        self.assertEqual(stale, [])


if __name__ == "__main__":
    unittest.main()
