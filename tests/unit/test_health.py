"""Endpoint health checks (T015, US1)."""

import sys
import types
import urllib.error
from types import SimpleNamespace

import pytest

from digital_twins import health
from digital_twins.health import HealthResult, ServiceDependencyError

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

                    class _R:
                        def single(self_):
                            return {"n": 0}
                        def data(self_):
                            return []
                    return _R()
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


def test_neo4j_healthy_probes_sourceitem(monkeypatch):
    """S4 shape: the health probe counts :SourceItem nodes (the label the
    pipeline writes after s4-graph-alignment), not the legacy :KbItem."""
    seen = []
    Driver = _neo4j_driver_class(run_ok=True)

    class _CapDriver:
        """Wraps the fake driver; records every Cypher the probe runs."""

        def __init__(self, url, auth=None):
            self._d = Driver(url, auth)
            self.url = url
            self.auth = auth
            self.closed = False

        def verify_connectivity(self):
            self._d.verify_connectivity()

        def session(self):
            real = self._d.session()

            class _Cap:
                def __enter__(self_):
                    real.__enter__()
                    return self_

                def __exit__(self_, *a):
                    return real.__exit__(*a)

                def run(self_, query, **params):
                    seen.append(query)
                    return real.run(query, **params)
            return _Cap()

        def close(self):
            self._d.close()
            self.closed = True

    _install_module(monkeypatch, "neo4j",
                    GraphDatabase=type("G", (),
                                       {"driver": staticmethod(_CapDriver)}))
    r = health.check_neo4j(
        _cfg(neo4j={"url": "bolt://n:7687", "user": "neo4j",
                    "password": "x"}))
    assert r.ok
    assert "auth ok" in r.detail
    assert any("SourceItem" in q for q in seen), (
        f"probe must count SourceItem nodes, saw: {seen}")
    assert not any("KbItem" in q for q in seen)


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


# --- preflight soft-deps (preflight-optional-deps) ---------------------------
# pytestmark: keep the real health.preflight under test (the autouse
# _preflight_bypass fixture in tests/conftest.py replaces it for tests
# without this marker — preflight_real keeps the genuine gate, like
# test_preflight_gate.py does).
pytestmark = pytest.mark.preflight_real


def test_preflight_neo4j_unconfigured_is_skipped(caplog, monkeypatch):
    """Soft dep: unconfigured neo4j -> INFO skip, no ServiceDependencyError."""
    import logging
    monkeypatch.setattr(
        health, "run_health_checks",
        lambda cfg: [
            HealthResult("qdrant", True, "ok", status="ok"),
            HealthResult("neo4j", False, "neo4j.url is not configured",
                         "set neo4j.url in kb.local.yml",
                         status="unconfigured"),
            HealthResult("llm", True, "ok", status="ok"),
            HealthResult("embedding", True, "ok", status="ok"),
        ])
    with caplog.at_level(logging.INFO, logger="digital_twins.health"):
        ok = health.preflight(_cfg())
    assert ok == ["qdrant", "llm", "embedding"]
    msgs = [r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO]
    assert any("neo4j" in m and "unconfigured" in m for m in msgs)


def test_preflight_neo4j_auth_failed_still_hard_fails(monkeypatch):
    """Soft dep but configured-and-broken: auth-failed -> hard failure."""
    monkeypatch.setattr(
        health, "run_health_checks",
        lambda cfg: [
            HealthResult("qdrant", True, "ok", status="ok"),
            HealthResult("neo4j", False, "bad creds",
                         "correct neo4j.user / neo4j.password",
                         status="auth-failed"),
        ])
    with pytest.raises(health.ServiceDependencyError) as exc:
        health.preflight(_cfg())
    assert exc.value.service == "neo4j"
    assert exc.value.status == "auth-failed"


def test_preflight_neo4j_unreachable_still_hard_fails(monkeypatch):
    """Soft dep but configured-and-broken: unreachable -> hard failure."""
    monkeypatch.setattr(
        health, "run_health_checks",
        lambda cfg: [
            HealthResult("qdrant", True, "ok", status="ok"),
            HealthResult("neo4j", False, "connection refused",
                         "check neo4j.url",
                         status="unreachable"),
        ])
    with pytest.raises(health.ServiceDependencyError) as exc:
        health.preflight(_cfg())
    assert exc.value.service == "neo4j"
    assert exc.value.status == "unreachable"


def test_preflight_neo4j_ok_is_added(monkeypatch):
    """Soft dep, status ok -> added to ok_services as before."""
    monkeypatch.setattr(
        health, "run_health_checks",
        lambda cfg: [
            HealthResult("qdrant", True, "ok", status="ok"),
            HealthResult("neo4j", True, "reachable; auth ok",
                         status="ok"),
        ])
    assert health.preflight(_cfg()) == ["qdrant", "neo4j"]


def test_preflight_qdrant_unconfigured_still_raises(caplog, monkeypatch):
    """Hard dep unchanged: unconfigured qdrant still raises."""
    import logging
    monkeypatch.setattr(
        health, "run_health_checks",
        lambda cfg: [
            HealthResult("qdrant", False, "qdrant.url is not configured",
                         "set qdrant.url in kb.local.yml",
                         status="unconfigured"),
        ])
    with caplog.at_level(logging.WARNING, logger="digital_twins.health"):
        with pytest.raises(health.ServiceDependencyError) as exc:
            health.preflight(_cfg())
    assert exc.value.service == "qdrant"
    assert exc.value.status == "unconfigured"
    msgs = [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING]
    assert any("qdrant" in m and "unconfigured" in m for m in msgs)


def test_preflight_qdrant_unreachable_still_raises(monkeypatch):
    """Hard dep unchanged: unreachable qdrant still raises."""
    monkeypatch.setattr(
        health, "run_health_checks",
        lambda cfg: [
            HealthResult("qdrant", False, "unreachable: ConnectionError",
                         "check qdrant.url",
                         status="unreachable"),
        ])
    with pytest.raises(health.ServiceDependencyError) as exc:
        health.preflight(_cfg())
    assert exc.value.service == "qdrant"
    assert exc.value.status == "unreachable"


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
