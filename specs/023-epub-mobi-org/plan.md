# Plan: 023 — EPUB / MOBI / Org-mode text extraction

## Summary

Extends 017's text-extraction layer (`digital_twins/ingest/extract.py`)
with three more suffix routes — `.epub` → `epub` extractor (`ebooklib`),
`.mobi` → `mobi` extractor (`mobiparser`), `.org` → `raw` (plain text,
no new dependency) — so e-book and Org-mode files enter the
chunk/embed/dedup pipeline as clean text instead of garbled bytes. The
feature is a single addition to 017's existing `extract_text` routing
dict; `extractor_name` returns `"epub"` / `"mobi"` / `"raw"`
respectively. Everything sits behind one new knob,
`sources.epub.enabled` (bool, default `false`, env
`KB_SOURCES__EPUB__ENABLED`); when off, all three suffixes fall to
`raw` and behaviour is byte-for-byte unchanged (NFR-1 dedup still
holds — `content_hash` is computed over extracted text, not raw bytes).
New optional extra `digital-twins[epub]` (pulls in `ebooklib` +
`mobiparser`) is declared in `pyproject.toml`; the base distribution
installs neither. Per-file error contract is 017's unchanged:
`extractor_unavailable` (dep absent), `extraction_failed` (corrupt
file), empty book → zero-length text → item skipped. No host paths, no
GPL code (NFR-13), no `python3.12` / `CUDA_VISIBLE_DEVICES` leaks.

**Prerequisite:** the 017 extraction layer (`digital_twins/ingest/
extract.py` — `extract_text`, `ExtractorUnavailable`,
`ExtractionFailed`, suffix→backend registry) must have landed first;
this checkout does not yet contain it. 023's tasks assume 017's T2.x
(T extraction layer) is complete before 023 work starts.

## Approach

1. **Knob** (`digital_twins/config/knobs.py` +
   `digital_twins/config/schema.py`): add
   `sources.epub.enabled` (bool, default `false`, env
   `KB_SOURCES__EPUB__ENABLED`, `group=GROUP_SOURCES`). `epub` is **not**
   a `BUILTIN_SOURCES` entry — it is an extraction-routing gate, not a
   `Source`-conforming object — so it is a bespoke entry in the `KNOBS`
   dict, not part of the `BUILTIN_SOURCES` loop. `schema.py`'s
   `DEFAULTS` gains `sources.epub.enabled: False`. The T027
   knob-docs sync guard (`tests/unit/test_knob_docs.py`) sees the new
   key; `docs/configuration.md` documents it.

2. **Extraction layer** (`digital_twins/ingest/extract.py`, 017): add
   two entries to 017's suffix→backend registry (lazy imports,
   mirroring 017's `pypdf` / `openpyxl` / `python-docx` backends):
   - `.epub` → `epub` backend → `extract_epub` (lazy-import
     `ebooklib.epub`); one `# <chapter-title>` header + chapter body
     per chapter, `\n\n`-separated. Corrupt EPUB (missing
     `container.xml`) → raise `ExtractionFailed`.
   - `.mobi` → `mobi` backend → `extract_mobi` (lazy-import
     `mobiparser`); one `# Page: <n>` block per page, concatenated.
     Empty book (zero pages) → zero-length string → caller skips.
   - `.org` → `raw` (no new extractor; 017's raw path
     (`path.read_bytes().decode("utf-8", errors="replace")`) is
     already correct for UTF-8 plain-text Org-mode).
   The 017 `extract_text` API is unchanged — the routing dict is an
   addition, not a rewrite; the `raw` fallback and the
   `ExtractorUnavailable` / `ExtractionFailed` contracts are
   unchanged.

3. **Extractor module** (`digital_twins/ingest/extract_epub.py`, new
   — spec's FR-2 alternative location; `sources/epub.py` would mix
   source objects with extraction logic, `ingest/` is the right home):
   - `extract_epub(path: Path) -> str` — open with `ebooklib.epub`,
     iterate chapters, one `# <title>` + body per chapter,
     `\n\n`-separated; raises `ExtractionFailed` on corrupt / missing
     `container.xml`; raises `ExtractorUnavailable` when
     `ebooklib` is not importable (deferred to the 017 registry's
     lazy-import layer; the module itself assumes the dep is present).
   - `extract_mobi(path: Path) -> str` — thin wrapper around
     `mobiparser`'s `parse`; one `# Page: <n>` block per page,
     `\n\n`-separated; same exception contract.
   Both echo only the caller-supplied `path` in any error message
   (NFR-13).

4. **Wire into ingestion** (`digital_twins/sources/fs.py` +
   `digital_twins/ingest/onshot.py`): when `sources.epub.enabled` is
   on, `read()` calls `extract_text` (017's layer, which now carries
   the new routes); when off, fall through to the bare
   `read_text` / `read_bytes` path — byte-for-byte unchanged.
   `metadata["extractor"]` carries the 017 `extractor_name`
   (`"epub"` / `"mobi"` / `"raw"`), so FR-6's 016 tool-summary `pdf|
   xlsx|docx|raw` enum gains `epub` / `mobi`. `content_hash` is
   computed over the extracted text (NFR-1). Per-file extractor
   failure → item-level error, run continues.

5. **Optional extras** (`pyproject.toml`): add
   `epub = ["ebooklib>=0.19", "mobiparser>=1.8"]` under
   `[project.optional-dependencies]`. A combined
   `epub-mobi = ["ebooklib>=0.19", "mobiparser>=1.8"]` alias is a
   v1.1 add; the v1 shipping default is one combined `epub` extra
   (both deps are small; a separate `[mobi]` extra is a v1.1 add if
   the operator wants a leaner install). Base install pulls
   neither.

6. **Tests (Test-First, constitution Principle III)**:
   - `tests/unit/test_extract_epub.py` (new): a 1-chapter EPUB
     generated in-test (via `ebooklib.epub` if available, else a
     checked-in fixture under `tests/fixtures/`); assert
     `extract_text` routing to `epub` with the `# <title>` block
     shape; monkeypatch the 017 extractor registry to drop the
     `epub` backend → per-file `extractor_unavailable`, other
     files still extract; corrupt EPUB fixture (missing
     `container.xml`) → `extraction_failed`; empty EPUB →
     zero-length → skipped. No host paths (NFR-13) — `tmp_path`
     throughout.
   - `tests/unit/test_extract_mobi.py` (new): a 1-page MOBI fixture
     (generated in-test if `mobiparser` is importable, else a
     checked-in fixture under `tests/fixtures/`); assert
     per-page `# Page: <n>` block shape; missing `mobiparser` →
     per-file `extractor_unavailable`.
   - Extend `tests/unit/test_extract.py` (017): `.org` routes to
     `raw` (byte-for-byte identical to `FsSource.read`);
     `sources.epub.enabled: false` → all three suffixes fall to
     `raw` (byte-for-byte current behaviour).
   - Integration: `kb_ingest_folder` over a tmp folder containing
     `book.epub` + `manual.mobi` + `notes.org` → `kb_search` finds
     an EPUB chapter body under the `source_name` scope (007 ACL
     unchanged); re-ingest → 0 new points (NFR-1 dedup over
     extracted text).
   - Guards: `tests/integration/test_portability.py` (T006) and
     `tests/unit/test_knob_docs.py` (T027) stay green; new knob
     documented in `docs/configuration.md`.

## Files touched

- `digital_twins/config/knobs.py` (new `sources.epub.enabled` entry,
  `group=GROUP_SOURCES`, env `KB_SOURCES__EPUB__ENABLED`, default
  `false`)
- `digital_twins/config/schema.py` (`DEFAULTS["sources.epub.enabled"]
  = False`)
- `digital_twins/ingest/extract.py` (017 — add `.epub` / `.mobi`
  entries to the suffix→backend registry; document `.org` → `raw`
  as the expected case, not a new route)
- `digital_twins/ingest/extract_epub.py` (new — `extract_epub` /
  `extract_mobi`; lazy imports; `ExtractorUnavailable` /
  `ExtractionFailed` raised at call sites)
- `digital_twins/sources/fs.py` (when knob on, `read()` calls
  `extract_text`; `metadata["extractor"]` carries the name)
- `digital_twins/ingest/onshot.py` (016 parity — same change)
- `pyproject.toml` (new `epub` extra under
  `[project.optional-dependencies]`: `ebooklib>=0.19`,
  `mobiparser>=1.8`)
- `docs/configuration.md` (document `sources.epub.enabled` in the
  Sources group; default `false`; note the optional extra)
- `tests/unit/test_extract_epub.py` (new)
- `tests/unit/test_extract_mobi.py` (new)
- `tests/unit/test_extract.py` (extend — 017 file)
- `tests/integration/test_mcp_kb_tools.py` (extend — 016/017
  integration test)
- `CHANGELOG.md` (0.16.0 entry — new optional extra + default-off
  knob = MINOR)
- `digital_twins/__init__.py` (version bump 0.15.0 → 0.16.0)
- `specs/023-epub-mobi-org/tasks.md` (this feature's tasks)
- `.superpowers/sdd/023-epub-mobi-org/progress.md` (SDD ledger)
- `openspec/changes/023-epub-mobi-org/` (proposal / spec / design /
  tasks)

## Open questions

- `.org` extractor: dedicated `org` extractor (parse heading
  hierarchy into `# <heading>` blocks) or fall to `raw` (plain
  text, no structure)? — default per the spec: `raw` (plain text;
  a v1.1 heading-hierarchy add is a trivial extension).
- `digital-twins[epub]` extra: combined (`ebooklib` +
  `mobiparser`) or a separate `digital-twins[mobi]` extra? —
  default per the spec: one combined `digital-twins[epub]` extra
  (both deps are small; a separate `[mobi]` extra is a v1.1 add
  if the operator wants a leaner install).
- **017 dependency:** the 017 extraction layer
  (`digital_twins/ingest/extract.py`) is not yet present in this
  checkout — 023's T2.x extraction-layer tasks assume 017's
  extraction layer has landed (017's T2.1 / T2.2 complete). If 017
  has not landed, 023 blocks until it does; 023 cannot be
  implemented on top of a missing 017 `extract_text`.

## Semver

MINOR (0.15.0 → 0.16.0): additive, default-off knob
(`sources.epub.enabled: false`), new optional extras; when off,
behaviour is byte-for-byte unchanged. No DDL, no tool-surface
change (the 016 `extractor` enum gains two values, additive).
