"""T008: store-side green gate — resolve_stores + provision_tenant
collection creation at pinned dimension (025).

Closes the gap noted in ``tests/unit/test_tenant_service.py``: the
row-level contract of ``resolve_stores`` / ``provision_tenant`` is
green; this file verifies the *store-side* gate that FR-3 requires —
that provisioning a tenant actually creates the Qdrant collection at
the pinned embedding dimension.

Contract (plan T008 + FR-3):

- ``resolve_stores(conn, tenant_id)`` returns the correct
  ``(qdrant_collection, neo4j_database)`` pair for the install tenant
  (``("", "")`` — legacy shared bridge) and for a provisioned tenant
  (``("kb-kb-pk", "kb-pk")``).
- ``provision_tenant(conn, slug, dimension=384)`` creates the Qdrant
  collection ``kb-<slug>`` with ``vector_size = dimension`` when a
  Qdrant client is available.  When no client is passed (row-only
  path, e.g. migration bootstrap), the row is created without store
  side effects.
"""

import pytest

from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """A migrated (v6) state DB with the tenants table + install row."""
    conn = connect(tmp_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def ts():
    """Import tenant_service lazily (RED-safe)."""
    from digital_twins import tenant_service
    return tenant_service


class FakeQdrantClient:
    """In-memory stand-in for QdrantClient; records create_collection calls.

    Mirrors the real qdrant-client 1.x API: ``create_collection`` takes a
    ``vectors_config`` keyword (a VectorParams-like object with ``size`` and
    ``distance``), not a bare ``vector_size``.  The fake extracts the size
    from ``vectors_config.size`` (falling back to 384 when omitted) so the
    production call site and the fake share the same contract.
    """

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.create_calls: list[tuple[str, int]] = []

    def collection_exists(self, name: str) -> bool:
        return name in self.collections

    def create_collection(self, name: str, *, vectors_config=None,
                         vector_size: int = 384, **_kw):
        """Accept both kwarg shapes.  The production call site passes
        ``vectors_config`` (a real VectorParams with ``.size``); the
        legacy tests may still pass ``vector_size`` directly."""
        if vectors_config is not None:
            size = getattr(vectors_config, "size", vector_size)
        else:
            size = vector_size
        self.collections[name] = {"vector_size": size}
        self.create_calls.append((name, size))

    def get_collection(self, name: str):
        info = self.collections.get(name)
        if info is None:
            raise KeyError(name)
        return info


# ---------------------------------------------------------------------------
# T008: resolve_stores — store-side verification
# ---------------------------------------------------------------------------

def test_resolve_stores_install_tenant_returns_legacy_pair(db, ts):
    """The install tenant (id 1) resolves to ("", "") — the legacy
    shared bridge; NOT kb-install/install."""
    assert ts.resolve_stores(db, 1) == ("", "")


def test_resolve_stores_provisioned_tenant_returns_kb_pair(db, ts):
    """A provisioned tenant resolves to ("kb-kb-pk", "kb-pk")."""
    tid = ts.provision_tenant(db, "kb-pk", dimension=384)
    assert ts.resolve_stores(db, tid) == ("kb-kb-pk", "kb-pk")


# ---------------------------------------------------------------------------
# T008: provision_tenant — Qdrant collection creation at pinned dim
# ---------------------------------------------------------------------------

def test_provision_tenant_creates_collection_at_pinned_dim(db, ts):
    """FR-3: provisioning auto-creates the collection at the pinned
    embedding dimension.  With a fake client injected, the collection
    ``kb-kb-pk`` must exist with vector_size=384."""
    client = FakeQdrantClient()
    ts.provision_tenant(db, "kb-pk", dimension=384, qdrant_client=client)
    assert client.collection_exists("kb-kb-pk")
    assert client.collections["kb-kb-pk"]["vector_size"] == 384


def test_provision_tenant_custom_dimension(db, ts):
    """The dimension parameter controls the created collection's
    vector_size (not hard-coded to 384)."""
    client = FakeQdrantClient()
    ts.provision_tenant(db, "kb-test", dimension=768, qdrant_client=client)
    assert client.collections["kb-kb-test"]["vector_size"] == 768


def test_provision_tenant_no_client_is_row_only(db, ts):
    """When no qdrant_client is passed (row-only path, e.g. migration
    bootstrap), provision_tenant still creates the row successfully —
    store-side effects are skipped, not errored."""
    tid = ts.provision_tenant(db, "kb-rowonly", dimension=384)
    row = db.execute(
        "SELECT qdrant_collection, neo4j_database FROM tenants WHERE id=?",
        (tid,)).fetchone()
    assert row == ("kb-kb-rowonly", "kb-rowonly")
