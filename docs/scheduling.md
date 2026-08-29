# Scheduling: Presets, `serve`, and the Host-Cron Alternative

This page documents the 002 scheduling surface: the five preset cadences,
the `digital-twins serve` process, and the one-shot host-cron alternative.
All values resolve through the config layer at runtime; nothing here is a
host-specific constant.

## Presets

A schedule is per-user, per-source. There are exactly five presets; any
other value is rejected with a clear error listing these five.

| Preset | Expansion semantics |
|---|---|
| `hourly` | Every 60 minutes from **now**. No fire-time anchor. |
| `every-N-hours` | `now + N*3600 s` after each fire. `N` is an integer ≥ 1. |
| `daily` | Next local occurrence of the schedule's `fire_time`. |
| `weekly` | Next local `fire_time` on the **creation weekday** (the day-of-week the schedule was added). |
| `monthly` | Next local `fire_time` on the **creation day-of-month**, clamped to the month length. |

### Default fire time

Time-anchored presets (`daily`, `weekly`, `monthly`) default to **03:00**
local time when no `--fire-time` is supplied. This is a *default*, not a
hard-coded constant: it is set by the config layer and can be overridden
per schedule via `--fire-time HH:MM`.

### Month-length clamping

`monthly` clamps the creation day-of-month to the target month's length.
Example: a schedule created on **Jan 31** fires on **Feb 28** (or **Feb 29**
in a leap year), then **Mar 31**, **Apr 30**, and so on — always the last
valid day of the shorter month when the creation day exceeds the month's
length.

## `digital-twins serve` — the long-running scheduler

`digital-twins serve` starts the scheduler loop: it polls for due schedules
(~3 s tick), runs the ingestion pipeline for each due schedule over the
shared state store, and advances `next_fire_at` after each fire.

- **Single instance.** One `serve` per host. The process holds a pidfile
  at `<state_dir>/serve.lock`. A second live instance **fails fast** with a
  named error (exit code 2); it does not queue, retry, or steal the lock.
- **Status endpoint.** `GET /status` on port `scheduler.status_port`
  (default `8765`). Set the knob to `0` to disable the server entirely;
  the CLI remains the fallback surface.
- **Clean shutdown.** `SIGTERM` / `SIGINT` flush state and remove the
  pidfile.

## Host-cron alternative to `serve`

If you prefer to drive ingestion from your host's own scheduler rather than
running a `serve` process, use the one-shot path. The package does **not**
parse or store cron strings — the cron entry lives on *your* host, and the
command it invokes is the stateless `run --once` path:

```cron
0 3 * * * digital-twins run --once --as <user>
```

- The `<user>` placeholder is **required**. It names the account whose
  credentials are used for this run; there is no default, and no concrete
  username ships in this document.
- `run --once` performs a single run over enabled sources, writes its
  audit row, and exits. It does not advance schedules, does not take the
  `serve.lock` pidfile, and does not hold any long-lived state.
- Because both `serve` fires and `run --once` runs go through the same
  pipeline over the same state store, the dedup invariant holds: the same
  content ingested via either path produces **one** point, not two.

> **No cron parsing in the package.** The package neither parses nor stores
> cron strings. Custom recurrence (arbitrary cron expressions, e.g.
> "every 15 min between 09:00 and 17:00") is a follow-up (BRD follow-up,
> per the spec's A-boundaries), not a 002 feature.

## `--as <user>` credential transport

`--as <user>` authenticates the run as the named account (001 `accounts`
store, email + password hash). The password is transported **only** by:

1. The `DT_USER_PASSWORD` environment variable, or
2. An interactive prompt (when no env var is set and stdin is a TTY).

The password is **never placed in argv** (so it does not appear in
`ps` output or shell history) and **never written to logs**. `DT_USER_PASSWORD`
is auth-only: it is not a config knob and does not participate in the
four-layer config precedence; the CLI reads it directly from the
environment.

Without `--as`, the run is host-neutral: `scheduled_by = system`, no
authentication required.

## See also

- `specs/002-scheduled-runs/quickstart.md` — runnable validation scenarios.
- `specs/002-scheduled-runs/contracts/scheduler.md` — scheduler core contract.
- `specs/002-scheduled-runs/contracts/cli.md` — CLI contract (002 additions).
- `specs/002-scheduled-runs/data-model.md` — `Schedule` entity and expansion rules.
