# Inbox — drop exports here to make them searchable

Anything dropped in this directory gets indexed into `/memory/search` on the
next dispatcher start or `POST /memory/reindex` (ask Jarvis to "reindex the
vault").

Supported:

- `.md` — chunked by heading, ancestry preserved
- `.txt` / `.text` — chunked by paragraph
- `.html` / `.htm` — saved pages and **browser bookmark exports** (links keep
  their URLs)
- `.pdf` / `.epub` — text per page (pymupdf); scanned/textless pages get OCR
  (first 10 pages per file)
- `.png` / `.jpg` / `.jpeg` / `.webp` / `.tiff` — OCR via rapidocr (~1.5s per
  image, so a big dump makes the reindex noticeably slower once)

Still unsupported: `.docx` / `.doc` / `.gif` — counted as `dep_gated` in the
reindex stats.

Files are indexed in place — nothing is moved or renamed. Delete a file and
its entries disappear on the next reindex.
