# Contract: Scheduler Core

## `digital_twins.scheduler` module surface

```python
# presets.py — pure functions, no I/O (property-testable)
def expand_next(preset: str, param: int | None, fire_time: str, now: datetime, anchor: datetime) -> datetime: ...
# anchor: schedule creation time; weekly/monthly anchor rules per data-model.md
# param validation: int >= 1, only for every-N-hours; other values raise ValueError

def preset_values() -> list[str]:  # ['daily','hourly','weekly','monthly','every-N-hours']

# schedules.py — state store (001 db), CRUD only, no fire logic
def create_schedule(db, owner, source, preset, param=None, fire_time='03:00', now=None) -> dict: ...
def list_schedules(db, owner: str | None = None) -> list[dict]: ...
def update_schedule(db, schedule_id, **fields) -> dict: ...      # cadence/param/fire_time/enabled
def delete_schedule(db, schedule_id) -> None: ...
def due_schedules(db, now=None) -> list[dict]: ...              # enabled AND next_fire_at <= now
#   ordering: oldest next_fire_at first (ties broken by schedule id ascending)

def claim_and_advance(db, schedule_id, fired_at) -> None: ...
# fired_at = actual fire time (run start); atomically: next_fire_at = expand_next(fired_at); the row is the fire's ticket

# loop.py
def serve_once_tick(db, config) -> dict: ...
# one pass: due_schedules -> run pipeline per schedule (001 pipeline, shared state,
# trigger='schedule', scheduled_by=schedule owner) -> claim_and_advance
# per-source caps/timeouts read fresh from `config` on every tick (no caching)
# returns {fired: [schedule id...], skipped: [schedule id...], queue_depth: int}

def run_serve(db, config, status_port: int) -> None: ...
# pidfile guard at config["state_dir"]/"serve.lock" (R4) -> status server if status_port>0
# -> loop{ serve_once_tick; sleep } until SIGTERM/SIGINT
# migration v2 must have been applied before the loop starts (001 pre_command pattern);
# signal handlers do not run migrations
# per-source prerequisite failure during a fire: write a `failed` audit row for that
# schedule and keep serving (next tick retries); serve itself exits 2 only for
# config/health preconditions at startup, not for per-source fire-time failures

# status.py
def status_payload(db, config, pending_fires: int) -> dict:
# {
#   "schedules": [ {id, owner, source, preset, next_fire_at, enabled} ],
#   "last_run":  { "source": str, "status": "ok|partial|failed", "run_id": str, "completed_at": str } | null,
#   "queue_depth": int
# }
# last_run = single object: the most recent run across all sources;
# "per source" in the spec wording is satisfied by the source field on that object
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
