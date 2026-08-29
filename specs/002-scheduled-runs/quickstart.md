# Quickstart / Validation Guide: Scheduled & On-Demand Runs

Runnable validation scenarios proving the slice works end-to-end. Assumes the
feature is implemented per `plan.md`. The default test suite needs no live
stores: Qdrant runs in-memory and Neo4j/LLM are stubbed (001 pattern).

## Prerequisites

- 001 installed and initialized (`digital-twins init` completed, one source enabled for the test)
- Python ≥ 3.11
- Endpoints reachable: `digital-twins validate` exits 0 before any scenario

State directory: the 001 `KB_STATE_DIR` env var (or the `state_dir` config
default). All shell checks below use `$KB_STATE_DIR` — never `$DT_STATE_DIR`.

## Scenario 1 — Serve fires a schedule (US1, SC-004)

```bash
# a schedule that fires on the next tick (param 1 → fires within ~1 minute of now)
digital-twins schedule add --as system --source hermes --preset every-N-hours --param 1
digital-twins serve --port 8765 &
SERVE_PID=$!
sleep 5
curl -s localhost:8765/status | python3 -m json.tool
# expect: schedules[] with the entry, last_run.status in {ok, partial, failed},
#         queue_depth >= 0
kill $SERVE_PID
# audit check via the state DB (points live in Qdrant, not SQLite):
sqlite3 "$KB_STATE_DIR/state.db" \
  "SELECT trigger, scheduled_by FROM audit_runs ORDER BY started_at DESC LIMIT 1;"
# expect: schedule | system
```

Note: `--param 1` (not `0`): `expand_next` validates `param >= 1` (T003); a
freshly added schedule's `next_fire_at` is computed from `now`, so it fires on
the first tick within a minute.

## Scenario 2 — One point, two trigger paths (US2, SC-001, top acceptance check)

```bash
digital-twins run --once                       # path A: manual
# path B: same content again — dedup must hold (points live in Qdrant; the
# in-suite form of this check is tests/integration/test_serve_once_dedup.py,
# which asserts exactly one point per fixture in both orderings)
digital-twins run --once
# expect: second run's audit row is `ok` (or `partial`) with zero new points,
# and the Qdrant point count for the fixture content is still exactly 1
```

Automated form: `tests/integration/test_serve_once_dedup.py` covers both orderings (serve-then-once and once-then-serve) and asserts the Qdrant point count directly.

## Scenario 3 — Kill and resume (US4, SC-002)

```bash
# test suite: start a 50-item run, SIGKILL the process at ~20 items committed,
# restart, and assert total points == 50 and audit status == ok
pytest tests/integration/test_resumable_run.py -v
```

## Scenario 4 — Preset expansion is deterministic (US3, SC-003)

```bash
pytest tests/unit/test_presets.py -v    # property tests: fixed (preset,param,fire_time,now,anchor) -> fixed fire time
```

## Scenario 5 — Caps and pause without restart (US4, SC-005)

```bash
digital-twins serve --port 8765 &
# lower sources.hermes.max_items to 1 in kb.local.yml, then fire again
# expect: next run ingests <= 1 item, audit status partial, no restart needed
```

## Exit criteria

All scenarios pass; `pytest` green; `tests/integration/test_portability.py` green (SC-006); `docs/scheduling.md` documents the five presets and the host-cron snippet.
