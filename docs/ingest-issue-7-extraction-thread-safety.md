# Issue 7 — `extraction.concurrency > 1` crashes the run: shared SQLite connection used across threads

Status: **FIXED** — main-thread audit write hoisted (7-A) · 2026-10-22 · severity: **high** (run-killer, not silent — it aborts the whole ingestion, not one item)
Companion to: `docs/ingest-reliability-traceability.md` (issues 1–5) and `docs/ingest-issue-6-supersede-edge-loss.md` (issue 6).

## One-liner

When `extraction.concurrency > 1`, `process_item` runs on `ThreadPoolExecutor` workers, but the **shared** `db` (a single `sqlite3` connection created on the main thread, `check_same_thread=True` by default) is passed into every worker. Each worker's `record_audit_item(db, …)` calls `conn.execute()` on that main-thread connection from a *different* thread → `sqlite3.ProgrammingError`, which escapes the per-item `except` and **kills the entire run**. Any run that enables entity extraction *and* sets `extraction.concurrency > 1` is doomed the first time it writes an audit row.

## Root cause (code trace)

Per-item loop in `digital_twins/ingest/pipeline.py` `_run_extraction`:

| line | call | effect |
|---|---|---|
| `554` | `concurrency = int(get(cfg, "extraction.concurrency") or 1)` | picks the execution mode |
| `555-557` | `if concurrency <= 1: for item in items: process_item(item)` | **safe** — single-threaded, `db` stays on the main thread |
| `558-560` | `else: with ThreadPoolExecutor(max_workers=concurrency) as pool: list(pool.map(process_item, items))` | `process_item` now runs on **worker threads** |
| `544` | `record_audit_item(db, …, status="ok")` | writes the audit row on the **shared** `db` — **on a worker thread** when `concurrency>1` |
| `550` | `record_audit_item(db, …, status="failed")` | the `except` handler *also* writes on `db` from the worker thread |
| `models.py:260` | `conn.execute("INSERT INTO audit_items …")` | `conn` is the main-thread connection → **cross-thread use** |

The `db` passed into `process_item` is the connection created on the main thread (`digital_twins/state/db.py`). Python's `sqlite3` opens with `check_same_thread=True` (the default), so the moment a worker thread touches it:

```
sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread.
The object was created in thread id 124047371616384 and this is thread id 124046131926720.
```

### Why it takes the whole run down (not just one item)

The irony: the audit ledger (Issue 4 — the thing built to record *per-item* failures) is itself the crash site.
- `record_audit_item` at `pipeline.py:544` is inside the item's `try`. If it raises `ProgrammingError`, the `except Exception` at `pipeline.py:545` **catches it** — and immediately calls `record_audit_item` *again* at `pipeline.py:550` to log `status="failed"`.
- That second call raises `ProgrammingError` **inside the `except` block**, which is **not** caught by the same `except` → it propagates out of `process_item` → out of `pool.map` → out of `run_pipeline` → **the entire run aborts**.

So with `concurrency>1` + extraction enabled, the *first* audit-row write from a worker thread kills the run. It cannot even complete a single item.

## Live evidence (this deployment, 2026-10-22)

- `kb.local.yml` on OCI set `extraction.concurrency: 4`. The `run` (`trigger=manual`, run `05d469d5`, started 13:41:59) **failed after 18 s** with the `ProgrammingError` above; `audit_runs` shows `status=failed`, `completed_at` set, and it leaked its `ingest_locks` row (no process released it → Issue-3-style stale lock).
- After patching `extraction.concurrency → 1` and clearing the stale lock, the follow-up run (`66012fb6`, started 13:44:57) is **live and advancing** — `highwater` count climbed 1443 → 1489+ with `updated_at` ticking. Single-threaded path = no cross-thread `db` use = no crash.
- `dmesg`: **no OOM** lines; the death signature (dead process, no Python traceback in the log past model-load, leaked lock) is the thread-safety abort, not a memory kill.

## Impact

- **Any `concurrency>1` + extraction run aborts** — the whole sweep dies, not just a flaky item. The "speed it up" knob (Issue 2) is currently a **footgun**: raising concurrency is exactly what triggers the crash.
- **Leaked ingest lock** on abort (the run dies before releasing `ingest_locks`), so the *next* run is blocked by a stale lock (compounds Issue 3).
- **Silent-ish at glance**: the run exits non-zero and the log shows the traceback, but on a long detached run you can miss it and just see "stuck / not advancing."
- Blocks the one safe path to faster ingestion: you cannot turn up `extraction.concurrency` (the LLM calls are the bottleneck) until this is fixed.

## Fix options (ranked)

**7-A (recommended) — keep the LLM calls parallel, keep SQLite single-threaded: write audit rows on the main thread only.**
Let workers do the parallel *extraction* (the part that actually benefits from concurrency), but have them **return** their audit results instead of writing `db` themselves. The main thread drains results and calls `record_audit_item` sequentially:
```python
results = list(pool.map(extract_item, items))      # workers: LLM extract, NO db writes
for res in results:                                  # main thread: single-writer audit
    record_audit_item(db, run_id, source, res.key,
                      status=res.status, error=res.error)
```
- Matches the existing single-writer design intent (`ingest_locks` is already single-writer).
- No `check_same_thread=False`, no locks, no per-thread connections.
- Parallel LLM extraction is preserved (the real win); SQLite stays on one thread.

**7-B — per-thread SQLite connection.** Give each worker its own `sqlite3.connect()` (thread-local), so each `record_audit_item` uses a same-thread connection.
- Simpler diff than 7-A (a thread-local factory), but now **N concurrent writers** hit the DB → need `busy_timeout` + serialization to avoid `database is locked`.
- More moving parts than 7-A; only choose if you want per-item atomicity with concurrency.

**7-C — `check_same_thread=False` + a `threading.Lock` around every `db` write.**
- One-line-ish at the connection, but you must wrap *every* `conn.execute/commit` (audit + highwater + lock) in the lock or you get corruption, not just a `ProgrammingError`.
- Highest risk (a missed lock = silent SQLite corruption). Not recommended unless 7-A is infeasible.

**7-D (guard, do with any of the above) — fail the `run` early with a clear error when `extraction.concurrency > 1` and extraction is enabled, until 7-A/7-B land.**
- `if extraction.enabled and concurrency > 1: log.warning("extraction.concurrency>1 is not yet thread-safe (Issue 7); forcing concurrency=1") ; concurrency = 1`
- Prevents the silent abort + lock leak in the meantime. This is the **immediate safe default** (I've already set `concurrency=1` on OCI; this makes it the *code* default, not a manual config patch).

## Recommended sequence

1. **7-D now** — clamp `concurrency` to 1 (with a loud log) when extraction is enabled. Makes the shipped default safe; no behavior regression.
2. **7-A** — move audit writes to the main thread; keep extraction parallel. This is the real fix that unlocks raising `extraction.concurrency` safely.
3. **Test-First (constitution):** add a unit test that runs `_run_extraction` with `concurrency=2` and a stubbed `extract`, and asserts (a) no `ProgrammingError`, (b) both items' audit rows are present in `audit_items`, (c) the run completes. Fails on current code (thread crash), passes when 7-A lands.
4. After 7-A: revisit Issue 2 (concurrency + retry) — safe to turn `extraction.concurrency` back up to speed up ingestion.

## Applied fix (7-A) — 2026-10-22

Implemented 7-A: `process_item` no longer touches the SQLite `db` — it returns `(item.key, status, error)`, and `_run_extraction` drains the results and calls `record_audit_item` on the **main thread** for both the serial and parallel branches. LLM extract stays parallel (the slow work); every audit write is single-writer on the thread that owns the connection.

- `digital_twins/ingest/pipeline.py` — `process_item` returns the record; `concurrency` branch now joins `pool.map` results then loops `record_audit_item` on the main thread.
- Regression test `tests/unit/test_pipeline_extraction.py::test_extraction_concurrency_with_real_db_no_cross_thread_crash` — builds a **real** main-thread `sqlite3` connection (`check_same_thread=True`, the CLI's shape), runs `_run_extraction(concurrency=4, db=conn)`. **Red-green verified**: fails on the pre-fix code with the live `sqlite3.ProgrammingError`, passes post-fix with 6/6 audit rows.
- `7-D` (the concurrency clamp) is now moot for the *safe* path — concurrency is safe to raise — but the live keyva run was already parked at `concurrency=1` on OCI; re-deploy then re-enable `concurrency>1`.

## Out of scope / notes

- Does not touch the atomic-commit, highwater, or dedup invariants.
- The `db` connection is the *only* shared mutable object passed into workers; `neo4j` (the graph driver) is also shared — verify it's thread-safe for concurrent `supersede`/`materialize` under `concurrency>1` (the `neo4j` Python driver's `Session`/`Driver` are generally thread-safe, but confirm in the same test before re-enabling concurrency).
- Should land as a Spec-Kit feature (next available spec, likely **014** alongside Issue 6): `specify → clarify → plan → tasks → implement`, SDD ledger in `.superpowers/sdd/`, `specs/<n>/tasks.md` kept in sync.
- Immediate mitigation already applied on OCI: `extraction.concurrency=1` (the safe path); ingestion is running and advancing.

## References

- `digital_twins/ingest/pipeline.py:544-552` (`process_item` `try/except` + the two `record_audit_item` calls), `554-560` (the `concurrency` branch / `ThreadPoolExecutor`)
- `digital_twins/state/models.py:252-265` (`record_audit_item` — `conn.execute` + `conn.commit` on the passed-in shared `db`)
- `digital_twins/state/db.py` (where the main-thread `sqlite3` connection is created, `check_same_thread` default)
- Live traceback: `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread` (run `05d469d5`, OCI, 2026-10-22 13:41:59–13:42:17)
- `docs/ingest-issue-6-supersede-edge-loss.md` (issue 6), `docs/ingest-reliability-traceability.md` (issues 1–5, esp. 2 = concurrency, 3 = lock leak)
