"""007 T014 RED: kb_ingest unit tests (007-R3, SC-004).

kb_ingest triggers a pipeline run for the caller (or all enabled
sources when ``source`` is omitted / ``"all"``).  The body mirrors
006's ``_handle_ingest_run`` order:

  fail-closed → capability gate → source validation → run_pipeline
  hand-off (positional ``(merged_cfg, db, qdrant_factory, embedder)``,
  kwarg ``source_names`` / ``trigger="mcp"`` /
  ``scheduled_by=caller`` / ``owner=caller`` — NO ``agent_kind``
  kwarg; ``agent_kind`` is stamped post-call via
  ``_stamp_agent_kind(ctx.db, summary.run_id, ctx.agent_kind)``
  mirroring ``_kb_schedule_run_body`` step 5) → success result or
  error mapping.

The fake ``run_pipeline`` is monkeypatched on the ``dispatch``
module attribute (006's ``_pipeline_mod.run_pipeline`` pattern) so
the hand-off is captured.

Tests:
  (a) capability gate FIRST — reader → permission_denied naming
      trigger_run, zero run_pipeline calls, zero audit_rows.
  (b) scheduler/admin + enabled source → run_pipeline called once
      with the exact positional + kwarg shape, and the audit row's
      ``per_source_counts`` has ``agent_kind`` stamped on it.
  (c) source validation (exact 006 messages, all ``bad_request``,
      zero pipeline calls): unknown → "unknown source '<name>'",
      disabled → "source '<name>' is not enabled", source
      omitted / "all" with none enabled → "no sources enabled".
  (d) PrerequisiteError → prerequisite_missing
      "source '<name>': missing prerequisite(s): ..." with exactly
      one ``failed`` audit row (the pipeline's own — the body writes
      no second row).
  (e) UnknownSourceError from the pipeline → "unknown source '<name>'"
      (bad_request).
  (f) success → {"ok": True, "run_id", "status", "counts", "points"}.
  (g) config=None → config_not_loaded.
  (h) 007-t024 D-007-3: merged_cfg keeps sibling schema defaults under a
  subtree the caller partially overrides (nested merge, not a flat
  dotted-key overlay).
"""
from __future__ import annotations

import json
import pytest

import digital_twins.mcp.dispatch as dispatch_mod
from digital_twins.mcp.dispatch import dispatch
from digital_twins.mcp.registry import MCPContext
from digital_twins.ingest.pipeline import PrerequisiteError
from digital_twins.sources import UnknownSourceError
from digital_twins.config.schema import DEFAULTS as _SCHEMA_DEFAULTS


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    from digital_twins.state.db import connect
    conn = connect(str(tmp_path / "kb.sqlite3"))
    try:
        yield conn
    finally:
        conn.close()


def _ctx(db, config, email="alice@example.com", role="reader",
         agent_kind="dsh"):
    return MCPContext(
        db=db,
        caller_email=email,
        caller_role=role,
        agent_kind=agent_kind,
        config=config,
    )


# A config where "hermes" is enabled and "pi" is not.
_CFG_ENABLED = {
    "qdrant": {"url": "http://127.0.0.1:6333"},
    "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
    "sources": {
        "hermes": {"enabled": True},
        "pi": {"enabled": False},
    },
}


class _RunRecorder:
    """Spy on the module-attribute seam for run_pipeline.

    Records the call signature and returns a canned RunSummary (or
    raises a canned exception) so the test can assert on the hand-off
    shape without loading a real pipeline.
    """

    def __init__(self, summary=None, exc=None):
        self.summary = summary
        self.exc = exc
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.exc is not None:
            raise self.exc
        return self.summary


def _mk_summary(run_id="run-1", status="ok",
                counts=None, points=3):
    from digital_twins.ingest.pipeline import RunSummary
    return RunSummary(
        run_id=run_id,
        counts=counts if counts is not None else {"hermes": 3},
        points=points,
        status=status,
    )


def _audit_rows(db):
    rows = db.execute(
        "SELECT run_id, status, trigger, scheduled_by, "
        "per_source_counts FROM audit_runs"
    ).fetchall()
    return [dict(zip(
        ("run_id", "status", "trigger", "scheduled_by",
         "per_source_counts"), r)) for r in rows]


# ---------------------------------------------------------------------------
# (a) capability gate FIRST — reader → permission_denied, no run
# ---------------------------------------------------------------------------

def test_kb_ingest_reader_denied(db, monkeypatch):
    """007-R3a: reader role → permission_denied naming trigger_run;
    run_pipeline records ZERO calls; no audit row is written."""
    rec = _RunRecorder(summary=_mk_summary())
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)

    result = dispatch(_ctx(db, config=dict(_CFG_ENABLED), role="reader"),
                      "kb_ingest", {"source": "hermes"})
    assert result["ok"] is False
    assert result["error"]["code"] == "permission_denied"
    # The message must name the capability that was denied.
    assert "trigger_run" in json.dumps(result["error"]), (
        f"permission_denied must name trigger_run: {result!r}")
    # Zero pipeline calls.
    assert rec.calls == [], (
        f"run_pipeline was called on a refused call: {rec.calls}")
    # No audit row.
    assert _audit_rows(db) == [], (
        f"audit row written on a refused call: {_audit_rows(db)}")


# ---------------------------------------------------------------------------
# (b) scheduler/admin + enabled source → run_pipeline called once
#     with the exact positional + kwarg shape, agent_kind stamped
# ---------------------------------------------------------------------------

def test_kb_ingest_scheduler_success_shape(db, monkeypatch):
    """007-R3b: scheduler role + enabled source → run_pipeline called
    once with positional ``(merged_cfg, ctx.db, <qdrant factory>,
    <embedder>)`` and kwargs ``source_names=["hermes"]``,
    ``trigger="mcp"``, ``scheduled_by=caller``, ``owner=caller`` —
    NO ``agent_kind`` kwarg (run_pipeline's signature has none,
    ingest/pipeline.py:105).  agent_kind is stamped on the audit row
    via the post-call _stamp_agent_kind pattern.

    The fake run_pipeline does NOT write its own audit row (the real
    one does via start_audit_run); we pre-insert the row the way
    start_audit_run would, so _stamp_agent_kind has a row to merge
    into.
    """
    db.execute(
        "INSERT INTO audit_runs (run_id, started_at, completed_at, status, "
        "trigger, scheduled_by, per_source_counts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("run-x", "2025-01-01T00:00:00Z", "2025-01-01T00:00:01Z",
         "ok", "mcp", "admin@example.com", "null"),
    )
    db.commit()

    rec = _RunRecorder(summary=_mk_summary(run_id="run-x"))
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)

    caller = "admin@example.com"
    result = dispatch(
        _ctx(db, config=dict(_CFG_ENABLED), email=caller, role="scheduler"),
        "kb_ingest", {"source": "hermes"})

    assert result["ok"] is True, f"unexpected error: {result!r}"
    assert result["run_id"] == "run-x"
    assert result["status"] == "ok"
    assert result["counts"] == {"hermes": 3}
    assert result["points"] == 3

    assert len(rec.calls) == 1, f"expected exactly 1 call: {rec.calls}"
    call = rec.calls[0]
    args = call["args"]
    kwargs = call["kwargs"]

    # Positional: (merged_cfg, db, qdrant_factory, embedder)
    assert len(args) >= 2, f"expected ≥2 positional args: {args}"
    merged_cfg, db_arg = args[0], args[1]
    assert db_arg is db, "second positional arg must be ctx.db"
    assert isinstance(merged_cfg, dict), (
        f"first positional arg must be a dict: {merged_cfg!r}")
    # merged_cfg must carry the caller's enabled source.
    assert merged_cfg["sources"]["hermes"]["enabled"] is True

    # The caller's qdrant.url must survive the merge (006 _merge_defaults
    # equivalent: caller config wins over schema defaults).
    assert merged_cfg["qdrant"]["url"] == "http://127.0.0.1:6333"

    # No agent_kind kwarg.
    assert "agent_kind" not in kwargs, (
        f"run_pipeline must NOT receive agent_kind: {kwargs}")
    assert kwargs.get("source_names") == ["hermes"]
    assert kwargs.get("trigger") == "mcp"
    assert kwargs.get("scheduled_by") == caller
    assert kwargs.get("owner") == caller

    # The audit row is stamped with agent_kind (004 D6 pattern).
    rows = _audit_rows(db)
    assert len(rows) == 1, f"expected exactly 1 audit row: {rows}"
    counts = json.loads(rows[0]["per_source_counts"] or "{}")
    assert counts.get("agent_kind") == "dsh", (
        f"agent_kind not stamped on audit row: {counts}")


# ---------------------------------------------------------------------------
# (c) source validation (exact 006 messages, all bad_request, zero calls)
# ---------------------------------------------------------------------------

def test_kb_ingest_unknown_source(db, monkeypatch):
    """007-R3c: unknown source → bad_request "unknown source '<name>'";
    zero pipeline calls."""
    rec = _RunRecorder(summary=_mk_summary())
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)

    result = dispatch(
        _ctx(db, config=dict(_CFG_ENABLED), role="scheduler"),
        "kb_ingest", {"source": "does-not-exist"})
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_request"
    assert result["error"]["message"] == "unknown source 'does-not-exist'"
    assert rec.calls == []


def test_kb_ingest_disabled_source(db, monkeypatch):
    """007-R3c: disabled source → bad_request
    "source '<name>' is not enabled"; zero pipeline calls."""
    rec = _RunRecorder(summary=_mk_summary())
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)

    result = dispatch(
        _ctx(db, config=dict(_CFG_ENABLED), role="scheduler"),
        "kb_ingest", {"source": "pi"})
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_request"
    assert result["error"]["message"] == "source 'pi' is not enabled"
    assert rec.calls == []


def test_kb_ingest_no_sources_enabled(db, monkeypatch):
    """007-R3c: source omitted / "all" with none enabled → bad_request
    "no sources enabled"; zero pipeline calls."""
    cfg = {
        "qdrant": {"url": "http://127.0.0.1:6333"},
        "sources": {"hermes": {"enabled": False},
                    "pi": {"enabled": False}},
    }
    rec = _RunRecorder(summary=_mk_summary())
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)

    for args in ({}, {"source": "all"}):
        result = dispatch(
            _ctx(db, config=dict(cfg), role="scheduler"),
            "kb_ingest", args)
        assert result["ok"] is False, f"{args} → {result!r}"
        assert result["error"]["code"] == "bad_request"
        assert result["error"]["message"] == "no sources enabled"
        assert rec.calls == []


def test_kb_ingest_source_all_runs_all_enabled(db, monkeypatch):
    """007-R3c: source omitted / "all" with some enabled → run_pipeline
    called with source_names = [all enabled names]."""
    cfg = {
        "qdrant": {"url": "http://127.0.0.1:6333"},
        "sources": {
            "hermes": {"enabled": True},
            "pi": {"enabled": False},
            "dsh": {"enabled": True},
        },
    }
    rec = _RunRecorder(summary=_mk_summary())
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)

    result = dispatch(
        _ctx(db, config=dict(cfg), role="scheduler"),
        "kb_ingest", {"source": "all"})
    assert result["ok"] is True, f"unexpected: {result!r}"
    assert len(rec.calls) == 1
    assert sorted(rec.calls[0]["kwargs"]["source_names"]) == ["dsh", "hermes"]


# ---------------------------------------------------------------------------
# (d) PrerequisiteError → prerequisite_missing; one failed audit row
# ---------------------------------------------------------------------------

def test_kb_ingest_prerequisite_missing(db, monkeypatch):
    """007-R3d: PrerequisiteError → prerequisite_missing with the
    "source '<name>': missing prerequisite(s): ..." message; exactly
    one ``failed`` audit row (the pipeline's own — the body writes no
    second row)."""
    # Pre-insert the audit row the way the pipeline's
    # start_audit_run would (so _stamp_agent_kind has a row to find).
    db.execute(
        "INSERT INTO audit_runs (run_id, started_at, completed_at, status, "
        "trigger, scheduled_by, per_source_counts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("run-prereq", "2025-01-01T00:00:00Z", "2025-01-01T00:00:01Z",
         "failed", "mcp", "admin@example.com", "null"),
    )
    db.commit()

    exc = PrerequisiteError("hermes", ["hermes_api_key"])
    rec = _RunRecorder(summary=_mk_summary(run_id="run-prereq"), exc=exc)
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)

    result = dispatch(
        _ctx(db, config=dict(_CFG_ENABLED), role="scheduler"),
        "kb_ingest", {"source": "hermes"})
    assert result["ok"] is False
    assert result["error"]["code"] == "prerequisite_missing"
    assert result["error"]["message"] == (
        "source 'hermes': missing prerequisite(s): hermes_api_key")
    # Exactly one audit row (the pre-inserted one).
    rows = _audit_rows(db)
    assert len(rows) == 1, f"expected exactly 1 audit row: {rows}"
    assert rows[0]["run_id"] == "run-prereq"
    assert rows[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# (e) UnknownSourceError from the pipeline → unknown source message
# ---------------------------------------------------------------------------

def test_kb_ingest_unknown_source_from_pipeline(db, monkeypatch):
    """007-R3e: UnknownSourceError raised by run_pipeline →
    bad_request "unknown source '<name>'" (the source name comes from
    exc.args[0], the 006 web pattern)."""
    exc = UnknownSourceError("ghost-source")
    rec = _RunRecorder(summary=_mk_summary(), exc=exc)
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)

    result = dispatch(
        _ctx(db, config=dict(_CFG_ENABLED), role="scheduler"),
        "kb_ingest", {"source": "ghost-source"})
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_request"
    assert result["error"]["message"] == "unknown source 'ghost-source'"


# ---------------------------------------------------------------------------
# (f) success → {"ok": True, "run_id", "status", "counts", "points"}
# ---------------------------------------------------------------------------

def test_kb_ingest_success_shape(db, monkeypatch):
    """007-R3f: success → {"ok": True, "run_id", "status", "counts",
    "points"} — the 006 web result shape."""
    rec = _RunRecorder(summary=_mk_summary(
        run_id="run-ok", status="ok",
        counts={"hermes": 5, "dsh": 2}, points=7))
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)

    result = dispatch(
        _ctx(db, config=dict(_CFG_ENABLED), role="scheduler"),
        "kb_ingest", {"source": "hermes"})
    assert result == {
        "ok": True,
        "run_id": "run-ok",
        "status": "ok",
        "counts": {"hermes": 5, "dsh": 2},
        "points": 7,
    }, f"success shape drifted: {result!r}"


# ---------------------------------------------------------------------------
# (g) config=None → config_not_loaded
# ---------------------------------------------------------------------------

def test_kb_ingest_config_none_fails_closed(db, monkeypatch):
    """007-R3g: config=None → config_not_loaded before any other
    work (no capability check, no source validation, no pipeline
    call)."""
    rec = _RunRecorder(summary=_mk_summary())
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)

    result = dispatch(
        _ctx(db, config=None, role="scheduler"),
        "kb_ingest", {"source": "hermes"})
    assert result["ok"] is False
    assert result["error"]["code"] == "config_not_loaded"
    assert rec.calls == []


# ---------------------------------------------------------------------------
# (h) 007-t024 D-007-3: nested default merge — sibling schema defaults
#     survive a partial subtree override
# ---------------------------------------------------------------------------

def test_kb_ingest_merged_cfg_keeps_sibling_schema_defaults(db, monkeypatch):
    """007-t024 D-007-3: ``ctx.config`` that sets ONE value under an
    existing schema-default subtree (``chunking.max_chars``) must reach
    ``run_pipeline`` with the sibling default (``chunking.overlap``) still
    at its schema value.

    The pre-fix body overlaid the flat dotted-key ``DEFAULTS`` onto a
    copy of ``ctx.config`` — a nested caller key like ``chunking``
    blocked the dotted key ``chunking.max_chars``, so the whole
    ``chunking`` subtree in the merged config came ONLY from the caller
    and every sibling schema default (``chunking.overlap`` = 100) was
    silently dropped.  006's ``web/app.py::_merge_defaults`` instead
    builds the nested dict from the flat DEFAULTS first and then
    deep-merges the caller's config on top (caller wins per key, sibling
    defaults preserved).  This test asserts both sides of that shape via
    the fake run_pipeline seam (first positional arg is the merged
    config, the 007 contract).
    """
    # Sibling defaults asserted at their schema values (imported from the
    # schema, not guessed): config/schema.py DEFAULTS has
    #   "chunking.max_chars": 800, "chunking.overlap": 100
    assert _SCHEMA_DEFAULTS["chunking.max_chars"] == 800
    assert _SCHEMA_DEFAULTS["chunking.overlap"] == 100

    cfg = {
        "qdrant": {"url": "http://127.0.0.1:6333"},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 999},  # partial override: no overlap
        "sources": {"hermes": {"enabled": True}},
    }
    rec = _RunRecorder(summary=_mk_summary())
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)

    result = dispatch(
        _ctx(db, config=dict(cfg), role="scheduler"),
        "kb_ingest", {"source": "hermes"})
    assert result["ok"] is True, f"unexpected error: {result!r}"
    assert len(rec.calls) == 1, f"expected exactly 1 pipeline call: {rec.calls}"

    merged_cfg = rec.calls[0]["args"][0]
    # (a) the caller's override wins
    assert merged_cfg["chunking"]["max_chars"] == 999
    # (b) the sibling schema default survived the merge, at its schema value
    assert merged_cfg["chunking"]["overlap"] == 100, (
        f"flat merge dropped the sibling chunking.overlap default "
        f"(schema DEFAULTS['chunking.overlap'] == 100): "
        f"chunking subtree = {merged_cfg.get('chunking')!r}")
