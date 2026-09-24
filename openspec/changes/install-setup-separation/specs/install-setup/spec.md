# Capability: install-setup separation (delta)

## ADDED Requirements

### Requirement: Installation writes no user configuration or state
The install step — the `curl … | bash` one-liner, `bash
scripts/install-local.sh`, or `pip install digital-twins-kb[<extras>]`
(pip/venv/uv are implementation details the user never has to type) —
shall not create or modify `kb.local.yml`, the state directory, the state
DB, or an admin account. An installation is complete when the console
script `digital-twins --version` exits 0. (Later commands — `run`, `setup` — *may* create the state dir;
this requirement is scoped to the install step only.)

#### Scenario: Fresh install step is inert
- **WHEN** the install step of `digital-twins-kb` completes on a host with
  no prior config
- **THEN** the install step created neither `kb.local.yml` nor the state
  dir/state DB/admin account, and `digital-twins --version` exits 0 with
  no prompts. (A subsequent `digital-twins setup` or `run` may create the
  state dir; that is out of this requirement's scope.)

### Requirement: Installers are install-only by default
`scripts/install.sh` and `scripts/install-local.sh` shall complete the
package install and stop, printing the follow-up
("run `digital-twins setup` to choose local vs. external backends"). The
wizard runs only when `--with-setup` is passed. `--no-setup` remains an
accepted, documented no-op alias for one release. `--run-ingest` implies
`--with-setup` (ingesting into an unconfigured host would fail-fast).

#### Scenario: Default installer behavior
- **WHEN** `bash install.sh` runs on a clean host with no `--with-setup`
- **THEN** only Python discovery + venv + `pip install` run, no `setup`
  invocation occurs, and the exit status reflects install success only.

#### Scenario: Opt-in combined install + setup
- **WHEN** `bash install.sh --with-setup` runs
- **THEN** after the install step, `digital-twins setup` is invoked with the
  same flag pass-throughs as today (`--cloud`, `--cloud-env`,
  `--skip-services`).

#### Scenario: `--run-ingest` implies setup
- **WHEN** `bash install.sh --run-ingest` runs (no explicit `--with-setup`)
- **THEN** the wizard runs first (as if `--with-setup` were passed) and the
  first ingest follows; the install step alone does not run the wizard.

### Requirement: Setup is the single, idempotent configuration decision point
`digital-twins setup` shall be the one command that decides backends and
creates the state DB + first admin account + runs health checks. Re-running
it on an already-configured host shall be a no-op for the backend decision
(existing `kb.local.yml` is preserved; no re-prompting of endpoints). The
re-run no-op is the `kb.local.yml` *presence* short-circuit
(`has_valid_local_config()`), so it applies equally to hosts configured by
`setup` and hosts whose `kb.local.yml` was produced by the deprecated
`init` (whose merge-on-existing values live in the same file).

#### Scenario: Re-run is a no-op
- **WHEN** `digital-twins setup` is run twice on a host with a valid
  `kb.local.yml` (written by `setup` or by a legacy `init`)
- **THEN** the second run does not prompt for endpoints, does not overwrite
  `kb.local.yml`, and returns the health-check result only.

### Requirement: Per-service backend choice
`setup` shall let the user choose, independently for each of the four
services — `qdrant`, `neo4j`, `llm`, `embedding` — between *local*
(the bundled Docker service) and *external* (a user-supplied endpoint URL).
The resulting `kb.local.yml` shall mix local URLs and external URLs per
service accordingly. A service chosen *local* with no usable Docker/GPU
shall fail the health check for that service with a remediation, without
affecting the others.

#### Scenario: Local Qdrant + external LLM
- **WHEN** the user selects `qdrant=local` and `llm=<cloud URL>` in setup
- **THEN** `kb.local.yml` contains `qdrant.url: http://localhost:6333` and
  `llm.endpoint: <cloud URL>`, and only the qdrant service is started in
  the bundled stack.

#### Scenario: All-local shorthand
- **WHEN** the user runs `setup --local`
- **THEN** all four services resolve to their bundled local URLs (embedding
  + llm only when a GPU is present, as today).

#### Scenario: All-external shorthand (prompt mode)
- **WHEN** the user runs `setup --cloud`
- **THEN** no Docker service is started and all four services resolve to
  external endpoint prompts (today's `--cloud` behavior, unchanged); the
  interactive per-service second pass is suppressed.

#### Scenario: All-external, non-interactive (env mode)
- **WHEN** the user runs `setup --cloud-env`
- **THEN** no Docker service is started and all four services are resolved
  from the `KB_*` env vars only (never a prompt); a missing required var
  exits 5 naming the var. The interactive second pass is suppressed.

#### Scenario: `--skip-services` is a whole-stack decision
- **WHEN** the user runs `setup --skip-services`
- **THEN** setup never probes Docker, never prompts, and never writes
  `kb.local.yml` (the re-run fast path: init + admin + health only). It is
  orthogonal to per-service resolution; combining it with `--backends`
  (which implies a write) is a contradiction and setup exits with a clear
  error naming the conflict.

#### Scenario: Explicit flags suppress the interactive second pass
- **WHEN** the user runs `setup --backends qdrant=local`
- **THEN** `qdrant` resolves from the flag (local URL) and the second pass
  fires only for the remaining services (`neo4j`, `llm`, `embedding`);
  `setup --local` / `--cloud` / `--cloud-env` name all four services and
  suppress the second pass entirely.

## MODIFIED Requirements

### Requirement: `digital-twins init` (deprecated alias)
`digital-twins init` shall remain available as a deprecated alias that
delegates to the `setup` flow's *narrower* promise (state DB + migrations +
first admin + health report — not the backend decision). Running it prints
a one-line deprecation notice and redirects to `digital-twins setup`.
`init`'s merge-on-existing behavior is preserved: when a valid
`kb.local.yml` already exists, re-running `init` keeps existing values and
prompts only for the *missing* fields (today's `init` contract,
`tests/integration/test_cli_init.py`). Existing remediation text in
`cli.py` / `health.py` / `mcp/dispatch.py` / `scheduler/loop.py` shall say
"run `digital-twins setup`" rather than "run `digital-twins init`".

#### Scenario: init delegates to setup's state/admin/health subset
- **WHEN** `digital-twins init` is invoked
- **THEN** it prints a deprecation notice naming `digital-twins setup` as
  the canonical command, then runs state DB + migrations + first admin +
  health report, and (when a valid `kb.local.yml` already exists) keeps
  existing values, prompting only for missing fields.
