"""MCP credential gate (feature 004, R1 / R12, T004; 025 T010/T019).

``mcp_authenticator(db, service_account_email="system")`` returns a
callable ``authenticate(headers) -> (ok, tenant_id, account_email)``
that accepts, in the *same order* as 003's ``cli._auth_checker``:

  1. the shared service token (BR-10) — ``DT_SERVICE_TOKEN`` env var,
     constant-time compare via ``hmac.compare_digest``; on a match the
     caller resolves to the configured service-account email (the
     ``mcp.service_account_email`` knob, default ``"system"``) scoped to
     the install tenant (id 1) until the T2 backfill retires the shared
     store;
  2. a valid per-tenant service token (025 T019, C-1 hash pattern) —
     ``tenant_service.verify_service_token``; on a match the caller is
     the *issuing tenant* (int; a service token mints no account, so
     ``account_email`` is None);
  3. a valid personal token (003 ``auth.verify_personal_token``);
  4. a valid session token (003 ``auth.verify_session``).

It reuses 003's verifiers verbatim — there is no fifth credential type.

Return contract (025 T010: a 3-tuple so the MCP executor can resolve
the caller's role via ``accounts.get_role`` AND thread the tenant id
into the store-resolution seam without a second query):

* ``(True, 1, <email>)`` — credential accepted for the install tenant:
  the shared service token (tenant 1, service-account email), a personal
  token, or a session token (tenant 1, the account's email — v1 is
  install-tenant-only for accounts).
* ``(True, <tenant_id>, None)`` — per-tenant service token accepted; the
  issuing tenant's id (``int``) with ``account_email`` None.  The tenant
  row must exist and not be suspended; cross-tenant presentation is
  401-class (SC-001 fail-closed: the token verifies only against its own
  tenant, there is no install-wide fallback in v1).
* ``(False, None, None)`` — no credential, a credential that matches
  none of the four sources (unknown → 401-class), or a dead per-tenant
  service token (the tenant row is gone or deleted → 401-class).
* ``(False, None, <reason>)`` — a *known* credential whose account/tenant
  is suspended (fail closed, mirroring 003's checker: 403-class denial,
  the reason string names the failure).

Host-neutral (NFR-13): ``DT_SERVICE_TOKEN`` is read from
``os.environ`` at call time (never logged, never in argv); the service
account email is a config knob, not a host constant.
"""

from __future__ import annotations

import hmac
import os

from .. import accounts as _accounts
from .. import auth as _auth


def mcp_authenticator(db, service_account_email: str = "system"):
    """Build the MCP ``authenticate(headers) -> (ok, tenant_id, account_email)``
    callable.

    ``db`` is a migrated state connection (accounts / personal_tokens /
    sessions / tenant_service_tokens / tenants tables).
    ``service_account_email`` is the account the shared service token
    resolves to (the ``mcp.service_account_email`` knob, default
    ``"system"``).

    The credential order is identical to 003's ``cli._auth_checker``
    (service token → per-tenant service token → personal token →
    session token). The return is a 3-tuple:

    * ``ok`` — the credential was accepted.
    * ``tenant_id`` — the tenant the caller is scoped to: the install
      tenant (``1``) for the shared service token / personal tokens /
      session tokens (v1 is install-tenant-only for accounts), or the
      issuing tenant's id for a per-tenant service token; ``None`` on
      a denial.
    * ``account_email`` — the resolved account email (the service
      account email on a shared service-token success, the account's
      email on a personal/session success, ``None`` for a per-tenant
      service token — a service token mints no account) or the short
      reason string for a known-but-dead credential (403-class, fail
      closed); ``None`` for an unknown credential (401-class).
    """

    def authenticate(headers) -> tuple[bool, int | None, str | None]:
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return (False, None, None)  # no credential
        token = auth[len("Bearer "):].strip()

        # 1) shared service token (BR-10, constant-time compare) → the
        #    configured service-account email, scoped to the install
        #    tenant (id 1) until the T2 backfill retires the shared
        #    store.
        service = os.environ.get("DT_SERVICE_TOKEN")
        if service and hmac.compare_digest(token, service):
            return (True, 1, service_account_email)

        # 2) per-tenant service token (T019, C-1 hash pattern) → the
        #    issuing tenant's id.  The *tenant id* is the auth
        #    principal for a service token; ``account_email`` stays
        #    None (a service token mints no account).  A cross-tenant
        #    presentation is a 401-class unknown (SC-001 fail-closed:
        #    there is no install-wide fallback in v1).
        from .. import tenant_service as _tenant_service
        verified = _tenant_service.verify_service_token(db, token)
        if verified is not None:
            tenant_id, _ = verified
            # Fail closed: the tenant row must exist and not be
            # suspended (the tenant-service layer decides lifecycle;
            # the token path just pins the principal).  A deleted
            # tenant is 401-class (the row is gone); a suspended
            # tenant is 403-class (the row is there, status is the
            # denial).
            row = db.execute(
                "SELECT status FROM tenants WHERE id=?",
                (tenant_id,)).fetchone()
            if row is None:
                # The token row outlives the tenant row (NFR-15
                # additive-only): 401-class, the principal is gone.
                return (False, None, None)
            status = row[0]
            if status == "deleted":
                return (False, None, None)
            if status == "suspended":
                return (False, None, "tenant suspended")
            return (True, tenant_id, None)

        # 3) personal token (003 verify_personal_token) → its account
        #    email, scoped to the install tenant (id 1).
        email = _auth.verify_personal_token(db, token)
        if email is not None:
            # Fail closed: the credential is known (the token row is
            # live) but if its account no longer exists, deny with a
            # reason.
            if _accounts.get_role(db, email) is None:
                return (False, None, "account no longer exists")
            return (True, 1, email)

        # 4) session token (003 verify_session, TTL + revoked) → its
        #    account email, scoped to the install tenant (id 1).
        email = _auth.verify_session(db, token)
        if email is not None:
            if _accounts.get_role(db, email) is None:
                return (False, None, "account no longer exists")
            return (True, 1, email)

        # 5) a Bearer that matches none of the above: unknown →
        #    (False, None, None) (401-class; mirrors 003's /status
        #    behavior).
        return (False, None, None)

    return authenticate
