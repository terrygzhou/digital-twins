# Feature 017: Binary document extraction (PDF / XLSX / DOCX)

## User Scenarios & Testing

An operator ingests a folder that contains real documents, not just
plain text:

- `report.pdf` (scanned or text-layered)
- `fees.xlsx` (a spreadsheet of numbers)
- `notes.docx` (a Word document)

Today the `fs` source and the 016 one-shot folder source both do
`path.read_text(errors="replace")` on **every** file. For a binary
file that yields UTF-16/BOM mojibake (PDFs come back as garbled bytes,
XLSX as compressed-zip noise) — the chunk/embedder stores garbage, and
`kb_search` cannot retrieve it. This is exactly what forced a manual
"extract upstream, ship `.txt`" workaround on the OCI box.

**Primary scenario**

1. An operator (or agent) calls `kb_ingest_folder` with
   `{"path": "/reports"}` where `/reports` holds `report.pdf`,
   `fees.xlsx`, `notes.docx`, and a `readme.md`.
2. The server routes each file by suffix to a text extractor
   (PDF/XLSX/DOCX), falls back to raw-text for unknown/`.md`/`.txt`.
3. The extracted *text* is what enters the normal chunk/embed/dedup
   pipeline — so the stored point is clean, searchable content, and
   the Qdrant↔Neo4j association still works.
4. The JSON summary records per-file `extractor` used (`pdf`, `xlsx`,
   `docx`, `raw`) and any per-file error.

**Edge cases**

- Extractor dependency not installed → that file is recorded as a
  per-file error (`error: extractor_unavailable`, `extractor: pdf`),
  the run **continues** with the other files; no crash. (Mirrors the
  `preflight-optional-deps` change.)
- Corrupt / encrypted PDF → per-file error (`extraction_failed`),
  run continues.
- Unknown / no suffix (`.txt`, `.md`, no extension) → `raw`
  extractor (current `read_text` behaviour, `errors="replace"`).
- Empty spreadsheet / blank document → zero-length text → skipped
  (not a crash).
- NFR-13: no host paths in errors; only the caller-supplied `path`
  is echoed.

## Functional Requirements

- **FR-1**: Add a text-extraction layer
  `digital_twins/ingest/extract.py`: `extract_text(path: Path,
  suffix: str | None, cfg) -> tuple[bytes_text, extractor_name]`.
  Suffix routing:
  - `.pdf` → `pypdf` (or `pdfminer`) — concatenated page text
  - `.xlsx`, `.xls` → `openpyxl` — per-sheet cell values
    (header row + data; one `# Sheet: <name>` block per sheet,
    tab/`|`-separated, matching what a manual openpyxl dump produced)
  - `.docx` → `python-docx` — paragraph + table text
  - anything else → `raw` (`path.read_bytes().decode("utf-8",
    errors="replace")`, identical to `FsSource.read`)
- **FR-2**: The extractor is **config-gated + dependency-gated**:
  - knob `extraction.extract_binary` (bool, default `false`) — when
    false, no binary file is ever routed to an extractor (everything
    is `raw`), so behaviour is byte-for-byte unchanged for existing
    deployments.
  - when true, a missing Python dependency for a given suffix does
    NOT raise for the whole run — that one file yields a per-file
    `extractor_unavailable` error and the others proceed.
- **FR-3**: `kb_ingest_folder` (016) — and the `fs` source, for
  parity — use `extract_text` instead of a bare `read_text` when
  `extraction.extract_binary` is on. The stored `content_hash` is
  computed over the **extracted text** (so NFR-1 dedup is on content,
  not raw bytes).
- **FR-4**: Optional dependency extras, core stays lean (NFR-13
  portability): `pip install digital-twins[pdf]`,
  `digital-twins[xlsx]`, `digital-twins[docx]` (or a combined
  `digital-twins[docs]`). The base distribution installs none of them.
- **FR-5**: The tool summary's per-file entry gains `extractor`
  (`pdf|xlsx|docx|raw`) and, on extractor failure, the FR-specified
  `error` code. No new top-level field beyond `items[]`.
- **FR-6**: Test-First (constitution Principle III). Unit tests use
  `tmp_path` + tiny generated fixtures (a 1-page PDF, a 2-sheet XLSX,
  a 1-paragraph DOCX); no host paths. When an extractor's dep is
  absent in the test env, the test asserts the per-file
  `extractor_unavailable` contract (monkeypatch the extractor
  registry to simulate absence).

## Non-goals

- No OCR of scanned (image-only) PDFs — only text-layered PDFs.
  (OCR is a separate, model-dependent feature.)
- No `.doc` (legacy OLE) — that format needs an external tool
  (`antiword`/`soffice`); documented as out of scope, `.doc` falls to
  `raw` (and will be garbled) unless the operator pre-converts.
- No change to the chunker / embedder — extraction feeds them clean
  text; they are untouched.
- No new LLM call. Extraction is deterministic (no `extraction.enabled`
  coupling).

## Semver

MINOR. New optional feature behind a default-off knob; core behaviour
byte-for-byte unchanged when the knob is off. New optional extras are
additive.
