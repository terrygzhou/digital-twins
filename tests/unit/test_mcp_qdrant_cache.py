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

import sys
import types

import pytest

import digital_twins.mcp.dispatch as dispatch_mod

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
