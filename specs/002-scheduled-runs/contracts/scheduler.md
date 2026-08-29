# Contract: Scheduler Core

## `digital_twins.scheduler` module surface

```python
# presets.py — pure functions, no I/O (property-testable)
def expand_next(preset: str, param: int | None, fire_time: str, now: datetime, anchor: datetime) -> datetime: ...
# anchor: schedule creation time; weekly/monthly anchor rules per data-model.md

def preset_values() -> list[str]:  # ['daily','hourly','weekly','monthly','every-N-hours']

# schedules.py — state store (001 db), CRUD only, no fire logic
def create_schedule(db, owner, source, preset, param=None, fire_time='03:00', now=None) -> dict: ...
def list_schedules(db, owner: str | None = None) -> list[dict]: ...
def update_schedule(db, schedule_id, **fields) -> dict: ...      # cadence/param/fire_time/enabled
def delete_schedule(db, schedule_id) -> None: ...
def due_schedules(db, now=None) -> list[dict]: ...              # enabled AND next_fire_at <= now

def claim_and_advance(db, schedule_id, fired_at) -> None: ...
# atomically: next_fire_at = expand_next(fired_at); the row is the fire's ticket

# loop.py
def serve_once_tick(db, config) -> dict: ...
# one pass: due_schedules -> run pipeline per schedule (001 pipeline, shared state)
# -> claim_and_advance; returns {fired: [...], skipped: [...], queue_depth: int}

def run_serve(db, config, status_port: int) -> None: ...
# pidfile guard (R4) -> status server if port>0 -> loop{ serve_once_tick; sleep } until SIGTERM

# status.py
def status_payload(db, config, pending_fires: int) -> dict:
# {
#   "schedules": [ {id, owner, source, preset, next_fire_at, enabled} ],
#   "last_run":  { "source": str, "status": "ok|partial|failed", "run_id": str, "completed_at": str } | null,
#   "queue_depth": int
# }
```

## Invariants

1. `expand_next` is a pure function of (preset, param, fire_time, now, anchor) — the same inputs always give the same fire time (SC-003).
2. A fired schedule is advanced **before** its pipeline completes only in memory; the durable advance happens in `claim_and_advance` after the audit row is written. A crash between them re-fires the cycle; dedup (deterministic IDs + high-water) makes that safe.
3. Fire decisions read `next_fire_at` only — never recompute from a stale stored time (R3/clock-skew guard).
4. Every executed run writes exactly one `audit_runs` row (001 invariant, any trigger).

## Status HTTP surface

- `GET /status` → `200` JSON = `status_payload(...)`; `404` for any other path.
- Port `scheduler.status_port` (default 8765); `0` disables the server entirely.
- No auth in 002 (A3); auth for the HTTP surface ships with 003.
