"""T018 (RED): per-tenant service tokens — CRUD + resolution (025).

Mirrors the plan (specs/025-multi-tenant/plan.md T018/T019): CRUD +
verification for the per-tenant service-token layer that T015's
``tenant service-token create`` CLI already writes into
``tenant_service_tokens``:

- ``issue_service_token(conn, tenant_id) -> str`` — 32-byte hex
  plaintext shown once; only the ``pbkdf2$<salt>$<dk>`` hash is
  stored at rest (C-1 pattern, SHA-256 / 100 000 iters / 16-byte
  salt).  A suspended tenant cannot get a new token (fail-closed).
- ``verify_service_token(conn, plaintext) -> (tenant_id, None) | None``
  — re-derive the pbkdf2 hash from the plaintext, constant-time
  compare.  Returns ``(tenant_id, None)`` on success; a per-tenant
  token authenticates **only** its own tenant's surface (contract:
  token -> ``(tenant_id, account_email)``).
- ``revoke_service_token(conn, plaintext) -> None`` — mark the row
  revoked; subsequent verification fails.  Revoking tenant A's
  token does not affect tenant B's (per-tenant scope).
- ``list_service_tokens(conn, tenant_id=None) -> list[dict]`` —
  metadata only (id, tenant_id, created_at, revoked); never the
  plaintext or hash (C-1).

And the SC-001 cross-tenant rule (``contracts/mcp.md``):

- tenant A's token presented to tenant B's surface must fail with
  the 401 class, not fall through to another tenant.
- The legacy shared install service token (``DT_SERVICE_TOKEN``)
  maps to the install tenant (id 1) until the T2 backfill retires
  the shared store.

RED: ``tenant_service.verify_service_token`` / ``revoke_service_token``
/ ``list_service_tokens`` do not exist yet (the CLI ``create`` is the
only writer).  The ``mcp`` auth chain does not resolve a per-tenant
token to a ``(tenant_id, account_email)`` tuple yet — the 401
cross-tenant rule is not enforceable until T019 lands.
"""

from __future__ import annotations

import hashlib
import os
import re

import pytest

from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """A migrated (v6) state DB with the tenants +
    tenant_service_tokens tables."""
    conn = connect(tmp_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def tenant_service():
    """Import the module under test inside the test function so a
    missing function is a clean AttributeError (RED) instead of a
    collection error."""
    from digital_twins import tenant_service
    return tenant_service


def _seed_token_row(conn, tenant_id: int, plaintext: str) -> str:
    """Write a C-1-pattern token row the way the CLI ``create`` does,
    returning the at-rest hash so tests can assert on the format."""
    import secrets
    salt_hex = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac(
        "sha256", plaintext.encode(), bytes.fromhex(salt_hex), 100_000)
    token_hash = f"pbkdf2${salt_hex}${dk.hex()}"
    conn.execute(
        "INSERT INTO tenant_service_tokens "
        "(tenant_id, token_hash, created_at, revoked) "
        "VALUES (?, ?, datetime('now'), 0)",
        (tenant_id, token_hash),
    )
    conn.commit()
    return token_hash


def _provision(db, ts, slug: str) -> int:
    return ts.provision_tenant(db, slug, dimension=384)


# ---------------------------------------------------------------------------
# issue_service_token
# ---------------------------------------------------------------------------

def test_issue_returns_64_hex_plaintext(db, tenant_service):
    """The plaintext is ``secrets.token_hex(32)`` — 64 hex chars,
    shown once."""
    plaintext = tenant_service.issue_service_token(db, 1)
    assert re.fullmatch(r"[0-9a-f]{64}", plaintext)


def test_issue_stores_pbkdf2_hash_never_plaintext(db, tenant_service):
    """C-1: at rest is ``pbkdf2$salt_hex$dk_hex``; the plaintext is
    not derivable from, and not present in, the stored value."""
    plaintext = tenant_service.issue_service_token(db, 1)
    row = db.execute(
        "SELECT token_hash FROM tenant_service_tokens WHERE tenant_id=1"
    ).fetchone()
    assert row is not None
    stored = row[0]
    assert stored.startswith("pbkdf2$")
    assert stored.count("$") == 2
    assert plaintext not in stored
    salt_hex, dk_hex = stored.split("$")[1], stored.split("$")[2]
    expected = hashlib.pbkdf2_hmac(
        "sha256", plaintext.encode(), bytes.fromhex(salt_hex), 100_000)
    assert expected.hex() == dk_hex


def test_issue_rejects_suspended_tenant(db, tenant_service):
    """A suspended tenant's tokens are dead (403 class) — issuing a
    new one fails closed (NFR-15 additive-only, no resurrection)."""
    from digital_twins.tenant_service import SuspendedTenant, \
        suspend_tenant
    tid = _provision(db, tenant_service, "acme")
    suspend_tenant(db, tid)
    with pytest.raises(SuspendedTenant):
        tenant_service.issue_service_token(db, tid)


def test_issue_unknown_tenant_raises(db, tenant_service):
    """A deleted tenant row must not mint new tokens (NFR-15)."""
    from digital_twins.tenant_service import UnknownTenant, \
        delete_tenant
    tid = _provision(db, tenant_service, "acme")
    delete_tenant(db, tid)
    with pytest.raises(UnknownTenant):
        tenant_service.issue_service_token(db, tid)


# ---------------------------------------------------------------------------
# verify_service_token — per-tenant scoping (SC-001, contract:
# token -> (tenant_id, account_email))
# ---------------------------------------------------------------------------

def test_verify_returns_tenant_id_not_fallthrough(db, tenant_service):
    """A token authenticates ONLY its own tenant's surface.  The
    legacy shared install tenant (id 1) token maps to the install
    tenant, not to any provisioned tenant."""
    plaintext = tenant_service.issue_service_token(db, 1)
    result = tenant_service.verify_service_token(db, plaintext)
    assert result is not None
    tenant_id, account_email = result
    assert tenant_id == 1
    # No per-tenant account email is minted with a service token;
    # the tuple is (tenant_id, None) for a service token.
    assert account_email is None


def test_verify_provisioned_tenant_scoped_to_that_tenant(
        db, tenant_service):
    """Issuing into tenant B must not authenticate against
    tenant A's row — the row's tenant_id, not the tenant the
    caller intended, is returned."""
    a = _provision(db, tenant_service, "acme")
    b = _provision(db, tenant_service, "beta")
    tok_b = tenant_service.issue_service_token(db, b)
    tenant_id, _ = tenant_service.verify_service_token(db, tok_b)
    assert tenant_id == b
    assert tenant_id != a


def test_verify_wrong_plaintext_returns_none(db, tenant_service):
    """A token that was not issued for this DB fails (401-class
    unknown token)."""
    tenant_service.issue_service_token(db, 1)
    assert tenant_service.verify_service_token(
        db, "0" * 64) is None


def test_verify_garbage_returns_none(db, tenant_service):
    """Not-a-token is still 401, not a 500."""
    assert tenant_service.verify_service_token(db, "not-a-token") is None


# ---------------------------------------------------------------------------
# SC-001 cross-tenant rule: tenant A's token to tenant B's surface
# ---------------------------------------------------------------------------

def test_cross_tenant_token_is_401_not_fallthrough(
        db, tenant_service, monkeypatch, tmp_path):
    """Presenting tenant A's service token to tenant B's surface
    must fail closed with a 401-class unknown — no fall-through to
    the caller's own tenant.

    RED contract: the MCP auth chain does not know how to map a
    per-tenant service token to ``(tenant_id, account_email)`` yet
    (the shared ``DT_SERVICE_TOKEN`` is the only service credential
    it recognises, and it maps to the install tenant).  Once T019
    lands, this test asserts the per-tenant branch:
    - tenant A's token presented on tenant B's surface -> unknown
      (401), NOT "authentic but 403-for-B".
    """
    import digital_twins.mcp.auth as mcp_auth

    a = _provision(db, tenant_service, "acme")
    b = _provision(db, tenant_service, "beta")
    tok_a = tenant_service.issue_service_token(db, a)

    # Redirect the legacy shared-service-token env away from
    # interfering (the chain must check per-tenant rows).
    monkeypatch.delenv("DT_SERVICE_TOKEN", raising=False)

    # The 025 contract: authenticate returns a 3-tuple
    # (ok, tenant_id, account_email).  For a per-tenant service
    # token the *tenant id* is the auth principal; account_email is
    # None (a service token mints no account).
    ok, tenant_id, account_email = mcp_auth.mcp_authenticator(db)(
        {"Authorization": f"Bearer {tok_a}"})
    assert ok is True, \
        f"per-tenant token should authenticate to its own tenant, " \
        f"got tenant_id={tenant_id!r}, email={account_email!r}"
    # SC-001: the auth result must be scoped to tenant A, never B.
    assert tenant_id == a, \
        f"auth result must carry tenant {a}, got {tenant_id!r}"
    assert account_email is None, \
        f"a per-tenant service token mints no account, " \
        f"got {account_email!r}"


def test_revoked_token_is_403_class(db, tenant_service):
    """A known-but-revoked token is 403-class (the token was real,
    it was revoked), distinct from 401 unknown."""
    a = _provision(db, tenant_service, "acme")
    tok = tenant_service.issue_service_token(db, a)
    tenant_service.revoke_service_token(db, tok)
    # The 403-class reason surfaces via the (False, reason) tuple.
    result = tenant_service.verify_service_token(db, tok)
    assert result is None, "revoked token must not verify"


# ---------------------------------------------------------------------------
# revoke_service_token / list_service_tokens
# ---------------------------------------------------------------------------

def test_revoke_marks_row_revoked(db, tenant_service):
    """Revoking a token sets ``revoked=1`` on exactly that row;
    other rows for other tenants are untouched."""
    a = _provision(db, tenant_service, "acme")
    b = _provision(db, tenant_service, "beta")
    tok_a = tenant_service.issue_service_token(db, a)
    tenant_service.issue_service_token(db, b)

    tenant_service.revoke_service_token(db, tok_a)

    rows = db.execute(
        "SELECT tenant_id, revoked FROM tenant_service_tokens "
        "ORDER BY tenant_id"
    ).fetchall()
    assert rows[0] == (a, 1)   # acme revoked
    assert rows[1][1] == 0     # beta untouched
    assert tenant_service.verify_service_token(db, tok_a) is None


def test_revoke_unknown_token_is_noop(db, tenant_service):
    """Revoking a token that was never issued (or is already
    revoked) does not raise — it is a no-op, not an error."""
    a = _provision(db, tenant_service, "acme")
    tenant_service.issue_service_token(db, a)
    tenant_service.revoke_service_token(db, "0" * 64)  # unknown


def test_list_returns_metadata_only(db, tenant_service):
    """list_service_tokens returns id / tenant_id / created_at /
    revoked — never the plaintext or the hash (C-1)."""
    a = _provision(db, tenant_service, "acme")
    tenant_service.issue_service_token(db, a)
    rows = tenant_service.list_service_tokens(db, tenant_id=a)
    assert len(rows) == 1
    entry = rows[0]
    assert set(entry.keys()) == {
        "id", "tenant_id", "created_at", "revoked"}
    assert entry["tenant_id"] == a
    assert entry["revoked"] == 0
    # No plaintext or hash column in the returned dict.
    assert "token_hash" not in entry


def test_list_all_when_tenant_omitted(db, tenant_service):
    """Without a tenant_id, list_service_tokens returns rows across
    all tenants (admin view)."""
    a = _provision(db, tenant_service, "acme")
    b = _provision(db, tenant_service, "beta")
    tenant_service.issue_service_token(db, a)
    tenant_service.issue_service_token(db, b)
    rows = tenant_service.list_service_tokens(db)
    assert {r["tenant_id"] for r in rows} == {a, b}


def test_legacy_shared_install_token_maps_to_install_tenant(
        db, tenant_service, monkeypatch, tmp_path):
    """The legacy ``DT_SERVICE_TOKEN`` (the install-global shared
    service token) authenticates as the install tenant (id 1) until
    the T2 backfill retires the shared store.  A shared token must
    not be scoped to a provisioned tenant."""
    import digital_twins.mcp.auth as mcp_auth

    shared_secret = "s" * 64
    monkeypatch.setenv("DT_SERVICE_TOKEN", shared_secret)

    # T019 contract: mcp_authenticator returns a 3-tuple
    # (ok, tenant_id, account_email).  The shared install token
    # resolves to tenant id 1 with the service account email.
    ok, tenant_id, account_email = mcp_auth.mcp_authenticator(db)(
        {"Authorization": f"Bearer {shared_secret}"})
    assert ok is True, \
        f"shared install token must authenticate: " \
        f"tenant={tenant_id!r}, email={account_email!r}"
    assert tenant_id == 1, \
        f"shared install token must scope to the install tenant, " \
        f"got {tenant_id!r}"
    assert account_email == "system", \
        f"shared install token resolves to the service account, " \
        f"got {account_email!r}"
