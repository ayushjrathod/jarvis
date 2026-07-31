# Inbox — drop exports here to make them searchable

Anything dropped in this directory is **indexed automatically, within about a
minute** — the dispatcher watches this folder and reacts when a file lands
(you'll get a "Indexed … — searchable now" notification). It waits for the
file to stop changing first, so a large PDF still downloading isn't indexed
half-written.

You can still force a pass with `POST /memory/reindex` (or ask Jarvis to
"reindex the vault"), and everything here is re-checked on dispatcher start.

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

Files are indexed in place — nothing is moved or renamed. Delete one and its
entries disappear on the next pass (the watcher notices removals too, it just
doesn't announce them).

Config lives under `dispatcher.inbox` in `config.yaml`: `check_interval_s`
(60), `notify`, and `summarize` — the last is **off** by default, since
summarising each new file costs one model call; turn it on and Jarvis will
also tell you what the file *is*.
