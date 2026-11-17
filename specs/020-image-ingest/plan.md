# Plan: 020 — Multimodal image ingestion (extract + vision-LLM caption)

## Summary

Adds an *image* ingestion side to the 017 text pipeline: standalone
raster files (`.png/.jpg/.jpeg/.webp/.gif`) and embedded PDF images
become first-class content on the **same** Qdrant point the text
chunks ride on (NFR-14 one point, not four). Each `kb_search` hit
gains an `images[]` array (key always present, empty when the point
carries no images). Four new default-off knobs —
`extraction.extract_images` (bool, false),
`extraction.image_caption` (bool, false),
`extraction.image_store` (enum `payload`|`filesystem`, default
`payload`), `extraction.image_max_bytes` (int, default 10 000 000) —
gate the whole feature. With `extract_images` off (the default), the
pipeline is byte-for-byte unchanged (constitution VI, NFR-15).

## Approach

1. **Knobs (RED-first).** Add the four `extraction.*` image knobs to
   `digital_twins/config/knobs.py` (under the existing
   `GROUP_EXTRACTION` group) + `digital_twins/config/schema.py`
   `DEFAULTS`, the nested `extraction:` rows in
   `config.example.yml`, the four `KB_EXTRACTION__*` lines in
   `.env.example`, and the matching rows in
   `docs/configuration.md` — so the `tests/unit/test_knob_docs.py`
   guard (T027) stays green.
2. **Extraction module (RED-first).**
   `digital_twins/ingest/extract_images.py` (new):
   `extract_images(path: Path, suffix: str, cfg) ->
   list[ImageExtraction]` + the `ImageExtraction` dataclass
   (`bytes: bytes`, `mime: str`, `source_kind: "standalone" |
   "pdf"`, `page: int | None`, `caption: str | None`) +
   `caption_image(ext, cfg)`. Standalone rasters are read directly
   (MIME sniffed from the suffix); PDF-embedded XObjects are
   extracted via the `pypdf` already in the tree (017 dep). The
   `extraction.image_max_bytes` cap drops over-cap images with a
   per-item `image_too_large` warning (FR-7) — the run continues.
   `caption_image` sends an OpenAI-compatible `image_url` part to
   `digital_twins/llm.py` when `extraction.image_caption` is on and
   `llm.endpoint` is set; on any exception or absent endpoint it
   degrades to `caption: None` + a warning (FR-8 isolation — the
   image bytes are still stored).
3. **Pipeline routing (FR-2/FR-5).** `digital_twins/ingest/
   pipeline.py`: when `extract_images` is on, image items (raster
   suffixes + PDFs carrying embedded images) get an empty-text chunk
   at index 0 so the item still produces a point; `_run_image_
   extraction()` runs up-front before the sub-batch loop; the
   resulting `images[]` array is merged into the *same* point
   payload as the text chunks (NFR-14). The 017 text path runs in
   parallel for text-bearing files — unchanged.
4. **Store dispatch (FR-4).** `_run_image_extraction()` branches on
   `extraction.image_store`: `payload` (default) — each `images[]`
   entry carries `url` = `data:<mime>;base64,…`; `filesystem` —
   the bytes are written to
   `storage/images/<sha256_of_bytes>.<ext>` (relative to the data
   dir; NFR-13 — no `~` / no absolute prefix in the stored value)
   and the entry carries a *relative* `image_path` +
   `image_store: "filesystem"`.
5. **MCP `kb_search` hit contract (FR-6).** `digital_twins/mcp/
   dispatch.py`: hit rows gain `images` — key always present, empty
   `[]` when no image. `digital_twins/mcp/registry.py`: the
   `kb_search` tool description documents the `images[]` output
   field (self-documenting; no inputSchema change — output-only).
6. **Guards.** `tests/integration/test_portability.py` (T006) and
   `tests/unit/test_knob_docs.py` (T027) stay green; no host path
   (NFR-13) appears in any new code, fixture, or doc.

## Files touched

- `digital_twins/config/knobs.py` — +4 knobs under `GROUP_EXTRACTION`
- `digital_twins/config/schema.py` — +4 `DEFAULTS` entries
- `config.example.yml` — +`extraction:` image-knob rows
- `.env.example` — +4 `KB_EXTRACTION__*` lines
- `docs/configuration.md` — +image-knob rows
- `digital_twins/ingest/extract_images.py` — new (FR-1 module)
- `digital_twins/ingest/pipeline.py` — image routing + `_run_image_
  extraction()` + `images[]` merge into point payload
- `digital_twins/mcp/dispatch.py` — `kb_search` rows gain `images[]`
- `digital_twins/mcp/registry.py` — `kb_search` description
- `tests/unit/test_extract_images.py` — new
- `tests/unit/test_image_ingest.py` — new
- `tests/integration/test_image_ingest.py` — new
- `digital_twins/__init__.py` — version bump to `0.16.0`

## Open questions

None — the spec resolves all three (see `spec.md`
`## Open questions (max 3)`, all defaulted in-spec).

## Semver

MINOR — `0.15.0` → `0.16.0`. All changes are additive and
default-off: four new knobs, one new module, one new point-payload
key (`images[]`, always present, empty when no image), one
`kb_search` description update. No existing tool body, endpoint, or
default is altered — with `extract_images` off, behavior is
byte-for-byte unchanged (constitution VI, NFR-15). No breaking
change.
