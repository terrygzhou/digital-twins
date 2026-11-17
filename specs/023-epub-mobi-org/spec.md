# Feature 023: EPUB / MOBI / Org-mode text extraction (extends 017's suffix routing)

> **License note** (NFR-13 / constitution I): this spec is an
> inspiration-level design reference only. No code, prompts, or test
> fixtures are ported from `nashsu/llm_wiki` (GPL-3.0) into this MIT
> package.

## Background

017 added a text-extraction layer (`digital_twins/ingest/extract.py`)
with suffix routing for `.pdf`, `.xlsx`, `.xls`, `.docx`, falling back
to `raw` for everything else. `nashsu/llm_wiki` (the gap doc's
reference) supports `.epub` and `.mobi` (via `ebooklib` and
`mobiparser`) plus Org-mode plain text. This feature extends the
*same* `extract_text` layer with three more suffixes, following
017's config-gate + dependency-gate + `extractor_unavailable`
per-file-error contract.

## User Scenarios & Testing

An operator ingests a folder containing `book.epub`, `manual.mobi`,
and `notes.org`. Today all three fall to `raw` (an EPUB is a
compressed ZIP → UTF-8 mojibake; MOBI is a binary PDB derivative →
garbled bytes; `.org` is plain text so `raw` works but loses
structure). After this feature, all three extract *clean text*
entering the normal chunk/embed/dedup pipeline.

**Primary scenario**

1. `sources.epub.enabled: true` (default `false` — off, all three
   suffixes still fall to `raw`, behaviour byte-for-byte
   unchanged).
2. An ingest run reads `book.epub`, `manual.mobi`, `notes.org`.
3. 017's `extract_text` (suffix routing) dispatches:
   `.epub` → `ebooklib` (one H1/H2 header + per-chapter body,
   concatenated), `.mobi` → `mobiparser` (per-page text, one
   `# Page: <n>` block per page), `.org` → `raw` (plain-text
   `read_text` — no new extractor needed; Org-mode is plain
   text with an optional heading hierarchy the extractor
   preserves).
4. The extracted text enters the normal chunk/embed/dedup
   pipeline (017's `content_hash` is computed over the *extracted
   text*, not the raw bytes — NFR-1 dedup holds).

**Edge cases**

- `sources.epub.enabled: false` (default) → all three suffixes
  fall to `raw` (byte-for-byte unchanged).
- `ebooklib` not installed (core distro) → `.epub` yields a
  per-file `extractor_unavailable` error, the run continues
  with the other files (mirrors 017's `extractor_unavailable`
  contract).
- `mobiparser` not installed (core distro) → `.mobi` yields a
  per-file `extractor_unavailable` error, the run continues.
- `.org` file → `raw` extractor (plain text; no new dependency
  needed — Org-mode is plain text, not binary). The 017
  `raw` path (`path.read_bytes().decode("utf-8",
  errors="replace")`) already handles it correctly.
- A corrupt EPUB (missing `container.xml`) → per-file
  `extraction_failed` error, the run continues (mirrors 017's
  corrupt-PDF contract).
- An empty EPUB / MOBI (zero pages) → zero-length text →
  skipped (not a crash; mirrors 017's empty-spreadsheet
  contract).
- NFR-13: no host paths in errors; only the caller-supplied
  `path` is echoed.

## Functional Requirements

- **FR-1**: Extend 017's `digital_twins/ingest/extract.py::
  extract_text` suffix routing: `.epub` → `epub` (new
  extractor, `ebooklib`), `.mobi` → `mobi` (new extractor,
  `mobiparser`), `.org` → `raw` (the existing 017 `raw`
  path — plain text, no new extractor). The `extractor_name`
  returned is `"epub"` / `"mobi"` / `"raw"` respectively.
- **FR-2**: Add `digital_twins/sources/epub.py` (or a new
  `digital_twins/ingest/extract_epub.py`): `extract_epub(
  path: Path) -> str` (ebooklib: one H1/H2 header + per-
  chapter body, concatenated with `\n\n` separators, one
  `# <chapter-title>` block per chapter). The `.mobi`
  extractor is a thin wrapper around `mobiparser`'s `parse`
  API: one `# Page: <n>` block per page, concatenated.
- **FR-3**: The new extractors are **config-gated +
  dependency-gated** (mirrors 017 FR-2):
  - knob `sources.epub.enabled` (bool, default `false`) —
    when `false`, no `.epub` / `.mobi` file is routed to a
    new extractor (everything is `raw`, including `.org`,
    so behaviour is byte-for-byte unchanged for existing
    deployments).
  - when `true`, a missing Python dependency for a given
    suffix does NOT raise for the whole run — that one file
    yields a per-file `extractor_unavailable` error and the
    others proceed (017's per-file error-isolation
    contract).
- **FR-4**: `kb_ingest_folder` (016) and the `fs` source use
  017's `extract_text` when `sources.epub.enabled: true` (the
  suffix routing in FR-1 is the single source of truth — no
  second routing path).
- **FR-5**: Optional dependency extras (mirrors 017 FR-4,
  NFR-13 portability): `pip install digital-twins[epub]`
  (pulls in `ebooklib` + `mobiparser`), or a combined
  `digital-twins[epub-mobi]`. The base distribution installs
  neither. The `.org` extractor is plain-text (no extra
  dep — `raw` is already in core).
- **FR-6**: The 016 tool summary's per-file entry
  (`extractor` field, 017 FR-5) now includes `"epub"` /
  `"mobi"` in the `extractor` enum (017's existing
  `pdf|xlsx|docx|raw` gains two values); on extractor
  failure, the per-file `error` code is
  `extractor_unavailable` (017's existing contract, not a
  new code).
- **FR-7**: Test-First (Constitution III). Unit tests use
  `tmp_path` + tiny generated fixtures (a 1-chapter EPUB,
  a 1-page MOBI, a 1-paragraph `.org` file — all generated
  in-test, no host paths). When `ebooklib` / `mobiparser`
  are absent in the test env, the test asserts the per-file
  `extractor_unavailable` contract (monkeypatch the
  extractor registry to simulate absence, mirroring 017
  FR-6). `tests/unit/test_knob_docs.py` green.

## Non-goals

- No change to the chunker / embedder — extraction feeds
  them clean text; they are untouched.
- No change to 017's `extract_text` API (the suffix routing
  is an *addition* to the existing dict; the `raw` fallback
  and the 017 `extractor_unavailable` / `extraction_failed`
  contracts are unchanged).
- No LLM calls (extraction is deterministic; no new LLM
  prompt).
- No video / audio ingestion (only the three listed text
  suffixes).
- No change to 007's `kb_search` / `kb_chat` (the new
  extractors feed the same `IngestItem` contract; no new
  tool surface).

## Success Criteria

- With `sources.epub.enabled: true` + `digital-twins[epub]`
  installed, a 3-chapter EPUB produces 3 clean text blocks
  (one per chapter) in the `IngestItem` content.
- `kb_search` on a query that matches an EPUB chapter's
  body returns the hit under the document's `source_name`
  owner scope (the 007 ACL gate is unchanged).
- Re-ingesting the same EPUB yields 0 new points (NFR-1
  dedup holds; the `content_hash` is computed over the
  extracted text).
- With `sources.epub.enabled: false` (default), the pipeline's
  behaviour is byte-for-byte unchanged (all three suffixes
  fall to `raw`).
- The 6 edge cases in the user-scenarios section are all
  exercised by unit tests.
- `tests/unit/test_knob_docs.py` green.

## Key Entities

- `extract_text` (017's existing function) — gains two new
  suffix routes (`.epub` → `epub`, `.mobi` → `mobi`);
  `.org` falls to `raw` (no new route, just a documented
  case).
- `extract_epub` / `extract_mobi` — new extractor functions
  in `digital_twins/sources/epub.py` (or
  `digital_twins/ingest/extract_epub.py`).
- `sources.epub.enabled` — new config knob (bool, default
  `false`).
- `digital-twins[epub]` — new optional extra (pulls in
  `ebooklib` + `mobiparser`).

## Assumptions

- `ebooklib` (the EPUB library) and `mobiparser` (the MOBI
  library) are the v1 choice; a v1.1 add could support a
  second MOBI parser (`pybook` / `kindleunpack`) if the
  operator's MOBI files are PDB v1/v2 (the older format)
  rather than the PDB v4+ that `mobiparser` handles.
- `.org` files are plain text (Org-mode is a plain-text
  markup format, not a binary container — `raw` extraction
  is sufficient; a future v1.1 add could parse Org-mode
  heading hierarchy into a structured `# <heading>` block
  format, mirroring the EPUB chapter-block convention).
- The 017 `raw` path (`read_bytes().decode("utf-8",
  errors="replace")`) is correct for `.org` files (Org-mode
  is UTF-8 plain text; no BOM, no UTF-16).

## Open questions (max 3)

- [NEEDS CLARIFICATION: should the `.org` extractor be a
  dedicated `org` extractor (parsing heading hierarchy into
  a structured `# <heading>` block) or fall to `raw`
  (plain text, no structure)?] — default: `raw` (plain
  text; the v1.1 heading-hierarchy add is a trivial
  extension).
- [NEEDS CLARIFICATION: should `digital-twins[epub]` pull in
  both `ebooklib` and `mobiparser`, or should `.mobi` be a
  separate `digital-twins[mobi]` extra?] — default: one
  combined `digital-twins[epub]` extra (the two deps are
  small; a separate `[mobi]` extra is a v1.1 add if the
  operator wants a leaner install).

## Semver

MINOR. New optional feature behind a default-off knob
(`sources.epub.enabled`); when off, behaviour is
byte-for-byte unchanged. New optional extras are additive.


