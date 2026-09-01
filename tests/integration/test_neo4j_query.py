"""Neo4j query-path coverage: read + write Cypher, driver wiring, live query.

The shipped package talks to Neo4j through exactly three surfaces, and
this module exercises each one's *query* (not just its reachability):

* ``health.check_neo4j`` — the only read query the package issues:
  the driver executes ``RETURN 1`` inside a session (round-trip: the
  recording fake actually runs it).
* ``ingest.pipeline._upsert_graph`` — the write queries: one
  ``MERGE (n:KbItem ...)`` per distinct item URL (deduped via
  ``seen_items``) and one ``MERGE (c:KbChunk ...) ... HAS_CHUNK``
  per chunk, with the exact parametrised Cypher pinned here.
* ``scheduler.loop`` driver wiring — the lazy factory builds a
  driver from the ``neo4j.url/user/password`` knobs, and the
  fail-closed branch (unset knobs) raises ``ConfigError`` naming the
  ``KB_NEO4J__*`` env remediation (RED-first: at write-up the factory
  imported ``ConfigError`` from ``config.schema`` where it is not
  defined — it lives in ``config.loader`` — so this case failed with
  ``ImportError`` until the import was fixed).

Hermetic parts use recording fakes (no network, no host values —
portability-safe, NFR-13). The final case is the opt-in ``live``
marker test (declared in pyproject: "tests that require real external
services (opt-in via env)"): set ``DT_NEO4J_LIVE=1`` and it runs a
real Cypher read against whichever endpoint the config layer resolves
— the test file itself contains no host endpoint or credential.
"""
from __future__ import annotations

import os
import sys
import types
from types import SimpleNamespace

import pytest

from digital_twins import health
from digital_twins.config.loader import ConfigError
from digital_twins.config.schema import get
from digital_twins.ingest import pipeline
from digital_twins.scheduler import loop

# --- recording fakes -------------------------------------------------------


class _RecordingSession:
    def __init__(self, rec):
        self._rec = rec

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        self._rec.append((query, params))
        return "RESULT"


class _RecordingDriver:
    """Driver-shaped fake mirroring the real ``neo4j`` driver surface."""

    def __init__(self, url, auth=None):
        self.url = url
        self.auth = auth
        self.rec = []
        self.closed = False

    def verify_connectivity(self):
        pass

    def session(self):
        return _RecordingSession(self.rec)

    def close(self):
        self.closed = True


def _install_neo4j_module(monkeypatch, rec):
    mod = types.ModuleType("neo4j")

    def _driver(url, auth=None):
        d = _RecordingDriver(url, auth)
        rec.append(d)
        return d

    mod.GraphDatabase = type("G", (), {"driver": staticmethod(_driver)})
    monkeypatch.setitem(sys.modules, "neo4j", mod)


# --- 1. health check: the shipped read query --------------------------------


def test_check_neo4j_runs_return_one_query(monkeypatch):
    """Round-trip: check_neo4j executes exactly one read query (RETURN 1)."""
    rec = []
    _install_neo4j_module(monkeypatch, rec)
    r = health.check_neo4j(
        {"neo4j": {"url": "bolt://n:7687", "user": "u", "password": "p"}})
    assert r.ok and "auth ok" in r.detail
    assert len(rec) == 1
    queries = [q for q, _ in rec[0].rec]
    assert queries == ["RETURN 1"]
    assert rec[0].closed


# --- 2. graph writes: the shipped write queries ----------------------------


class _RecordingNeo4j:
    """The driver-shaped object the pipeline hand-off contract defines:
    ``.run(query, **params)`` (002 US2)."""

    def __init__(self):
        self.queries = []

    def run(self, query, **params):
        self.queries.append((query, params))


def test_upsert_graph_cypher_write_queries():
    """Exact Cypher + params: item MERGE once per distinct URL, chunk
    MERGE + HAS_CHUNK link per chunk."""
    fake = _RecordingNeo4j()
    a = SimpleNamespace(key="a", ts="ts-a")
    b = SimpleNamespace(key="b", ts="ts-b")
    chunks = [
        ("p1", 0, a, "text-a0"),
        ("p2", 1, a, "text-a1"),  # same item a, second chunk
        ("p3", 2, b, "text-b0"),
    ]
    pipeline._upsert_graph(fake, "notes", "fs:", chunks)

    item_q = [(q, p) for q, p in fake.queries if "HAS_CHUNK" not in q]
    chunk_q = [(q, p) for q, p in fake.queries if "HAS_CHUNK" in q]
    # 3 chunks -> 2 distinct item URLs + 3 chunk/link writes.
    assert len(item_q) == 2
    assert len(chunk_q) == 3
    # item writes: params pinned (url = prefix + key).
    assert item_q[0][1] == {"u": "fs:a", "s": "notes", "k": "a", "ts": "ts-a"}
    assert item_q[1][1] == {"u": "fs:b", "s": "notes", "k": "b", "ts": "ts-b"}
    # the first query is the item write for the first chunk's item.
    assert "MERGE (n:KbItem {source_url: $u})" in fake.queries[0][0]
    # chunk writes: node + link in one query, params pinned.
    assert chunk_q[0][1] == {"id": "p1", "t": "text-a0", "u": "fs:a"}
    for q in (q for q, _ in chunk_q):
        assert "MERGE (c:KbChunk {id: $id})" in q
        assert "MERGE (i)-[:HAS_CHUNK]->(c)" in q


# --- 3. driver wiring -------------------------------------------------------


def test_build_neo4j_driver_uses_configured_endpoint(monkeypatch):
    """Eager path: driver built with the configured url + (user, password)."""
    rec = []
    _install_neo4j_module(monkeypatch, rec)
    d = loop.build_neo4j_driver(
        {"neo4j": {"url": "bolt://n:7687", "user": "u", "password": "p"}})
    assert d.url == "bolt://n:7687"
    assert d.auth == ("u", "p")


def test_neo4j_driver_factory_fails_closed_with_config_error(monkeypatch):
    """RED-first: unset knobs must raise ConfigError naming the KB_NEO4J__*
    env remediation — not ImportError (the factory once imported
    ConfigError from the wrong module)."""
    rec = []
    _install_neo4j_module(monkeypatch, rec)
    factory = loop._neo4j_driver_factory({"neo4j": {}})
    with pytest.raises(ConfigError, match="KB_NEO4J__URL"):
        factory()
    assert rec == []  # never paid for a driver


# --- 4. live query (opt-in) -------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("DT_NEO4J_LIVE"),
    reason="opt-in: set DT_NEO4J_LIVE=1 (endpoint/creds resolve via the "
           "config layer — NFR-13: no host values in this test)")
def test_live_neo4j_read_query_against_configured_endpoint():
    """Real Cypher read against the configured endpoint: count the graph,
    then — when data exists — verify the HAS_CHUNK shape the pipeline
    writes (002 US2)."""
    from digital_twins.config import loader
    cfg = loader.load()
    url = get(cfg, "neo4j.url")
    user = get(cfg, "neo4j.user")
    password = get(cfg, "neo4j.password")
    if not (url and user and password):
        pytest.skip("neo4j.url/user/password not resolvable via config layer")
    driver = loop.build_neo4j_driver(cfg)
    try:
        with driver.session() as s:
            n = s.run("MATCH (i:KbItem) RETURN count(i) AS n").single()["n"]
        assert isinstance(n, int) and n >= 0
        if n:
            with driver.session() as s:
                rows = s.run(
                    "MATCH (i:KbItem)-[:HAS_CHUNK]->(c:KbChunk) "
                    "RETURN i.source_url AS u, c.id AS cid LIMIT 5").data()
            assert rows, "KbItem nodes exist but no HAS_CHUNK edges"
            for row in rows:
                assert "u" in row and "cid" in row
    finally:
        driver.close()
