"""008/US1 (T012): preflight gate — hard/soft dependency contract
(preflight-optional-deps): qdrant / llm / embedding are hard deps;
neo4j is soft — `unconfigured` logs INFO and skips (Qdrant-only mode),
while `auth-failed` / `unreachable` still hard-fail. A hard-dep
failure aborts the run before any write: no audit row, structured
log with service + remediation, no credential material.
"""
from __future__ import annotations

import logging

import pytest

import digital_twins.health as h
from digital_twins.config.schema import validate
from digital_twins.health import HealthResult, ServiceDependencyError, preflight
from digital_twins.ingest.pipeline import run_pipeline

pytestmark = pytest.mark.preflight_real


class BoomDB:
    """Any attribute access = a write/audit attempt that must not happen."""

    def __getattr__(self, name):
        raise AssertionError(f"db.{name} called before preflight passed")


def _ok(service):
    return lambda cfg: HealthResult(service, True, "ok", status="ok")


def make_cfg_bad_embedding():
    c = validate({})
    c["embedding"]["endpoint"] = "http://127.0.0.1:9/v1"  # closed port
    c["embedding"]["api_key"] = "sekret-x-never-leak"
    c["llm"]["api_key"] = "sekret-x-never-leak"
    return c


def _patch_all_but_embedding(monkeypatch):
    monkeypatch.setattr(h, "check_qdrant", _ok("qdrant"))
    monkeypatch.setattr(h, "check_neo4j", _ok("neo4j"))
    monkeypatch.setattr(h, "check_llm", _ok("llm"))


def test_preflight_raises_with_fields(monkeypatch):
    _patch_all_but_embedding(monkeypatch)
    with pytest.raises(ServiceDependencyError) as exc:
        preflight(make_cfg_bad_embedding())
    assert exc.value.service == "embedding"
    assert exc.value.status == "unreachable"
    assert "KB_EMBEDDING__ENDPOINT" in exc.value.remediation


def test_run_pipeline_gates_before_audit(caplog, monkeypatch):
    _patch_all_but_embedding(monkeypatch)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ServiceDependencyError):
            run_pipeline(make_cfg_bad_embedding(), db=BoomDB(), qdrant=None)
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "embedding" in msgs
    assert "KB_EMBEDDING__ENDPOINT" in msgs
    assert "sekret-x-never-leak" not in msgs


def test_preflight_passes_when_all_ok(monkeypatch):
    monkeypatch.setattr(h, "check_qdrant", _ok("qdrant"))
    monkeypatch.setattr(h, "check_neo4j", _ok("neo4j"))
    monkeypatch.setattr(h, "check_llm", _ok("llm"))
    monkeypatch.setattr(h, "check_embedding", _ok("embedding"))
    assert preflight(validate({})) == ["qdrant", "neo4j", "llm", "embedding"]
