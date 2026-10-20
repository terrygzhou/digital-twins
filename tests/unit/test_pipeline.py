"""Unit tests for pipeline._ensure_graph_schema (019 graph-index-bootstrap).

Pins the four idempotent CREATE INDEX IF NOT EXISTS statements issued on
every graph-enabled pipeline pass:

  1. SourceItem.item_id   (S4 join key into Qdrant payload)
  2. SourceItem.channel   (provenance / channel-scoped reads)
  3. Entity(name, type)   (composite key matching materialize() MERGE)
  4. REL.source_item      (supersede / drop / reconcile lookup key)
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from digital_twins.ingest.pipeline import _ensure_graph_schema


# --- fakes -----------------------------------------------------------------

class FakeSession:
    """Records every Cypher statement executed through the session API."""

    def __init__(self):
        self.queries: list[str] = []

    def run(self, cypher: str, **params):
        self.queries.append(cypher)
        return SimpleNamespace(data=lambda: [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDriver:
    """Driver-shaped fake that returns a recording session."""

    def __init__(self):
        self.session_obj = FakeSession()

    def session(self):
        return self.session_obj


# --- tests -------------------------------------------------------------------

def test_ensure_graph_schema_issues_four_index_statements():
    """019: the schema bootstrap now issues exactly four
    CREATE INDEX IF NOT EXISTS statements, covering both the
    original S4 SourceItem keys and the 018/019 Entity + REL keys.
    """
    fake = FakeDriver()
    _ensure_graph_schema(fake)

    index_qs = [q for q in fake.session_obj.queries if "CREATE INDEX" in q]
    assert len(index_qs) == 4, f"Expected 4 CREATE INDEX statements, got {len(index_qs)}: {index_qs}"

    # Assert each exact statement.
    expected = [
        "CREATE INDEX IF NOT EXISTS FOR (si:SourceItem) ON (si.item_id)",
        "CREATE INDEX IF NOT EXISTS FOR (si:SourceItem) ON (si.channel)",
        "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.name, e.type)",
        "CREATE INDEX IF NOT EXISTS FOR (r:REL) ON (r.source_item)",
    ]
    for stmt in expected:
        assert stmt in index_qs, f"Missing: {stmt!r}"


def test_entity_index_is_composite_name_type():
    """The Entity index must be composite on (name, type) — a single
    (name) index would not match the MERGE/MATCH predicate in entities.py.
    """
    fake = FakeDriver()
    _ensure_graph_schema(fake)
    entity_qs = [q for q in fake.session_obj.queries if "Entity" in q]
    assert len(entity_qs) == 1
    assert "e.name, e.type" in entity_qs[0]


def test_rel_index_is_on_source_item():
    """The REL index must be on the single property source_item — the
    lookup key used by supersede() and drop() in entities.py.
    """
    fake = FakeDriver()
    _ensure_graph_schema(fake)
    rel_qs = [q for q in fake.session_obj.queries if "REL" in q]
    assert len(rel_qs) == 1
    assert "r.source_item" in rel_qs[0]


def test_second_call_is_idempotent_no_duplicate_ddl():
    """NFR-1: a second call against the same driver must not emit
    duplicate DDL — IF NOT EXISTS makes each statement a no-op on an
    already-indexed database, so we simply re-issue the same four
    statements (idempotent, not stateful)."""
    fake = FakeDriver()
    _ensure_graph_schema(fake)
    first = list(fake.session_obj.queries)
    _ensure_graph_schema(fake)
    second = fake.session_obj.queries[len(first):]
    assert second == first, "Second call should re-issue the same 4 statements (idempotent no-op via IF NOT EXISTS)"


def test_all_statements_use_if_not_exists():
    """Every DDL statement must be IF NOT EXISTS for idempotent re-runs."""
    fake = FakeDriver()
    _ensure_graph_schema(fake)
    for q in fake.session_obj.queries:
        assert "IF NOT EXISTS" in q, f"Missing IF NOT EXISTS: {q!r}"
