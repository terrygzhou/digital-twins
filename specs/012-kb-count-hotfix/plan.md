# 012 — kb-count hotfix (spec-lite, ledger-first slice)

Backfilled 2026-09-08 from `.superpowers/sdd/012-kb-count-hotfix/progress.md`
(hotfix slice: pre-0.9.0-release defect found by owner re-test on a live
0.9.0 wheel — `/api/me.point_count` and `/api/kb/points.owner_count` read 0
while search returned real points).

## Defect (evidence in ledger, 2026-09-01)

- Shipped `web/app.py` called `client.count(coll, filter=...)`; the installed
  **qdrant-client 1.19.0** signature is `count(collection_name,
  count_filter=None, ...)` and the transport rejects unknown kwargs
  (`AssertionError: Unknown arguments: ['filter']`).
- Broken call sites: `web/app.py` `_count_points_for_owner` (`/api/me`,
  swallowed by `except Exception: return 0`) and `_count_via_client`
  (`/api/kb/points` + `?source=`-filtered count, mapped to 503).
- Why tests missed it: the Qdrant stubs pinned the **wrong kwarg**
  (`count(self, collection, filter=None, **_kw)`), silently accepting the
  buggy call.

## Fix

- `digital_twins/web/app.py`: `filter=` → `count_filter=` at both call sites
  (the no-filter branch `count(coll)` is unchanged).
- Stubs: pin the 1.19 signature in `tests/unit/test_web_app_kb.py`,
  `tests/unit/test_web_app_ingest.py`, `tests/unit/test_web_app_auth.py`,
  `tests/integration/test_web_app.py` so the wrong-kwarg regression cannot
  go green again.
- Ruling (mirrors 010): no version bump — 0.9.0 remains the current line.

## Status

Complete — commit `4356769` (fix) on main; see `tasks.md` + the SDD ledger
for the live-verification record.
