# Change: binary document extraction (PDF / XLSX / DOCX)

## Why
The `fs` source and 016's one-shot folder source do `read_text` on every
file, so binary documents (PDF/XLSX/DOCX) are stored as garbled bytes
and cannot be retrieved. This forces a manual "extract upstream, ship
`.txt`" workaround before ingesting.

## What changes
- New `digital_twins/ingest/extract.py`: `extract_text(path, suffix,
  cfg)` routing by suffix — `pdf`→`pypdf`, `xlsx`→`openpyxl`,
  `docx`→`python-docx`, else `raw` (current `read_text` behaviour).
  Backends are lazy-imported; a missing dependency or corrupt file is a
  **per-file** error, never a run crash.
- New knob `extraction.extract_binary` (bool, default `false`). Off →
  everything routes to `raw` (byte-for-byte unchanged). On → the `fs`
  source and `kb_ingest_folder` store **extracted text**; content hash
  (NFR-1) is over the extracted text.
- Optional extras `digital-twins[pdf|xlsx|docx|docs]`; base install
  pulls none (NFR-13 portability).
- `kb_ingest_folder` per-file summary gains `extractor` + the error
  codes `extractor_unavailable` / `extraction_failed`.

## Capabilities
- **Modified**: ingestion (a new, default-off extraction path on the
  `fs` / one-shot sources). **Added**: optional dependency extras.

## Impact
- Affected: `ingest/extract.py` (new), `ingest/onshot.py`,
  `sources/fs.py`, `config/knobs.py` + `config/schema.py`,
  `pyproject.toml`, tests.
- Unaffected (when knob off): every existing deployment, chunker,
  embedder, dedup, graph build.
- Semver: MINOR (additive, default-off).

## Non-goals
No OCR (image-only PDFs); no legacy `.doc` (OLE) → `raw`; no new LLM
call (extraction is deterministic, not coupled to `extraction.enabled`).
