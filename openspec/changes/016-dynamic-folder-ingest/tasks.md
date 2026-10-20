# Tasks: 016-dynamic-folder-ingest

Mirrors `specs/016-dynamic-folder-ingest/tasks.md` (canonical SDD
list); kept in sync with that file.

## 1. One-shot folder source
- [ ] 1.1 Unit tests `tests/unit/test_onshot_folder.py` (RED).
- [ ] 1.2 Implement `digital_twins/ingest/onshot.py`.

## 2. MCP tool surface
- [ ] 2.1 `registry.py`: schema + `kb_ingest_folder` entry.
- [ ] 2.2 Unit tests `tests/unit/test_mcp_ingest_folder.py` (RED).
- [ ] 2.3 `dispatch.py`: `_kb_ingest_folder_body` + wiring.

## 3. Integration + guards
- [ ] 3.1 Integration test (real pipeline, dedup on re-run).
- [ ] 3.2 Guards: `test_portability.py` + `test_knob_docs.py`.
- [ ] 3.3 Full suite.

## 4. Semver + release note
- [ ] 4.1 Bump `__version__` to 0.12.0.
- [ ] 4.2 CHANGELOG 0.12.0 entry.

## 5. Bookkeeping
- [ ] 5.1 OpenSpec artifacts (this folder) validated.
- [ ] 5.2 SDD ledger `.superpowers/sdd/016-dynamic-folder-ingest/progress.md`.
