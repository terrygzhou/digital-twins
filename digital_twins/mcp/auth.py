"""MCP credential gate (feature 004, R1 / R12, T004).

``mcp_authenticator(db, service_account_email="system")`` returns a
callable ``authenticate(headers) -> (bool, email | None)`` that accepts,
in the *same order* as 003's ``cli._auth_checker``:

  1. the shared service token (BR-10) — ``DT_SERVICE_TOKEN`` env var,
     constant-time compare via ``hmac.compare_digest``; on a match the
     caller resolves to the configured service-account email (the
     ``mcp.service_account_email`` knob, default ``"system"``);
  2. a valid personal token (003 ``auth.verify_personal_token``);
  3. a valid session token (003 ``auth.verify_session``).

It reuses 003's verifiers verbatim — there is no fourth credential type.

Return contract (differs from 003's ``bool | str`` so the MCP executor
in T010 can resolve the caller's role via ``accounts.get_role``):

* ``(True, <email>)`` — credential accepted; the caller's account email.
* ``(False, None)`` — no credential, or a credential that matches none
  of the three sources (unknown → 401-class denial).
* ``(False, <reason>)`` — a *known* credential whose account no longer
  exists (fail closed, mirroring 003's checker: 403-class denial, the
  reason string names the failure).

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
    """Build the MCP ``authenticate(headers) -> (bool, email|None)`` callable.

    ``db`` is a migrated state connection (accounts / personal_tokens /
    sessions tables). ``service_account_email`` is the account the shared
    service token resolves to (the ``mcp.service_account_email`` knob,
    default ``"system"``).

    The credential order is identical to 003's ``cli._auth_checker``
    (service token → personal token → session token). The return is a
    2-tuple rather than a bare ``bool | str``: the second element is the
    resolved account email on success, ``None`` for an unknown credential
    (401-class), or a short reason string for a known-but-dead credential
    (403-class, fail closed).
    """

    def authenticate(headers) -> tuple[bool, str | None]:
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return (False, None)  # no credential
        token = auth[len("Bearer "):].strip()

        # 1) shared service token (BR-10, constant-time compare) → the
        #    configured service-account email.
        service = os.environ.get("DT_SERVICE_TOKEN")
        if service and hmac.compare_digest(token, service):
            return (True, service_account_email)

        # 2) personal token (003 verify_personal_token) → its account email.
        email = _auth.verify_personal_token(db, token)
        if email is not None:
            # Fail closed: the credential is known (the token row is live)
            # but if its account no longer exists, deny with a reason.
            if _accounts.get_role(db, email) is None:
                return (False, "account no longer exists")
            return (True, email)

        # 3) session token (003 verify_session, TTL + revoked) → its account
        #    email.
        email = _auth.verify_session(db, token)
        if email is not None:
            if _accounts.get_role(db, email) is None:
                return (False, "account no longer exists")
            return (True, email)

        # 4) a Bearer that matches none of the above: unknown → (False, None)
        #    (401-class; mirrors 003's /status behavior).
        return (False, None)

    return authenticate
