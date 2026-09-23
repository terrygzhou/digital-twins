# Tasks: install-setup-separation

## 1. Per-service backend resolution (digital_twins/setup.py)
- [ ] 1.1 Add `resolve_backends(flags, env, gpu, docker) -> dict` returning
      per-service `{mode: local|external, url: str}` for
      `qdrant/neo4j/llm/embedding`; `local` maps to the bundled endpoint
      constants, `external` to the user/env value. Pure function — Test-First:
      unit tests in `tests/unit/test_setup.py` (all-local, all-external,
      mixed, llm-local-on-no-GPU error, env-var precedence in `--cloud-env`).
- [ ] 1.2 Add `--backends KEY=VAL,...`, `--local` flags to the `setup`
      subcommand (`cli.py`), threading them through `run_setup()`; keep
      `--cloud` / `--cloud-env` / `--skip-services` behavior unchanged.
- [ ] 1.3 Change `run_local_stack` to build `up_services` from the resolved
      map (only services with `mode == "local"`) instead of the hardcoded
      list; write `kb.local.yml` from the resolved map so mixed local/external
      configs land in one file.

## 2. Interactive per-service prompts (digital_twins/setup.py)
- [ ] 2.1 When the user accepts the bundled local stack, add a second pass
      that asks, per service, local-or-external (defaults: qdrant/neo4j →
      local; llm/embedding → "external" on no-GPU hosts, local on GPU
      hosts). An external answer prompts for the URL (or reads `KB_*` env).
- [ ] 2.2 Unit tests: prompt path for a mixed choice produces the right
      resolved map; no-GPU llm default is external.

## 3. Installers: install-only default (scripts/install.sh, install-local.sh)
- [ ] 3.1 Make the wizard step opt-in behind a new `--with-setup`; default
      path stops after `pip install` and prints "run `digital-twins setup`".
- [ ] 3.2 Keep `--no-setup` as an accepted no-op alias (prints a one-line
      note that it is now the default) for one release.
- [ ] 3.3 Update exit-code docs: 3/5/6 apply only under `--with-setup`.
- [ ] 3.4 **Installer doc-contract test delta (REQUIRED):** update
      `tests/unit/test_install_sh_script.py` + `test_install_script.py` from
      "wizard runs by default" to "install-only by default; `--with-setup`
      triggers the wizard; `--no-setup` is a no-op alias". Sync any
      `.superpowers/sdd/` ledger for these test files.

## 4. `init` deprecation (digital_twins/cli.py)
- [ ] 4.1 Turn `init` into a deprecated alias of the `setup` flow with a
      one-line deprecation notice naming `digital-twins setup`.
- [ ] 4.2 Reword all "run `digital-twins init`" remediation strings in
      `health.py` / `cli.py` to "run `digital-twins setup`".

## 5. Docs
- [ ] 5.1 README "Fast path": split into Step 1 (Install) and Step 2 (Setup);
      add the per-service backend table + `--backends` / `--local` examples.
- [ ] 5.2 `docs/configuration.md`: "Setup is separate and idempotent" section
      + per-service backend table.

## 6. Verification
- [ ] 6.1 Full `pytest`; the two installer doc-contract files + `tests/unit/
      test_setup.py` green; portability guard
      `tests/integration/test_portability.py` + `tests/unit/test_knob_docs.py`
      green (no host paths introduced).
- [ ] 6.2 Manual: fresh venv → `pip install digital-twins-kb[mcp]` → confirm
      no `kb.local.yml`/state dir → `digital-twins setup --backends
      qdrant=local,llm=https://example.com/v1` writes a mixed file.
