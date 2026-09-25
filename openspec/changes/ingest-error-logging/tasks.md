# Tasks: ingest-error-logging

## 1. Web app handler
- [ ] 1.1 In `digital_twins/web/app.py::_handle_ingest_run`, replace the bare
      `except Exception:` block (L973-976) with a version that calls
      `logging.exception("web ingest run failed")` (or an equivalent
      `logging.error(..., exc_info=True)`) before sending the 500.
      Test-First: a unit test in `tests/unit/test_web_app_kb.py` (or a new
      `tests/unit/test_web_ingest_logging.py`) that monkeypatches
      `pipeline.run_pipeline` to raise and asserts the server log captured
      a traceback.

## 2. Standing guards
- [ ] 2.1 Confirm `tests/integration/test_portability.py` (T006) and
      `tests/unit/test_knob_docs.py` (T027) remain green.
- [ ] 2.2 Run `PYTHONPATH=. python3.12 -m pytest
      tests/unit/test_web_app_kb.py -q` and verify the new logging test
      passes.

## Verification
- `PYTHONPATH=. python3.12 -m pytest tests/unit/test_web_app_kb.py -q`
