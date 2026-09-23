# Capability: install-setup separation (delta)

## ADDED Requirements

### Requirement: Installation writes no user configuration or state
Installing the package (via `pip install digital-twins-kb[<extras>]` or the
installer scripts) shall not create or modify `kb.local.yml`, the state
directory, the state DB, or an admin account. An installation is complete
when the console script `digital-twins --version` exits 0.

#### Scenario: Fresh install is inert
- **WHEN** `digital-twins-kb` is installed on a host with no prior config
- **THEN** neither `kb.local.yml` nor the state dir/state DB/admin account
  exists, and `digital-twins --version` exits 0 with no prompts.

### Requirement: Installers are install-only by default
`scripts/install.sh` and `scripts/install-local.sh` shall complete the
package install and stop, printing the follow-up
("run `digital-twins setup` to choose local vs. external backends"). The
wizard runs only when `--with-setup` is passed. `--no-setup` remains an
accepted, documented no-op alias for one release.

#### Scenario: Default installer behavior
- **WHEN** `bash install.sh` runs on a clean host with no `--with-setup`
- **THEN** only Python discovery + venv + `pip install` run, no `setup`
  invocation occurs, and the exit status reflects install success only.

#### Scenario: Opt-in combined install + setup
- **WHEN** `bash install.sh --with-setup` runs
- **THEN** after the install step, `digital-twins setup` is invoked with the
  same flag pass-throughs as today (`--cloud`, `--cloud-env`,
  `--skip-services`).

### Requirement: Setup is the single, idempotent configuration decision point
`digital-twins setup` shall be the one command that decides backends and
creates the state DB + first admin account + runs health checks. Re-running
it on an already-configured host shall be a no-op for the backend decision
(existing `kb.local.yml` is preserved; no re-prompting of endpoints).

#### Scenario: Re-run is a no-op
- **WHEN** `digital-twins setup` is run twice on a host with a valid
  `kb.local.yml`
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

#### Scenario: All-external shorthand
- **WHEN** the user runs `setup --cloud`
- **THEN** no Docker service is started and all four services are resolved
  from external endpoint prompts / `KB_*` env vars.

## MODIFIED Requirements

### Requirement: `digital-twins init` (deprecated alias)
`digital-twins init` shall remain available as a deprecated alias that
delegates to the `setup` flow (state DB + admin + health). Running it
prints a one-line deprecation notice and redirects to
`digital-twins setup`. Existing remediation text in `health.py`/`cli.py`
shall say "run `digital-twins setup`" rather than "run `digital-twins init`".

#### Scenario: init delegates to setup
- **WHEN** `digital-twins init` is invoked
- **THEN** it runs the `setup` flow and prints a deprecation notice naming
  `digital-twins setup` as the canonical command.
