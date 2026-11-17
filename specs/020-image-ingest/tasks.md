# Tasks: 020 — Multimodal image ingestion (extract + vision-LLM caption)

## 1. Knobs (RED-first, constitution Principle III)

- [ ] 1.1 Add `extraction.extract_images` (bool, default `false`),
      `extraction.image_caption` (bool, default `false`),
      `extraction.image_store` (str, default `"payload"`,
      `payload` | `filesystem`), `extraction.image_max_bytes`
      (int, default `10_000_000`) to
      `digital_twins/config/knobs.py` under `GROUP_EXTRACTION` +
      the four `DEFAULTS` entries in
      `digital_twins/config/schema.py`, the nested `extraction:`
      rows in `config.example.yml`, the four
      `KB_EXTRACTION__*` lines in `.env.example`, and the matching
      rows in `docs/configuration.md`. **Verification:**
      `tests/unit/test_knob_docs.py` (T027) green; fresh config →
      all four knobs absent → defaults apply.

- [ ] 1.2 Unit test: `extraction.extract_images` off (default) →
      the pipeline is byte-for-byte the 017 text-only path — no
      `images` key on the point payload, no `storage/images/`
      writes. **Verification:** test green against unmodified
      pipeline code.

## 2. Extraction module (RED-first)

- [ ] 2.1 Write unit tests in `tests/unit/test_extract_images.py`
      (new file): a generated minimal PNG fixture (in-memory bytes,
      no host fixture file — NFR-13) → `extract_images` returns
      one `ImageExtraction` with `source_kind="standalone"`,
      correct MIME, `page is None`; a `tmp_path`-generated PDF with
      one embedded image → one `ImageExtraction` with
      `source_kind="pdf"` + `page` an int; unsupported suffix
      (e.g. `.txt`) → `[]`; an over-cap standalone (exceeds
      `extraction.image_max_bytes`) → `[]` + a logged
      `image_too_large` warning (FR-7). **Verification:** tests
      fail on current code (module absent).
- [ ] 2.2 Implement `digital_twins/ingest/extract_images.py`
      (new): `ImageExtraction` dataclass (`bytes`, `mime`,
      `source_kind`, `page`, `caption=None`),
      `extract_images(path, suffix, cfg) -> list[ImageExtraction]`,
      `caption_image(ext, cfg) -> str | None`, `_sniff_mime()`,
      `_extract_standalone()`, `_extract_pdf()` (lazy `pypdf`
      import — 017 dep; FR-7 byte-cap drop; FR-8 caption-failure
      isolation — `caption: None` + warning when `llm.endpoint`
      is absent or the call raises; image bytes still returned).
      **Verification:** §2.1 tests green.

## 3. Pipeline routing + store dispatch (RED-first)

- [ ] 3.1 Write unit tests in `tests/unit/test_image_ingest.py`
      (new file): `monkeypatch.chdir` to a `tmp_path`; run
      `_run_image_extraction` over a generated PNG with
      `image_store="filesystem"` → the file lands under
      `storage/images/` (relative), `images_payload` entries carry
      `image_store: "filesystem"` + a *relative* `image_path`
      (no `~` / no absolute prefix — NFR-13); with
      `image_store="payload"` (default) → entries carry `url`
      starting with `data:image/png;base64,` and no
      `image_path`; with `extract_images=false` → the helper is
      not called, point payload has no `images` key.
      **Verification:** tests fail on current code.
- [ ] 3.2 Implement `digital_twins/ingest/pipeline.py`: image-item
      routing (empty-text chunk at index 0, NFR-14 one point not
      four) + up-front `_run_image_extraction()` before the
      sub-batch loop + FR-4 store dispatch (`payload` base64
      data-URI vs `filesystem`
      `storage/images/<sha256>.<ext>` relative write) + FR-7
      per-item isolation (017 pattern — an `image_too_large` drop
      never aborts the run) + `images[]` merged into the same
      point payload as the text chunks (FR-5). **Verification:**
      §3.1 tests green.

## 4. MCP `kb_search` hit contract (FR-6)

- [ ] 4.1 Write a unit test: a `kb_search` hit over a point with
      images → the row's `images` key is present (list, non-empty,
      entry shape `{caption, mime, url_or_base64, source,
      image_store, image_path}`); over a text-only point → the
      key is present and `[]` (stable-shape guarantee).
      **Verification:** test fails on current dispatch code.
- [ ] 4.2 Implement `digital_twins/mcp/dispatch.py`: `kb_search`
      rows gain `images` (key always present, empty `[]` when the
      point carries no images). **Verification:** §4.1 test
      green.
- [ ] 4.3 `digital_twins/mcp/registry.py`: the `kb_search` tool
      *description* documents the `images[]` output field (entry
      shape + "key always present, empty when no images"). No
      inputSchema change — `images` is output-only.
      **Verification:** `kb_search` in
      `build_tool_registry()` description mentions `images[]`.

## 5. Integration (RED-first)

- [ ] 5.1 Write integration test
      `tests/integration/test_image_ingest.py` (new file):
      `kb_ingest_folder` over a `tmp_path` folder containing a
      generated PNG → `kb_search` returns the hit with a non-empty
      `images[]` (payload store mode, the default); a 3-page PDF
      fixture with an embedded image → one point with ≥1 text
      chunk + ≥1 image entry on the *same* point (NFR-14);
      re-ingest the same folder → 0 new points (NFR-1
      idempotency). **Verification:** test fails on current code.
- [ ] 5.2 Run the §5.1 test green (the §2–§4 implementation makes
      it pass). **Verification:** test green.

## 6. Guards

- [ ] 6.1 `python3 -m pytest
      tests/integration/test_portability.py
      tests/unit/test_knob_docs.py -q` → both green (no host
      paths; knob docs in lock-step).
- [ ] 6.2 Full suite: `python3 -m pytest tests/ -q` → all green.

## 7. Semver + release note

- [ ] 7.1 Bump `__version__` (MINOR, `0.15.0` → `0.16.0`) +
      CHANGELOG entry (four new default-off knobs; one new
      extraction module; `images[]` on `kb_search` hits, key
      always present; byte-for-byte unchanged when
      `extraction.extract_images` is off).

## 8. Bookkeeping

- [ ] 8.1 OpenSpec artifacts for 020 validated (spec.md + plan.md +
      tasks.md + solution.md present, checkbox sync).
- [ ] 8.2 SDD ledger `.superpowers/sdd/020-image-ingest/progress.md`.

## Verification summary

- `pytest tests/unit/test_extract_images.py
  tests/unit/test_image_ingest.py
  tests/integration/test_image_ingest.py
  tests/unit/test_knob_docs.py -q` → all passed.
- `pytest tests/ -q` → all green.
- `pytest tests/integration/test_portability.py
  tests/unit/test_knob_docs.py -q` → both guards green.
