# Plan: 017 binary document extraction (PDF / XLSX / DOCX)

## Summary

Adds a text-extraction layer so `kb_ingest_folder` (016) and the `fs`
source can ingest binary document files as clean, searchable text
instead of storing raw bytes. Gated behind a default-off knob
(`extraction.extract_binary`) and optional dependency extras, so the
base distribution and every existing deployment are byte-for-byte
unchanged when the knob is off.

## Approach

1. **Extraction layer** (`digital_twings/ingest/extract.py`, new):
   - `extract_text(path, suffix, cfg) -> (text, extractor)`; a small
     suffix→backend registry:
     - `pdf` → `pypdf` (lazy-import; per-file failure on missing/corrupt)
     - `xlsx`/`xls` → `openpyxl` (per-sheet `# Sheet: <name>` blocks)
     - `docx` → `python-docx`
     - fallback `raw` → `path.read_bytes().decode("utf-8", errors="replace")`
       (identical to `FsSource.read`)
   - Each backend is a **lazy import** inside its function: an absent
     dependency yields a typed `ExtractorUnavailable` that the caller
     turns into a per-file error, never a run crash.

2. **Knob** (`config/knobs.py` + `config/schema.py`):
   `extraction.extract_binary` (bool, default `false`), nested under the
   existing `extraction.*` group. Off = everything routes to `raw`.

3. **Wire into ingestion**:
   - 016 `OneShotFolderSource.read` and `sources/fs.py` `read()`: when
     `extraction.extract_binary` is on, call `extract_text` instead of
     bare `read_text`; store `metadata["extractor"]`. The content hash
     is over the **extracted text** (NFR-1 dedup stays content-based).
   - Per-file extractor failure → item-level error, run continues
     (mirrors `preflight-optional-deps`).

4. **Optional extras** (`pyproject.toml`): `pdf`, `xlsx`, `docx`, and a
   combined `docs`. Base install pulls none. NFR-13: no host paths,
   deps declared, not hardcoded.

5. **Tests (Test-First)**:
   - `tests/unit/test_extract.py`: tmp fixtures (minimal real PDF/XLSX/
     DOCX generated in-test or checked in as tiny fixtures), assert
     routing + text shape; monkeypatch the registry to simulate a
     missing dep → per-file `extractor_unavailable`; `raw` fallback for
     unknown suffix; `extraction.extract_binary=false` → all `raw`.
   - Integration: `kb_ingest_folder` over a tmp folder with a real PDF
     → point is searchable by its text (mirror 016's integration test).
   - Guards: `test_portability.py` + `test_knob_docs.py` stay green;
     new knob documented (knob-docs test).

## Files touched

- `digital_twings/ingest/extract.py` (new)
- `digital_twings/ingest/onshot.py` (016 — use `extract_text`)
- `digital_twings/sources/fs.py` (parity — use `extract_text`)
- `digital_twings/config/knobs.py` + `config/schema.py` (knob)
- `pyproject.toml` (optional extras)
- `tests/unit/test_extract.py` (new), 016 integration test (extend)

## Open questions

- XLSX cell serialization format (tab vs `|`): default to `|`-separated
  to match the earlier manual openpyxl dump; a 017 follow-up could make
  it a knob.
- `.xls` (binary) vs `.xlsx` (zip): `openpyxl` reads `.xlsx` only;
  `.xls` needs `xlrd` — ship `.xls` → `raw` (documented) unless
  `xlrd` is added as an optional dep in a later version.
