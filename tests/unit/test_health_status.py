"""008/US1 (T010, RED-first): HealthResult.status fail-fast mapping (FR-001).

Status enum: ok | unconfigured | unreachable | auth-failed. The field is
optional so legacy positional consumers keep working; T014 implements the
mappings (qdrant 401/403, neo4j AuthError, llm 401/403 on /models).
"""
from __future__ import annotations

import sys
import types
import urllib.error

import pytest

from digital_twins import health
from digital_twins.health import HealthResult, check_llm, check_neo4j, check_qdrant
from digital_twins.config.schema import validate

VALID_STATUSES = {"ok", "unconfigured", "unreachable", "auth-failed"}


def make_cfg(qdrant_url="http://q:6333", qdrant_key="kq",
             neo4j_url="bolt://n:7687", neo4j_user="u", neo4j_pass="p",
             llm_endpoint="http://llm:8000/v1", llm_key="kl"):
    c = validate({})
    c["qdrant"]["url"] = qdrant_url
    c["qdrant"]["api_key"] = qdrant_key
    c["neo4j"]["url"] = neo4j_url
    c["neo4j"]["user"] = neo4j_user
    c["neo4j"]["password"] = neo4j_pass
    c["llm"]["endpoint"] = llm_endpoint
    c["llm"]["api_key"] = llm_key
    return c


def test_status_field_is_optional_for_legacy_consumers():
    r = HealthResult("qdrant", False, "detail")
    assert r.status == ""


def test_closed_status_enum():
    assert health.VALID_STATUSES == VALID_STATUSES


# --- qdrant ---------------------------------------------------------------

def _fake_qdrant(monkeypatch, exc):
    mod = types.ModuleType("qdrant_client")

    class QdrantClient:
        def __init__(self, url=None, api_key=None):
            pass

        def get_collections(self):
            raise exc

    mod.QdrantClient = QdrantClient
    monkeypatch.setitem(sys.modules, "qdrant_client", mod)


def test_qdrant_unset_is_unconfigured():
    r = check_qdrant(make_cfg(qdrant_url=""))
    assert r.status == "unconfigured"
    assert r.ok is False
    assert "qdrant.url" in r.remediation


def test_qdrant_http_401_is_auth_failed(monkeypatch):
    _fake_qdrant(monkeypatch, Exception("401 Unauthorized"))
    r = check_qdrant(make_cfg())
    assert r.status == "auth-failed"
    assert r.ok is False
    assert "qdrant" in r.remediation


def test_qdrant_network_error_is_unreachable(monkeypatch):
    _fake_qdrant(monkeypatch, Exception("Connection refused"))
    r = check_qdrant(make_cfg())
    assert r.status == "unreachable"
    assert r.ok is False
    assert "qdrant.url" in r.remediation


# --- neo4j ------------------------------------------------------------------

def _fake_neo4j(monkeypatch, exc):
    mod = types.ModuleType("neo4j")

    class _Driver:
        def verify_connectivity(self):
            raise exc

        def session(self):
            raise AssertionError("success path not expected")

        def close(self):
            pass

    class GraphDatabase:
        @staticmethod
        def driver(url, auth=None):
            return _Driver()

    mod.GraphDatabase = GraphDatabase
    monkeypatch.setitem(sys.modules, "neo4j", mod)


def test_neo4j_unset_credentials_is_unconfigured():
    r = check_neo4j(make_cfg(neo4j_user=""))
    assert r.status == "unconfigured"
    assert r.ok is False
    assert "neo4j.user" in r.remediation


def test_neo4j_auth_error_is_auth_failed(monkeypatch):
    class AuthError(Exception):
        pass

    _fake_neo4j(monkeypatch, AuthError("invalid credentials"))
    r = check_neo4j(make_cfg())
    assert r.status == "auth-failed"
    assert r.ok is False
    assert "neo4j" in r.remediation


def test_neo4j_network_error_is_unreachable(monkeypatch):
    class ServiceUnavailableError(Exception):
        pass

    _fake_neo4j(monkeypatch, ServiceUnavailableError("server not up"))
    r = check_neo4j(make_cfg())
    assert r.status == "unreachable"
    assert r.ok is False
    assert "neo4j.url" in r.remediation


# --- llm --------------------------------------------------------------------

class _Resp:
    status = 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_llm_unset_is_unconfigured():
    r = check_llm(make_cfg(llm_endpoint=""))
    assert r.status == "unconfigured"
    assert r.ok is False
    assert "llm.endpoint" in r.remediation


def test_llm_http_401_is_auth_failed_and_not_ok(monkeypatch):
    def boom(url, timeout=None):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(health.urllib.request, "urlopen", boom)
    r = check_llm(make_cfg())
    assert r.status == "auth-failed"
    assert r.ok is False
    assert "llm" in r.remediation


def test_llm_http_403_is_auth_failed_and_not_ok(monkeypatch):
    def boom(url, timeout=None):
        raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)

    monkeypatch.setattr(health.urllib.request, "urlopen", boom)
    r = check_llm(make_cfg())
    assert r.status == "auth-failed"
    assert r.ok is False


def test_llm_network_error_is_unreachable(monkeypatch):
    def boom(url, timeout=None):
        raise urllib.error.URLError("[Errno 111] Connection refused")

    monkeypatch.setattr(health.urllib.request, "urlopen", boom)
    r = check_llm(make_cfg())
    assert r.status == "unreachable"
    assert r.ok is False
    assert "llm.endpoint" in r.remediation


def test_llm_reachable_is_ok(monkeypatch):
    monkeypatch.setattr(health.urllib.request, "urlopen", lambda url, timeout=None: _Resp())
    r = check_llm(make_cfg())
    assert r.status == "ok"
    assert r.ok is True
