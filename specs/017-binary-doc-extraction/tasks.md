# Tasks: 017 — binary document extraction (PDF / XLSX / DOCX)

## 1. Knob (RED-first, constitution Principle III)
- [ ] 1.1 Add `extraction.extract_binary` (bool, default `false`) to
      `digital_twings/config/knobs.py` + `digital_twings/config/schema.py`
      (nested under the existing `extraction` group; env:
      `KB_EXTRACTION__EXTRACT_BINARY`). **Verification:** `test_knob_docs.py`
      (T027) updated + green; knob absent from a fresh config → default
      `false`.
- [ ] 1.2 Unit test: `extraction.extract_binary` off (default) → every file
      routes to `raw` extractor (byte-for-byte current behaviour).

## 2. Extraction layer (RED-first)
- [ ] 2.1 Write unit tests in `tests/unit/test_extract.py` (new file):
      - a real minimal `.pdf` (1 page, text layer) → `pypdf` text
      - a 2-sheet `.xlsx` → per-sheet `# Sheet: <name>` blocks,
        `|`-separated cells
      - a 1-paragraph `.docx` → paragraph text
      - unknown / `.txt` / `.md` / no-extension → `raw`
      - monkeypatch the extractor registry to drop a backend → per-file
        `extractor_unavailable`, other files still extract
      - corrupt PDF fixture → `extraction_failed`
      **Verification:** tests fail on current code (module absent).
      Fixtures live under `tests/fixtures/` (tiny, checked in); no host
      paths (NFR-13).
- [ ] 2.2 Implement `digital_twings/ingest/extract.py`:
      `extract_text(path, suffix, cfg) -> tuple[str, str]` +
      `ExtractorUnavailable` / `ExtractionFailed` types + a
      suffix→backend registry with **lazy** imports. `raw` fallback =
      `path.read_bytes().decode("utf-8", errors="replace")` (same as
      `FsSource.read`). **Verification:** §2.1 tests green.

## 3. Optional extras
- [ ] 3.1 `pyproject.toml`: `[project.optional-dependencies]`
      `pdf = ["pypdf"]`, `xlsx = ["openpyxl"]`, `docx = ["python-docx"]`,
      `docs = [pdf + xlsx + docx]`. Base install pulls none.
      **Verification:** `pip install .` (no extras) still imports
      `digital_twings` cleanly; `pip install .[docs]` installs all three.

## 4. Wire into ingestion
- [ ] 4.1 `digital_twings/ingest/onshot.py` (016): when
      `extraction.extract_binary` is on, `read()` calls `extract_text`
      instead of bare `read_text`; set `metadata["extractor"]`; content
      hash computed over extracted text (NFR-1). Per-file extractor
      failure → item error, run continues. **Verification:** 016 unit
      tests extended to assert `extractor` metadata + continuation on
      failure.
- [ ] 4.2 `digital_twings/sources/fs.py` (parity): same change.
      **Verification:** an `fs`-source unit test with the knob on over a
      tmp dir containing a PDF stores extracted text, not bytes.

## 5. Integration + guards
- [ ] 5.1 Integration test (mirror 016's): `kb_ingest_folder` over a tmp
      folder with a real 1-page PDF → `kb_search` finds its text; a
      `.xlsx` → finds a cell value. **Verification:** test green.
- [ ] 5.2 Guards: `python3 -m pytest tests/integration/test_portability.py
      tests/unit/test_knob_docs.py -q` → both green (knob documented; no
      host paths).
- [ ] 5.3 Full suite.

## 6. Semver + release note
- [ ] 6.1 Bump `__version__` (MINOR) + CHANGELOG entry (new optional
      extras; default-off knob; core unchanged).

## 7. Bookkeeping
- [ ] 7.1 OpenSpec artifacts for 017 validated.
- [ ] 7.2 SDD ledger `.superpowers/sdd/017-binary-doc-extraction/progress.md`.
