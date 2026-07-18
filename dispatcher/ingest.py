"""Vault ingest: markdown → heading-ancestry chunks → hash-diff FTS index.

Chunking and change-detection design after khoj's TextToEntries /
markdown_to_entries (AGPL-3.0 — patterns re-implemented from scratch, no code
copied): every chunk carries its full heading ancestry so a search hit is
self-describing, and re-indexing is a per-chunk content-hash set-difference so
cost is proportional to what actually changed. Dates are extracted at index
time (ISO only — our vault convention) so "last week" queries stay pure SQL.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from pathlib import Path

from .db import Database

log = logging.getLogger("dispatcher.ingest")

MAX_WORDS = 256          # khoj-validated chunk budget
MAX_WORD_LEN = 500       # longer "words" are URL/base64 junk — drop them

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def extract_dates(text: str) -> list[str]:
    out = []
    for m in DATE_RE.findall(text):
        try:
            date.fromisoformat(m)
        except ValueError:
            continue
        if m not in out:
            out.append(m)
    return out


def _split_oversize(body: str, max_words: int = MAX_WORDS) -> list[str]:
    """Paragraph-first split of a too-large section; a single oversized
    paragraph falls back to word windows."""
    words = body.split()
    if len(words) <= max_words:
        return [body]
    parts, cur, cur_words = [], [], 0
    for para in re.split(r"\n\s*\n", body):
        n = len(para.split())
        if n > max_words:
            if cur:
                parts.append("\n\n".join(cur))
                cur, cur_words = [], 0
            pw = para.split()
            for i in range(0, len(pw), max_words):
                parts.append(" ".join(pw[i:i + max_words]))
            continue
        if cur_words + n > max_words and cur:
            parts.append("\n\n".join(cur))
            cur, cur_words = [], 0
        cur.append(para)
        cur_words += n
    if cur:
        parts.append("\n\n".join(cur))
    return [p for p in (s.strip() for s in parts) if p]


def chunk_markdown(file_label: str, text: str) -> list[dict]:
    """One chunk per heading section (pre-heading preamble included), split
    when oversized. compiled = "path / H1 / H2\\n\\nbody" — the text that gets
    indexed and hashed."""
    chunks = []
    stack: list[tuple[int, str]] = []   # (level, title)
    buf: list[str] = []
    buf_start = 1

    def flush():
        body = "\n".join(buf).strip()
        if not body:
            return
        body = " ".join(w for w in body.split(" ") if len(w) <= MAX_WORD_LEN)
        ancestry = " / ".join(t for _, t in stack)
        heading = ancestry or None
        label = f"{file_label} / {ancestry}" if ancestry else file_label
        for part in _split_oversize(body):
            compiled = f"{label}\n\n{part}"
            chunks.append({
                "heading": heading,
                "line_no": buf_start,
                "raw": part,
                "compiled": compiled,
                "hash": hashlib.md5(compiled.encode()).hexdigest(),
                "dates": extract_dates(compiled),
            })

    for i, line in enumerate(text.splitlines(), start=1):
        m = HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            buf, buf_start = [], i + 1
        else:
            buf.append(line)
    flush()
    return chunks


def ingest_vault(db: Database, root: Path, dirs: list[str]) -> dict:
    """Incremental walk: unchanged mtimes are skipped entirely; changed files
    get the per-chunk hash diff; files gone from disk lose their entries.
    One unreadable file never aborts the walk."""
    indexed = db.vault_file_mtimes()
    seen: set[str] = set()
    stats = {"files_scanned": 0, "files_changed": 0, "added": 0, "deleted": 0}
    for d in dirs:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            rel = str(p.relative_to(root))
            if any(part.startswith(".") for part in Path(rel).parts):
                continue
            seen.add(rel)
            stats["files_scanned"] += 1
            try:
                mtime = p.stat().st_mtime
                if indexed.get(rel) == mtime:
                    continue
                chunks = chunk_markdown(rel, p.read_text(errors="replace"))
                added, deleted = db.replace_file_entries(rel, mtime, chunks)
            except OSError as e:
                log.warning("ingest: skipping %s: %s", rel, e)
                continue
            stats["files_changed"] += 1
            stats["added"] += added
            stats["deleted"] += deleted
    prefixes = tuple(d.rstrip("/") + "/" for d in dirs)
    for path in indexed:
        if path not in seen and path.startswith(prefixes):
            stats["deleted"] += db.delete_file_entries(path)
    return stats
