"""Endpoint health checks (T015, US1)."""

import sys
import types
import urllib.error
from types import SimpleNamespace

import pytest

from digital_twins import health

PINNED = "BAAI/bge-small-en-v1.5"


def _cfg(**sections):
    return sections


def _install_module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)


# --- qdrant fakes ----------------------------------------------------------

class _Unreachable:
    def __init__(self, url=None, api_key=None):
        raise ConnectionError("connection refused")


class _NoCollections:
    def __init__(self, url=None, api_key=None):
        pass

    def get_collections(self):
        return SimpleNamespace(collections=[])


def _dim_client(dim):
    class _DimClient:
        def __init__(self, url=None, api_key=None):
            pass

        def get_collections(self):
            return SimpleNamespace(
                collections=[SimpleNamespace(name=health.QDRANT_COLLECTION)])

        def get_collection(self, name):
            return SimpleNamespace(config=SimpleNamespace(
                params=SimpleNamespace(vectors=SimpleNamespace(size=dim))))
    return _DimClient


# --- neo4j fakes -----------------------------------------------------------

def _neo4j_driver_class(connect_exc=None, run_ok=False):
    class _AuthFailure(Exception):
        pass

    class _Driver:
        def __init__(self, url, auth=None):
            self.url = url
            self.auth = auth
            self.closed = False

        def verify_connectivity(self):
            if connect_exc is not None:
                raise connect_exc

        def session(self):
            class _S:
                def __enter__(self_):
                    return self_

                def __exit__(self_, *a):
                    return False

                def run(self_, query):
                    if not run_ok:
                        raise _AuthFailure("denied")
            return _S()

        def close(self):
            self.closed = True
    return _Driver


# --- qdrant tests -----------------------------------------------------------

def test_qdrant_unconfigured():
    r = health.check_qdrant(_cfg(qdrant={"url": None}))
    assert not r.ok
    assert "not configured" in r.detail
    assert "KB_QDRANT__URL" in r.remediation


def test_qdrant_unreachable(monkeypatch):
    _install_module(monkeypatch, "qdrant_client", QdrantClient=_Unreachable)
    r = health.check_qdrant(_cfg(qdrant={"url": "https://q:6333"}))
    assert not r.ok
    assert "unreachable" in r.detail
    assert "live Qdrant" in r.remediation


def test_qdrant_missing_collection_is_creatable(monkeypatch):
    _install_module(monkeypatch, "qdrant_client", QdrantClient=_NoCollections)
    r = health.check_qdrant(_cfg(qdrant={"url": "https://q:6333"}))
    assert r.ok
    assert "will be created" in r.detail


def test_qdrant_dimension_mismatch_fails(monkeypatch):
    _install_module(monkeypatch, "qdrant_client", QdrantClient=_dim_client(512))
    r = health.check_qdrant(
        _cfg(qdrant={"url": "https://q:6333"},
             embedding={"model": PINNED}))
    assert not r.ok
    assert "512" in r.detail and "384" in r.detail
    assert "recreate" in r.remediation or "re-embed" in r.remediation


def test_qdrant_healthy(monkeypatch):
    _install_module(monkeypatch, "qdrant_client", QdrantClient=_dim_client(384))
    r = health.check_qdrant(
        _cfg(qdrant={"url": "https://q:6333"},
             embedding={"model": PINNED}))
    assert r.ok
    assert "384-dim" in r.detail


# --- neo4j tests ------------------------------------------------------------

def test_neo4j_unconfigured():
    r = health.check_neo4j(_cfg(neo4j={"url": None}))
    assert not r.ok
    assert "KB_NEO4J__URL" in r.remediation


def test_neo4j_missing_credentials():
    r = health.check_neo4j(_cfg(neo4j={"url": "bolt://n:7687", "user": None, "password": None}))
    assert not r.ok
    assert "KB_NEO4J__PASSWORD" in r.remediation


def test_neo4j_auth_failure_gets_credential_hint(monkeypatch):
    class _AuthFailure(Exception):
        pass
    Driver = _neo4j_driver_class(connect_exc=_AuthFailure("bad creds"))
    _install_module(monkeypatch, "neo4j", GraphDatabase=type("G", (), {"driver": staticmethod(lambda url, auth=None: Driver(url, auth))}))
    r = health.check_neo4j(
        _cfg(neo4j={"url": "bolt://n:7687", "user": "neo4j", "password": "x"}))
    assert not r.ok
    assert "correct neo4j.user" in r.remediation


def test_neo4j_healthy(monkeypatch):
    Driver = _neo4j_driver_class(run_ok=True)
    _install_module(monkeypatch, "neo4j", GraphDatabase=type("G", (), {"driver": staticmethod(lambda url, auth=None: Driver(url, auth))}))
    r = health.check_neo4j(
        _cfg(neo4j={"url": "bolt://n:7687", "user": "neo4j", "password": "x"}))
    assert r.ok
    assert "auth ok" in r.detail


# --- llm tests ---------------------------------------------------------------

def test_llm_unconfigured():
    r = health.check_llm(_cfg(llm={"endpoint": None}))
    assert not r.ok
    assert "KB_LLM__ENDPOINT" in r.remediation


def test_llm_reachable(monkeypatch):
    class _Resp:
        status = 200
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(health.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    r = health.check_llm(_cfg(llm={"endpoint": "https://llm.example/v1", "api_key": "k"}))
    assert r.ok
    assert "HTTP 200" in r.detail


def test_llm_http_error_still_reachable(monkeypatch):
    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://llm.example/v1/models", 401, "unauthorized", {}, None)
    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    r = health.check_llm(_cfg(llm={"endpoint": "https://llm.example/v1"}))
    assert r.ok
    assert "HTTP 401" in r.detail


def test_llm_unreachable(monkeypatch):
    def _boom(req, timeout=None):
        raise urllib.error.URLError("dns failure")
    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    r = health.check_llm(_cfg(llm={"endpoint": "https://llm.example/v1"}))
    assert not r.ok
    assert "unreachable" in r.detail
    assert "KB_LLM__ENDPOINT" in r.remediation


def test_run_health_checks_returns_one_per_endpoint():
    results = health.run_health_checks(_cfg(qdrant={}, neo4j={}, llm={}))
    assert [r.endpoint for r in results] == ["qdrant", "neo4j", "llm"]
