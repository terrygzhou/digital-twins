# Design: install/setup separation + per-service backend choice

## Context
Today (v0.11.0 line):
- `scripts/install.sh` / `install-local.sh` run 4 steps: find Python, venv,
  `pip install`, **then `digital-twins setup` (the wizard)** — auto, unless
  `--no-setup`.
- `digital_twins/setup.py::run_setup()` decides the backend **once, as a
  binary**: `has_valid_local_config()` → `docker_available()` → confirm
  "start the bundled local stack?" → `run_local_stack()` (starts *all*
  bundled services) or `run_cloud_stack()` / `run_cloud_env_stack()`
  (all endpoints external). `run_local_stack()` itself already starts only a
  *subset* (qdrant + neo4j + embedding-model, plus llm only when a GPU is
  present) — so "per-service" exists in part, but the user cannot *choose*
  "Qdrant local, LLM external" in one run.
- `digital_twins/health.py` already emits **per-service** remediation
  ("point KB_LLM__ENDPOINT at an external LLM; the rest of the stack is
  kept"), so the config layer already supports per-service mixing.

The gap is (a) installers auto-running the wizard, and (b) the wizard's
backend decision being all-or-nothing per run.

## Decisions

### D1. Installer default flips to install-only
- Remove the "run `digital-twins setup`" step from the default path in both
  installers. New step-4 (optional) runs only under `--with-setup`.
- `--no-setup` is kept as an **accepted alias** that is now a no-op (it was
  already "stop after pip install"). It prints one note
  ("'--no-setup' is now the default; pass --with-setup to run the wizard")
  and proceeds. Kept for one release so existing scripts/CI don't break.
- The "stale CLI forces reinstall" check in `install.sh` (the `*setup*`
  grep on the installed CLI's `--help`) stays — it still keys on the CLI
  having a `setup` subcommand.
- Exit-code contract: install-only runs no longer surface the wizard's
  exit codes (3/5/6) — those apply only under `--with-setup`.

### D2. Per-service backend choice, additive
- New `setup` flag: `--backends qdrant=local,llm=<url>,...` (comma-separated
  `service=value` pairs; `value` is `local` or a URL). Also accepted:
  `--local` (= all four local) and `--cloud` (= all four external, *prompt*
  mode — today's `--cloud` behavior, unchanged; env-var resolution stays
  `--cloud-env`'s job).
- **Flag precedence (deterministic, no prompt):** a flag *present on the
  command line* wins for the services it names and **suppresses the
  interactive second pass for those services**. `--local`/`--cloud`/`--cloud-env`
  name all four services, so they fully suppress the second pass (they
  deterministically set the resolved map). `--backends qdrant=local` names
  only `qdrant`: `qdrant` is resolved from the flag, the other three fall
  through to the second pass (or env, in `--cloud-env`). A service named in
  more than one source (flag + env) → the flag wins.
- Without any of these, setup keeps the current
  interactive detection (docker probe → confirm → local, else cloud prompt)
  **but now per-service**: when the user chooses the local stack, a second
  pass asks, per service in `{qdrant,neo4j,llm,embedding}`, "local or
  external?" (default local for qdrant/neo4j, and "external (no GPU)"
  default for llm/embedding on no-GPU hosts).
- Resolution is a pure function
  `resolve_backends(flags, env, gpu: bool, docker: bool) -> dict[service,
  {"mode": "local"|"external", "url": str}]`.
  - `local` URL = the bundled constants (`_QDRANT_EP`, `_NEO4J_EP`,
    `_LLM_EP`, `_EMBED_EP`).
    - `neo4j` local: the bolt URL `_NEO4J_EP`; the compose `NEO4J_USER/
      NEO4J_PASSWORD` prompt still runs (unchanged).
    - `llm` local: only when `gpu=True`; otherwise `llm` cannot be local on
      the bundled stack → an error/re-prompt to an external URL (this is
      today's "no GPU → llm skipped" path, now explicit).
  - `external` URL = user value (from `--backends`, from the per-service
    prompt, or from the `KB_*` env var when in `--cloud-env` mode).
- **Only the services chosen `local` are passed to `docker compose up`.**
  Today `run_local_stack` hardcodes `up_services = ["qdrant","neo4j",
  "embedding-model"] (+llm if gpu)`. It becomes `up_services = [svc for
  svc in resolved if resolved[svc]["mode"]=="local"]`.
- `kb.local.yml` is written from the resolved map: each service's entry is
  its local URL or its external URL. Services left external with no URL and
  no env var → the same "required endpoint empty" gate as today (exit 5
  in `--cloud-env` mode; prompt otherwise).
- `write_kb_local()`'s "leave an existing file alone" rule is **kept for a
  re-run on an already-configured host**, but a *first* run (no valid
  `kb.local.yml`) always writes the full resolved map (including any
  external URLs for services the user pointed elsewhere).
- **`--skip-services` is pinned as a whole-stack decision, not per-service:**
  "assume the backend is already up" → never probe Docker, never prompt,
  never write `kb.local.yml` (the re-run fast path). It is orthogonal to
  `--backends`: passing both is a contradiction (skip-services says
  "don't write", backends says "resolve and write") and setup exits with a
  clear error naming the conflict. It does *not* participate in per-service
  resolution.

### D3. `init` → deprecated alias of `setup`
- `cli.py::init` becomes a thin wrapper: print one deprecation line
  ("`digital-twins init` is deprecated; use `digital-twins setup`") then
  run the *subset* of `run_setup` that is `init`'s narrower promise —
  state DB + migrations + first admin + health report — **and migrates
  `init`'s merge-on-existing behavior into `setup`'s `kb.local.yml` write**:
  when a valid `kb.local.yml` already exists, re-run prompts fill only the
  *missing* fields (today's `init` `_merge(starter, existing)`, cli.py
  L500-503), rather than `write_kb_local()`'s "leave the file untouched".
  This keeps `tests/integration/test_cli_init.py`'s existing-value
  assertions green (init's re-run is "existing values kept, missing
  built-ins added, exit 0"). `--yes` is preserved. Kept for one release.
- Remediation-string sweep (user-facing only, all four files): every
  "run `digital-twins init`" / "re-run init/validate" string in
  `digital_twins/cli.py` (incl. the "no state db" branch at L290-295),
  `digital_twins/health.py` (L74, L124),
  `digital_twins/mcp/dispatch.py` (L261), and
  `digital_twins/scheduler/loop.py` (L210, L236) changes to "run
  `digital-twins setup`". The test assertion in `tests/unit/test_auth.py`
  L359 (`"init" in result.output.lower()`) updates to `"setup"`.

### D4. Docs
- README "Fast path" splits into **Step 1: Install** (pip / installer /
  manual) and **Step 2: Setup** (`digital-twins setup`, per-service table).
- `docs/configuration.md` gains a "Setup is separate and idempotent"
  section + a per-service backend table.

## Consequences / notes
- The two installer doc-contract test files assert wizard-by-default; they
  must be updated to assert install-only-by-default + `--with-setup`.
  That is a **spec-013-style test-contract delta** (the installers'
  doc contract lives in those tests) — flag as an explicit task.
- No new host paths / dependencies (NFR-13 / BR-11). `--backends` is a
  CLI arg only; nothing is written to disk beyond `kb.local.yml`.
- NFR-1 (dedup) unaffected — no change to point-ID or payload.
- `--run-ingest` (installer step 5) now implies `--with-setup`: the
  installers' `--run-ingest` flag sets `WITH_SETUP=1` internally, so a
  combined "install + configure + first ingest" one-liner still works;
  a bare `--run-ingest` without `--with-setup` would ingest into an
  unconfigured host and fail-fast, so the two flags are not independent.
