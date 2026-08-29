# Quickstart / Validation Guide: Scheduled & On-Demand Runs

Runnable validation scenarios proving the slice works end-to-end. Assumes the
feature is implemented per `plan.md`. The default test suite needs no live
stores: Qdrant runs in-memory and Neo4j/LLM are stubbed (001 pattern).

## Prerequisites

- 001 installed and initialized (`digital-twins init` completed, one source enabled for the test)
- Python ≥ 3.11

## Scenario 1 — Serve fires a schedule (US1, SC-004)

```bash
digital-twins schedule add --as system --source hermes --preset every-N-hours --param 0   # fires immediately
# (param 0 is the test affordance: next fire = now)
digital-twins serve --port 8765 &
sleep 5
curl -s localhost:8765/status | python3 -m json.tool
# expect: schedules[] with the entry, last_run.status in {ok, partial, failed}, queue_depth >= 0
digital-twins run 2>/dev/null || true   # audit check via DB:
sqlite3 $DT_STATE_DIR/state.db "SELECT trigger, scheduled_by FROM audit_runs ORDER BY started_at DESC LIMIT 1;"
# expect: schedule | system
```

## Scenario 2 — One point, two trigger paths (US2, SC-001, top acceptance check)

```bash
digital-twins run --once                       # path A: manual
sqlite3 $DT_STATE_DIR/state.db "SELECT COUNT(*) FROM points WHERE source_url LIKE 'hermes:%fixture1%';"   # via test helper / qdrant query
# (in the test suite: query the in-memory Qdrant) expect: 1
digital-twins run --once                       # path B: same content again
# expect: still 1 point, audit row ok with zero new counts
```

Automated form: `tests/integration/test_serve_once_dedup.py` covers both orderings (serve-then-once and once-then-serve).

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
