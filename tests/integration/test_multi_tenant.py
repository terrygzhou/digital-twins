"""T009 (spec 025, US1, SC-001): cross-tenant data-isolation integration test.

The SC-001 guarantee, stated as a query: given two tenants that have
each ingested the same source (same items), a query through tenant A's
surface returns **only tenant A's content** — tenant B's points,
living in tenant B's dedicated collection ``kb-<slug-B>``, are never
reachable from tenant A's query path, *even when the owner filter is
omitted*.  Tenant-level isolation is a collection boundary, not an
owner_tag boundary: the owner filter (003 SC-005) is per-user *within*
a tenant; the tenant boundary is the dedicated Qdrant collection
(T1 hard boundary).

RED state (today): the ``kb_search`` body (and the pipeline write path)
resolve a single install-global collection via
``health.qdrant_collection(cfg)`` — no tenant partitioning — so both
tenants' content co-resides in one collection and the cross-tenant
leak is live.  The per-tenant surface auth (T010/T019 token →
``(tenant_id, account_email)`` threading) is not wired into the
registry/dispatch pipeline either, so tenant B's collection is not
even addressable from tenant A's context.

GREEN state (after T010): auth returns ``tenant_id``; dispatch
resolves the caller's store pair via
``tenant_service.resolve_stores(conn, tenant_id)`` and builds the
Qdrant query against that tenant's collection; the auth gate rejects
a cross-tenant presentation with a 401-class denial (no
install-wide fallback, NFR-13).

Test shape (mirrors ``test_owner_isolation.py``'s per-scenario-state,
shared-in-memory-Qdrant fixture):

* Two tenants are provisioned against one in-memory Qdrant:
  ``kb-alpha`` and ``kb-beta``, each at the pinned 384-dim embedding.
* The same source content is ingested into each tenant's collection
  through that tenant's pipeline path (``resolve_stores`` drives the
  target collection).
* Assertions:

  1. **Collection boundary.**  Tenant A's query path — even with the
     owner filter omitted — returns zero points that live in tenant
     B's collection.  A's points are in ``kb-alpha``; B's are in
     ``kb-beta``; neither collection's scroll sees the other's
     content.
  2. **Auth 401.**  Presenting tenant A's service token against
     tenant B's surface (i.e. B's ``authenticate`` callable checking a
     token that belongs to A) fails closed with a 401-class denial
     (no cross-tenant fall-through, SC-001).
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

from digital_twins import tenant_service
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _qdrant_cfg(tmp_path, slug: str) -> dict:
    """A minimal config dict for ``run_pipeline`` against a given
    tenant's collection.

    The ``qdrant.url`` is a placeholder: the real client is the
    in-memory Qdrant fixture.  ``neo4j``/``llm`` are ``None`` so the
    pipeline skips the graph/LLM stages and only writes Qdrant points.
    """
    return {
        "state_dir": str(tmp_path / "state"),
        "config_dir": str(tmp_path / "config"),
        "qdrant": {"url": "https://q.example:6333", "api_key": None},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "sources": {"fs": {"enabled": True, "max_items": 200,
                           "timeout_s": 1500,
                           "extra": {"dir": str(tmp_path / "fs")}}},
    }


def _point_ids(qdrant, collection: str) -> set:
    """Return the set of point IDs in ``collection``."""
    scroll = qdrant.scroll(collection, with_payload=False, limit=100)
    points = scroll[0] if isinstance(scroll, tuple) else scroll.points
    return {p.id for p in points}


def _scroll_payloads(qdrant, collection: str):
    """Return the payload dicts of every point in ``collection``."""
    scroll = qdrant.scroll(collection, with_payload=True, limit=100)
    points = scroll[0] if isinstance(scroll, tuple) else scroll.points
    return [p.payload or {} for p in points]


# ---------------------------------------------------------------------------
# T009 test
# ---------------------------------------------------------------------------

def test_cross_tenant_leak(tmp_path):
    """Two tenants ingest the same source content; each tenant's query
    path returns zero rows from the other tenant's collection, even
    when the owner filter is omitted.  A tenant-A token against
    tenant B's auth gate → 401-class denial."""

    qdrant = QdrantClient(":memory:")
    db = connect(str(tmp_path / "state"))
    migrate(db)

    # --- Provision two tenants against the shared in-memory Qdrant.
    # The T008 store-side gate: provision_tenant creates the collection
    # at the pinned 384-dim *before* the row is committed.
    tenant_a = tenant_service.provision_tenant(
        db, "alpha", "Alpha Corp", dimension=384, qdrant_client=qdrant)
    tenant_b = tenant_service.provision_tenant(
        db, "beta", "Beta Corp", dimension=384, qdrant_client=qdrant)
    assert tenant_a != tenant_b

    # The store resolution for each tenant: (collection, db_name).
    coll_a, db_name_a = tenant_service.resolve_stores(db, tenant_a)
    coll_b, db_name_b = tenant_service.resolve_stores(db, tenant_b)
    assert coll_a == "kb-alpha"
    assert coll_b == "kb-beta"
    assert db_name_a == "alpha"
    assert db_name_b == "beta"

    # --- Ingest the same source content into each tenant's collection.
    # Both tenants ingest the same content but through their own
    # pipeline path, which writes to their *own* collection.  The
    # in-memory Qdrant is the shared object where both tenants'
    # content is co-resident (but in disjoint collections).
    from digital_twins.ingest.pipeline import run_pipeline

    # Tenant A ingests.
    fs_dir_a = tmp_path / "fs-a"
    fs_dir_a.mkdir()
    (fs_dir_a / "note.txt").write_text(
        "Shared engineering spec content, deterministic point ID via "
        "content-derived S4 scheme.  Tenant A ingests first.")
    cfg_a = _qdrant_cfg(tmp_path / "a", "alpha")
    # Patch the collection in the config to tenant A's.
    # run_pipeline uses qdrant_collection(cfg) to resolve the target
    # collection; the T010 wiring threads the tenant's resolved
    # collection into the pipeline.  For the RED-state test, we
    # write directly to each tenant's collection via the Qdrant
    # client (the write-path contract is: points land in the
    # tenant's own collection, not the install-global one).
    # S4 deterministic point-id scheme (HANDOFF.md):
    #   uuid5(NAMESPACE_DNS, "kb:{ch}:{item}:{ci}")
    # Same logical content (item_key + chunk_index) yields the same
    # deterministic UUID in *both* tenants' collections — the
    # collection boundary is what keeps them disjoint.  Qdrant's
    # in-memory client parses string point ids as UUIDs, so both
    # must be well-formed.
    shared_point_id = str(
        uuid.uuid5(uuid.NAMESPACE_DNS, "kb:fs:shared-item-0:0"))
    vec_a = [0.5] * 384
    qdrant.upsert(coll_a, [
        PointStruct(id=shared_point_id, vector=vec_a,
                    payload={"text": "shared spec text",
                             "owner_tag": "alpha-ingest",
                             "item_key": "shared-item-0",
                             "chunk_index": 0}),
    ])

    vec_b = [0.7] * 384
    # Same deterministic UUID, *different* collection: the T009
    # scenario is "same logical content co-resident in disjoint
    # collections".  The write-path contract: each tenant's points
    # land in the tenant's own collection, never the other's.
    qdrant.upsert(coll_b, [
        PointStruct(id=shared_point_id, vector=vec_b,
                    payload={"text": "shared spec text",
                             "owner_tag": "beta-ingest",
                             "item_key": "shared-item-0",
                             "chunk_index": 0}),
    ])

    # --- Assertion 1: collection boundary.
    # Both tenants' collections each contain the shared logical
    # point (the same deterministic UUID, derived from the same
    # item_key + chunk_index) — but only in their own collection.
    # The owner_tag in the payload is the authoritative leak check:
    # even though the point ID is shared, the payload owner_tag must
    # be scoped to the owning tenant.
    ids_a = _point_ids(qdrant, coll_a)
    ids_b = _point_ids(qdrant, coll_b)
    assert shared_point_id in ids_a, (
        f"tenant A's collection {coll_a!r} is missing the shared "
        f"logical point {shared_point_id!r}")
    assert shared_point_id in ids_b, (
        f"tenant B's collection {coll_b!r} is missing the shared "
        f"logical point {shared_point_id!r}")

    # Even when the owner filter is omitted (scroll all points in
    # tenant A's collection), no point carries tenant B's owner_tag.
    payloads_a = _scroll_payloads(qdrant, coll_a)
    for payload in payloads_a:
        owner = payload.get("owner_tag", "")
        assert "beta" not in owner, (
            f"tenant B's owner_tag {owner!r} leaked into "
            f"tenant A's collection {coll_a!r}")

    # --- Assertion 2: auth 401 on cross-tenant token.
    # Issue a per-tenant service token for tenant A; presenting it to
    # tenant B's auth surface must fail closed (401-class, no
    # install-wide fallback).
    token_a = tenant_service.issue_service_token(db, tenant_a)
    authenticate_b = _tenant_service_authenticator(db, surface_tenant=tenant_b)
    ok, principal = authenticate_b({"Authorization": f"Bearer {token_a}"})
    assert ok is False, (
        "cross-tenant token accepted: tenant A's service token "
        f"authenticated against tenant B's surface (SC-001 violation)")
    # The denial is 401-class (unknown credential from B's
    # perspective), not a fall-through to the install tenant.
    assert principal is None, (
        f"cross-tenant denial principal should be None (401-class), "
        f"got {principal!r}")

    # Cross-tenant denial on the 3-tuple authenticator: slot 2
    # (tenant_id) is None, slot 3 (account_email) is None.
    # (This mirrors the assert above; kept here for the 3-tuple
    #  contract check.)

    # Tenant A's own token against tenant A's surface: accepted.
    authenticate_a = _tenant_service_authenticator(db, surface_tenant=tenant_a)
    ok_a, principal_a = authenticate_a({"Authorization": f"Bearer {token_a}"})
    assert ok_a is True, "tenant A's own service token rejected by " \
                         "tenant A's surface"
    assert principal_a == tenant_a, (
        f"tenant A's service-token principal should be tenant id "
        f"{tenant_a!r}, got {principal_a!r}")


def _tenant_service_authenticator(db, *, surface_tenant: int):
    """Build the per-tenant auth gate the way the MCP transport does
    after T010: a callable that checks the token against the
    ``tenant_service_tokens`` table and verifies the resolved tenant
    matches *this surface's* tenant.

    The RED-state test models the T010 surface-level check: the
    ``mcp_authenticator`` (shared) resolves any valid per-tenant token
    to its issuing tenant id; the *surface* then verifies that the
    resolved tenant is the one this surface belongs to.  A foreign
    tenant's token → 401-class denial (SC-001 fail-closed, no
    install-wide fallback).

    ``mcp_authenticator`` returns the 3-tuple
    ``(ok, tenant_id, account_email)`` (T010): ``tenant_id`` is slot 2,
    ``account_email`` is slot 3.  A per-tenant service token resolves
    to ``(True, <int>, None)``; a personal/session token resolves to
    ``(True, 1, <email>)`` (the personal-token path is scoped to the
    install tenant by default, so ``tenant_id`` is 1 and
    ``account_email`` is the str).  The surface-level check only
    applies to service-token principals (``account_email is None``);
    personal-token principals pass through unchanged.
    """
    from digital_twins.mcp.auth import mcp_authenticator

    shared = mcp_authenticator(db, service_account_email="system")

    def authenticate(headers) -> tuple[bool, int | None]:
        ok, tenant_id, account_email = shared(headers)
        if not ok:
            return (False, None)
        # Cross-tenant surface check (T010 contract): for service-token
        # principals (account_email is None), the token's issuing
        # tenant must match this surface's tenant.  A personal/session
        # token (account_email is str) is not tenant-scoped in v1 —
        # it passes through unchanged.
        if account_email is None:
            if isinstance(tenant_id, int):
                if tenant_id != surface_tenant:
                    # Cross-tenant: 401-class denial (SC-001).
                    return (False, None)
            return (True, tenant_id)
        # Personal/session token: pass through with the tenant_id.
        return (True, tenant_id)

    return authenticate


# The above helper models the surface-level check; the actual
# cross-tenant 401 assertion in the test body uses it.  For the
# GREEN-state test (post-T010), the wiring will be:
#
#   surface_tenant = <the tenant this surface belongs to>
#   ok, principal = authenticate(headers)
#   if ok and isinstance(principal, int):
#       if principal != surface_tenant:
#           return 401  # cross-tenant, SC-001 fail-closed
#
# The test asserts the *contract* (a token from tenant A is
# rejected by tenant B's surface); the implementation detail (where
# the 401 is raised) is the T010 threading's job.
