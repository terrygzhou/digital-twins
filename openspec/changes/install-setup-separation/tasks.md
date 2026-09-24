# Tasks: install-setup-separation

## 1. Per-service backend resolution (digital_twins/setup.py)
- [ ] 1.1 Add `resolve_backends(flags, env, gpu, docker) -> dict` returning
      per-service `{mode: local|external, url: str}` for
      `qdrant/neo4j/llm/embedding`; `local` maps to the bundled endpoint
      constants, `external` to the user/env value. Pure function — Test-First:
      unit tests in `tests/unit/test_setup.py` (all-local, all-external,
      mixed, llm-local-on-no-GPU error, env-var precedence in `--cloud-env`).
- [x] 1.2 Add `--backends KEY=VAL,...`, `--local` flags to the `setup`
      subcommand (`cli.py`), threading them through `run_setup()`; keep
      `--cloud` (prompt mode) / `--cloud-env` (env mode) / `--skip-services`
      behavior unchanged. Flag precedence is deterministic: a flag present
      on the command line wins for the services it names and suppresses the
      interactive second pass for those services; `--local`/`--cloud`/
      `--cloud-env` name all four and suppress the second pass entirely.
      `--skip-services` + `--backends` is a contradiction → setup exits
      with a clear error naming the conflict.
- [x] 1.3 Change `run_local_stack` to build `up_services` from the resolved
      map (only services with `mode == "local"`) instead of the hardcoded
      list; write `kb.local.yml` from the resolved map so mixed local/external
      configs land in one file.

## 2. Interactive per-service prompts (digital_twins/setup.py)
- [x] 2.1 When the user accepts the bundled local stack, add a second pass
      that asks, per service, local-or-external (defaults: qdrant/neo4j →
      local; llm/embedding → "external" on no-GPU hosts, local on GPU
      hosts). An external answer prompts for the URL (or reads `KB_*` env).
      The second pass fires **only** on the default interactive detection
      path; `--local`/`--cloud`/`--cloud-env` (which name all four
      services) and any service named in `--backends` are resolved from the
      flag/env and do not re-prompt.
- [x] 2.2 Unit tests: prompt path for a mixed choice produces the right
      resolved map; no-GPU llm default is external; services named in
      `--backends` are not re-prompted.

## 3. Installers: install-only default (scripts/install.sh, install-local.sh)
- [x] 3.1 Make the wizard step opt-in behind a new `--with-setup`; default
      path stops after `pip install` and prints "run `digital-twins setup`".
- [x] 3.2 Keep `--no-setup` as an accepted no-op alias (prints a one-line
      note that it is now the default) for one release.
- [x] 3.3 Update exit-code docs: 3/5/6 apply only under `--with-setup`.
- [x] 3.4 **Installer doc-contract test delta (REQUIRED):** update
      `tests/unit/test_install_sh_script.py` + `test_install_script.py` from
      "wizard runs by default" to "install-only by default; `--with-setup`
      triggers the wizard; `--no-setup` is a no-op alias". Sync any
      `.superpowers/sdd/` ledger for these test files.

## 4. `init` deprecation (digital_twins/cli.py)
- [x] 4.1 Turn `init` into a deprecated alias of the *narrower* `setup`
      subset (state DB + migrations + first admin + health report — not the
      backend decision) with a one-line deprecation notice naming
      `digital-twins setup`. **Preserve `init`'s merge-on-existing behavior**
      (when a valid `kb.local.yml` exists, keep existing values and prompt
      only for missing fields — today's `init` contract that
      `tests/integration/test_cli_init.py` asserts); migrate that merge
      behavior into `setup`'s `kb.local.yml` write path so both commands
      agree. `--yes` is preserved.
- [x] 4.2 Reword **all** user-facing "run `digital-twins init`" /
      "re-run init/validate" remediation strings to "run
      `digital-twins setup`" — sweep all four files: `digital_twins/cli.py`
      (incl. the "no state db" branch at L290-295 and L1073/L1133/L1211/
      L1374/L1426/L1485/L1533), `digital_twins/health.py` (L74, L124),
      `digital_twins/mcp/dispatch.py` (L261),
      `digital_twins/scheduler/loop.py` (L210, L236). Update the test
      assertion in `tests/unit/test_auth.py` L359 (`"init" in
      result.output.lower()` → `"setup"`). Keep `tests/integration/
      test_cli_init.py` + `test_quickstart_scenarios.py` + `test_init.py`
      green (they exercise `init`'s merge-on-existing contract).

## 5. Docs
- [ ] 5.1 README "Fast path": split into Step 1 (Install) and Step 2 (Setup);
      add the per-service backend table + `--backends` / `--local` examples.
      **Explicitly rewrite** the current "It ends with the `digital-twins
      setup` wizard" sentence (README L35-37) to the install-only default
      + "run `digital-twins setup`" follow-up, and update the option list
      (drop `--no-setup` as the opt-out, add `--with-setup` as the opt-in).
- [ ] 5.2 `docs/configuration.md`: add a **new** "Setup is separate and
      idempotent" section (this file has no `setup`/`init` mention today —
      it is a new section, not an edit) + the per-service backend table.

## 6. Verification
- [ ] 6.1 Full `pytest`; the two installer doc-contract files + `tests/unit/
      test_setup.py` green; portability guard
      `tests/integration/test_portability.py` + `tests/unit/test_knob_docs.py`
      green (no host paths introduced).
- [ ] 6.2 Manual: fresh venv → `pip install digital-twins-kb[mcp]` → confirm
      no `kb.local.yml`/state dir → `digital-twins setup --backends
      qdrant=local,llm=https://example.com/v1` writes a mixed file.
