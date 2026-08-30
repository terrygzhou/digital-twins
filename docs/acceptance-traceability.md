# Acceptance Traceability — requirement.md → tests

> Durable answer to "how do we know the implementation is complete?"
> Every acceptance criterion (AC) from `requirement.md` §4 is mapped to
> executable test evidence. **Last verified: 2026-08-30 — 834/834 passed
> (67s).** Re-verify any time with:
>
> ```sh
> .venv/bin/python -m pytest -q
> ```

## Evidence hierarchy

1. **Test-first suites** — each `specs/*/tasks.md` checkbox was a failing
   test turned green (834 tests, `tests/unit/` + `tests/integration/`).
2. **Standing guards** — `tests/integration/test_portability.py` (NFR-13:
   no host path/username/pin in shipped files) and `tests/unit/test_knob_docs.py`
   (BR-11.2.4: no undocumented knob) must stay green.
3. **Ledger integrity** — all 8 `specs/*/tasks.md` ledgers 100% checked;
   cited test paths verified to exist (2026-08-30, commit `7945776`).
4. **Per-feature gate** — AGENTS.md "done" = tests green + no lint +
   diff-reviewer verdict, recorded in the SDD ledgers.

## AC traceability (requirement.md §4)

| # | Acceptance criterion | Status | Test evidence |
|---|---|---|---|
| 1 | `pip install` on clean host → binary; `init` first-run; `validate` health | ✅ code / 🔶 PyPI publish | `tests/integration/test_pypi_build.py` (wheel builds, valid zip, LICENSE, release runbook); `tests/unit/test_init.py` (starter+state, prompts for endpoints, keeps values, resumes if interrupted, unhealthy → exit 1) |
| 2 | Each source enable/disable w/o code change; missing prereq fails fast; add custom source | ✅ | `tests/unit/test_config_precedence.py::test_fresh_config_has_all_builtin_sources_disabled`; `tests/unit/test_imap_mail_source.py::test_{gmail,yahoo}_missing_credential_is_prerequisite`; `tests/unit/test_custom_source.py` (fail-fast naming, prerequisites, contract adherence) |
| 3 | Web app reachable from another machine; email+password sign-in; query/chat/ingest from UI | ✅ local / 👤 LAN | `tests/unit/test_web_app_auth.py` (first account admin, second reader, sign-in/out); `tests/integration/test_web_app.py` (SC-004 end-to-end); `tests/unit/test_web_app_{chat,ingest,kb}.py`. Bind address is configurable — LAN/remote reachability is a deployment check |
| 4 | Same content via schedule / `run --once` / MCP / web → **one** point | ✅ | `tests/integration/test_serve_once_dedup.py` (manual→schedule, schedule→manual, content-level dedupe, schedule-advance no re-embed) |
| 5 | alice/bob: independent schedules + caps; each sees only own history; shared KB | ✅ | `tests/integration/test_serve_once_multi_user.py`; `tests/integration/test_cli_config.py` (per-user override ACL: own ok, other's denied, admin sees all); `tests/integration/test_mcp_integration.py` (own-scope listings) |
| 6 | External MCP agent w/ token: `kb_search`, admin `kb_schedule_run`; audit `trigger: "mcp"` | ✅ / 👤 3rd-party client | `tests/integration/test_mcp_kb_tools.py`; `tests/integration/test_mcp_integration.py::test_sc003_audit_row_written`; `tests/unit/test_mcp_tools.py`. A real third-party client (Claude Desktop etc.) is wire-compatible but unexercised |
| 7 | Interrupted run resumes from last committed item | ✅ | `tests/unit/test_init.py::test_init_interrupted_resumes`; `tests/unit/test_serve_once_tick.py` (high-water marks, tick semantics); `tests/unit/test_run_once.py` |
| 8 | v1→v2 upgrade preserves `.kbstate/`, account DB, config | ✅ | `tests/integration/test_multi_user_upgrade.py` (all preexisting tables preserved, audit run counts preserved, upgrade idempotent); `tests/unit/test_upgrade.py` (migration idempotency, v3) |
| 9 | Dimension mismatch = hard error + remediation, never silent | ✅ | `tests/unit/test_dimension_mismatch.py` (hard error, actionable remediation, matching passes); `tests/unit/test_pipeline_dimension_guard.py` (unpinned model rejected, pinned = 384) |
| 10 | README + MIT LICENSE + CHANGELOG + documented config schema; license in pyproject | ✅ | `tests/integration/test_pypi_build.py::test_license_exists_at_repo_root`; `tests/unit/test_knob_docs.py` (every knob documented); `README.md`, `LICENSE`, `CHANGELOG.md`, `pyproject.toml` present at repo root |
| 11 | `scheduler` role: schedules + runs ok; endpoint reconfig / account mgmt → 403 | ✅ | Capability map `digital_twins/accounts.py` (`ROLE_CAPABILITIES["scheduler"]` lacks `write_global_config`, `manage_accounts`, `manage_all_user_config`); `tests/integration/test_cli_account.py` (scheduler denied list/set-role/delete); `tests/integration/test_cli_config.py::test_config_set_scheduler_another_user_denied`; `tests/integration/test_cli_run_schedule.py::test_schedule_add_scheduler_succeeds`; web 403 pattern `tests/integration/test_web_app.py` SC-004 |
| 12 | `serve` and `run --once` identical results; no duplicate points | ✅ | `tests/integration/test_serve_once_dedup.py`; `tests/unit/test_serve_once_tick.py`; `tests/unit/test_run_once.py` |
| 13 | `docker compose up` → working stack; endpoints overridable | ⚠️ file only | `tests/integration/test_docker_compose.py` (4 tests: 5 services present, all images pinned no `:latest`, host-neutral, named volumes). **The image has never been built** — first `docker build`/`up` is still ahead |
| 14 | Presets (`daily`/`hourly`/`weekly`/`monthly`/`every-N-hours`) without cron, in UI/CLI/MCP | ✅ | `tests/unit/test_presets.py` (DB CHECK constraint accepts valid, rejects invalid); `tests/unit/test_schedules.py` (invalid preset rejected on create/update); `tests/unit/test_schedule_cli.py`; `tests/unit/test_mcp_tools.py` |
| 15 | MCP non-admin: own `kb_schedule_run` + own `kb_run_history`; not another user's | ✅ | `tests/unit/test_mcp_acl.py` (owner/admin/other role matrix); `tests/unit/test_mcp_dispatch.py::test_sc002_non_owner_gets_schedule_not_found`; `tests/unit/test_mcp_tools.py` (`kb_schedule_list` own-scope vs admin-all) |

## NFR guards

| NFR | Status | Evidence |
|---|---|---|
| NFR-12 (clean host < 30 min) | 👤 human | `docs/release-runbook.py` exists (asserted by `test_pypi_build.py`); the timed clean-host trial itself is a release check |
| NFR-13 (environment neutrality) | ✅ standing guard | `tests/integration/test_portability.py` |
| NFR-14 (idempotent scheduling) | ✅ | `tests/integration/test_serve_once_dedup.py` |
| NFR-15 (upgrade safety) | ✅ | `tests/integration/test_multi_user_upgrade.py` |
| NFR-16 (per-user auditability) | ✅ | `tests/integration/test_cli_run_history.py` (own vs all) |
| NFR-17 (credential scoping) | ✅ | `tests/integration/test_cli_token.py`; `tests/unit/test_mcp_auth.py` (service token value check, denied paths) |

## Known thin spots (honest residuals)

- **No dedicated end-to-end test**: "scheduler writes global qdrant host → 403".
  Denial is proven per-surface (CLI account/config, capability map, MCP ACL);
  add one web/CLI e2e if this path changes.
- **Docker image never built** — compose invariants are file-level only (AC 13).
- **No real third-party MCP client** exercised (AC 6); local stdio/HTTP clients
  cover the wire protocol.
- **PyPI publication + clean-host trial** (AC 1, NFR-12) are release acts,
  not code — see `docs/release-runbook.py`.
- **LAN/remote browser reachability** (AC 3) — tested locally; remote use is a
  deployment check.

## How to keep this doc alive

- New AC in `requirement.md` → add a row here in the same commit as the spec.
- Test files renamed/moved → update paths here (this doc was repaired 2026-08-30
  when ledger path drift was found).
- Any test referenced here goes red → the AC it covers regresses; treat as
  release-blocking.
