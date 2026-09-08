# 013 — tasks (spec-lite; derived from the SDD ledger, closed 2026-09-08)

- [x] T1: NEW `tests/integration/test_neo4j_query.py` — 4 hermetic cases
      + 1 opt-in live case (no host values in the file; NFR-13-safe).
- [x] T2: RED — fail-closed factory case: `ConfigError` import defect in
      `scheduler/loop.py` driver factories.
- [x] T3: GREEN — 2-line import-path fix (`ConfigError` from
      `config.loader`, not `config.schema`).
- [x] V1: full suite 982 passed / 1 skipped; guards
      (`test_portability.py`, `test_knob_docs.py`) green; commit 00cf529.
