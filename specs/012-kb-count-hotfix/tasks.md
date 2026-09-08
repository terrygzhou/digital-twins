# 012 — tasks (spec-lite; derived from the SDD ledger, closed 2026-09-08)

- [x] T1: live repro + root cause pinned in the SDD ledger (qdrant-client
      1.19 `count()` kwarg rename; broken call sites at `web/app.py`
      `_count_points_for_owner` / `_count_via_client`).
- [x] T2: `web/app.py` — `filter=` → `count_filter=` at both call sites.
- [x] T3: Qdrant test stubs re-pinned to the 1.19 `count_filter` signature
      (test_web_app_kb, test_web_app_ingest, test_web_app_auth,
      test_web_app.py) so the wrong-kwarg path fails, not silently passes.
- [x] V1: full suite green; commit 4356769 on main.
