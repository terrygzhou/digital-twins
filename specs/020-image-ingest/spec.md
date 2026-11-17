# Feature 020: Multimodal image ingestion (extract + vision-LLM caption)

> **License note** (NFR-13 / constitution I): this spec is an
> inspiration-level design reference only. No code, prompts, or test
> fixtures are ported from `nashsu/llm_wiki` (GPL-3.0) into this MIT
> package.

## User Scenarios & Testing

017 gave the pipeline a text-extraction layer (`digital_twins/ingest/extract.py`);
this feature adds the *image* side: embedded images inside PDFs, plus
standalone image files (`.png/.jpg/.jpeg/.webp/.gif`), become first-class
content on the point that 017 already created for the document. An operator
or agent wants `kb_search` to return a figure's caption (and a viewable
image reference) without a separate point per image — NFR-1/NFR-14
("one point, not four") must hold.

**Primary scenario**

1. `extraction.extract_images: true` (default `false` — off, behaviour
   byte-for-byte unchanged).
2. An ingest run reads `report.pdf` (017's text extraction still runs) and
   a `chart.png` in the same folder.
3. The pipeline extracts embedded raster images from the PDF's image
   streams via `pypdf` (the dependency 017 already ships) and the
   standalone PNG directly.
4. If `extraction.image_caption: true`, each image is sent to the
   existing `digital_twins/llm.py` endpoint with an image-capable model
   class (OpenAI-compatible `image_url` part); the caption is stored on
   the point's metadata as `image_caption`.
5. The image bytes are stored either on the Qdrant point's binary
   payload (default) or as a file under `storage/images/` when
   `extraction.image_store: filesystem`; the point's metadata records
   which (`image_store` + `image_path` or an in-payload flag).
6. `kb_search` responses gain an `images[]` array per hit: each entry is
   `{caption, url | base64, mime, source}`.

**Edge cases**

- `extraction.extract_images: false` → the pipeline never calls
  `extract_images`, never stores image bytes, `kb_search`'s
  `images[]` is absent or empty. (Default.)
- `extraction.image_caption: true` but the LLM endpoint is
  unreachable → the image is still stored, `image_caption` is
  `null`, a per-item warning is logged, the run continues (mirrors
  017's per-file error-isolation pattern).
- Standalone image file with no vision-LLM configured → image bytes
  are stored, `image_caption` is `null`; the point is not skipped.
- No embedded images in a PDF (text-only) → `images[]` is empty,
  no LLM call.
- Image exceeds a size cap (`extraction.image_max_bytes`, default
  10 MB) → the image is dropped, a per-item `image_too_large`
  error is recorded, other images on the point proceed.
- NFR-13: no host paths in `image_path` when `image_store:
  filesystem` — the path is relative to `storage/images/` (a
  config-resolved root), never an absolute host path.

## Functional Requirements

- **FR-1**: Add `digital_twins/ingest/extract_images.py`:
  `extract_images(path: Path, suffix: str, cfg) -> list[ImageExtraction]`
  where `ImageExtraction = dataclass(bytes: bytes, mime: str,
  source_kind: "pdf" | "standalone", page: int | None)`. PDF image
  extraction reuses `pypdf` (the 017 dep); standalone files are
  read directly (mime sniffed from suffix).
- **FR-2**: Add a new optional source extension
  `digital_twins/sources/image.py` (or an `extract_images` hook in
  `digital_twins/ingest/pipeline.py`) that, when
  `extraction.extract_images: true`, routes `.png/.jpg/.jpeg/.webp/
  .gif` standalone files and extracts embedded images from `.pdf`
  files (017's `extract_text` still runs in parallel — image
  extraction does not replace text extraction).
- **FR-3**: Add knob `extraction.image_caption` (bool, default
  `false`). When true, each `ImageExtraction` is sent to
  `digital_twins/llm.py`'s endpoint with an OpenAI-compatible
  `image_url` part and the caption stored on the point's metadata
  as `image_caption`. No new LLM call for non-image content.
- **FR-4**: Add knob `extraction.image_store` (enum `payload` |
  `filesystem`, default `payload`). When `payload`, the image bytes
  go on the Qdrant point's binary field. When `filesystem`, the
  bytes are written under `storage/images/<sha256>.<ext>` and the
  point's metadata carries the relative `image_path` (never an
  absolute host path — NFR-13).
- **FR-5**: The image is stored on the *same* point as the
  document's text chunks (an additional chunk on the point, not a
  new point) — NFR-1/NFR-14 "one point, not four" holds. The
  caption is searchable text; the raw image is binary (payload or
  filesystem path).
- **FR-6**: `kb_search`'s hit contract (in `digital_twins/mcp/
  dispatch.py`) gains an `images[]` array per hit: each entry is
  `{"caption": str | null, "mime": str, "url_or_base64": str,
  "source": "pdf" | "standalone"}`. When a hit carries no image
  metadata, `images` is `[]` (key present, empty).
- **FR-7**: Add knob `extraction.image_max_bytes` (int, default
  10_000_000). A single image above the cap is dropped with a
  per-item `image_too_large` error; other images on the same point
  proceed.
- **FR-8**: Test-First (Constitution III). Unit tests use `tmp_path`
  + a tiny generated PNG fixture and a PDF with one embedded image
  (both generated in-test, no host paths). When the LLM endpoint
  is absent in the test env, the test asserts the per-item
  warning-isolation contract (the image is stored, `image_caption`
  is `null`, the run continues). The new knobs land in
  `config/knobs.py` + `config.example.yml` + knob docs together
  (`tests/unit/test_knob_docs.py` green).

## Non-goals

- No OCR of scanned (image-only) PDFs — OCR is a separate, model-
  dependent feature (same non-goal as 017).
- No new vector store; images ride on the existing Qdrant points.
- No per-image point (the image is an additional chunk on the
  document's point, keeping NFR-14 intact).
- No video/audio ingestion (only the listed raster image suffixes +
  embedded PDF raster streams).
- No change to the chunker / embedder (they still see the text
  extraction from 017; the image is a separate chunk type).
- No change to the LLM prompt for text extraction — the vision
  call is a separate, caption-only call.

## Success Criteria

- With `extraction.extract_images: true` + `extraction.image_caption:
  true`, a 3-page PDF with one embedded figure produces one point
  with ≥1 text chunk + ≥1 image chunk + `image_caption` metadata.
- `kb_search` on a query that matches the caption returns the hit
  with a non-empty `images[]` (caption + viewable reference).
- Re-ingesting the same PDF yields 0 new points (NFR-1 dedup holds).
- With `extraction.extract_images: false` (default), the pipeline's
  behaviour is byte-for-byte unchanged (no image extraction, no
  image store writes).
- The 4 edge cases in the user-scenarios section are all exercised
  by unit tests.
- `tests/unit/test_knob_docs.py` green.

## Key Entities

- `ImageExtraction` (dataclass: bytes, mime, source_kind, page).
- Point metadata: `image_caption` (str | null), `image_store`
  (payload | filesystem), `image_path` (relative, when
  filesystem), `images[]` (array of embedded-image descriptors).
- `extraction.extract_images`, `extraction.image_caption`,
  `extraction.image_store`, `extraction.image_max_bytes` — new
  config knobs.
- `kb_search` response: gains `images[]` per hit.

## Assumptions

- `pypdf` (already a 017 dep) is sufficient for embedded-image
  stream extraction; no new PDF dependency.
- The LLM endpoint (`digital_twins/llm.py`) accepts an
  OpenAI-compatible `image_url` part for the configured model
  class; if the operator's endpoint does not, the caption
  gracefully degrades to `null` (warning logged, image still
  stored).
- The Qdrant binary payload field (or `storage/images/` filesystem
  dir) is acceptable storage for up to `extraction.image_max_bytes`
  per image.

## Open questions (max 3)

- [NEEDS CLARIFICATION: should `extraction.image_store: filesystem`
  write under a fixed `storage/images/` directory, or under the
  operator's Qdrant data directory?] — default: `storage/images/`
  (config-resolved, never an absolute host path).
- [NEEDS CLARIFICATION: should `kb_search`'s `images[]` always be
  present (empty array when no image) or omitted when empty?] —
  default: always present (empty array), for stable response
  shape.

## Semver

MINOR. New optional feature behind a default-off knob
(`extraction.extract_images`); when off, behaviour is byte-for-byte
unchanged. New optional knobs are additive.


