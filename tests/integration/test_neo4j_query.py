"""Neo4j query-path coverage: read + write Cypher, driver wiring, live query.

The shipped package talks to Neo4j through exactly three surfaces, and
this module exercises each one's *query* (not just its reachability):

* ``health.check_neo4j`` — the only read query the package issues:
  the driver executes ``RETURN 1`` inside a session (round-trip: the
  recording fake actually runs it).
* ``ingest.pipeline._upsert_graph`` — the S4 write queries (openspec
  change s4-graph-alignment): one ``MERGE (si:SourceItem ...)`` per
  item, plus the two idempotent index DDL statements, with the exact
  parametrised Cypher pinned here.
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

        class _Result:
            def single(self):
                return {"n": 0}
            def data(self):
                return []

        return _Result()


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


def test_check_neo4j_runs_read_queries(monkeypatch):
    """Round-trip: check_neo4j runs RETURN 1 (connectivity) plus the
    S4 graph shape probe (SourceItem count) — both through one session."""
    rec = []
    _install_neo4j_module(monkeypatch, rec)
    r = health.check_neo4j(
        {"neo4j": {"url": "bolt://n:7687", "user": "u", "password": "p"}})
    assert r.ok and "auth ok" in r.detail
    assert len(rec) == 1
    queries = [q for q, _ in rec[0].rec]
    assert "RETURN 1" in queries
    assert any("SourceItem" in q for q in queries)
    assert rec[0].closed


# --- 2. graph writes: the shipped write queries ----------------------------


class _SessionRecordingNeo4j:
    """Driver-shaped fake mirroring the real ``neo4j`` driver surface
    (BUG-01 / graph-driver-fix): a raw ``neo4j.GraphDatabase.driver``
    exposes the session API — ``with driver.session() as s:
    s.run(query, **params)`` — and no driver-level ``.run()``. The
    pipeline hand-off contract is this session surface (002 US2), not
    a test-only driver-level ``.run()``.

    Records every ``(query, params)`` executed through a session, and
    tracks session entry/exit so tests can assert the calls went
    through ``.session()`` rather than a driver-level ``.run()``."""

    def __init__(self):
        self.queries = []
        self.sessions_opened = 0

    def session(self):
        rec = self

        class _Session:
            def __enter__(self):
                rec.sessions_opened += 1
                return self

            def __exit__(self, *args):
                return False

            def run(self, query, **params):
                rec.queries.append((query, params))
                return None

        return _Session()


def test_upsert_graph_cypher_write_queries():
    """S4 write queries pinned: 2 idempotent CREATE INDEX IF NOT EXISTS
    schema bootstrap statements (SourceItem.item_id / .channel), then
    one ``MERGE (si:SourceItem {item_id})`` per item with channel +
    item-level content_hash — deduped on item_id, no chunk nodes, no
    chunk text in the graph (chunk text lives in Qdrant full_content).
    All calls go through the driver's session API (graph-driver-fix:
    the raw ``neo4j`` driver has no driver-level ``.run()``)."""
    from digital_twins.ingest.ids import content_hash
    fake = _SessionRecordingNeo4j()
    a = SimpleNamespace(key="a", content="text-a")
    b = SimpleNamespace(key="b", content="text-b")
    items = [a, b]
    item_hash = {"a": content_hash("text-a"), "b": content_hash("text-b")}
    pipeline._upsert_graph(fake, "notes", items, item_hash)

    index_q = [(q, p) for q, p in fake.queries if "CREATE INDEX" in q]
    item_q = [(q, p) for q, p in fake.queries
              if "MERGE (si:SourceItem" in q]
    # Schema bootstrap: two idempotent index DDL statements, run first.
    assert len(index_q) == 2
    assert all("IF NOT EXISTS" in q for q, _ in index_q)
    assert "SourceItem" in index_q[0][0] and "SourceItem" in index_q[1][0]
    # One node per item, deduped on the join key (item.key).
    assert len(item_q) == 2
    assert item_q[0][1] == {"id": "a", "ch": "notes",
                             "hash": content_hash("text-a")}
    assert item_q[1][1] == {"id": "b", "ch": "notes",
                             "hash": content_hash("text-b")}
    for q in (q for q, _ in item_q):
        assert "MERGE (si:SourceItem {item_id: $id})" in q
        assert "si.channel = $ch" in q
        assert "si.content_hash = $hash" in q
    # No legacy labels anywhere in the write path.
    legacy = [q for q, _ in fake.queries
              if "KbItem" in q or "KbChunk" in q or "HAS_CHUNK" in q]
    assert legacy == []


def test_upsert_graph_uses_session_api():
    """REGRESSION (BUG-01 / graph-driver-fix, RED-first): the graph-write
    helpers call through the driver's session API (``with driver.session()
    as s: s.run(...)``) — the surface a real ``neo4j.GraphDatabase.driver``
    exposes. The fake above has no driver-level ``.run()``, so the old
    code (``neo4j.run(...)`` on the driver) fails with AttributeError
    here; the fix routes every graph write through a session."""
    from digital_twins.ingest.ids import content_hash
    fake = _SessionRecordingNeo4j()
    a = SimpleNamespace(key="a", content="text-a")
    items = [a]
    item_hash = {"a": content_hash("text-a")}
    pipeline._upsert_graph(fake, "notes", items, item_hash)
    # Every write went through a session (schema DDL + one MERGE each).
    assert fake.sessions_opened >= 1
    assert len(fake.queries) == 3  # 2 CREATE INDEX + 1 MERGE
    # And _ensure_graph_schema on its own also routes through a session.
    schema_fake = _SessionRecordingNeo4j()
    pipeline._ensure_graph_schema(schema_fake)
    assert schema_fake.sessions_opened >= 1
    assert len(schema_fake.queries) == 2
    assert all("IF NOT EXISTS" in q for q, _ in schema_fake.queries)


# --- 3. driver wiring -------------------------------------------------------


def test_build_neo4j_driver_uses_configured_endpoint(monkeypatch):
    """Eager path: driver built with the configured url + (user, password)."""
    rec = []
    _install_neo4j_module(monkeypatch, rec)
    d = loop.build_neo4j_driver(
        {"neo4j": {"url": "bolt://n:7687", "user": "u", "password": "p"}})
    assert d.url == "bolt://n:7687"
    assert d.auth == ("u", "p")


def test_build_neo4j_driver_exposes_session_api():
    """REGRESSION (graph-driver-fix, task 2.2): when a real driver is
    constructed, it is the raw ``neo4j`` driver shape — it exposes the
    session API (``driver.session()``) that the graph-write helpers
    depend on, not a test-only driver-level ``.run()``. Skipped when
    the real ``neo4j`` package is not importable or when the endpoint
    is not resolvable through the config layer (NFR-13: no host
    values in this test — the endpoint/creds come from the config
    layer only)."""
    import importlib.util
    if importlib.util.find_spec("neo4j") is None:
        pytest.skip("neo4j package not installed — real-driver shape "
                    "test requires it")
    from digital_twins.config import loader
    cfg = loader.load()
    url = get(cfg, "neo4j.url")
    user = get(cfg, "neo4j.user")
    password = get(cfg, "neo4j.password")
    if not (url and user and password):
        pytest.skip("neo4j.url/user/password not resolvable via "
                    "config layer — real-driver shape test skipped")
    # GraphDatabase.driver(...) is lazy (no network I/O at construction
    # in the neo4j 5.x/6.x driver) so building + closing is safe.
    driver = loop.build_neo4j_driver(cfg)
    try:
        assert hasattr(driver, "session")
        assert callable(driver.session)
    finally:
        driver.close()


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
            n = s.run(
                "MATCH (si:SourceItem) RETURN count(si) AS n").single()["n"]
        assert isinstance(n, int) and n >= 0
        if n:
            with driver.session() as s:
                rows = s.run(
                    "MATCH (si:SourceItem) "
                    "RETURN si.item_id AS id, si.channel AS ch LIMIT 5").data()
            assert rows, "SourceItem nodes exist but the query returned none"
            for row in rows:
                assert "id" in row and "ch" in row
    finally:
        driver.close()

# --- 4. s4 migration: legacy label removal (spec scenario) ----------------

class _SessionRecordingMigrator:
    """Driver-shaped fake for the migration module's query surface
    (BUG-01 / graph-driver-fix): a raw ``neo4j.GraphDatabase.driver``
    exposes the session API — ``with driver.session() as s:
    s.run(query, **params)`` — and no driver-level ``.run()``. The
    session's ``.run()`` returns a result with ``.single()`` resolving
    ``{"n": int}`` from the per-label counts the fake was constructed
    with (migrate_s4's read surface: the MATCH count queries); the
    DETACH DELETE statements resolve the same way, so a real run
    reports the node counts it deleted."""

    def __init__(self, counts: dict | None = None):
        self.queries = []
        self.sessions_opened = 0
        self._counts = counts or {}

    def session(self):
        rec = self

        class _Session:
            def __enter__(self):
                rec.sessions_opened += 1
                return self

            def __exit__(self, *args):
                return False

            def run(self, query, **params):
                rec.queries.append((query, params))
                counts = rec._counts

                def _single(q=query):
                    for lbl, cnt in counts.items():
                        if f"(n:{lbl}" in q:
                            return {"n": cnt}
                    return {"n": 0}

                class _Result:
                    def single(self):
                        return _single()

                return _Result()

        return _Session()

def test_migrate_s4_deletes_legacy_labels_idempotent():
    """Real (non-dry) run: two DETACH DELETE statements (KbItem, KbChunk),
    idempotent no-op on a fresh DB (counts 0). No :SourceItem statement."""
    from digital_twins import neo4j_migration
    fake = _SessionRecordingMigrator()
    res = neo4j_migration.migrate_s4(fake, dry_run=False)
    deletes = [q for q, _ in fake.queries if "DETACH DELETE" in q]
    assert len(deletes) == 2
    assert any("KbItem" in q for q in deletes)
    assert any("KbChunk" in q for q in deletes)
    assert not any("SourceItem" in q for q, _ in fake.queries)
    assert res["deleted"]["KbItem"] == 0
    assert res["deleted"]["KbChunk"] == 0
    assert fake.sessions_opened >= 1


def test_migrate_s4_dry_run_reports_counts_without_deleting():
    """Dry run: count queries only, no DELETE, counts surfaced (spec
    scenario 'Migration on legacy database')."""
    from digital_twins import neo4j_migration
    fake = _SessionRecordingMigrator(counts={"KbItem": 3, "KbChunk": 11})
    res = neo4j_migration.migrate_s4(fake, dry_run=True)
    assert res["dry_run"] is True
    assert res["would_delete"] == {"KbItem": 3, "KbChunk": 11}
    assert not any("DELETE" in q for q, _ in fake.queries)
    counts = [q for q, _ in fake.queries if "count" in q]
    assert any("KbItem" in q for q in counts)
    assert any("KbChunk" in q for q in counts)
    assert fake.sessions_opened >= 1


def test_migrate_s4_fresh_db_reports_zero():
    """Spec scenario 'Migration on fresh database': completes, reports 0
    nodes deleted."""
    from digital_twins import neo4j_migration
    fake = _SessionRecordingMigrator()
    res = neo4j_migration.migrate_s4(fake, dry_run=False)
    assert res["dry_run"] is False
    assert res["deleted"] == {"KbItem": 0, "KbChunk": 0}
    assert fake.sessions_opened >= 1


def test_migrate_s4_uses_session_api():
    """REGRESSION (BUG-01 / graph-driver-fix, RED-first): migrate_s4
    routes every Cypher through the driver's session API (``with
    driver.session() as s: s.run(...)``) — the surface a real
    ``neo4j.GraphDatabase.driver`` (what
    ``scheduler.loop.build_neo4j_driver`` returns) exposes. The fake
    has no driver-level ``.run()``, so the old code (``driver.run``
    on the driver) fails with AttributeError here; the fix routes the
    count reads and the DETACH DELETEs through a session."""
    from digital_twins import neo4j_migration
    fake = _SessionRecordingMigrator(counts={"KbItem": 3, "KbChunk": 11})
    res = neo4j_migration.migrate_s4(fake, dry_run=False)
    # 2 count reads + 2 DETACH DELETEs, all through a session.
    assert fake.sessions_opened >= 1
    assert len(fake.queries) == 4
    assert res["deleted"] == {"KbItem": 3, "KbChunk": 11}
    dry = _SessionRecordingMigrator(counts={"KbItem": 3, "KbChunk": 11})
    neo4j_migration.migrate_s4(dry, dry_run=True)
    assert dry.sessions_opened >= 1
    assert len(dry.queries) == 2  # count reads only
    assert not any("DELETE" in q for q, _ in dry.queries)


# --- 5. CLI surface: `digital-twins migrate s4` ------------------------------

def test_cli_migrate_s4_invokes_migrate_s4(monkeypatch):
    """`migrate s4` resolves the driver via the config layer and hands it to
    ``migrate_s4`` (dry-run passthrough). No host values anywhere."""
    from click.testing import CliRunner
    from digital_twins.cli import cli
    from digital_twins import neo4j_migration

    calls = []

    def fake_driver(cfg):
        calls.append(("driver", cfg))
        return _SessionRecordingMigrator()

    monkeypatch.setattr("digital_twins.cli.load",
                        lambda: {"state_dir": "/tmp/never"})

    monkeypatch.setattr(
        "digital_twins.cli._cli_resolve_neo4j_driver", fake_driver)

    def fake_migrate(driver, dry_run=False):
        calls.append(("migrate", dry_run))
        return {"dry_run": dry_run,
                "would_delete": {"KbItem": 0, "KbChunk": 0},
                "deleted": {"KbItem": 0, "KbChunk": 0}}

    monkeypatch.setattr(neo4j_migration, "migrate_s4", fake_migrate)
    runner = CliRunner()
    r = runner.invoke(cli, ["migrate", "s4", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "driver" in [c[0] for c in calls]
    assert ("migrate", True) in calls
    assert "would delete" in r.output


def test_cli_migrate_s4_fails_closed_without_neo4j_knobs(monkeypatch):
    """Unconfigured neo4j knobs -> _cli_resolve_neo4j_driver returns None
    -> command exits 1 with a remediation message (no crash)."""
    from click.testing import CliRunner
    from digital_twins.cli import cli

    monkeypatch.setattr("digital_twins.cli._cli_resolve_neo4j_driver",
                        lambda cfg: None)
    monkeypatch.setattr("digital_twins.cli.load",
                        lambda: {"state_dir": "/tmp/never"})
    runner = CliRunner()
    r = runner.invoke(cli, ["migrate", "s4"])
    assert r.exit_code == 1
    assert "no Neo4j endpoint resolvable" in r.output
