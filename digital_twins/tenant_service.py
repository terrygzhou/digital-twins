"""T007: the tenant service layer (spec 025 multi-tenancy).

Owns the *resolution* and *lifecycle* layer for tenants:

- ``resolve_stores(conn, tenant_id)`` — one lookup into the ``tenants``
  table -> ``(qdrant_collection, neo4j_database)``.  The install tenant
  (id 1) carries the legacy shared collection + default DB until the T2
  backfill retires them.  Unknown ids raise ``UnknownTenant``; a
  suspended tenant raises ``SuspendedTenant`` (the MCP auth path maps
  these to 401 / 403 respectively, per ``contracts/mcp.md``).
- ``provision_tenant(conn, slug, name, dimension=384)`` — create the
  tenants row with ``qdrant_collection = "kb-" + slug`` and
  ``neo4j_database = slug`` (T1 hard boundary).  Returns the new
  ``tenant_id``.  Slug validation happens *before* the INSERT so
  callers get a clean :class:`InvalidSlug` rather than a raw
  ``sqlite3.IntegrityError`` from the DDL CHECK.
- ``suspend_tenant`` / ``delete_tenant`` — the SQLite row-state
  transitions (suspended is reversible; deleted is not, and stamps
  ``deleted_at``).  The store-side effects (Qdrant ``DELETE
  COLLECTION``, Neo4j ``DROP DATABASE``) live in the provision CLI,
  not here (NFR-15 additive-only: the row is retained for audit).

This module does **not** touch Qdrant or Neo4j.  The MCP/CLI surfaces
call :func:`resolve_stores` with the tenant id they resolved from
auth, then build their drivers/collections from the returned names
(FR-7 fail-closed: a driver URL lacking ``?db=`` on a tenant install
must fail fast — the guard lives at the driver-construction sites,
task T026).

Caching: the spec says "one lookup cached per request".  We do NOT
cache here — a cache would have to be invalidated on suspend/delete
within the same process, which is an open question the spec leaves to
a later pass.  Each request does one SELECT.
"""

from __future__ import annotations

import re
import sqlite3

#: Slug shape: lower-case alphanumerics and dashes only (the DDL CHECK
#: is ``GLOB '[a-z0-9-]*'``; a module-level ``re.fullmatch`` mirrors it
#: so validation errors are raised before SQLite's CHECK fires).
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")

#: Reserved slugs.  ``install`` is seeded by apply_v6 as the install
#: tenant (id 1); a user-supplied slug claiming that identity would
#: collide with the legacy shared store and must be rejected.
_RESERVED_SLUGS = frozenset({"install"})


# ---------------------------------------------------------------------------
# named exceptions (T007)
# ---------------------------------------------------------------------------

class TenantError(Exception):
    """Base class for all tenant service errors.

    All subclasses carry a human-readable message suitable for CLI
    output (the T022 ``digital-twins tenant`` commands surface these
    verbatim on stderr and exit 2).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class UnknownTenant(TenantError):
    """The tenant id does not exist (or is deleted).

    Raised by :func:`resolve_stores` for an id with no live row.  The
    MCP auth path maps this to HTTP 401 (cross-tenant denial).

    >>> raise UnknownTenant(9999)
    Traceback (most recent call last):
        ...
    UnknownTenant: unknown tenant id 9999
    """

    def __init__(self, tenant_id: int) -> None:
        super().__init__(f"unknown tenant id {tenant_id}")


class SuspendedTenant(TenantError):
    """The tenant is suspended: its stores still exist, but the
    tenant is not allowed to authenticate.

    The MCP auth path maps this to HTTP 403 ("tenant suspended"),
    distinct from :class:`UnknownTenant` (401).

    >>> raise SuspendedTenant(2)
    Traceback (most recent call last):
        ...
    SuspendedTenant: tenant 2 is suspended
    """

    def __init__(self, tenant_id: int) -> None:
        super().__init__(f"tenant {tenant_id} is suspended")


class InvalidSlug(TenantError):
    """The slug does not match ``[a-z0-9-]*`` (empty also invalid).

    Raised by :func:`provision_tenant` *before* the INSERT, so the
    caller never sees a raw ``sqlite3.IntegrityError`` from the DDL
    CHECK constraint.

    >>> raise InvalidSlug("KB-PK")
    Traceback (most recent call last):
        ...
    InvalidSlug: slug 'KB-PK' is invalid (must be [a-z0-9-]+, no leading dash)
    """

    def __init__(self, slug: str) -> None:
        super().__init__(
            f"slug {slug!r} is invalid (must be [a-z0-9-]+, no leading dash)"
        )


class ReservedSlug(TenantError):
    """The slug is reserved (``install`` — the seeded install tenant).

    >>> raise ReservedSlug("install")
    Traceback (most recent call last):
        ...
    ReservedSlug: slug 'install' is reserved
    """

    def __init__(self, slug: str) -> None:
        super().__init__(f"slug {slug!r} is reserved")


class DuplicateSlug(TenantError):
    """The slug is already taken by another tenant.

    Raised by :func:`provision_tenant` when the ``UNIQUE(slug)``
    constraint fires; the CLI surfaces this as exit 2 with the slug in
    the message so the operator can pick another.

    >>> raise DuplicateSlug("keyva")
    Traceback (most recent call last):
        ...
    DuplicateSlug: slug 'keyva' is already in use
    """

    def __init__(self, slug: str) -> None:
        super().__init__(f"slug {slug!r} is already in use")


class InstallTenantProtected(TenantError):
    """The install tenant (id 1) cannot be deleted.

    It is the identity of the legacy shared collection + default DB;
    deleting it would orphan the pre-backfill data path (the install
    row is what :func:`resolve_stores` returns for legacy consumers
    until the T2 backfill retires them).

    >>> raise InstallTenantProtected()
    Traceback (most recent call last):
        ...
    InstallTenantProtected: the install tenant (id 1) cannot be deleted
    """

    def __init__(self) -> None:
        super().__init__("the install tenant (id 1) cannot be deleted")


# ---------------------------------------------------------------------------
# resolution (T007)
# ---------------------------------------------------------------------------

def resolve_stores(conn: sqlite3.Connection,
                   tenant_id: int) -> tuple[str, str]:
    """Return ``(qdrant_collection, neo4j_database)`` for ``tenant_id``.

    One SELECT into the ``tenants`` table.  The install tenant (id 1)
    resolves to its stored row verbatim — an empty-string pair, the
    legacy shared collection + default DB bridge, until the T2
    backfill retires it (task-11).  A *provisioned* tenant resolves
    to ``kb-<slug>`` / ``<slug>``.

    Raises
    ------
    UnknownTenant
        The id has no row, or the row is in ``deleted`` status.
    SuspendedTenant
        The row exists but ``status = 'suspended'`` (auth maps to 403).
    """
    row = conn.execute(
        "SELECT qdrant_collection, neo4j_database, status FROM tenants WHERE id=?",
        (tenant_id,),
    ).fetchone()
    if row is None:
        raise UnknownTenant(tenant_id)
    collection, database, status = row
    if status == "suspended":
        raise SuspendedTenant(tenant_id)
    if status == "deleted":
        # A deleted row still exists (audit trail, NFR-15 additive-only);
        # resolution is a 401-class denial, not a 403.
        raise UnknownTenant(tenant_id)
    return (collection, database)


# ---------------------------------------------------------------------------
# provisioning (T007)
# ---------------------------------------------------------------------------

def provision_tenant(conn: sqlite3.Connection,
                     slug: str,
                     name: str | None = None,
                     dimension: int = 384) -> int:
    """Create a tenant row; return the new ``tenants.id``.

    Stores are derived from the slug (T1 hard boundary):
    ``qdrant_collection = "kb-" + slug``, ``neo4j_database = slug``.
    The Qdrant/Neo4j store-side effects (CREATE COLLECTION / CREATE
    DATABASE) are the provision CLI's job — this function only writes
    the SQLite row.

    Raises
    ------
    InvalidSlug
        The slug does not match ``[a-z0-9-]+`` (checked before the
        INSERT, so the caller never sees a raw IntegrityError).
    ReservedSlug
        The slug is one of :data:`_RESERVED_SLUGS` (``install``).
    DuplicateSlug
        The slug is already in use (``UNIQUE(slug)`` fired).

    ``dimension`` is accepted for API symmetry with the provision CLI
    (the Qdrant collection's vector size) but is *not* persisted in
    the tenants row in v1 — the install-global embedding knob governs
    (T4: no per-tenant endpoint overrides, NFR-13 install-global).
    """
    if not slug:
        raise InvalidSlug(slug)
    if not _SLUG_RE.fullmatch(slug):
        raise InvalidSlug(slug)
    if slug in _RESERVED_SLUGS:
        raise ReservedSlug(slug)

    display_name = name if name is not None else slug.capitalize()
    try:
        cur = conn.execute(
            "INSERT INTO tenants (slug, name, status, qdrant_collection, "
            "neo4j_database, created_at) VALUES (?, ?, 'active', ?, ?, datetime('now'))",
            (slug, display_name, f"kb-{slug}", slug),
        )
    except sqlite3.IntegrityError as exc:
        # UNIQUE(slug) is the only constraint that can fire here
        # (the CHECK is validated above; the insert path cannot trip
        # the DDL CHECK).
        raise DuplicateSlug(slug) from exc
    conn.commit()
    return cur.lastrowid or 0


# ---------------------------------------------------------------------------
# lifecycle (T015 / T016)
# ---------------------------------------------------------------------------

def suspend_tenant(conn: sqlite3.Connection, tenant_id: int) -> None:
    """Set ``tenants.status = 'suspended'`` for ``tenant_id``.

    Idempotent: suspending an already-suspended tenant is a no-op,
    not an error (the status is unchanged, ``deleted_at`` is not
    stamped).  Reversible — a future ``resume_tenant`` (out of scope
    for v1) flips it back to ``active``.

    The install tenant (id 1) cannot be suspended (it is the legacy
    shared store's identity; suspending it would 403 every legacy
    consumer until the backfill lands).  Raises ``UnknownTenant`` if
    the id does not exist.
    """
    if tenant_id == 1:
        # The install tenant is not suspendable (same protection as
        # delete, but as a suspension guard: a suspended install
        # tenant would 403 every legacy consumer).
        # Not a distinct exception class — the operator-facing message
        # is "unknown" because from a lifecycle standpoint, id 1 is
        # not a provisionable tenant.
        raise UnknownTenant(tenant_id)
    cur = conn.execute(
        "UPDATE tenants SET status='suspended' WHERE id=?",
        (tenant_id,),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise UnknownTenant(tenant_id)


def delete_tenant(conn: sqlite3.Connection, tenant_id: int) -> None:
    """Set ``tenants.status = 'deleted'`` and stamp ``deleted_at``.

    The row is retained for the audit trail (NFR-15 additive-only);
    the store-side drop (Qdrant ``DELETE COLLECTION``, Neo4j
    ``DROP DATABASE``) is the provision CLI's job.  A deleted row
    raises :class:`UnknownTenant` from :func:`resolve_stores`.

    The install tenant (id 1) cannot be deleted — it is the legacy
    shared store's identity.  Raises :class:`InstallTenantProtected`.
    """
    if tenant_id == 1:
        raise InstallTenantProtected()
    cur = conn.execute(
        "UPDATE tenants SET status='deleted', deleted_at=datetime('now') "
        "WHERE id=? AND status != 'deleted'",
        (tenant_id,),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise UnknownTenant(tenant_id)
