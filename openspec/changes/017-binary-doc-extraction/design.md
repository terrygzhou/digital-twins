# Design: 017 binary document extraction

## Context
`FsSource.read` and 016's `OneShotFolderSource.read` do
`path.read_text(errors="replace")` on every file. For PDF/XLSX/DOCX
that stores binary bytes, not content. The OCI keyva test had to
pre-extract to `.txt` for this reason.

## Decisions
1. **Deterministic extraction, no LLM.** `extract.py` is pure
   text-from-bytes; it is NOT coupled to `extraction.enabled` (that
   knob gates the LLM entity half). Binary text extraction is a
   separate concern and runs even when LLM extraction is off.
2. **Default-off knob.** `extraction.extract_binary` (bool, default
   `false`) keeps the base behaviour (raw bytes) for every deployment
   that doesn't opt in. On = route by suffix to an extractor; off = all
   `raw`.
3. **Lazy, per-file-fail-soft backends.** Each backend is imported
   inside its handler; `ExtractorUnavailable` (dep missing) and
   `ExtractionFailed` (corrupt/unreadable) are per-item errors, the
   pipeline continues (mirrors `preflight-optional-deps`).
4. **Optional extras, lean core.** `pypdf` / `openpyxl` / `python-docx`
   are optional-dependencies, not core requirements — the base package
   installs none (NFR-13). `.xls` (binary) is out of scope
   (`openpyxl` is xlsx-only) and falls to `raw`.
5. **Content hash over extracted text.** NFR-1 dedup is on stored
   content; since we store the extracted text, the hash is over it, so
   a re-ingest of the same file dedups correctly.

## Non-goals
No OCR; no legacy `.doc`; no new LLM call; no change to chunker/embedder
/ graph build.
