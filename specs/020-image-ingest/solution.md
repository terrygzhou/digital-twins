# Solution skeleton: 020 — Multimodal image ingestion (extract + vision-LLM caption)

Per-file concrete signatures, config snippets, and test outlines for
the 020 workstream. Prerequisite: 017's `digital_twins/ingest/extract.py`
+ `pypdf` dependency; 020 extends 017's extraction layer, it does not
rewrite it. All four knobs are default-off (or default `payload` /
`10_000_000`); when `extraction.extract_images` is `false` the
pipeline is byte-for-byte unchanged (constitution VI, NFR-15).

---

## `digital_twins/config/knobs.py`

Four new entries in the `KNOBS` dict under the existing
`GROUP_EXTRACTION` group:

```python
"extraction.extract_images": {
    "type": "bool",
    "default": False,
    "env": "KB_EXTRACTION__EXTRACT_IMAGES",
    "group": GROUP_EXTRACTION,
    "doc": "When true, standalone raster files (.png/.jpg/.jpeg/"
           ".webp/.gif) and embedded PDF images are extracted as "
           "first-class content on the same Qdrant point as the text "
           "chunks (NFR-14 one point, not four).  Default off — "
           "byte-for-byte unchanged pipeline when false.",
}
"extraction.image_caption": {
    "type": "bool",
    "default": False,
    "env": "KB_EXTRACTION__IMAGE_CAPTION",
    "group": GROUP_EXTRACTION,
    "doc": "When true (and llm.endpoint is set), extracted images are "
           "sent to the LLM endpoint as an image_url part for "
           "captioning.  Default off; when on and the endpoint is "
           "absent, caption degrades to null + warning (FR-8).",
}
"extraction.image_store": {
    "type": "str",
    "default": "payload",
    "env": "KB_EXTRACTION__IMAGE_STORE",
    "group": GROUP_EXTRACTION,
    "doc": "Where image bytes live — 'payload' (in Qdrant point "
           "metadata as a base64 data-URI) or 'filesystem' "
           "(storage/images/<sha256>.<ext>, relative path only — "
           "NFR-13).  Default payload.",
}
"extraction.image_max_bytes": {
    "type": "int",
    "default": 10_000_000,
    "env": "KB_EXTRACTION__IMAGE_MAX_BYTES",
    "group": GROUP_EXTRACTION,
    "doc": "Per-image byte cap; oversized images are dropped with a "
           "per-item image_too_large error (the run continues).  "
           "Default 10 MB.",
}
```

Matching `DEFAULTS` entries in `digital_twins/config/schema.py`;
nested `extraction:` rows in `config.example.yml`; the four
`KB_EXTRACTION__*` lines in `.env.example`; and a new `## Extraction`
subsection (image knobs) in `docs/configuration.md` — so
`tests/unit/test_knob_docs.py` (T027) stays green.

## `digital_twins/ingest/extract_images.py`  (new)

The FR-1 module API:

```python
"""020: image extraction from files and embedded PDF images.

FR-1: module API — extract_images(path, suffix, cfg) ->
list[ImageExtraction].  FR-7: image_max_bytes cap — over-cap images
are dropped (per-item error, not a run abort).  FR-8: caption
failure degrades to None with a warning; the image bytes are still
returned (run continues).

Supported standalone suffixes: .png .jpg .jpeg .webp .gif.
For PDFs: embedded image XObjects are extracted via pypdf (lazy
import; 017 dep).  MIME is sniffed from the suffix for standalone
files; for PDF-embedded images the MIME is read from the XObject's
Subtype.

All paths are relative (NFR-13); no host paths leak.
"""

@dataclass
class ImageExtraction:
    bytes: bytes
    mime: str                 # "image/png", "image/jpeg", ...
    source_kind: str          # "standalone" | "pdf"
    page: Optional[int]       # PDF-embedded page index; None for standalone
    caption: Optional[str] = None   # FR-8: None when LLM absent / failed


def extract_images(
    path: Path, suffix: str, cfg: Config
) -> list[ImageExtraction]: ...

def caption_image(ext: ImageExtraction, cfg: Config) -> Optional[str]: ...
```

Implementation outline (no full bodies):

- `_sniff_mime(suffix) -> str` — static table for the 5 supported
  suffixes; unknown → `None` (item is skipped, not an error).
- `_extract_standalone(path, suffix, cfg) -> list[ImageExtraction]`
  — read the file, check `len(data) <= extraction.image_max_bytes`
  (FR-7 drop + warning when over cap), build one
  `ImageExtraction(bytes=data, mime=..., source_kind="standalone",
  page=None)`.
- `_extract_pdf(path, cfg) -> list[ImageExtraction]` — lazy
  `pypdf` import; walk `page.images`, build one
  `ImageExtraction(bytes=..., mime=..., source_kind="pdf",
  page=page_index)` per XObject; byte-cap drop per image (FR-7).
- `caption_image(ext, cfg)` — when `extraction.image_caption` is
  true and `llm.endpoint` is set, send an OpenAI-compatible
  `image_url` part (data-URI from `ext.bytes` + `ext.mime`) to
  `digital_twins/llm.py`; on any exception / absent endpoint, log a
  warning and return `None` (FR-8 isolation — the image is still
  stored, caption is `null`).

## `digital_twins/ingest/pipeline.py`

- **FR-2 routing**: when `extraction.extract_images` is `true`, image
  items (raster suffixes + PDFs that carry embedded images) get an
  empty-text chunk at index 0 so the item still produces a Qdrant
  point (NFR-14 one point, not four); the text extraction path
  (017) runs in parallel for text-bearing files.
- **FR-4 / FR-5 store dispatch** in `_run_image_extraction()`:
  - `image_store="payload"` (default) — each `images[]` entry carries
    `url` = `data:<mime>;base64,<...>`; `image_path` absent.
  - `image_store="filesystem"` — write to
    `storage/images/<sha256_of_bytes>.<ext>` (relative to the data
    dir; NFR-13 — no `~` or absolute prefix in the stored value);
    each `images[]` entry carries `image_path` (relative) +
    `image_store: "filesystem"`; `url` absent.
- **FR-7 per-item isolation** (017 pattern): an
  `image_too_large` drop for one item does not abort the run — the
  item's point is still written, its `images[]` is empty (or the
  dropped entry is omitted), a warning is logged.
- **FR-5**: the `images[]` array is merged into the *same* point
  payload as the text chunks (one point, not four).

## `digital_twins/mcp/dispatch.py` + `registry.py`

- **FR-6**: `kb_search` hit rows gain `images` — an `images[]`
  array, *key always present*, empty `[]` when the point carries no
  images. Entry shape:
  `{"caption": str | None, "mime": str, "url": str (payload mode) |
  "url_or_base64": str, "source": "pdf" | "standalone",
  "image_store": str, "image_path": str | None}`.
- **registry.py**: the `kb_search` tool *description* documents the
  `images[]` output field (self-documenting contract; no inputSchema
  change — `images` is output-only).

## `tests/unit/test_extract_images.py`  (new)

- A generated minimal PNG fixture (in-memory, no fixture file on
  disk needed — `bytes.fromhex(...)` for a 1×1 PNG) →
  `extract_images` returns one `ImageExtraction` with
  `source_kind="standalone"`, correct MIME, `page is None`.
- A generated minimal PDF with one embedded image (built via
  `pypdf` in a `tmp_path` fixture) → `extract_images` returns one
  `ImageExtraction` with `source_kind="pdf"`, `page` an int.
- Unsupported suffix (e.g. `.txt`) → `[]` (no error).
- Over-cap standalone (a PNG larger than
  `extraction.image_max_bytes`) → `[]` + a logged
  `image_too_large` warning (FR-7).
- **Caption isolation (FR-8)**: `cfg` with `llm.endpoint` unset +
  `extraction.image_caption=true` → `caption_image` returns `None`,
  no exception, image bytes still present on the
  `ImageExtraction`.  **Verification:** all five green.

## `tests/unit/test_image_ingest.py`  (new)

- Filesystem-store mode: `monkeypatch.chdir` to a `tmp_path`; run
  `_run_image_extraction` over a generated PNG with
  `image_store="filesystem"` → the file lands under
  `storage/images/` (relative), `images_payload` entries carry
  `image_store: "filesystem"` + a *relative* `image_path` (no
  `~` / no absolute prefix — NFR-13).
- Payload-store mode: same fixture with `image_store="payload"` →
  `images_payload` entries carry `url` starting with
  `data:image/png;base64,` and no `image_path`.
- Default-off guard: `extraction.extract_images=false` →
  `_run_image_extraction` is not called, point payload has no
  `images` key, pipeline output is byte-for-byte the 017 shape.
  **Verification:** test green.

## `tests/integration/test_image_ingest.py`  (new)

- `kb_ingest_folder` over a `tmp_path` folder containing a generated
  PNG → `kb_search` returns the hit with a non-empty `images[]`
  (payload store mode — the default).
- 3-page PDF fixture with an embedded image on page 2 → one point
  with ≥1 text chunk + ≥1 image entry on the *same* point (NFR-14);
  re-ingest the same folder → 0 new points (NFR-1 idempotency).
- **Verification:** test green.

## Guards

- `tests/unit/test_knob_docs.py` (T027) — green: the four new knobs
  are in `knobs.py` + `schema.py` `DEFAULTS` + `docs/configuration.md`
  + `config.example.yml` + `.env.example` in lock-step.
- `tests/integration/test_portability.py` (T006) — green: no host
  paths in any new code or fixture.

## Verification

- `pytest tests/unit/test_extract_images.py
  tests/unit/test_image_ingest.py tests/integration/test_image_ingest.py
  tests/unit/test_knob_docs.py -q` → all green.
- `pytest tests/ -q` → all green.

## What was *not* built (out of scope, per spec Non-goals)

- No OCR, no video/audio, no new vector store, no per-image Qdrant
  point (NFR-14 one point, not four), no chunker / embedder change,
  no text-prompt change.  Captioning is a single `image_url` LLM
  call when the knob is on; no model-specific code paths.
