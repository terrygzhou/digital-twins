"""016 TDD red tests: kb_ingest_folder MCP tool body.

Seam: monkeypatch ``dispatch.run_pipeline`` (the body reads the module
attribute at call time). Happy path asserts the pipeline got the one-shot
source instance (injected under the chosen source name), trigger "mcp",
scheduled_by = caller email, and the chosen source name. Role gate,
path validation codes, and the fail-closed config guard are covered
per spec FR-2.

No host paths (NFR-13): folders are tmp_path fixtures; error messages
echo only the caller-supplied value.
"""
from pathlib import Path

import pytest

from digital_twins.mcp import dispatch
from digital_twins.mcp.dispatch import MCPContext


class _FakeQdrant:
    def get(self, key, default=None):
        return default

    def upsert(self, *a, **kw):
        return None

    def count(self, *a, **kw):
        return {"result": {"count": 0}}

    def get_collection(self, *a, **kw):
        class _C:
            pass
        return _C()

    def vector_size(self, *a, **kw):
        return 0

    def get_model(self, *a, **kw):
        return None


def _run_summary():
    from digital_twins.ingest.pipeline import RunSummary
    return RunSummary(run_id="run-016", counts={"folder": 1},
                      points=1, status="ok")


def _ctx(config, role="admin", email="alice@example.com",
         agent_kind="hermes", db=None):
    return MCPContext(db=db or _FakeDb(), caller_email=email,
                      caller_role=role, agent_kind=agent_kind,
                      config=config)


def _make_folder(tmp_path):
    folder = tmp_path / "folder016"
    folder.mkdir()
    (folder / "a.txt").write_text("alpha")
    return folder


class _FakeCursor:
    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeDb:
    """A no-op sqlite-like object for start_audit_run's up-front insert.

    The real pipeline's audit + highwater seams need an execute/commit
    surface; a no-op keeps the pipeline seam test able to exercise the
    real run_pipeline without a database.
    """

    def execute(self, *a, **kw):
        return _FakeCursor()

    def commit(self):
        pass

    def close(self):
        pass


def _make_folder(tmp_path):
    folder = tmp_path / "folder016"
    folder.mkdir()
    (folder / "a.txt").write_text("alpha")
    return folder


# ---------------------------------------------------------------------------
# Happy path: pipeline gets the one-shot source + mcp trigger + caller id
# ---------------------------------------------------------------------------

def test_happy_path_injects_one_shot_source(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path)
    captured = {}

    def fake_run_pipeline(cfg, db, qdrant, embedder=None, neo4j=None,
                          source_names=None, max_items=None, dry_run=False,
                          trigger="manual", scheduled_by="system",
                          owner=None, injected_sources=None):
        captured["source_names"] = source_names
        captured["injected"] = injected_sources
        captured["trigger"] = trigger
        captured["scheduled_by"] = scheduled_by
        captured["owner"] = owner
        return _run_summary()

    monkeypatch.setattr(dispatch, "run_pipeline", fake_run_pipeline)
    out = dispatch.dispatch(
        _ctx({"sources": {}}), "kb_ingest_folder",
        {"path": str(folder), "source_name": "notes"},
    )
    assert out["ok"] is True
    assert out["run_id"] == "run-016"
    assert out["points"] == 1
    assert captured["source_names"] == ["notes"]
    assert set(captured["injected"]) == {"notes"}
    src = captured["injected"]["notes"]
    assert type(src).__name__ == "OneShotFolderSource"
    assert src.path == folder
    assert captured["trigger"] == "mcp"
    assert captured["scheduled_by"] == "alice@example.com"
    assert captured["owner"] == "alice@example.com"


def test_defaults_source_folder_and_recursive(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path)
    captured = {}

    def fake_run_pipeline(cfg, db, qdrant, embedder=None, neo4j=None,
                          source_names=None, max_items=None, dry_run=False,
                          trigger="manual", scheduled_by="system",
                          owner=None, injected_sources=None):
        captured["injected"] = injected_sources
        return _run_summary()

    monkeypatch.setattr(dispatch, "run_pipeline", fake_run_pipeline)
    out = dispatch.dispatch(
        _ctx({"sources": {}}), "kb_ingest_folder",
        {"path": str(folder)},
    )
    assert out["ok"] is True
    assert set(captured["injected"]) == {"folder"}
    src = captured["injected"]["folder"]
    assert src.recursive is True


# ---------------------------------------------------------------------------
# Role gate: reader -> permission_denied, no pipeline call
# ---------------------------------------------------------------------------

def test_reader_role_denied(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path)
    called = []

    def fake_run_pipeline(*a, **kw):
        called.append(1)
        return _run_summary()

    monkeypatch.setattr(dispatch, "run_pipeline", fake_run_pipeline)
    out = dispatch.dispatch(
        _ctx({"sources": {}}, role="reader"), "kb_ingest_folder",
        {"path": str(folder)},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "permission_denied"
    assert called == []


# ---------------------------------------------------------------------------
# Path validation codes (echo only the caller-supplied value)
# ---------------------------------------------------------------------------

def test_missing_path(tmp_path):
    out = dispatch.dispatch(
        _ctx({"sources": {}}), "kb_ingest_folder",
        {"path": str(tmp_path / "no-such-dir")},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "path_not_found"
    assert "no-such-dir" in out["error"]["message"]


def test_file_not_directory(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    out = dispatch.dispatch(
        _ctx({"sources": {}}), "kb_ingest_folder",
        {"path": str(f)},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "not_a_directory"


# ---------------------------------------------------------------------------
# Fail-closed config guard
# ---------------------------------------------------------------------------

def test_config_none_fails_closed(tmp_path):
    folder = _make_folder(tmp_path)
    out = dispatch.dispatch(
        _ctx(None), "kb_ingest_folder",
        {"path": str(folder)},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "config_not_loaded"


# ---------------------------------------------------------------------------
# Pipeline exception -> run_failed
# ---------------------------------------------------------------------------

def test_run_failed(tmp_path, monkeypatch):
    folder = _make_folder(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(dispatch, "run_pipeline", boom)
    out = dispatch.dispatch(
        _ctx({"sources": {}}), "kb_ingest_folder",
        {"path": str(folder)},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "run_failed"
    assert "kaboom" in out["error"]["message"]


# ---------------------------------------------------------------------------
# Registry: the 11th tool is present with the exact schema
# ---------------------------------------------------------------------------

def test_registry_exposes_eleventh_tool():
    from digital_twins.mcp.registry import build_tool_registry
    tools = {t["name"]: t for t in build_tool_registry()}
    assert len(tools) == 12
    assert "kb_ingest_folder" in tools
    assert "kb_extraction_heal" in tools  # 019b, issue 6
    schema = tools["kb_ingest_folder"]["inputSchema"]
    assert schema["type"] == "object"
    props = schema["properties"]
    assert set(props) == {"path", "source_name", "recursive", "agent_kind"}
    assert schema["required"] == ["path"]
    assert props["path"]["type"] == "string"
    assert props["source_name"]["type"] == "string"
    assert props["source_name"]["default"] == "folder"
    assert props["recursive"]["type"] == "boolean"
    assert props["recursive"]["default"] is True
    assert props["agent_kind"]["type"] == "string"


# ---------------------------------------------------------------------------
# Pipeline seam: injected_sources reaches the source resolution loop
# (guards the run_pipeline parameter added for 016)
# ---------------------------------------------------------------------------

def test_run_pipeline_accepts_injected_sources(tmp_path):
    from digital_twins.ingest.pipeline import RunSummary, run_pipeline
    from digital_twins.ingest.onshot import OneShotFolderSource

    folder = _make_folder(tmp_path)

    class FakeClient:
        def upsert(self, *a, **kw):
            return None

        def count(self, *a, **kw):
            return {"result": {"count": 0}}

        def get_collection(self, *a, **kw):
            class _C:
                pass
            return _C()

        def vector_size(self, *a, **kw):
            return 0

        def get_model(self, *a, **kw):
            return None

    def fake_embed(texts):
        return [[0.0] * 4 for _ in texts]

    class FakeQFactory:
        def __call__(self):
            return FakeClient()

    cfg = {
        "sources": {},
        "chunking": {"max_chars": 800, "overlap": 0},
        "embedding": {"model": "none", "dim": 4},
    }

    def fake_preflight(_cfg):
        return None

    monkeypatch = None  # no fixture needed; use direct patching below
    import digital_twins.ingest.pipeline as pipe
    import digital_twins.health as health
    real_get = health.get if hasattr(health, "get") else None

    # Patch preflight + collection + dimension assert inside the pipeline
    import unittest.mock as mock
    with mock.patch.object(pipe, "preflight", side_effect=lambda c: None), \
         mock.patch.object(pipe, "qdrant_collection", return_value="personal_kb"), \
         mock.patch.object(pipe, "model_dimension", return_value=4), \
         mock.patch.object(pipe, "assert_dimension"), \
         mock.patch.object(pipe, "_ensure_collection"), \
         mock.patch.object(pipe, "upsert_highwater"), \
         mock.patch.object(pipe, "finish_audit_run"):
        summary = run_pipeline(
            cfg, _FakeDb(), FakeQFactory(), fake_embed,
            source_names=["notes"],
            trigger="mcp", scheduled_by="alice@example.com",
            owner="alice@example.com",
            injected_sources={"notes": OneShotFolderSource(folder,
                                                           source_name="notes")},
        )
        assert summary.status == "ok"
        assert summary.counts.get("notes") >= 1
