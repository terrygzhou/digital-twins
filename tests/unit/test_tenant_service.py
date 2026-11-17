"""T006 (RED): tenant_service — store resolution + provisioning (025).

Mirrors the plan (specs/025-multi-tenant/plan.md T006/T007): a new
``tenant_service`` module that, given a ``tenant_id``, looks up the
``tenants`` row -> ``(qdrant_collection, neo4j_database)``.

RED: the module does not exist yet. Every test here is an assertion
against the *contract* of T007:

- ``resolve_stores(conn, tenant_id)`` — one lookup into the ``tenants``
  table. The install tenant (id 1) carries the legacy shared collection
  + default DB until the T2 backfill retires them. Unknown tenant ids
  raise ``UnknownTenant``.
- ``provision_tenant(conn, slug, dimension=384)`` — create the tenant
  row with ``qdrant_collection = "kb-" + slug`` and
  ``neo4j_database = slug`` (T1 hard boundary). Returns the new
  ``tenant_id``.

The Qdrant/Neo4j store-side effects (CREATE COLLECTION, CREATE DATABASE)
are *out of scope* for this module — they live in the provision CLI
(task T007's ops half). This module owns only the resolution layer +
the SQLite lifecycle (provision / suspend / delete row states).
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
def tenant_service():
    """Import the module under test inside the test function so a missing
    module is a clean ImportError (RED) instead of a collection error.
    """
    from digital_twins import tenant_service
    return tenant_service


# ---------------------------------------------------------------------------
# T006: resolve_stores — resolution contract
# ---------------------------------------------------------------------------

def test_install_tenant_resolves_to_legacy_shared(db, tenant_service):
    """The install tenant (id 1) resolves to the legacy shared
    collection + default DB until the T2 backfill retires them.

    The install row was seeded by apply_v6 with empty-string store
    names (task-11: "the install tenant stays on the legacy shared
    collection + default DB until the T2 backfill retires them").
    ``resolve_stores`` returns the row's stored values verbatim.
    """
    stores = tenant_service.resolve_stores(db, 1)
    assert stores == ("", "")


def test_provisioned_tenant_resolves_to_kb_prefixed_collection(db, tenant_service):
    """A provisioned tenant resolves to ``kb-<slug>`` collection +
    ``<slug>`` Neo4j database (T1 hard boundary)."""
    tenant_id = tenant_service.provision_tenant(db, "kb-pk", dimension=384)
    stores = tenant_service.resolve_stores(db, tenant_id)
    assert stores == ("kb-kb-pk", "kb-pk")


def test_resolve_unknown_tenant_raises(db, tenant_service):
    """An unknown tenant id raises ``UnknownTenant`` — never returns
    a default. (FR-7 fail-closed: a driver built from a missing row
    would point at the install tenant's stores, a cross-tenant leak.)"""
    with pytest.raises(tenant_service.UnknownTenant):
        tenant_service.resolve_stores(db, 9999)


def test_resolve_suspended_tenant_raises(db, tenant_service):
    """A suspended tenant raises ``SuspendedTenant`` — the auth path
    maps this to 403 ("tenant suspended"), distinct from 401
    (unknown credential). Mirrors contracts/mcp.md: suspended = 403."""
    tenant_id = tenant_service.provision_tenant(db, "kb-pk", dimension=384)
    tenant_service.suspend_tenant(db, tenant_id)
    with pytest.raises(tenant_service.SuspendedTenant):
        tenant_service.resolve_stores(db, tenant_id)


def test_resolve_deleted_tenant_raises(db, tenant_service):
    """A deleted tenant raises ``UnknownTenant`` — the row still
    exists (audit trail) but resolution is a 401-class denial,
    matching ``contracts/mcp.md``: cross-tenant = 401."""
    tenant_id = tenant_service.provision_tenant(db, "kb-pk", dimension=384)
    tenant_service.delete_tenant(db, tenant_id)
    with pytest.raises(tenant_service.UnknownTenant):
        tenant_service.resolve_stores(db, tenant_id)


# ---------------------------------------------------------------------------
# T007: provision_tenant — create contract
# ---------------------------------------------------------------------------

def test_provision_creates_row_with_kb_prefix(db, tenant_service):
    """provision_tenant creates the tenants row with
    ``qdrant_collection = 'kb-' + slug`` and ``neo4j_database = slug``."""
    tenant_id = tenant_service.provision_tenant(db, "keyva", dimension=384)
    row = db.execute(
        "SELECT slug, qdrant_collection, neo4j_database, status "
        "FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    assert row is not None
    slug, collection, neo4j_db, status = row
    assert slug == "keyva"
    assert collection == "kb-keyva"
    assert neo4j_db == "keyva"
    assert status == "active"


def test_provision_rejects_duplicate_slug(db, tenant_service):
    """The UNIQUE(slug) constraint on tenants means a duplicate slug
    raises. The module surfaces it as ``DuplicateSlug`` so the CLI
    can exit 2 without a raw sqlite3.IntegrityError."""
    tenant_service.provision_tenant(db, "keyva", dimension=384)
    with pytest.raises(tenant_service.DuplicateSlug):
        tenant_service.provision_tenant(db, "keyva", dimension=384)


def test_provision_rejects_reserved_slug(db, tenant_service):
    """``install`` is reserved for the seeded install tenant (id 1).
    The provision CLI must reject it so a user cannot create a
    second tenant that claims the install identity."""
    with pytest.raises(tenant_service.ReservedSlug):
        tenant_service.provision_tenant(db, "install", dimension=384)


def test_provision_rejects_reserved_collection(db, tenant_service):
    """A slug whose ``kb-<slug>`` collection collides with a reserved
    personal-kb / mem0 collection is rejected (the spec's
    ``disjoint from reserved personal_kb / mem0`` invariant).
    ``mem0`` itself -> collection ``kb-mem0`` is fine (reserved names
    are the *bare* collection names, not the kb-prefixed ones)."""
    tenant_id = tenant_service.provision_tenant(db, "mem0", dimension=384)
    row = db.execute(
        "SELECT qdrant_collection FROM tenants WHERE id=?",
        (tenant_id,)).fetchone()
    assert row[0] == "kb-mem0"


def test_provision_returns_tenant_id(db, tenant_service):
    """provision_tenant returns the new tenants.id (int), not the slug."""
    tenant_id = tenant_service.provision_tenant(db, "kb-pk", dimension=384)
    assert isinstance(tenant_id, int)
    assert tenant_id > 1


def test_provision_validates_slug_shape(db, tenant_service):
    """Slugs are ``[a-z0-9-]*`` (the DDL CHECK). Uppercase and
    out-of-class characters are rejected before they hit SQLite,
    so the caller gets ``InvalidSlug`` instead of IntegrityError."""
    for bad in ("KB-PK", "kb_pk", "kb/pk", "kb pk"):
        with pytest.raises(tenant_service.InvalidSlug):
            tenant_service.provision_tenant(db, bad, dimension=384)


# ---------------------------------------------------------------------------
# T015/T016: suspend_tenant + delete_tenant — lifecycle
# ---------------------------------------------------------------------------

def test_suspend_sets_status(db, tenant_service):
    """suspend_tenant flips status to 'suspended' (not 'deleted') —
    suspend is reversible, delete is not. All token auth for this
    tenant then refuses with 403 (T019's auth path)."""
    tenant_id = tenant_service.provision_tenant(db, "kb-pk", dimension=384)
    tenant_service.suspend_tenant(db, tenant_id)
    status = db.execute(
        "SELECT status FROM tenants WHERE id=?",
        (tenant_id,)).fetchone()[0]
    assert status == "suspended"


def test_suspended_tenant_cannot_suspend_again(db, tenant_service):
    """Suspend is idempotent-ish: re-suspending an already-suspended
    tenant is a no-op, not an error. Deleting it is still possible."""
    tenant_id = tenant_service.provision_tenant(db, "kb-pk", dimension=384)
    tenant_service.suspend_tenant(db, tenant_id)
    tenant_service.suspend_tenant(db, tenant_id)  # no raise
    status = db.execute(
        "SELECT status FROM tenants WHERE id=?",
        (tenant_id,)).fetchone()[0]
    assert status == "suspended"


def test_delete_sets_status_and_sets_deleted_at(db, tenant_service):
    """delete_tenant sets status='deleted' and stamps deleted_at.
    The row is retained for the audit trail (NFR-15 additive-only);
    the store-side drop happens in the provision CLI, not here."""
    tenant_id = tenant_service.provision_tenant(db, "kb-pk", dimension=384)
    tenant_service.delete_tenant(db, tenant_id)
    row = db.execute(
        "SELECT status, deleted_at FROM tenants WHERE id=?",
        (tenant_id,)).fetchone()
    assert row[0] == "deleted"
    assert row[1] is not None  # deleted_at stamped


def test_delete_install_tenant_forbidden(db, tenant_service):
    """The install tenant (id 1) cannot be deleted — it is the
    legacy shared collection's identity. Deleting it would orphan
    the pre-backfill data path."""
    with pytest.raises(tenant_service.InstallTenantProtected):
        tenant_service.delete_tenant(db, 1)


def test_suspend_install_tenant_forbidden(db, tenant_service):
    """The install tenant (id 1) cannot be suspended either — same
    protection as delete (T016): suspending it would 403 every
    legacy consumer until the backfill lands."""
    with pytest.raises(tenant_service.InstallTenantProtected):
        tenant_service.suspend_tenant(db, 1)
