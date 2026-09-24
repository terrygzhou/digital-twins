# Tasks: channels-config

## 1. Channel view helper (digital_twins/config/)
- [ ] 1.1 Add `channel_view(config_dir, env)` in `digital_twins/config/`
      (new module or `loader.py` extension): returns
      `{source_name: {enabled, max_items, timeout_s, credential_set,
      prerequisites: [...]}}` for all built-in + registered custom
      sources. `credential_set` reads `os.environ.get(cred_var, "")`
      (bool — never the value). `prerequisites` calls
      `Source.prerequisites()` on a constructed instance, catching
      exceptions → `["<error summary>"]`. Test-First: unit tests in
      `tests/unit/test_channel_view.py` (fresh-install all-disabled,
      credential-set boolean, prerequisites populated).
- [ ] 1.2 Add `channel_write(config_dir, updates: dict)` in
      `digital_twins/config/local_io.py`: merges
      `{"sources": {<name>: {...}}}` into `kb.local.yml` via the
      existing `merge_write` path, preserving all unrelated keys.
      Unit test: write preserves existing keys; unknown source name
      raises `SchemaError`.

## 2. CLI channels group (digital_twins/cli.py)
- [ ] 2.1 Add `channels` command group with `list` subcommand:
      calls `channel_view()`, prints table (name, enabled, max_items,
      timeout_s, credential_set, prerequisites). Test-First: CLI test
      in `tests/unit/test_cli.py::test_channels_list` (mock config
      dir, verify all built-in sources appear).
- [ ] 2.2 Add `channels enable <name> [--max-items N] [--timeout-s N]`
      and `channels disable <name>`: call `channel_write()`. Unknown
      source → exit 1 with "unknown source: <name>". Test-First:
      `tests/unit/test_cli.py::test_channels_enable_disable`.
- [ ] 2.3 Add `channels status <name>`: print effective config +
      prerequisites for one source. Unknown source → exit 1.
      Test-First: `tests/unit/test_cli.py::test_channels_status`.
- [ ] 2.4 Add `channels add <name> --entrypoint m.factory
      [--credential ENV] [--prefix PREFIX]`: validate entrypoint
      importable (call `build_custom` validation from
      `sources/custom.py`), then write `sources.<name>` to
      `kb.local.yml` with `enabled: false`. Test-First:
      `tests/unit/test_cli.py::test_channels_add`.

## 3. Web admin API (digital_twins/web/app.py)
- [ ] 3.1 Add `GET /api/config/channels` handler (admin-gated via
      `_require_admin`): returns `channel_view()` shape +
      `env_overrides` list. Test-First:
      `tests/unit/test_web_app_kb.py` or new
      `tests/unit/test_web_channels_api.py` (200 admin, 403
      non-admin, credential values absent from response).
- [ ] 3.2 Add `POST /api/config/channels` handler (admin-gated):
      body `{<name>: {enabled, max_items?, timeout_s?}}`; unknown
      source → 404; schema-invalid → 422; success → 200 with
      post-write masked view. Persist via `channel_write()`.
      Credential values never logged. Test-First:
      `tests/unit/test_web_channels_api.py` (404 unknown source,
      200 happy path, 422 invalid value, no credential leakage in
      logs).

## 4. Web admin UI Channels panel (static HTML/JS)
- [ ] 4.1 Add "Channels" tab/panel to the admin HTML page
      (`digital_twins/web/static/` or equivalent), alongside Services:
      fetch `GET /api/config/channels`, render one row per source with
      toggle + cap/timeout inputs + credential indicator +
      prerequisites warning. Save button POSTs to
      `/api/config/channels`, re-fetches on success. Test: manual
      QA check (no unit test for HTML rendering; verify via
      `digital-twins web` + browser).

## 5. Per-user channel overrides (digital_twins/user_config.py)
- [ ] 5.1 Add `channel_overrides` to the per-user config schema in
      `user_config.py`: `{source_name: {enabled, max_items?,
      timeout_s?}}`. `get_user_channel_overrides(user_id)` returns the
      mapping. Test-First: `tests/unit/test_user_config.py`
      (write/read round-trip).
- [ ] 5.2 Scheduler resolution: in the scheduler's per-run source
      resolution path, merge in order
      `user_override > env > kb.local.yml > kb.yml > defaults`.
      A user's `channel_overrides.<name>.enabled: false` suppresses
      that source for that user's runs only. Test-First:
      `tests/unit/test_scheduler.py` or new test — per-user disable
      does not affect other users.

## 6. Docs (docs/configuration.md + .env.example)
- [ ] 6.1 Document the `channels` CLI group in `docs/configuration.md`
      (list, enable, disable, status, add — with examples).
- [ ] 6.2 Document `/api/config/channels` GET/POST in
      `docs/configuration.md` (request/response shape, admin-gated).
- [ ] 6.3 Document `channel_overrides` in `docs/configuration.md`
      (per-user section).
- [ ] 6.4 Sync `.env.example` / `config.example.yml` if any new env
      vars are introduced (the per-source `KB_SOURCES__<NAME>__*`
      vars already exist — confirm no additions needed).

## 7. SDD ledger + constitution gate
- [ ] 7.1 Create `.superpowers/sdd/015-channels-config/progress.md`.
- [ ] 7.2 Run `openspec validate channels-config` — must pass.
- [ ] 7.3 Confirm `tests/integration/test_portability.py` (T006) and
      `tests/unit/test_knob_docs.py` (T027) remain green after all
      changes.

## Verification
- Run: `python3.12 -m pytest tests/unit/test_channel_view.py
  tests/unit/test_cli.py tests/unit/test_web_channels_api.py
  tests/unit/test_user_config.py -q`
- Plus standing guards: `tests/integration/test_portability.py`
  `tests/unit/test_knob_docs.py`
