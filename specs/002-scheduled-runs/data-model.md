# Data Model: Scheduled & On-Demand Runs

## Entities

### Schedule

Per-user, per-source schedule. New table in the 001 state store (migration v2).

- `id` — INTEGER PK
- `owner` — TEXT NOT NULL (user email or `system`); who the schedule belongs to
- `source` — TEXT NOT NULL (source name from the config layer)
- `preset` — TEXT NOT NULL CHECK (`daily` | `hourly` | `weekly` | `monthly` | `every-N-hours`)
- `param` — INTEGER (N for `every-N-hours`; NULL otherwise)
- `fire_time` — TEXT `HH:MM` (local time; default 03:00 for time-anchored presets, A5)
- `enabled` — INTEGER NOT NULL DEFAULT 1
- `next_fire_at` — TEXT ISO-8601 (recomputed from actual fire time; never stale-fetched)
- `acl` — TEXT NOT NULL DEFAULT 'owner' (**reserved**, 004 placeholder per BR-11.5.5 — v1 enforces owner-only)
- `created_at` / `updated_at` — TEXT ISO-8601

Uniqueness: one active schedule per `(owner, source, preset, param, fire_time)` — upsert on conflict (re-adding the same schedule is a no-op, not a duplicate).

Expansion rules (deterministic, the contract the presets expose):
- `hourly` → every 60 min from now (no fire-time anchor)
- `every-N-hours` → now + N*3600s after each fire
- `daily`/`weekly`/`monthly` → next local `fire_time` occurrence (weekly anchors to the creation weekday, monthly to the creation day-of-month, clamped to month length)

### AuditRun (001 shape, values extended)

No schema change; 001's `audit_runs` already carries `run_id`, `started_at`,
`completed_at`, `status`, `trigger`, `scheduled_by`, `per_source_counts`.
002 legalizes the full `trigger` value set: `schedule` | `manual` | `mcp` |
`api` (`mcp`/`api` reserved until 004/003 ship — A6). `scheduled_by` is the
schedule owner, `system` when unset.

### HighWater (001, unchanged)

Per `(source, item_key)` last committed key — the resume point (FR-5).

## State interactions

- A run (any trigger) reads: enabled schedules + enabled sources + per-source knobs (`max_items`, `timeout_s`, 001 Source model).
- A run writes: high-water marks per committed item, one `audit_runs` row, points (001 deterministic IDs), and `next_fire_at` for fired schedules.
- `serve` additionally owns: in-memory pending-fire queue (A4) and `serve.lock` pidfile (A2/R4).
