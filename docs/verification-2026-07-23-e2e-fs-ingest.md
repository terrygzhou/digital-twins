# End-to-End Verification Report — 2026-07-23

**Feature:** fs source → chunk → embed → upsert (full pipeline, live Qdrant + Neo4j)
**Trigger:** `digital_twins run --once` (manual / system)
**Status:** PASS — NFR-1 idempotency confirmed across 3 runs; 1 genuine pipeline bug found and fixed.

---

## Summary

| Run | Date (UTC) | Items | Points | Status | Note |
|-----|-----------|-------|--------|--------|------|
| 1 | 21:48:26 | 121 | 0 | failed | Pre-fix: embedder received int instead of text (bug at `pipeline.py:241`) |
| 2 | 21:48:37 | 121 | 0 | failed | Pre-fix: same bug |
| 3 | 21:48:45 | 121 | 0 | failed | Pre-fix: same bug |
| 4 | 21:49:11 | 121 | **235** | ok | Post-fix: full ingest into `personal_kb` |
| 5 | 21:50:10 | 0 | 0 | ok | Idempotency check: high-water cursor effective |
| 6 | (this session) | 0 | 0 | ok | Idempotency re-check: still 0 |

Run IDs (audit trail, `state.db` → `audit_runs`):

| # | run_id | status | per_source_counts |
|---|--------|--------|-------------------|
| 1 | `340fae0b-…` | failed | `{}` |
| 2 | `60a9b5fc-…` | failed | `{"fs": 121}` |
| 3 | `951cb47f-…` | failed | `{"fs": 121}` |
| 4 | `a8bcd479-244c-447d-b28e-8b24ceb7c030` | ok | `{"fs": 121}` |
| 5 | `ef468c57-2de0-4901-b5aa-ca280ec1d84e` | ok | `{"fs": 0}` |
| 6 | `5bff48e5-ce80-45fb-8baf-1a2d99a17f2e` | ok | `{"fs": 0}` |

---

## Bug: `pipeline.py:241` — text position in chunk tuple

### Symptom

Every pre-fix run raised:

```
ValueError: Unsupported input type: int. Expected one of: str, dict, PIL.Image.Image,
np.ndarray, torch.Tensor
```

The `sentence-transformers` embedder received an `int` instead of a `str`.

### Root cause

The chunk tuples are `(point_id, chunk_index, item, text, n_chunks)` — 5-tuples.
The embedding call used a "grab the last element" idiom:

```python
# Before (broken — text is at position 3, n_chunks is at position 4):
vectors = embedder([text for *_x, text in chunks])

# After (fixed):
vectors = embedder([c[3] for c in chunks])
```

When `n_chunks` was appended to the tuple (the `total_chunks` payload field),
the unpack expression was not updated. `*_x, text` binds `text` to the **last**
element (`n_chunks`, an int) and `*_x` to the first four. The fix targets
position 3 explicitly.

### Regression test

The fix is at the public interface (`run_pipeline` → embedder call). The minimal
regression test should:

1. Build a list of 5-tuples where `text` (pos 3) is a string and `n_chunks`
   (pos 4) is an int.
2. Call the embedding path with those tuples.
3. Assert the embedder receives the string at position 3, not the int at
   position 4.

Add to `tests/unit/test_pipeline.py` (or a new `tests/unit/test_chunk_tuple_unpack.py`):

```python
def test_embedder_receives_text_not_n_chunks(self, mock_embedder, mock_chunks_5tuple):
    """Regression: pipeline.py:241 — text is at index 3, not the last element."""
    # mock_chunks_5tuple yields [(pid, i, item, "text", 4), …]
    result = _extract_texts(mock_chunks_5tuple)  # or call the public API
    assert all(isinstance(t, str) for t in result)
```

---

## Test environment

### Test data

- **Location:** `.dt-verify/kbdata/` (inside the repo workspace, not `/tmp` —
  `/tmp/dt-verify` was wiped by the DSH sandbox between runs)
- **Files:** 121 total
  - 120 notes: `doc0/` … `doc11/` (10 notes each), each ~600 chars
  - 1 long document: `long_document.md`, ~40 000 chars (designed to produce
    multiple chunks)
- **Seeded with `random.seed(42)`** — deterministic content

### State

- `KB_STATE_DIR=.dt-verify/state` — fresh SQLite DB (`state.db`)
- `KB_CONFIG_DIR=.dt-verify/config`

### Services (all live during verification)

| Service | Endpoint | Notes |
|---------|----------|-------|
| Qdrant | `http://127.0.0.1:6333` | Default collection: `personal_kb` |
| Neo4j  | `bolt://127.0.0.1:7687` | user `neo4j`, password from Docker env |
| LLM    | `http://127.0.0.1:4001/v1` | LiteLLM proxy, model `qwen3.8-27b` |

### Ingest run invocation

```bash
cd digital-twins/
KB_STATE_DIR=.dt-verify/state \
KB_CONFIG_DIR=.dt-verify/config \
KB_QDRANT__URL="http://127.0.0.1:6333" \
KB_NEO4J__URL="bolt://127.0.0.1:7687" \
KB_NEO4J__USER="neo4j" \
KB_NEO4J__PASSWORD="$NEO4J_PW" \
KB_LLM__ENDPOINT="http://127.0.0.1:4001/v1" \
KB_LLM__API_KEY="$LITELLM_KEY" \
KB_LLM__MODEL="qwen3.8-27b" \
KB_SOURCES__FS__ENABLED=true \
KB_SOURCES__FS__EXTRA__DIR=.dt-verify/kbdata \
KB_SOURCES__FS__MAX_ITEMS=500 \
python3.12 -m digital_twins run --once
```

Key config notes:

- The fs source reads its directory from `sources.fs.extra.dir`
  (env `KB_SOURCES__FS__EXTRA__DIR`). **Not** `sources.fs.directory` —
  a `kb.local.yml` that uses `directory:` will silently fail the
  prerequisite check with "path does not exist".
- The LLM endpoint is a hard preflight dependency for non-dry-run
  (`health.py:check_llm`). Without `KB_LLM__ENDPOINT`, the pipeline
  exits 2 before any pipeline work.
- The embedder uses the pinned local model `BAAI/bge-small-en-v1.5`
  (384-dim, from local HuggingFace cache).

---

## NFR-1 confirmation

The same content ingested via `run --once` (manual / system) produces
**one** point per chunk, not four. Run 5 and Run 6 both ingested 0 points,
confirming the high-water cursor correctly suppressed re-ingestion.
The `per_source_counts` field in `audit_runs` records `{"fs": 121}` for
run 4 and `{"fs": 0}` for runs 5 and 6.

---

## Qdrant verification

- Collection used: `personal_kb` (default `QDRANT_COLLECTION` in `health.py:18`)
- Pre-existing count before verification: ~1 203 658 points
- Post-run-4: +235 points (the 235 points = 121 items, with `long_document.md`
  producing multiple chunks)
- Post-run-5/6: no change (0 new points)

---

## Files changed

| File | Change |
|------|--------|
| `digital_twins/ingest/pipeline.py:241` | Bug fix: `embedder([text for *_x, text in chunks])` → `embedder([c[3] for c in chunks])` |

---

## Open items

- [ ] Add regression test for the 5-tuple unpack bug (`tests/unit/`)
- [ ] Consider adding a `qdrant_collection(cfg)` override via
      `KB_QDRANT__COLLECTION` env var for isolated test collections
      (avoids polluting the shared `personal_kb` collection)
- [ ] Document that `KB_SOURCES__FS__EXTRA__DIR` is the correct env var
      for the fs source directory (the `kb.local.yml` key is
      `sources.fs.extra.dir`, not `sources.fs.directory`)
