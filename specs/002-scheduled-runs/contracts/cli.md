# Contract: CLI (002 additions)

Extends the 001 `click` CLI. Existing 001 commands unchanged.

## `digital-twins serve [--port N]`

Start the long-running scheduler (US1). `--port` overrides `scheduler.status_port`;
`--port 0` disables the status endpoint entirely (the CLI remains the fallback surface).
Fails fast with exit code 2 (clear, named error) if:
- another live `serve` holds `serve.lock` (A2/R4; pidfile at `config["state_dir"]/serve.lock`),
- or a required endpoint fails 001's health preconditions at startup.
Per-source prerequisite failures at fire time do NOT stop `serve`: the run for that
schedule writes a `failed` audit row naming the source and the missing prerequisite,
and `serve` keeps running (next tick retries). SIGTERM → clean shutdown (flush state,
remove pidfile); SIGINT same. Migration v2 is applied before the serve loop starts
(001 `pre_command` pattern), not inside the loop.

## `digital-twins run --once [--as USER] [--source NAME]`

Single run over enabled sources (or one named source), then exit (US2).
`--once` marks the run as one-shot: no schedule advance, no pidfile.
`--as` authenticates against the 001 `accounts` store via the `DT_USER_PASSWORD`
env var or an interactive prompt (R5); never in argv, never in logs.
Role policy (002): any account with a set `password_hash` may authenticate
as itself; role *enforcement* (admin/reader permissions on data) is slice 003.
Without `--as`, `scheduled_by = system` and no auth is required
(host-neutral operation). `--source` limits the run to one source;
default = all enabled sources.
Exit codes: `0` ok/partial, `1` failed, `2` auth/config error.
Exit code 2 covers both 002's auth failure and 001's fail-fast prerequisite
behavior (001's T019/T025 follow-up) — both are configuration/auth-class errors.
The `run` command's pre-existing behavior (prerequisite gating, audit row on
failure) is preserved unchanged; `--once` only changes the trigger and the
post-run behavior (no schedule advance, exit after the run).

## `digital-twins schedule add|list|remove` (FR-3 v1 CRUD)

- `schedule add --as USER --source NAME --preset {daily|hourly|weekly|monthly|every-N-hours} [--param N] [--fire-time HH:MM]`
- `schedule list [--as USER]` — table: owner, source, preset, fire time, next fire, enabled
- `schedule remove --id N`

Owner = the `--as` user (default `system`).
For `schedule add`, `--as` names the schedule owner; the operator's own
authentication is a separate concern (002 ships no role/permission model;
role enforcement is slice 003). Invalid preset → clear error listing the five
presets (FR-8).

## `digital-twins` credential env vars (002 additions)

- `DT_USER_PASSWORD` — account password for `--as <user>` authentication.
  Auth-only: not a config knob, does not participate in the four-layer
  config precedence; read directly from the environment by the CLI.
  Documented in `.env.example` and `docs/scheduling.md`.
- No `DT_STATE_DIR`: the state directory is the 001 `KB_STATE_DIR` knob
  (env) or `state_dir` config; the quickstart uses that name.

## Documentation obligation (FR-8)

`docs/` gains the host-cron snippet block:

```cron
0 3 * * * digital-twins run --once --as <user>
```

presented explicitly as the *alternative* to `digital-twins serve`; the docs
state that the package neither parses nor stores cron strings and that custom
recurrence is a follow-up. The `<user>` placeholder keeps the snippet
host-neutral (no concrete username in shipped docs — NFR-13 / SC-006).
