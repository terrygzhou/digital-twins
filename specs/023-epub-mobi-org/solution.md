# Solution skeleton: 023 — EPUB / MOBI / Org-mode text extraction

Per-file concrete signatures, config schema snippets, and test outlines
for the 023 workstream. Prerequisite: 017's `digital_twins/ingest/
extract.py` (017 T2.1 / T2.2) must have landed before 023 work
starts; 023 extends 017's suffix→backend registry, it does not
rewrite it.

---

## digital_twins/config/knobs.py

Add a bespoke entry to the `KNOBS` dict. `epub` is **not** a
`BUILTIN_SOURCES` entry — it is an extraction-routing gate, not a
`Source`-conforming object — so it is a custom key, not part of the
`BUILTIN_SOURCES` loop:

```python
# --- Sources — new extraction-routing gate (023) ---
KNOBS["sources.epub.enabled"] = {
    "type": "bool",
    "default": False,
    "env": "KB_SOURCES__EPUB__ENABLED",
    "group": GROUP_SOURCES,
    "doc": "When true, .epub / .mobi files are routed to the "
           "epub / mobi extractors (023); when false (default), "
           "all three new suffixes fall to the 017 raw path and "
           "behaviour is byte-for-byte unchanged.",
}
```

## digital_twins/config/schema.py

Add the matching `DEFAULTS` entry so `_declared_type` resolves it
to `bool` (not `str` via the `sources.*` type table):

```python
DEFAULTS["sources.epub.enabled"] = False
```

The T027 knob-docs sync guard (`tests/unit/test_knob_docs.py`)
sees the new key; `docs/configuration.md` documents it under the
Sources group.

## digital_twins/ingest/extract_epub.py  (new)

The two new extractor functions. Both lazy-import their
dependencies (deferred to the 017 registry's lazy-import layer);
the module itself assumes the dep is present at call time and
raises `ExtractorUnavailable` when it is not. Both echo only the
caller-supplied `path` in any error message (NFR-13).

```python
"""EPUB / MOBI extractors for 023 (lazy imports; NFR-13).

Mirrors 017's `digital_twins/ingest/extract.py` backend pattern.
`.org` is plain text — it falls to 017's `raw` path, not this
module. No host paths; only the caller-supplied `path` is echoed
in error messages.
"""

from __future__ import annotations

from pathlib import Path

from digital_twings.ingest.extract import (
    ExtractorUnavailable,
    ExtractionFailed,
)


def extract_epub(path: Path) -> str:
    """Extract clean text from an EPUB file.

    One `# <chapter-title>` header + body per chapter, `\n\n`
    separated. Raises `ExtractionFailed` on corrupt / missing
    `container.xml`; raises `ExtractorUnavailable` when
    `ebooklib` is not importable.
    """
    try:
        from ebooklib import epub  # lazy (017 pattern)
    except ImportError as exc:
        raise ExtractorUnavailable(
            f"ebooklib not installed for .epub file '{path}'"
        ) from exc

    try:
        book = epub.read_epub(str(path))
    except Exception as exc:
        raise ExtractionFailed(
            f"failed to parse EPUB '{path}': {exc.__class__.__name__}"
        ) from exc

    blocks: list[str] = []
    for item in book.items:
        if not isinstance(item, epub.EpubHtml):
            continue
        title = item.get_name() or "untitled"
        # Strip HTML tags — ebooklib returns raw content;
        # the minimal approach here is to use html.parser
        # (stdlib) to pull text out.
        import html.parser

        class _TextCollector(html.parser.HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self._text: list[str] = []

            def handle_data(self, data: str) -> None:
                self._text.append(data)

        collector = _TextCollector()
        try:
            collector.feed(item.get_content().decode("utf-8", errors="replace"))
        except Exception:
            pass
        body = "".join(collector._text).strip()
        if not body:
            continue
        blocks.append(f"# {title}\n\n{body}")
    return "\n\n".join(blocks)


def extract_mobi(path: Path) -> str:
    """Extract clean text from a MOBI file.

    One `# Page: <n>` block per page, `\n\n` separated.
    Empty book (zero pages) → zero-length string → caller skips.
    Raises `ExtractionFailed` on corrupt / unparseable input;
    raises `ExtractorUnavailable` when `mobiparser` is not
    importable.
    """
    try:
        import mobiparser  # lazy (017 pattern)
    except ImportError as exc:
        raise ExtractorUnavailable(
            f"mobiparser not installed for .mobi file '{path}'"
        ) from exc

    try:
        result = mobiparser.parse(str(path))
    except Exception as exc:
        raise ExtractionFailed(
            f"failed to parse MOBI '{path}': {exc.__class__.__name__}"
        ) from exc

    pages = result.get("pages", []) if isinstance(result, dict) else []
    if not pages:
        return ""
    blocks = [f"# Page: {i}\n\n{str(page).strip()}" for i, page in enumerate(pages, start=1)]
    return "\n\n".join(blocks)
```

## digital_twins/ingest/extract.py  (017 — extend, not rewrite)

Add two entries to the 017 suffix→backend registry (lazy imports,
mirroring the 017 `pypdf` / `openpyxl` / `python-docx` backends):

```python
# 017's registry (existing) + 023's two new entries:
_REGISTRY = {
    ".pdf":   ("pypdf",      None),   # 017
    ".xlsx":  ("openpyxl",   None),   # 017
    ".xls":   ("openpyxl",   None),   # 017
    ".docx":  ("python-docx", None),  # 017
    # --- 023 additions ---
    ".epub":  ("ebooklib",   "epub"), # new — extract_epub
    ".mobi":  ("mobiparser", "mobi"), # new — extract_mobi
}
# .org → raw (documented in the docstring as the expected case,
# not a new route — Org-mode is UTF-8 plain text, 017's raw
# path `path.read_bytes().decode("utf-8", errors="replace")`
# is already correct for it).
```

The `extractor_name` returned is `"epub"` / `"mobi"` / `"raw"`
respectively (FR-1). The 017 `extract_text` API is unchanged —
the routing dict is an addition, not a rewrite; the `raw`
fallback and the `ExtractorUnavailable` / `ExtractionFailed`
contracts are unchanged.

## digital_twins/sources/fs.py  (wire — parity with onshot.py)

When `sources.epub.enabled` is on, `read()` calls `extract_text`
(017's layer, which now carries the new routes); when off, fall
through to the bare `read_bytes().decode("utf-8",
errors="replace")` path — byte-for-byte unchanged. Set
`metadata["extractor"]` to the 017 `extractor_name`;
`content_hash` is computed over the extracted text (NFR-1).

```python
# Inside FsSource.read() (skeleton)
from digital_twings.ingest.extract import extract_text, ExtractorUnavailable, ExtractionFailed
from digital_twings.config import get_cfg

cfg = get_cfg()
epub_enabled = bool(cfg.get("sources.epub.enabled", False))
ext = path.suffix.lower()

if epub_enabled and ext in (".epub", ".mobi", ".org"):
    text, extractor_name = extract_text(path, ext, cfg)
    metadata["extractor"] = extractor_name  # "epub" | "mobi" | "raw"
else:
    text = path.read_bytes().decode("utf-8", errors="replace")
    metadata["extractor"] = "raw"
```

Per-file extractor failure → item-level error (`extractor_unavailable`
or `extraction_failed`), run continues (017's per-file
error-isolation contract).

## digital_twins/ingest/onshot.py  (016 parity — same change)

Identical wiring to `fs.py` — when the knob is on, `read()` calls
`extract_text`; when off, bare `read_bytes`. Same
`metadata["extractor"]` and `content_hash` semantics. No
second routing path (FR-4).

## pyproject.toml  (new optional extra)

Under `[project.optional-dependencies]`, add:

```toml
epub = [
    "ebooklib>=0.19",
    "mobiparser>=1.8",
]
```

Base install pulls neither. A combined `epub-mobi` alias and a
separate `[mobi]` extra are v1.1 adds; the v1 shipping default is
one combined `epub` extra.

## docs/configuration.md  (document the knob)

Under the Sources group, add:

```markdown
### `sources.epub.enabled`

Type: `bool`, default `false`, env `KB_SOURCES__EPUB__ENABLED`.

When `true`, `.epub` / `.mobi` files are routed to the `epub` /
`mobi` extractors (023); when `false` (default), all three new
suffixes (`.epub`, `.mobi`, `.org`) fall to the 017 `raw` path
and behaviour is byte-for-byte unchanged. The `epub` / `mobi`
extractors require `digital-twins[epub]` to be installed
(`ebooklib` + `mobiparser`); without the extra, the first
`.epub` / `.mobi` file in a run yields a per-file
`extractor_unavailable` error and the run continues.
```

## Tests

### tests/unit/test_knob_docs.py  (extend T027 guard)

```python
def test_sources_epub_enabled_knob_registered(self) -> None:
    entry = KNOBS["sources.epub.enabled"]
    assert entry["type"] == "bool"
    assert entry["default"] is False
    assert entry["env"] == "KB_SOURCES__EPUB__ENABLED"
    assert entry["group"] == GROUP_SOURCES
    assert DEFAULTS["sources.epub.enabled"] is False
```

### tests/unit/test_extract_epub.py  (new)

- Generate a 1-chapter EPUB fixture in-test (via
  `ebooklib.epub` if importable, else a checked-in fixture
  under `tests/fixtures/`): assert `extract_text` routes to the
  `epub` backend, returns one `# <chapter-title>` header + body
  per chapter, `\n\n`-separated.
- Monkeypatch the 017 extractor registry to drop the `epub`
  backend → per-file `extractor_unavailable`, other files still
  extract.
- Corrupt EPUB fixture (missing `container.xml`) →
  `extraction_failed`.
- Empty EPUB (zero chapters) → zero-length text → item skipped.
- No host paths (NFR-13) — use `tmp_path` / checked-in fixtures
  only.

### tests/unit/test_extract_mobi.py  (new)

- Generate a 1-page MOBI fixture in-test (via `mobiparser` if
  importable, else a checked-in fixture under
  `tests/fixtures/`): assert `extract_text` routes to the `mobi`
  backend, returns one `# Page: <n>` block per page, `\n\n`
  separated.
- Missing `mobiparser` → per-file `extractor_unavailable`.
- Empty MOBI (zero pages) → zero-length text → item skipped.

### tests/unit/test_extract.py  (extend 017)

- A `.org` file routes to `raw` (byte-for-byte identical to
  `FsSource.read`).
- `sources.epub.enabled: false` → all three new suffixes fall to
  `raw` (byte-for-byte current behaviour).
- `sources.epub.enabled: true` → `.epub` / `.mobi` route to the
  new extractors; `.org` still routes to `raw`.

### tests/integration/test_mcp_kb_tools.py  (extend)

- `kb_ingest_folder` over a tmp folder with a real 1-chapter
  EPUB + a 1-page MOBI + a 1-paragraph `.org` → `kb_search`
  finds the EPUB chapter body, the MOBI per-page text, and the
  `.org` paragraph under the document's `source_name` owner
  scope (007 ACL unchanged); re-ingest → 0 new points (NFR-1
  dedup over extracted text).

### Guard tests

- `tests/integration/test_portability.py` (T006): no host paths
  anywhere in the new extractor error messages (NFR-13 — only
  the caller-supplied `path` is echoed); no `python3.12` /
  `CUDA_VISIBLE_DEVICES` leaks; the new `epub` / `mobi` suffix
  routes are importable without the optional extras installed
  (lazy imports).
- `tests/unit/test_knob_docs.py` (T027): the new
  `sources.epub.enabled` knob is documented in
  `docs/configuration.md` and present in the `KNOBS` dict with
  the correct type / default / env / group.
