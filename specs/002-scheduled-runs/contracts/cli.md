# Contract: CLI (002 additions)

Extends the 001 `click` CLI. Existing 001 commands unchanged.

## `digital-twins serve [--port N]`

Start the long-running scheduler (US1). `--port` overrides `scheduler.status_port`.
Fails fast with a clear error if: another live `serve` holds `serve.lock` (A2/R4),
or a required endpoint fails 001's health preconditions.
SIGTERM → clean shutdown (flush state, remove pidfile). SIGINT same.

## `digital-twins run --once [--as USER] [--source NAME]`

Single run, then exit (US2). `--as` authenticates against the 001 accounts
store via `DT_USER_PASSWORD` env var or interactive prompt (R5); without
`--as`, `scheduled_by = system` and no auth is required (host-neutral
operation). `--source` limits the run to one source; default = all enabled
sources. Exit codes: `0` ok/partial, `1` failed, `2` auth/config error.

## `digital-twins schedule add|list|remove` (FR-3 v1 CRUD)

- `schedule add --as USER --source NAME --preset {daily|hourly|weekly|monthly|every-N-hours} [--param N] [--fire-time HH:MM]`
- `schedule list [--as USER]` — table: owner, source, preset, fire time, next fire, enabled
- `schedule remove --id N`

Owner = the `--as` user (default `system`). Invalid preset → clear error listing the five presets (FR-8).

## Documentation obligation (FR-8)

`docs/` gains the host-cron snippet block:

```cron
0 3 * * * digital-twins run --once --as alice
```

presented explicitly as the *alternative* to `digital-twins serve`; the docs
state that the package neither parses nor stores cron strings and that custom
recurrence is a follow-up.
