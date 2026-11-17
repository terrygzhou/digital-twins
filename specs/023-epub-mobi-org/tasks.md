# Tasks: 023 — EPUB / MOBI / Org-mode text extraction

**Prerequisite:** 017's extraction layer (`digital_twins/ingest/
extract.py` — `extract_text`, `ExtractorUnavailable`,
`ExtractionFailed`, suffix→backend registry) must have landed
first (017 T2.1 / T2.2). If 017's T2.x are still open, block here;
023 extends 017's routing dict and cannot be implemented on top of
a missing `extract_text`.

## 1. Knob (RED-first, constitution Principle III)

- [ ] 1.1 Write a RED test in `tests/unit/test_knob_docs.py` (T027
      guard): assert `KNOBS` has `sources.epub.enabled` with
      `type == "bool"`, `default is False`, `env ==
      "KB_SOURCES__EPUB__ENABLED"`, `group == GROUP_SOURCES`;
      assert `schema.py`'s `DEFAULTS` carries
      `DEFAULTS["sources.epub.enabled"] is False`. **Verification:**
      test fails on current code (knob absent from both files).
- [ ] 1.2 Add `sources.epub.enabled` to `digital_twins/config/knobs.py`
      (bespoke entry, **not** part of the `BUILTIN_SOURCES` loop —
      `epub` is an extraction-routing gate, not a `Source` object)
      and to `digital_twins/config/schema.py` `DEFAULTS` as
      `False`. **Verification:** T027 updated + green; a fresh
      config load with the knob absent returns `False`; setting
      `KB_SOURCES__EPUB__ENABLED=true` in the env returns `True`.

## 2. Extraction layer (RED-first)

- [ ] 2.1 Write unit tests in `tests/unit/test_extract_epub.py`
      (new file): a 1-chapter EPUB fixture (generated in-test via
      `ebooklib.epub` if importable, else a checked-in fixture
      under `tests/fixtures/`) → `extract_text` routes to the
      `epub` backend, returns one `# <chapter-title>` header + body
      per chapter, `\n\n`-separated; monkeypatch the 017 extractor
      registry to drop the `epub` backend → per-file
      `extractor_unavailable`, other files still extract; corrupt
      EPUB fixture (missing `container.xml`) → `extraction_failed`;
      empty EPUB (zero chapters) → zero-length text → item
      skipped. No host paths (NFR-13) — use `tmp_path` /
      checked-in fixtures only. **Verification:** tests fail on
      current code (no `epub` route in the 017 registry).
- [ ] 2.2 Write unit tests in `tests/unit/test_extract_mobi.py`
      (new file): a 1-page MOBI fixture (generated in-test via
      `mobiparser` if importable, else a checked-in fixture under
      `tests/fixtures/`) → `extract_text` routes to the `mobi`
      backend, returns one `# Page: <n>` block per page,
      `\n\n`-separated; missing `mobiparser` → per-file
      `extractor_unavailable`; empty MOBI (zero pages) →
      zero-length text → item skipped. **Verification:** tests
      fail on current code (no `mobi` route in the 017 registry).
- [ ] 2.3 Implement `digital_twins/ingest/extract_epub.py` (new):
      `extract_epub(path: Path) -> str` (lazy-import
      `ebooklib.epub`; one `# <title>` + body per chapter,
      `\n\n`-separated; raises `ExtractionFailed` on corrupt /
      missing `container.xml`) and `extract_mobi(path: Path) ->
      str` (lazy-import `mobiparser`; one `# Page: <n>` block per
      page, `\n\n`-separated; same exception contract). Both echo
      only the caller-supplied `path` in error messages (NFR-13).
      **Verification:** §2.1 / §2.2 tests green.
- [ ] 2.4 Extend the 017 registry in `digital_twins/ingest/
      extract.py`: add two entries — `.epub` → `epub` (→
      `extract_epub`), `.mobi` → `mobi` (→ `extract_mobi`) — with
      lazy imports, mirroring the 017 `pypdf` / `openpyxl` /
      `python-docx` backends. `.org` is **not** a new route; it
      falls to the 017 `raw` path (document it in the registry
      docstring as the expected case, not a new entry). **Verification:**
      `tests/unit/test_extract.py` extended — a `.org` file
      routes to `raw` (byte-for-byte identical to
      `FsSource.read`); `sources.epub.enabled: false` → all three
      new suffixes fall to `raw` (byte-for-byte current behaviour);
      `tests/unit/test_extract_epub.py` +
      `tests/unit/test_extract_mobi.py` green.

## 3. Optional extras

- [ ] 3.1 `pyproject.toml`: under
      `[project.optional-dependencies]`, add
      `epub = ["ebooklib>=0.19", "mobiparser>=1.8"]`. A
      combined `epub-mobi = ["ebooklib>=0.19",
      "mobiparser>=1.8"]` alias is a v1.1 add; the v1 shipping
      default is one combined `epub` extra. Base install pulls
      neither. **Verification:** `pip install .` (no extras) still
      imports `digital_twins` cleanly; `pip install ".[epub]"`
      installs both `ebooklib` and `mobiparser`.

## 4. Wire into ingestion

- [ ] 4.1 `digital_twins/ingest/onshot.py` (016): when
      `sources.epub.enabled` is on, `read()` calls
      `extract_text` instead of bare `read_text`; set
      `metadata["extractor"]` to the 017 `extractor_name`
      (`"epub"` / `"mobi"` / `"raw"`); `content_hash` computed
      over extracted text (NFR-1). Per-file extractor failure →
      item error, run continues. **Verification:** 016 unit
      tests extended — a tmp folder with a real 1-chapter EPUB +
      a 1-page MOBI + a 1-paragraph `.org` stores extracted text
      under the `epub` / `mobi` / `raw` names respectively, with
      the right `extractor` metadata; re-ingest → 0 new points
      (NFR-1 dedup over extracted text).
- [ ] 4.2 `digital_twins/sources/fs.py` (parity): same change —
      when the knob is on, `read()` calls `extract_text` for
      `.epub` / `.mobi` / `.org` (suffix routing in 017's
      `extract_text` is the single source of truth; no second
      routing path); when off, fall through to bare
      `read_bytes().decode("utf-8", errors="replace")` —
      byte-for-byte unchanged. **Verification:** an
      `fs`-source unit test with the knob on over a tmp dir
      containing a 1-chapter EPUB + a 1-page MOBI + a 1-paragraph
      `.org` stores extracted text, not bytes; with the knob off,
      all three fall to `raw` (byte-for-byte current behaviour).

## 5. Integration + guards

- [ ] 5.1 Integration test (mirror 016/017's): `kb_ingest_folder`
      over a tmp folder with a real 1-chapter EPUB → `kb_search`
      finds the chapter body under the document's `source_name`
      owner scope (007 ACL unchanged); a 1-page MOBI → finds the
      per-page text; a 1-paragraph `.org` → finds the paragraph.
      Re-ingest → 0 new points. **Verification:** test green.
- [ ] 5.2 Guards: `python3 -m pytest tests/integration/
      test_portability.py tests/unit/test_knob_docs.py -q` → both
      green (knob documented; no host paths; NFR-13: the new
      `extract_epub` / `extract_mobi` error messages echo only the
      caller-supplied `path`, no host paths). **Verification:**
      both guards green.
- [ ] 5.3 Full suite: `python3 -m pytest tests/ -q` → all green.

## 6. Semver + release note

- [ ] 6.1 Bump `digital_twins/__init__.py` `__version__` to
      0.16.0 (MINOR — additive, default-off knob; new optional
      extras; core unchanged).
- [ ] 6.2 `CHANGELOG.md` 0.16.0 entry: "Added: EPUB / MOBI /
      Org-mode text extraction (023) — extends 017's `extract_text`
      suffix routing with `.epub` (→ `epub` extractor,
      `ebooklib`) and `.mobi` (→ `mobi` extractor, `mobiparser`);
      `.org` falls to `raw` (plain text). New default-off knob
      `sources.epub.enabled` (env `KB_SOURCES__EPUB__ENABLED`);
      when off, all three suffixes fall to `raw` and behaviour is
      byte-for-byte unchanged. New optional extra
      `digital-twins[epub]` (pulls in `ebooklib>=0.19`,
      `mobiparser>=1.8`); the base distribution installs neither.
      Per-file error contract unchanged from 017
      (`extractor_unavailable` / `extraction_failed`); the 016
      tool-summary `extractor` enum gains `epub` / `mobi`
      (additive)."

## 7. Bookkeeping

- [ ] 7.1 OpenSpec artifacts: `openspec/changes/
      023-epub-mobi-org/` (proposal / spec / design / tasks)
      validated with `openspec validate 023-epub-mobi-org` if the
      CLI is available.
- [ ] 7.2 SDD ledger: `.superpowers/sdd/023-epub-mobi-org/
      progress.md` created and kept in sync with commits.

## Verification summary

- `pytest tests/unit/test_extract_epub.py tests/unit/test_extract_mobi.py
  tests/unit/test_extract.py tests/unit/test_onshot_folder.py
  tests/unit/test_fs_source.py tests/unit/test_knob_docs.py -q`
  → all passed (knob present; `epub` / `mobi` / `raw` routing
  correct; off-by-default → byte-for-byte unchanged; NFR-13
  errors echo only the caller-supplied path).
- `pytest tests/ -q` → all green.
- `pytest tests/integration/test_portability.py
  tests/integration/test_mcp_kb_tools.py -q` → both guards green
  (portability T006; 016/017/023 integration dedup over extracted
  text).
