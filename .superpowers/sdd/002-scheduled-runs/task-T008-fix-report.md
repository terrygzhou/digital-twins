# T008 Fix Report — health gate stricter than 001's `run`

## What I found

### 001's `run` gate (cli.py:141–270)
001's `run` command **does not call `health.run_health_checks` at all**. It gates on
**per-source `source.prerequisites()`** (cli.py:219–234): for each enabled (or
`--source`-named) source it builds the source object, calls `prerequisites()`, and if
any prerequisite is missing, writes a failed audit row, names the source + missing
prerequisite, and exits 2. Endpoint errors (dead qdrant, unreachable LLM) surface
later, per-source, inside the pipeline — a qdrant-down failure fails only the
sources that actually write to qdrant.

### T006's `serve_once_tick` (loop.py:76–142)
Per-source failures at fire time are already handled: `serve_once_tick` catches *any*
exception from `run_pipeline`, backstops a `failed` audit row if none was written,
and advances the schedule (R-07: "reported, never silent"). A dead qdrant at fire
time produces a failed audit row, not a serve crash.

### T008's gate (cli.py:359–364, original)
```python
results = health.run_health_checks(cfg)
if any(not r.ok for r in results):
    _print_report(results)
    click.echo("serve: required endpoint(s) failed health preconditions", err=True)
    raise SystemExit(2)
```
This gates on **all three** endpoints (qdrant, neo4j, llm). If neo4j is down but the
configured pipeline is LLM-only, `serve` still refuses to start — stricter than 001's
`run`, which would have started and let the pipeline fail per-source. The brief's global
constraint (task-T008-brief.md:23–31) says explicitly: "Match 001's actual behavior —
do not invent a stricter gate than 001 uses for `run`."

## What I changed

1. **`digital_twins/cli.py`** — removed the health gate block from `serve` entirely.
   Updated the docstring to explain *why*: 001's `run` does not gate on endpoint
   health; it gates on per-source prerequisites. `serve` matches that. A down endpoint
   at startup is not a reason to refuse to start; per-source failures at fire time are
   handled by T006's `serve_once_tick` (failed audit row + advance, R-07).

2. **`tests/unit/test_serve_cli.py`**:
   - Removed the `_stub_health` helper and its `health` import from the module-level
     imports (no longer needed for the other tests).
   - Removed `test_serve_missing_endpoint_exit_2` (it pinned the stricter behavior).
   - Added `test_serve_no_endpoint_health_gate`: stubs `health.run_health_checks` to
     report every endpoint down *and* record that it was called; invokes `serve --port
     0`; asserts exit 0, `run_serve` WAS called, and the health stub was NOT called
     (`called["n"] == 0`). This pins the new contract: `serve` never consults
     endpoint-health at startup.
   - Updated the module docstring to note the no-health-gate rationale.
   - Updated the four remaining tests' docstrings and removed their `_stub_health`
     calls.

## Test results

```
$ python3 -m pytest tests/unit/test_serve_cli.py -v
5 passed in 0.03s
```

Full suite:
```
$ python3 -m pytest -q
263 passed, 1 failed in 1.26s
```

The 1 failure is `test_scheduler_imports.py::test_status_module_exposes_status_payload`
(T017's status payload) — **pre-existing on a clean checkout** (verified via
`git stash && pytest … ; git stash pop`). Unrelated to this change.

## Commit

See commit SHA below (created after this report).
