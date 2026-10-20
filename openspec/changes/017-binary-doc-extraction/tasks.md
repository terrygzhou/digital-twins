# Tasks: 017 — binary document extraction (PDF / XLSX / DOCX)
(Mirrors `specs/017-binary-doc-extraction/tasks.md`.)

## 1. Knob
- [ ] 1.1 `extraction.extract_binary` (bool, default `false`) in
      `config/knobs.py` + `config/schema.py`; env `KB_EXTRACTION__EXTRACT_BINARY`.
- [ ] 1.2 Unit: knob off → all files route `raw`.

## 2. Extraction layer
- [ ] 2.1 `tests/unit/test_extract.py` (RED): PDF/XLSX/DOCX/unknown
      routing, `extractor_unavailable` (dep missing), `extraction_failed`
      (corrupt), `raw` fallback, knob-off path. Fixtures in
      `tests/fixtures/`.
- [ ] 2.2 Implement `digital_twins/ingest/extract.py`
      (`extract_text`, lazy backend imports, typed errors, `raw`
      fallback).

## 3. Optional extras
- [ ] 3.1 `pyproject.toml`: `[pdf] [xlsx] [docx] [docs]` optional
      deps; base pulls none.

## 4. Wire into ingestion
- [ ] 4.1 `ingest/onshot.py` (016): route through `extract_text` when
      knob on; `metadata["extractor"]`; content hash over extracted text.
- [ ] 4.2 `sources/fs.py`: parity.

## 5. Integration + guards
- [ ] 5.1 Integration: `kb_ingest_folder` over a tmp folder with a real
      PDF → searchable by its text; XLSX → searchable by a cell.
- [ ] 5.2 Guards: `test_portability.py` + `test_knob_docs.py` green.
- [ ] 5.3 Full suite.

## 6. Semver + release note
- [ ] 6.1 `__version__` MINOR bump + CHANGELOG.

## 7. Bookkeeping
- [ ] 7.1 OpenSpec 017 validated.
- [ ] 7.2 SDD ledger `017-binary-doc-extraction/progress.md`.
