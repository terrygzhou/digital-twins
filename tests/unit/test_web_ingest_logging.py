"""UAT BUG follow-up (ingest-error-logging): a failed web-triggered ingest
run (``POST /api/ingest/run``) must leave a traceback in the server log.

The handler's catch-all ``except Exception:`` previously swallowed the
exception silently (bare ``except`` + 500 JSON, no logging), so a failed
ingest run on the web UI was undiagnosable — no traceback in the server
log.  This test pins the observability contract:

- ``run_pipeline`` (as resolved by the handler, i.e. the
  ``digital_twins.ingest.pipeline`` module attribute) is monkeypatched to
  raise an unhandled ``RuntimeError``.
- The handler must still answer the request with a clean 500 JSON body
  (the generic error, no raw exception text leaked to the client).
- The server log (the ``digital_twins`` logger, ERROR level, via
  ``caplog``) must contain the exception's traceback: the record's
  ``exc_info`` carries the exception type/message, and the formatted
  record text contains the traceback header.

Harness pattern mirrors ``tests/unit/test_web_app_ingest.py``: v3 state DB
in ``tmp_path``, ``build_web_app`` on 127.0.0.1:0, requests driven with
``http.client``, monkeypatch of ``pipeline_mod.run_pipeline`` intercepting
the handler's call-time resolution.
"""

from __future__ import annotations

import logging
import traceback

from digital_twins.ingest import pipeline as pipeline_mod
from tests.unit.test_web_app_ingest import _http_post, web_app  # noqa: F401


def test_ingest_run_failure_logs_traceback(web_app, monkeypatch, caplog,
                                           tmp_path):
    """POST /api/ingest/run with a failing pipeline → 500 + logged traceback.

    RED (pre-fix): the handler's bare ``except Exception:`` swallows the
    exception without logging — ``caplog`` captures nothing, so the
    exc_info / traceback assertions fail.  GREEN (post-fix): the handler
    logs the failure (``logging.exception``-equivalent, i.e.
    ``error(..., exc_info=True)``) before sending the 500, and the 500
    body stays the generic error (no raw exception text).
    """
    _app, _db, host, port, session_token = web_app

    def _boom(*_args, **_kwargs):
        raise RuntimeError("fake pipeline failure: embedding endpoint down")

    monkeypatch.setattr(pipeline_mod, "run_pipeline", _boom)

    # Enable the fs source (fixture config has ``sources: {}`` → the
    # handler 400s with "no sources enabled" before reaching the pipeline).
    # No prerequisite (fs.dir) is configured, so the real hand-off raises
    # the moment ``_resolve_qdrant``-adjacent setup runs the pipeline —
    # the monkeypatched ``_boom`` intercepts the call.
    fs_dir = tmp_path / "fs-src"
    fs_dir.mkdir()
    _app.config["sources"]["fs"] = {
        "enabled": True,
        "extra": {"dir": str(fs_dir)},
    }

    # NOTE: the handler logs via the root logger (module-level
    # ``logging.error``), so the caplog level is set on the root logger.
    with caplog.at_level(logging.ERROR):
        code, parsed, raw = _http_post(
            host, port, "/api/ingest/run",
            {"sources": ["fs"]},
            headers={"Authorization": f"Bearer {session_token}"})

    # 1. The request still fails closed with a clean 500 JSON body.
    assert code == 500, (
        f"failed ingest run expected 500, got {code}: {raw[:300]!r}")
    assert parsed is not None, f"500 body must be JSON: {raw[:300]!r}"
    assert parsed.get("error") == (
        "ingest run failed: see server log for details"), (
        f"500 body must be the generic error (no exception leak), "
        f"got {parsed!r}")

    # 2. The server log captured the exception WITH its traceback.
    failed = [
        rec for rec in caplog.records
        if rec.levelno >= logging.ERROR and rec.exc_info is not None]
    assert failed, (
        "the failed ingest run must log at ERROR with exc_info=True "
        "(traceback); no such record was captured")
    exc_record = failed[0]
    assert "fake pipeline failure" in str(exc_record.exc_info[1]), (
        "the logged exception must be the raised RuntimeError, "
        f"got {exc_record.exc_info[1]!r}")
    # The traceback body is attached to the record's exc_info — the
    # spec's scenario pins a "Traceback (most recent call last):" line
    # in the server log (the standard handler formats it from exc_info).
    tb_text = "".join(traceback.format_exception(*exc_record.exc_info))
    assert "Traceback (most recent call last):" in tb_text, (
        "the logged record's exc_info must carry the traceback body, "
        f"got:\n{tb_text}")
