"""007 T024 follow-up, Task 2 (D-007-2): keyed + locked qdrant client cache.

``dispatch._resolve_qdrant_client`` caches the Qdrant client in a
module-global so repeated calls don't reconstruct it.  Pre-fix the cache
held at most ONE client (a flat module global, effectively URL-only), and
the check-then-set was unguarded (benign race under threaded HTTP) — finding
D-007-2, recorded in
``.superpowers/sdd/deferred-minors/007-t024-findings.md``: two configs with
the same ``qdrant.url`` but a different ``qdrant.api_key`` silently shared
one client.

These tests pin the fix at the constructor seam: the lazy
``from qdrant_client import QdrantClient`` inside the resolver is spied on
by injecting a fake ``qdrant_client`` module into ``sys.modules``.  The
fake's ``QdrantClient`` records each construction (its kwargs) and returns
a fresh sentinel object per call, so cache identity is observable without
ever touching the network.  The construction must SUCCEED in the fake (the
resolver's try/except maps construction failure to ``QdrantUnavailable``;
we assert on the resolved client object, not an error dict).

GREEN: one construction per distinct config key; the same key reuses the
same cached object.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import types

import pytest

import digital_twins.mcp.dispatch as dispatch_mod
from digital_twins.accounts import create_account
from digital_twins.state.db import state_db_path
from digital_twins.state.migrations import migrate

URL = "http://127.0.0.1:6333"


@pytest.fixture()
def qdrant_spy(monkeypatch):
    """Spy the Qdrant client constructor at the module seam.

    The resolver does a lazy ``from qdrant_client import QdrantClient``
    inside its try/except, so the spy is installed as a fake
    ``qdrant_client`` module in ``sys.modules`` (the import is then
    satisfied by it, and any later test that imports the real package
    still gets the real one — monkeypatch restores ``sys.modules``).

    Yields an object with:
      ``calls``   — list of kwargs dicts, one per construction;
      ``sentinels`` — one fresh sentinel client object per construction.
    """
    spy = types.SimpleNamespace(calls=[], sentinels=[])

    def fake_ctor(url=None, api_key=None, **kwargs):
        spy.calls.append({"url": url, "api_key": api_key,
                          **kwargs})
        sentinel = object()
        spy.sentinels.append(sentinel)
        return sentinel

    fake_module = types.ModuleType("qdrant_client")
    fake_module.QdrantClient = fake_ctor
    monkeypatch.setitem(sys.modules, "qdrant_client", fake_module)
    yield spy


@pytest.fixture(autouse=True)
def clean_client_cache(monkeypatch):
    """Each test starts and ends with a clean qdrant client cache.

    The cache is module-global (D-007-2: it outlived configs).  Snapshot
    whatever form it currently takes (a legacy single client, or the keyed
    dict) and restore it afterwards, so test order never leaks a client into
    the next test.  ``raising=False`` tolerates the pre-fix module where the
    cache may not exist yet.
    """
    monkeypatch.setattr(
        dispatch_mod, "_qdrant_client_cache",
        getattr(dispatch_mod, "_qdrant_client_cache", {}), raising=False)
    yield


def test_same_config_reuses_one_client(qdrant_spy):
    """Same config dict called twice → the SAME client object, and the
    constructor is invoked exactly once (cache identity)."""
    cfg = {"qdrant": {"url": URL, "api_key": "secret-a"}}
    c1 = dispatch_mod._resolve_qdrant_client(cfg)
    c2 = dispatch_mod._resolve_qdrant_client(cfg)
    assert c1 is c2
    assert len(qdrant_spy.calls) == 1
    assert qdrant_spy.calls[0]["url"] == URL
    assert qdrant_spy.calls[0]["api_key"] == "secret-a"
    # the resolved object is the constructor's sentinel (not an error dict)
    assert c1 is qdrant_spy.sentinels[0]


def test_api_key_change_builds_a_fresh_client(qdrant_spy):
    """Same URL, different ``qdrant.api_key`` → a DIFFERENT client object
    (one construction per distinct key; no silent client sharing)."""
    cfg_a = {"qdrant": {"url": URL, "api_key": "secret-a"}}
    cfg_b = {"qdrant": {"url": URL, "api_key": "secret-b"}}
    c_a = dispatch_mod._resolve_qdrant_client(cfg_a)
    c_b = dispatch_mod._resolve_qdrant_client(cfg_b)
    assert c_a is not c_b
    assert c_a is qdrant_spy.sentinels[0]
    assert c_b is qdrant_spy.sentinels[1]
    assert len(qdrant_spy.calls) == 2
    assert [c["api_key"] for c in qdrant_spy.calls] == ["secret-a",
                                                        "secret-b"]
    # the original client is still cached under its own key
    assert dispatch_mod._resolve_qdrant_client(cfg_a) is c_a
    assert len(qdrant_spy.calls) == 2


# ---------------------------------------------------------------------------
# 008/US2 secret-hygiene: credential values must never appear in the MCP
# layer's tool responses, exception strings, or log records (FR-004).
#
# The cache tests above pin the keyed-client contract; this section pins the
# secret-hygiene guarantee at the MCP surface: when a credential-bearing
# config drives a failing tool call, the credential value must not leak into
# (a) the JSON tool response, (b) the exception string surfaced to the
# caller, or (c) any log record emitted under the ``digital_twins`` logger
# tree.
# ---------------------------------------------------------------------------

SECRET_A = "sk-mcp-hygiene-secret-a"
SECRET_B = "sk-mcp-hygiene-secret-b"


@pytest.fixture()
def db(tmp_path):
    """A migrated state DB with one reader account (the caller)."""
    path = state_db_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    create_account(conn, "alice@example.com", "pw-alice")
    conn.execute(
        "UPDATE accounts SET role='reader' WHERE email='alice@example.com'")
    conn.commit()
    yield conn
    conn.close()


class _LogCapture(logging.Handler):
    """Capture handler on the ``digital_twins`` logger tree."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture()
def log_capture():
    logger = logging.getLogger("digital_twins")
    cap = _LogCapture()
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(cap)
    try:
        yield cap
    finally:
        logger.removeHandler(cap)
        logger.setLevel(old_level)


def _all_log_text(records) -> str:
    """Flatten captured log records into a single searchable string
    (message + repr of args, mirroring test_secret_hygiene's helper)."""
    parts = []
    for r in records:
        msg = r.getMessage()
        if r.args:
            if isinstance(r.args, (list, tuple)):
                msg += " " + " ".join(repr(a) for a in r.args)
            else:
                msg += " " + repr(r.args)
        parts.append(msg)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# (a) failed kb_search: response JSON + logs carry no credential value
# ---------------------------------------------------------------------------

def test_kb_search_qdrant_failure_no_credential_leak(db, monkeypatch,
                                                     log_capture):
    """A failed kb_search call (Qdrant construction failure) must not
    echo the credential into the tool response or any log record.

    The construction-failure path in ``_resolve_qdrant_client`` wraps
    ``QdrantClient(url=..., api_key=...)`` in a try/except that maps to
    ``QdrantUnavailable(str(exc))``; the body maps that to the clean
    ``qdrant_unavailable`` error code with a fixed remediation string.
    Neither surface may carry the credential value.
    """
    import json
    import sys
    import types
    from digital_twins.mcp.dispatch import dispatch
    from digital_twins.mcp.registry import MCPContext

    cfg = {
        "qdrant": {"url": URL, "api_key": SECRET_A},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
    }
    ctx = MCPContext(db=db, caller_email="alice@example.com",
                     caller_role="reader", agent_kind="hermes",
                     config=cfg)

    # Force a construction failure by making the ctor raise with a
    # credential-bearing message (the worst-case leak scenario).
    def failing_ctor(url=None, api_key=None, **kwargs):
        raise RuntimeError(
            f"connection refused for {url} (api_key={api_key!r})")

    fake_module = types.ModuleType("qdrant_client")
    fake_module.QdrantClient = failing_ctor
    monkeypatch.setitem(sys.modules, "qdrant_client", fake_module)

    # Also seed the cache to ensure a fresh build.
    import digital_twins.mcp.dispatch as dispatch_mod
    dispatch_mod._qdrant_client_cache.clear()

    result = dispatch(ctx, "kb_search", {"query": "hi"})

    # The tool response JSON must not carry the credential.
    response_blob = json.dumps(result)
    assert SECRET_A not in response_blob, (
        f"qdrant api_key leaked into the kb_search tool response "
        f"(FR-004): {result!r}"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "qdrant_unavailable"

    # No log record may carry the credential.
    text = _all_log_text(log_capture.records)
    assert SECRET_A not in text, (
        f"qdrant api_key leaked into a log record (FR-004): {text!r}"
    )


# ---------------------------------------------------------------------------
# (b) resolver construction-failure: QdrantUnavailable str() carries no key
# ---------------------------------------------------------------------------

def test_resolver_construction_failure_no_key_in_exception(monkeypatch):
    """When the QdrantClient constructor raises with a credential-bearing
    message, the wrapped QdrantUnavailable exception string must not
    carry the key (the resolver maps construction failure to
    QdrantUnavailable(str(exc)); str(exc) must be safe)."""
    import sys
    import types

    class _CredLeakyCtor:
        def __init__(self, url=None, api_key=None, **kwargs):
            raise RuntimeError(
                f"qdrant unreachable at {url} "
                f"(token={api_key!r} rejected)")

    fake_module = types.ModuleType("qdrant_client")
    fake_module.QdrantClient = _CredLeakyCtor
    monkeypatch.setitem(sys.modules, "qdrant_client", fake_module)

    cfg = {"qdrant": {"url": URL, "api_key": SECRET_A}}
    with pytest.raises(dispatch_mod.QdrantUnavailable) as exc_info:
        dispatch_mod._resolve_qdrant_client(cfg)

    err_str = str(exc_info.value)
    assert SECRET_A not in err_str, (
        f"qdrant api_key leaked into QdrantUnavailable exception string "
        f"(FR-004): {err_str!r}"
    )


# ---------------------------------------------------------------------------
# (c) failed kb_ingest: run_failed message + logs carry no credential
# ---------------------------------------------------------------------------

def test_kb_ingest_failure_no_credential_leak(db, monkeypatch, log_capture):
    """A failed kb_ingest call (pipeline exception) must not echo the
    credential into the run_failed response or any log record.

    The kb_ingest body resolves the Neo4j driver (logging a warning on
    construction failure) and runs the pipeline.  The run_failed error
    message is ``str(exc)`` — the credential must not appear in it.
    """
    import json
    from digital_twins.mcp.dispatch import dispatch
    from digital_twins.mcp.registry import MCPContext

    cfg = {
        "qdrant": {"url": URL, "api_key": SECRET_A},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "neo4j": {"url": "bolt://127.0.0.1:7687",
                  "user": "neo4j", "password": SECRET_B},
        "sources": {"hermes": {"enabled": True}},
    }
    ctx = MCPContext(db=db, caller_email="alice@example.com",
                     caller_role="scheduler", agent_kind="dsh",
                     config=cfg)

    import digital_twins.mcp.dispatch as dispatch_mod

    # Spy the pipeline to force a failure.
    class _RunRecorder:
        def __init__(self, exc):
            self.calls = []
            self.exc = exc

        def __call__(self, *args, **kwargs):
            self.calls.append({"args": args, "kwargs": kwargs})
            raise self.exc

    rec = _RunRecorder(exc=RuntimeError("pipeline exploded"))
    monkeypatch.setattr(dispatch_mod, "run_pipeline", rec, raising=False)
    # Clear the qdrant cache to ensure a fresh build.
    dispatch_mod._qdrant_client_cache.clear()

    result = dispatch(ctx, "kb_ingest", {"source": "hermes"})

    # The tool response JSON must not carry the credential.
    response_blob = json.dumps(result)
    assert SECRET_A not in response_blob, (
        f"qdrant api_key leaked into the kb_ingest tool response "
        f"(FR-004): {result!r}"
    )
    assert SECRET_B not in response_blob, (
        f"neo4j password leaked into the kb_ingest tool response "
        f"(FR-004): {result!r}"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "run_failed"

    # No log record may carry the credential.
    text = _all_log_text(log_capture.records)
    assert SECRET_A not in text, (
        f"qdrant api_key leaked into a log record (FR-004): {text!r}"
    )
    assert SECRET_B not in text, (
        f"neo4j password leaked into a log record (FR-004): {text!r}"
    )
