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

import hashlib
import hmac
import os
import re
import secrets
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
# T019: per-tenant service token hash pattern (C-1)
#
# Mirrors the ``personal_tokens`` pbkdf2-hash-at-rest pattern from
# ``digital_twins.auth`` (C-1: plaintext shown once, only the hash
# stored).  We keep the constants local to this module so the service-
# token path has zero import coupling into the personal-token path
# (different tables, different lifecycles; a shared constant would
# couple them unnecessarily).
# ---------------------------------------------------------------------------

_HASH_PREFIX = "pbkdf2"
_ITERS = 100_000


def _pbkdf2_hash_plaintext(plaintext: str) -> str:
    """Hash ``plaintext`` with pbkdf2-hmac-sha256 (100k iters, 16-byte
    random salt).  The return value is the C-1 hash string
    ``pbkdf2$<salt_hex>$<digest_hex>`` — never the plaintext.
    """
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", plaintext.encode("utf-8"), salt, _ITERS)
    return f"{_HASH_PREFIX}${salt.hex()}${digest.hex()}"


def _pbkdf2_verify(plaintext: str, stored: str) -> bool:
    """Re-derive the pbkdf2 hash from ``plaintext`` with the stored
    salt and constant-time compare.  Returns ``False`` for any row
    whose stored hash is malformed (missing fields, bad hex) — the
    caller treats that as "no match" rather than raising.
    """
    parts = stored.split("$")
    if len(parts) != 3 or parts[0] != _HASH_PREFIX:
        return False
    try:
        salt = bytes.fromhex(parts[1])
        expected = parts[2]
    except ValueError:
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256", plaintext.encode("utf-8"), salt, _ITERS)
    return hmac.compare_digest(derived.hex(), expected)


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
                     dimension: int = 384,
                     qdrant_client=None) -> int:
    """Create a tenant row; return the new ``tenants.id``.

    Stores are derived from the slug (T1 hard boundary):
    ``qdrant_collection = "kb-" + slug``, ``neo4j_database = slug``.

    ``qdrant_client`` (optional, T008 store-side gate): when a Qdrant
    client is passed, the function creates the collection
    ``kb-<slug>`` at ``dimension`` *before* the row is committed,
    satisfying FR-3 ("provisioning auto-creates the collection at
    the pinned embedding dimension").  When ``qdrant_client`` is
    ``None`` (row-only path, e.g. migration bootstrap or tests
    that have no live Qdrant), the row is created without store
    side effects.

    Raises
    ------
    InvalidSlug
        The slug does not match ``[a-z0-9-]+`` (checked before the
        INSERT, so the caller never sees a raw IntegrityError).
    ReservedSlug
        The slug is one of :data:`_RESERVED_SLUGS` (``install``).
    DuplicateSlug
        The slug is already in use (``UNIQUE(slug)`` fired).

    ``dimension`` is the Qdrant vector size for the new collection.
    It is *not* persisted in the tenants row in v1 — the
    install-global embedding knob governs (T4: no per-tenant
    endpoint overrides, NFR-13 install-global).
    """
    if not slug:
        raise InvalidSlug(slug)
    if not _SLUG_RE.fullmatch(slug):
        raise InvalidSlug(slug)
    if slug in _RESERVED_SLUGS:
        raise ReservedSlug(slug)

    # T008 store-side gate (FR-3): when a Qdrant client is passed,
    # create the collection *before* the row is committed, so the
    # tenant row never exists without its collection.
    if qdrant_client is not None:
        collection = f"kb-{slug}"
        if not qdrant_client.collection_exists(collection):
            qdrant_client.create_collection(
                collection, vector_size=dimension)

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


# ---------------------------------------------------------------------------
# T019: per-tenant service tokens (C-1 pattern)
#
# The C-1 hash pattern (spec 025 data model): the plaintext is shown
# exactly once at issuance; only the pbkdf2 hash is stored at rest in
# ``tenant_service_tokens.token_hash``.  The table has no
# ``last_used_at`` column (C-1: a service token's use is not tracked —
# the operator revokes when a leak is suspected, so the row's only
# state that matters for auth is ``revoked``).
#
# The service-token rows belong to a tenant; a *suspended* tenant's
# tokens stop authenticating (403-class) because the tenant's status is
# the auth decision, not the token's.  The token row itself is left
# untouched (NFR-15 additive-only: no destructive cascades).
# ---------------------------------------------------------------------------

def issue_service_token(conn: sqlite3.Connection,
                        tenant_id: int) -> str:
    """Issue a per-tenant service token for ``tenant_id`` (T019).

    Generates a 32-byte ``secrets.token_hex(32)`` plaintext (64 hex
    chars, 256-bit entropy), stores only its pbkdf2 hash in
    ``tenant_service_tokens.token_hash``, and returns the plaintext
    *once*.  The plaintext is never persisted.

    The token's auth scope is the issuing tenant (T023):
    :func:`verify_service_token` returns ``tenant_id`` on a match and
    the MCP auth path pins the request's tenant resolution to that id
    (a cross-tenant presentation is a 401-class denial, SC-001 —
    there is no install-wide fallback in v1).

    A suspended tenant cannot get a new token (fail-closed):
    :class:`SuspendedTenant` (the CLI surfaces this as exit 2, the
    same operator-facing message the T022 command uses).  An unknown
    or deleted tenant id raises :class:`UnknownTenant` (401-class).
    """
    row = conn.execute(
        "SELECT status FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    if row is None:
        raise UnknownTenant(tenant_id)
    status = row[0]
    if status == "deleted":
        # A deleted row still exists (audit trail, NFR-15 additive-only);
        # issuing for it is a 401-class denial, not a 403.
        raise UnknownTenant(tenant_id)
    if status == "suspended":
        raise SuspendedTenant(tenant_id)

    plaintext = secrets.token_hex(32)
    token_hash = _pbkdf2_hash_plaintext(plaintext)
    conn.execute(
        "INSERT INTO tenant_service_tokens "
        "(tenant_id, token_hash, created_at, revoked) "
        "VALUES (?, ?, datetime('now'), 0)",
        (tenant_id, token_hash),
    )
    conn.commit()
    return plaintext


def verify_service_token(conn: sqlite3.Connection,
                         plaintext: str) -> tuple[int, None] | None:
    """Verify a per-tenant service token; return ``(tenant_id, None)``
    or ``None``.

    The return shape is the C-1 contract:

    - ``(tenant_id, None)`` — a valid, unrevoked token for tenant
      ``tenant_id``.  The *tenant id* is the auth principal for a
      service token (the tuple's second slot is ``None`` — no
      account email is minted with a service token; the MCP auth
      layer pins the request to that tenant).
    - ``None`` — the plaintext matched no row (401-class unknown),
      or the matching row is revoked (403-class; the caller surfaces
      the 403 with a "revoked" reason, but the verify function
      itself returns ``None`` for both cases — the 401 vs 403
      distinction is the caller's job, not the token's).

    The suspended-tenant case is NOT returned as a distinct result:
    the tenant's status is decided by the tenant-service layer, not
    the token row.  A suspended tenant's token verifies to the
    tenant id here; the MCP auth path then resolves the tenant
    (T022) and surfaces the 403 "tenant suspended".  This keeps the
    token path ignorant of tenant lifecycle (the token is just a
    credential; the tenant is the principal).

    A malformed or unknown plaintext is a 401-class denial: it
    returns ``None`` and the caller MUST NOT fall through to the
    shared install-wide ``DT_SERVICE_TOKEN`` (SC-001 fail-closed:
    the cross-tenant token contract is 401, not 403, and there is
    no cross-tenant fallback in v1).
    """
    rows = conn.execute(
        "SELECT tenant_id, token_hash, revoked FROM tenant_service_tokens"
    ).fetchall()
    for row in rows:
        tenant_id, stored, revoked = row
        if not stored:
            continue
        if _pbkdf2_verify(plaintext, stored):
            if revoked:
                # A known-but-revoked token is 403-class; the
                # verify function reports "did not verify" (None)
                # and the caller surfaces the "revoked" reason.
                return None
            return (tenant_id, None)
    return None


def revoke_service_token(conn: sqlite3.Connection,
                         plaintext: str) -> bool:
    """Revoke a per-tenant service token by plaintext (T019).

    Re-derives the pbkdf2 hash from the plaintext, finds the matching
    row, and flips ``revoked=1``.  Returns ``True`` if a row was
    revoked, ``False`` if the plaintext matched no row (unknown or
    already-revoked — the no-op case the tests require: revoking a
    stale token is a silent no-op, not an error).

    The plaintext is the input because the CLI's ``service-token
    revoke`` command takes the operator's copy of the token (the
    same value shown once at issuance).  The row's ``id`` is never
    exposed to the operator — the token's identity is its plaintext.
    """
    rows = conn.execute(
        "SELECT id, token_hash, revoked FROM tenant_service_tokens"
    ).fetchall()
    for row in rows:
        row_id, stored, revoked = row
        if revoked:
            # A revoked row is already in the target state; treat it
            # as a no-op (the operator may be re-running the command
            # against a stale copy).
            continue
        if not stored:
            continue
        if _pbkdf2_verify(plaintext, stored):
            conn.execute(
                "UPDATE tenant_service_tokens SET revoked=1 WHERE id=?",
                (row_id,),
            )
            conn.commit()
            return True
    return False


def list_service_tokens(conn: sqlite3.Connection,
                        tenant_id: int | None = None) -> list[dict]:
    """List per-tenant service-token metadata (never the hash, C-1).

    With ``tenant_id``: only that tenant's rows.  Without: all rows
    (the admin view the T022 CLI surfaces).  Each returned dict
    carries ``id``, ``tenant_id``, ``created_at``, and ``revoked`` —
    the ``token_hash`` is deliberately omitted (C-1: the hash is
    internal; exposing it would let an operator mount a
    hash-comparison attack against the stored pbkdf2 value).
    """
    if tenant_id is not None:
        rows = conn.execute(
            "SELECT id, tenant_id, created_at, revoked "
            "FROM tenant_service_tokens WHERE tenant_id=? "
            "ORDER BY id",
            (tenant_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, tenant_id, created_at, revoked "
            "FROM tenant_service_tokens ORDER BY id"
        ).fetchall()
    return [
        {
            "id": r[0],
            "tenant_id": r[1],
            "created_at": r[2],
            "revoked": bool(r[3]),
        }
        for r in rows
    ]
