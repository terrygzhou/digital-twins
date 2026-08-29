# T026 Report: Config Precedence Tests

## What I Implemented

Added 9 new tests to the existing `tests/unit/test_config_precedence.py` (which already had 17 tests from T007+T008). The new tests cover the brief's explicit requirements that the existing tests did not:

| Brief Item | Test(s) Added | Status |
|---|---|---|
| 1. Built-in default wins | (existing: `test_defaults_when_nothing_else`) | PASSED |
| 2. kb.yml overrides default | (existing: `test_yaml_beats_defaults`) | PASSED |
| 3. kb.local.yml overrides kb.yml | (existing: `test_local_yaml_beats_yaml`) | PASSED |
| 4. env overrides kb.local.yml | `test_env_param_overrides_kb_local_yaml` | PASSED |
| 5. env overrides kb.yml | `test_env_param_overrides_kb_yaml` | PASSED |
| 6. Deterministic | `test_deterministic_repeated_load`, `test_deterministic_cross_run` | PASSED |
| 7. Conflicting layers (edge) | `test_all_four_layers_conflict`, `test_all_four_layers_no_env`, `test_all_four_layers_no_env_no_local` | PASSED |
| 8. Debug layer-wins report | `test_debug_layer_wins_report_exists`, `test_debug_reports_winner_per_knob` | XFAIL (strict) |

### New test details

- **`test_env_param_overrides_kb_yaml`** — Uses the `env` dict parameter to `load()` (not `monkeypatch.setenv`), confirming env > kb.yml when only kb.yml is present.
- **`test_env_param_overrides_kb_local_yaml`** — Same, confirming env > kb.local.yml.
- **`test_deterministic_repeated_load`** — Two consecutive `load()` calls with identical layers produce identical config dicts.
- **`test_deterministic_cross_run`** — Four consecutive loads; the winner is always the same value.
- **`test_all_four_layers_conflict`** — All four layers set `chunking.max_chars` to different values (1000/2000/3000/default 800). Env wins with 3000.
- **`test_all_four_layers_no_env`** — Three layers (no env). kb.local.yml wins.
- **`test_all_four_layers_no_env_no_local`** — Two layers (no env, no local). kb.yml wins.
- **`test_debug_layer_wins_report_exists`** — `@pytest.mark.xfail(strict=True)`: asserts the loader module exposes a `load_debug` attribute. Fails because it doesn't yet.
- **`test_debug_reports_winner_per_knob`** — `@pytest.mark.xfail(strict=True)`: calls `load_debug()` and asserts it returns a `knob → layer_name` mapping with the correct winners. Fails because the function doesn't exist.

## What I Tested and Test Results

- **Full test suite:** 92 passed, 2 xfailed (the debug tests), 0 failed
- **Config precedence file alone:** 24 passed, 2 xfailed
- **No regressions** in any other test file

The 2 xfail tests are **expected red** per TDD: they define the interface for the debug layer-wins API that T028/T029 will implement. `strict=True` ensures that if T028/T029 implements the API and the tests start passing, the xfail markers must be removed (a test that unexpectedly passes is a failure under `strict=True`).

## Files Changed

- `tests/unit/test_config_precedence.py` — added 9 tests (105 lines)

## Self-Review Findings

1. **Coverage of brief items 1-3:** These were already covered by the 17 existing tests. I verified they pass and did not duplicate them.
2. **`env` parameter vs `monkeypatch.setenv`:** The brief says "The `env` parameter to `load()` should be a dict of `KB_*` env vars." The existing tests use `monkeypatch.setenv` (which populates `os.environ`). My new tests use the `env` dict parameter directly, which is a cleaner, more isolated approach and matches the brief's intent.
3. **Debug API interface:** I chose `load_debug()` as the expected function name (a separate function, not a `debug=True` parameter on `load()`). This keeps the main `load()` API clean and makes the debug output a distinct concern. The tests assert `hasattr(_loader, "load_debug")` so T028/T029 can implement either a standalone function or a method.
4. **`strict=True` on xfail:** Ensures the tests will FAIL the suite if they start passing (i.e., T028/T029 implements the API but forgets to remove the xfail markers). This is the correct TDD behavior.
5. **No modifications to loader or schema:** Per the brief, I only wrote tests. No changes to `digital_twins/config/loader.py` or `digital_twins/config/schema.py`.

## Issues or Concerns

- **None.** All requirements are met. The 2 xfail tests are correctly red and define the expected interface for T028/T029.
