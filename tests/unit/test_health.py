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


def test_llm_http_401_is_auth_failed_not_ok(monkeypatch):
    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://llm.example/v1/models", 401, "unauthorized", {}, None)
    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    r = health.check_llm(_cfg(llm={"endpoint": "https://llm.example/v1"}))
    assert not r.ok
    assert r.status == "auth-failed"
    assert "HTTP 401" in r.detail
    assert "llm.api_key" in r.remediation


def test_llm_unreachable(monkeypatch):
    def _boom(req, timeout=None):
        raise urllib.error.URLError("dns failure")
    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    r = health.check_llm(_cfg(llm={"endpoint": "https://llm.example/v1"}))
    assert not r.ok
    assert "unreachable" in r.detail
    assert "KB_LLM__ENDPOINT" in r.remediation


class _JsonResp:
    def __init__(self, body):
        self._body = body
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _plain200():
    return type("R", (), {
        "status": 200,
        "__enter__": lambda s: s,
        "__exit__": lambda s, *a: False,
    })()


def _llm_urlopen(routes):
    seen = []

    def _fake(req, timeout=None):
        seen.append(req.full_url)
        key = req.full_url.rstrip("/").rsplit("/", 1)[-1]
        fn = routes.get(key)
        if fn is None:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)
        return fn()

    return _fake, seen


def test_llm_context_window_reported(monkeypatch):
    fake, _ = _llm_urlopen({
        "models": _plain200,
        "get_model_info": lambda: _JsonResp(b'{"context_len": 32768}'),
    })
    monkeypatch.setattr(health.urllib.request, "urlopen", fake)
    r = health.check_llm(_cfg(llm={"endpoint": "http://llm:30000"}))
    assert r.ok
    assert "context window 32768" in r.detail


def test_llm_context_window_probe_strips_v1(monkeypatch):
    fake, seen = _llm_urlopen({
        "models": _plain200,
        "get_model_info": lambda: _JsonResp(b'{"context_len": 8192}'),
    })
    monkeypatch.setattr(health.urllib.request, "urlopen", fake)
    r = health.check_llm(_cfg(llm={"endpoint": "http://llm:30000/v1"}))
    assert r.ok
    assert "context window 8192" in r.detail
    assert "http://llm:30000/get_model_info" in seen
    assert "http://llm:30000/v1/get_model_info" not in seen


def test_llm_context_window_unknown_for_non_sglang(monkeypatch):
    fake, _ = _llm_urlopen({"models": _plain200})
    monkeypatch.setattr(health.urllib.request, "urlopen", fake)
    r = health.check_llm(_cfg(llm={"endpoint": "http://llm:30000"}))
    assert r.ok
    assert "context window unknown" in r.detail


def test_llm_context_window_malformed_json(monkeypatch):
    fake, _ = _llm_urlopen({
        "models": _plain200,
        "get_model_info": lambda: _JsonResp(b"not json"),
    })
    monkeypatch.setattr(health.urllib.request, "urlopen", fake)
    r = health.check_llm(_cfg(llm={"endpoint": "http://llm:30000"}))
    assert r.ok
    assert "context window unknown" in r.detail


def test_run_health_checks_returns_one_per_endpoint():
    results = health.run_health_checks(_cfg(qdrant={}, neo4j={}, llm={}))
    assert [r.endpoint for r in results] == [
        "qdrant", "neo4j", "llm", "embedding"]


def test_llm_models_404_is_not_ok(monkeypatch):
    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://llm.example/v1/models", 404, "not found", {}, None)
    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    r = health.check_llm(_cfg(llm={"endpoint": "https://llm.example/v1"}))
    assert not r.ok
    assert r.status == "unreachable"
    assert "HTTP 404" in r.detail
    assert "KB_LLM__ENDPOINT" in r.remediation


def test_llm_models_500_is_not_ok(monkeypatch):
    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://llm.example/v1/models", 500, "server error", {}, None)
    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    r = health.check_llm(_cfg(llm={"endpoint": "https://llm.example/v1"}))
    assert not r.ok
    assert "HTTP 500" in r.detail


def test_llm_unreachable_scrubs_url_credentials(monkeypatch):
    def _boom(req, timeout=None):
        raise urllib.error.URLError(
            "http://h:9999/ingest?api_key=supersecret failed")
    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    r = health.check_llm(_cfg(llm={"endpoint": "http://h:9999"}))
    assert not r.ok
    assert "supersecret" not in r.detail
    assert "api_key=" not in r.detail


def test_qdrant_exception_detail_scrubs_url_credentials(monkeypatch):
    import qdrant_client

    class _BoomClient:
        def __init__(self, **kw):
            raise RuntimeError(
                "GET https://q:6333/collections?api_key=topsecret failed")

    monkeypatch.setattr(qdrant_client, "QdrantClient", _BoomClient)
    r = health.check_qdrant(
        _cfg(qdrant={"url": "https://q:6333", "api_key": "topsecret"}))
    assert not r.ok
    assert "topsecret" not in r.detail
    assert "api_key=" not in r.detail
