# Inbox — drop exports here to make them searchable

Anything dropped in this directory gets indexed into `/memory/search` on the
next dispatcher start or `POST /memory/reindex` (ask Jarvis to "reindex the
vault").

Supported now (no extra dependencies):

- `.md` — chunked by heading, ancestry preserved
- `.txt` / `.text` — chunked by paragraph
- `.html` / `.htm` — saved pages and **browser bookmark exports** (links keep
  their URLs)

Recognized but **not indexed yet** (needs dependency approval — pymupdf for
PDF/docx, rapidocr-onnxruntime for images): `.pdf`, `.docx`, `.png`, `.jpg`,
and friends. They're counted as `dep_gated` in the reindex stats so you can
see what's waiting.

Files are indexed in place — nothing is moved or renamed. Delete a file and
its entries disappear on the next reindex.
