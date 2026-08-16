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
PDFs/epubs extract text per page via pymupdf, with a budget-capped OCR
fallback for textless (scanned) pages; images OCR via rapidocr — both deps
user-approved 2026-07-19 and lazily imported, so an uninstalled dep degrades
back to dep_gated counting instead of breaking the walk.
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


def _drop_junk_words(body: str) -> str:
    """Drop tokens longer than MAX_WORD_LEN (URL/base64/log junk).

    Tokenize on *all* whitespace so newline-delimited junk is filtered per
    real token, not as one giant pseudo-word: a bookmarks .txt of one URL per
    line, a wrapped base64 blob, or multi-line CJK used to collapse into a
    single >500-char "word" and the whole section was dropped (indexed as an
    empty entry). Line breaks are preserved — the original filter split on
    single spaces precisely to keep them, and _split_oversize's paragraph
    logic (and the stored `raw`) still rely on them for normal prose."""
    kept = []
    for line in body.split("\n"):
        kept.append(" ".join(w for w in line.split() if len(w) <= MAX_WORD_LEN))
    return "\n".join(kept)


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
    in_fence = False

    def flush():
        body = "\n".join(buf).strip()
        if not body:
            return
        body = _drop_junk_words(body).strip()
        if not body:  # all-junk section: skip, don't index an empty entry
            return
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
        stripped = line.strip()
        # Fenced code is payload, not structure: a ```bash block containing
        # "# install the thing" used to register as a level-1 heading, evict
        # the real stack, and brand every later chunk with the comment as its
        # ancestry — plus spurious hash churn on every reindex. Tildes count:
        # GFM allows ~~~ fences too.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            buf.append(line)
            continue
        m = None if in_fence else HEADING_RE.match(line)
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
    body = _drop_junk_words(body).strip()
    if not body:  # all-junk file: skip, don't index an empty entry
        return []
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


class DepMissing(Exception):
    """A format's parser dependency isn't installed — count as dep_gated."""


_ocr_engine = None


def _ocr(src) -> str | None:
    """OCR a path or PNG bytes via rapidocr (lazy warm singleton, ~1.5s/image
    on this CPU). None = dependency missing; "" = no text found."""
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            return None
        log.info("loading OCR engine (rapidocr)…")
        _ocr_engine = RapidOCR()
    result, _elapse = _ocr_engine(src)
    if not result:
        return ""
    return "\n".join(t for _box, t, score in result if float(score) >= 0.5)


OCR_MAX_PAGES_PER_FILE = 10  # scanned-PDF OCR budget; log what's skipped


def chunk_pdf_file(file_label: str, path: Path) -> list[dict]:
    """Per-page text extraction; a textless page (scan) falls back to OCR
    within the per-file budget. Pages become '## page N' sections so hits are
    self-describing. Also handles epub/xps — pymupdf opens those too."""
    try:
        import pymupdf
    except ImportError as e:
        raise DepMissing(str(e))
    parts, ocr_budget = [], OCR_MAX_PAGES_PER_FILE
    skipped_pages = 0
    with pymupdf.open(path) as doc:
        for page in doc:
            text = page.get_text().strip()
            if not text:
                if ocr_budget <= 0:
                    skipped_pages += 1
                    continue
                ocr = _ocr(page.get_pixmap(dpi=150).tobytes("png"))
                if ocr is None:  # no OCR dep: index the text pages we do have
                    log.info("%s: textless page %d skipped (no OCR dep)",
                             file_label, page.number + 1)
                    continue
                ocr_budget -= 1
                text = ocr.strip()
            if text:
                parts.append(f"## page {page.number + 1}\n\n{text}")
    if skipped_pages:
        log.info("%s: %d textless page(s) beyond the %d-page OCR budget, "
                 "not indexed", file_label, skipped_pages,
                 OCR_MAX_PAGES_PER_FILE)
    return chunk_markdown(file_label, "\n\n".join(parts))


def chunk_image_file(file_label: str, path: Path) -> list[dict]:
    text = _ocr(str(path))
    if text is None:
        raise DepMissing("rapidocr-onnxruntime not installed")
    if not text.strip():
        return []
    return chunk_plaintext(file_label, text)


def _text_chunker(fn):
    def chunk(file_label: str, path: Path) -> list[dict]:
        return fn(file_label, path.read_text(errors="replace"))
    return chunk


# Contract: chunker(rel_label, Path) -> chunks. Text formats read+delegate;
# binary formats parse the file themselves.
CHUNKERS = {
    ".md": _text_chunker(chunk_markdown),
    ".txt": _text_chunker(chunk_plaintext),
    ".text": _text_chunker(chunk_plaintext),
    ".html": _text_chunker(chunk_html),
    ".htm": _text_chunker(chunk_html),
    ".pdf": chunk_pdf_file,
    ".epub": chunk_pdf_file,
    ".png": chunk_image_file,
    ".jpg": chunk_image_file,
    ".jpeg": chunk_image_file,
    ".webp": chunk_image_file,
    ".tiff": chunk_image_file,
}

# Formats we recognize but still can't parse. Counted in the reindex stats so
# the cost stays visible (a runtime DepMissing lands in the same counter).
DEP_GATED_EXTS = {".docx", ".doc", ".gif"}


def ingest_vault(db: Database, root: Path, dirs: list[str]) -> dict:
    """Incremental walk: unchanged mtimes are skipped entirely; changed files
    get the per-chunk hash diff; files gone from disk lose their entries.
    One unreadable file never aborts the walk."""
    indexed = db.vault_file_mtimes()
    seen: set[str] = set()
    stats = {"files_scanned": 0, "files_changed": 0, "added": 0, "deleted": 0,
             "dep_gated": 0, "unsupported": 0, "unparseable": 0}
    gated_names: list[str] = []
    unsupported_names: list[str] = []
    walked_prefixes: list[str] = []
    for d in dirs:
        base = root / d
        if not base.is_dir():
            continue
        walked_prefixes.append(d.rstrip("/") + "/")
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
                else:
                    # .csv/.eml/.json/.zip and friends: no chunker, and until
                    # now not even counted — the inbox announced "Indexed N
                    # files — searchable now" over files that indexed nothing.
                    stats["unsupported"] += 1
                    if len(unsupported_names) < 5:
                        unsupported_names.append(rel)
                continue
            # stat() before anything is counted: a file that vanished between
            # the rglob and here must fall through to the prune pass below
            # rather than being marked `seen` and held in the index until the
            # next restart.
            try:
                mtime = p.stat().st_mtime
            except OSError as e:
                log.warning("ingest: skipping %s: %s", rel, e)
                continue
            seen.add(rel)
            stats["files_scanned"] += 1
            try:
                if indexed.get(rel) == mtime:
                    continue
                chunks = chunker(rel, p)
                added, deleted = db.replace_file_entries(rel, mtime, chunks)
            except DepMissing:
                # `rel` deliberately STAYS in `seen`: a missing parser dep is a
                # statement about this process, not about the file, so the prune
                # pass below must not treat it as "gone from disk". Discarding it
                # here (until 2026-08-10) meant an already-good index was DELETED
                # the first time a dep broke — index a PDF, let rapidocr fail to
                # import after an onnxruntime upgrade, touch the file, and the
                # walk reported {'deleted': 1, 'dep_gated': 1} and every OCR'd
                # document silently left search. The sibling OSError/Exception
                # handlers never discarded, and the module docstring promises we
                # "degrade back to dep_gated counting" — this is that promise.
                # The stat adjustments stay: nothing was scanned, one was gated.
                stats["files_scanned"] -= 1
                stats["dep_gated"] += 1
                gated_names.append(rel)
                continue
            except OSError as e:
                log.warning("ingest: skipping %s: %s", rel, e)
                continue
            except Exception as e:  # one corrupt pdf/image must not end the walk
                # ...but it must not bill us forever either. Until now a file
                # whose chunker raised never recorded its mtime, so with the
                # inbox reindexing the whole vault per arrival, one corrupt
                # scanned PDF re-paid up to 10 OCR pages (~15s) per arrival,
                # indefinitely, with files_changed at 0 hiding it. Record the
                # mtime with zero chunks: skipped until touched again, and any
                # stale entries for it are dropped rather than quoted forever.
                # (DepMissing is separate above on purpose: a broken dep comes
                # back, a corrupt file doesn't fix itself.)
                log.warning("ingest: failed to parse %s: %s", rel, e)
                added, deleted = db.replace_file_entries(rel, mtime, [])
                stats["files_changed"] += 1
                stats["added"] += added
                stats["deleted"] += deleted
                stats["unparseable"] += 1
                continue
            stats["files_changed"] += 1
            stats["added"] += added
            stats["deleted"] += deleted
    # Prune stale entries only under dirs we actually walked this pass: a
    # briefly-absent index_dir (unmounted, mid-sync) must not purge its whole
    # index — nothing under it was `seen`, so a naive prefix match over all
    # configured dirs would delete every entry and force a full re-chunk +
    # re-embed when it returns (L6).
    prefixes = tuple(walked_prefixes)
    if prefixes:
        for path in indexed:
            if path not in seen and path.startswith(prefixes):
                stats["deleted"] += db.delete_file_entries(path)
    if gated_names:
        log.info("ingest: %d file(s) need PDF/OCR deps, not indexed: %s%s",
                 len(gated_names), ", ".join(gated_names[:5]),
                 "…" if len(gated_names) > 5 else "")
    if unsupported_names:
        log.info("ingest: %d unsupported file(s), not indexed: %s%s",
                 stats["unsupported"], ", ".join(unsupported_names),
                 "…" if stats["unsupported"] > 5 else "")
    return stats
