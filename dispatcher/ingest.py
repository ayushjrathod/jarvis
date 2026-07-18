"""Vault ingest: files → heading-ancestry chunks → hash-diff FTS index.

Chunking and change-detection design after khoj's TextToEntries /
markdown_to_entries (AGPL-3.0 — patterns re-implemented from scratch, no code
copied): every chunk carries its full heading ancestry so a search hit is
self-describing, and re-indexing is a per-chunk content-hash set-difference so
cost is proportional to what actually changed. Dates are extracted at index
time (ISO only — our vault convention) so "last week" queries stay pure SQL.

Phase I: per-format processors normalizing everything to the same entry shape
(khoj's per-format pattern). Markdown chunks by heading; plaintext by
paragraph windows; HTML (browser bookmark exports, saved pages) is converted
to markdown-ish text by a stdlib parser and rides the markdown chunker.
PDF/image formats are counted as dep_gated, not indexed — they need
pymupdf/rapidocr, which the user hasn't approved yet.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from html.parser import HTMLParser
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


def chunk_plaintext(file_label: str, text: str) -> list[dict]:
    """Plaintext has no heading structure: the whole file is one section under
    the file path, paragraph-windowed when oversized."""
    body = text.strip()
    if not body:
        return []
    body = " ".join(w for w in body.split(" ") if len(w) <= MAX_WORD_LEN)
    chunks = []
    for part in _split_oversize(body):
        compiled = f"{file_label}\n\n{part}"
        chunks.append({
            "heading": None,
            "line_no": 1,
            "raw": part,
            "compiled": compiled,
            "hash": hashlib.md5(compiled.encode()).hexdigest(),
            "dates": extract_dates(compiled),
        })
    return chunks


class _HTMLToText(HTMLParser):
    """Lossy HTML → markdown-ish text: h1-h6 become # headings (so the
    markdown chunker's ancestry logic applies), links keep their URL (the
    payload of a browser bookmarks export), script/style are dropped."""

    SKIP = {"script", "style", "noscript"}
    BLOCK = {"p", "div", "li", "ul", "ol", "table", "tr", "td", "section",
             "article", "header", "footer", "blockquote", "pre", "dl", "dt",
             "dd", "hr", "br"}
    HEADINGS = {f"h{i}": i for i in range(1, 7)}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._href: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.HEADINGS:
            self.out.append("\n\n" + "#" * self.HEADINGS[tag] + " ")
        elif tag == "a":
            self._href = dict(attrs).get("href")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in self.HEADINGS:
            self.out.append("\n\n")
        elif tag == "a":
            if self._href and self._href.startswith(("http://", "https://")):
                self.out.append(f" ({self._href})")
            self._href = None
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        else:
            self.out.append(data)


def html_to_markdown(text: str) -> str:
    p = _HTMLToText()
    p.feed(text)
    p.close()
    body = "".join(p.out)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    title = p.title.strip()
    if title:
        body = f"# {title}\n\n{body}"
    return body


def chunk_html(file_label: str, text: str) -> list[dict]:
    return chunk_markdown(file_label, html_to_markdown(text))


CHUNKERS = {
    ".md": chunk_markdown,
    ".txt": chunk_plaintext,
    ".text": chunk_plaintext,
    ".html": chunk_html,
    ".htm": chunk_html,
}

# Formats we recognize but can't parse without unapproved dependencies
# (pymupdf for PDF, rapidocr-onnxruntime for images). Counted in the reindex
# stats so the cost of not approving them stays visible.
DEP_GATED_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff",
                  ".docx", ".doc", ".epub"}


def ingest_vault(db: Database, root: Path, dirs: list[str]) -> dict:
    """Incremental walk: unchanged mtimes are skipped entirely; changed files
    get the per-chunk hash diff; files gone from disk lose their entries.
    One unreadable file never aborts the walk."""
    indexed = db.vault_file_mtimes()
    seen: set[str] = set()
    stats = {"files_scanned": 0, "files_changed": 0, "added": 0, "deleted": 0,
             "dep_gated": 0}
    gated_names: list[str] = []
    for d in dirs:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(root))
            if any(part.startswith(".") for part in Path(rel).parts):
                continue
            chunker = CHUNKERS.get(p.suffix.lower())
            if chunker is None:
                if p.suffix.lower() in DEP_GATED_EXTS:
                    stats["dep_gated"] += 1
                    gated_names.append(rel)
                continue
            seen.add(rel)
            stats["files_scanned"] += 1
            try:
                mtime = p.stat().st_mtime
                if indexed.get(rel) == mtime:
                    continue
                chunks = chunker(rel, p.read_text(errors="replace"))
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
    if gated_names:
        log.info("ingest: %d file(s) need PDF/OCR deps, not indexed: %s%s",
                 len(gated_names), ", ".join(gated_names[:5]),
                 "…" if len(gated_names) > 5 else "")
    return stats
