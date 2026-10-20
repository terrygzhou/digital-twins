## Purpose
Behavior contract for binary document extraction: when
`extraction.extract_binary` is on, the `fs` source and `kb_ingest_folder`
store clean extracted text for PDF/XLSX/DOCX files; when off, behaviour
is byte-for-byte the current raw-text path.

## ADDED Requirements

### Requirement: Binary files extract to text when extraction is enabled
When `extraction.extract_binary` is `true`, a file routed to an
available extractor (`.pdf`→`pdf`, `.xlsx`→`xlsx`, `.docx`→`docx`)
SHALL have its extracted text — not its raw bytes — enter the
chunk/embed/dedup pipeline, and the content hash SHALL be computed over
the extracted text (NFR-1).

#### Scenario: A text-layered PDF is stored as its text
- **WHEN** `extraction.extract_binary` is on and `kb_ingest_folder` is
  called on a dir containing a 1-page, text-layered `report.pdf`
- **THEN** the stored point's content is the PDF's text, and
  `kb_search` for a phrase in that text returns the point; the item
  reports `extractor: "pdf"`

#### Scenario: An XLSX is stored as per-sheet cell text
- **WHEN** a `fees.xlsx` is ingested with extraction on
- **THEN** its text is a per-sheet `# Sheet: <name>` block of
  `|`-separated cells, and a `kb_search` for a cell value finds it

### Requirement: Missing extractor dependencies are per-file errors
When `extraction.extract_binary` is on but a backend's dependency is not
installed, that file SHALL produce a per-file `extractor_unavailable`
error and the run SHALL continue with the other files.

#### Scenario: No pypdf installed → the PDF is skipped, others ingest
- **WHEN** extraction is on, `pypdf` is absent, and the folder has
  `a.pdf`, `b.md`
- **THEN** `a.pdf` reports `extractor_unavailable` and `b.md` ingests
  normally; the run returns `ok: true` with 1 error

### Requirement: Extraction is default-off
A fresh deployment (no `extraction.extract_binary` set) SHALL behave
exactly as before this feature: every file routes to the `raw`
extractor (byte-for-byte current behaviour).

#### Scenario: Fresh install ignores a binary file's bytes
- **WHEN** a fresh install ingests a `.pdf`
- **THEN** the item's `extractor` is `raw` and the stored content equals
  `raw` decode of the file (current behaviour), unchanged by this feature
