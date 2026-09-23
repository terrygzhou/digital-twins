# Change: Separate installation from setup; make backend choice per-service

## Why
"Install" and "setup" are currently opt-in separated, not structurally:
both installers (`scripts/install.sh`, `scripts/install-local.sh`) *run the
`digital-twins setup` wizard by default* and only `--no-setup` opts out. So a
user who runs the one-liner is pulled into backend configuration mid-install.
Worse, the setup wizard's backend decision is a *binary* — whole local Docker
stack **or** all-cloud — whereas the user's real intent is to decide,
**per service** (LLM, Qdrant, Neo4j, embedding), whether that one is
locally hosted or an external endpoint. Today that per-service mix is only
possible by hand-editing `kb.local.yml`; the config layer already supports
it, the wizard does not offer it.

## What changes
- **Install is pure package install.** `pip install digital-twins-kb[extras]`
  (or the installers) never writes `kb.local.yml`, never creates the state DB,
  never prompts. Install = "the software is on the machine".
- **Setup is the explicit, separate, re-runnable second act.**
  `digital-twins setup` is where the user decides backends, creates the
  state DB + first admin, and runs health checks. It is idempotent.
- **Per-service backend choice.** `setup` gains a per-service decision for
  each of `qdrant`, `neo4j`, `llm`, `embedding`: *local* (bundled Docker
  service) or *external* (user-supplied endpoint URL). A `setup --local` /
  `--cloud` shorthand still means "all four local" / "all four external".
  Mixed configs (e.g. local Qdrant + cloud LLM) are first-class, not a
  hand-edit.
- **Installers default to install-only.** `install.sh` / `install-local.sh`
  stop auto-running the wizard; they end with "run `digital-twins setup`".
  A `--with-setup` opt-in reproduces today's combined behavior. `--no-setup`
  is retired (it becomes the default; kept as an accepted no-op alias for
  one release).
- **Retire / alias `digital-twins init`.** `init` and `setup` overlap
  (both create state DB + admin). `init` becomes a deprecated alias of
  `setup`; remediation text converges on "run `digital-twins setup`".

## Impact
- Affected code: `scripts/install.sh`, `scripts/install-local.sh`,
  `digital_twins/setup.py`, `digital_twins/cli.py` (`setup`/`init`
  subcommands + `--backends` flag), `digital_twins/config/local_io.py`,
  `digital_twins/health.py` (per-service remediation already exists — reuse).
- Affected specs: none of 001–013 test the *installer auto-run* contract
  directly, but the **installer doc-contract tests**
  (`tests/unit/test_install_sh_script.py`, `test_install_script.py`) assert
  the current "wizard runs by default" behavior and MUST be updated to the
  install-only default.
- Docs: README "Fast path" restructured into a distinct *Install* step and a
  *Setup* step; `docs/configuration.md` gains a "Setup (separate,
  idempotent, per-service)" section.
- No new dependencies, no host paths (NFR-13 / BR-11).

## Non-goals
- No change to the config precedence model (env → kb.local.yml → kb.yml →
  defaults) or to how `kb.local.yml` is read.
- No change to the bundled compose file's *capabilities*; per-service choice
  only affects which services are started / which URLs are written.
- No migration of an existing host's `kb.local.yml` (an already-configured
  host re-running setup stays a no-op via the existing
  `has_valid_local_config()` short-circuit).
- No new installer entry points; `curl | bash` and `bash install-local.sh`
  keep their single-command UX — they just end earlier.
