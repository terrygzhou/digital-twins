"""008/US1 (T011, RED-first): check_embedding (FR-001).

Endpoint set → probe `{endpoint}/models` (OpenAI-compatible base URL, same
convention as llm.endpoint — the compose value already carries /v1).
Endpoint unset → in-process check via model_dimension.
Dimension-vs-collection mismatch stays qdrant's job, not embedding's.
"""
from __future__ import annotations

import urllib.error

from digital_twins.health import check_embedding
from digital_twins.config.schema import validate


def make_cfg(endpoint="", api_key="ke"):
    c = validate({})
    c["embedding"]["endpoint"] = endpoint
    c["embedding"]["api_key"] = api_key
    return c


class _Resp:
    status = 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_endpoint_set_reachable_is_ok(monkeypatch):
    calls = []

    def fake(url, timeout=None):
        calls.append(getattr(url, "full_url", url))
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    r = check_embedding(make_cfg(endpoint="http://embed:8080/v1"))
    assert r.ok is True
    assert r.status == "ok"
    assert r.endpoint == "embedding"
    assert calls == ["http://embed:8080/v1/models"]
    assert "8080" in r.detail


def test_endpoint_set_401_is_auth_failed(monkeypatch):
    def boom(url, timeout=None):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = check_embedding(make_cfg(endpoint="http://embed:8080/v1"))
    assert r.ok is False
    assert r.status == "auth-failed"
    assert "embedding.api_key" in r.remediation


def test_endpoint_set_network_error_is_unreachable(monkeypatch):
    def boom(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = check_embedding(make_cfg(endpoint="http://embed:8080/v1"))
    assert r.ok is False
    assert r.status == "unreachable"
    assert "embedding.endpoint" in r.remediation


def test_endpoint_unset_in_process_ok():
    r = check_embedding(make_cfg(endpoint=""))
    assert r.ok is True
    assert r.status == "ok"
    assert "384" in r.detail  # pinned bge-small dim detail


def test_endpoint_unset_ok_even_when_qdrant_missing():
    # dim-vs-collection mismatch stays the qdrant check's job
    c = make_cfg(endpoint="")
    c["qdrant"]["url"] = ""
    r = check_embedding(c)
    assert r.ok is True


def test_endpoint_404_is_not_ok(monkeypatch):
    def boom(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "no such route", None, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = check_embedding(make_cfg(endpoint="http://embed:8080/v1"))
    assert r.ok is False
    assert r.status == "unreachable"
    assert "HTTP 404" in r.detail
    assert "KB_EMBEDDING__ENDPOINT" in r.remediation
