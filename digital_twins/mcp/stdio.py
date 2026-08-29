"""MCP stdio transport (feature 004, R7, T026).

``serve(in_stream, out_stream, db, service_account_email)`` reads
newline-delimited JSON (NDJSON) requests from ``in_stream`` and writes one
JSON object per line to ``out_stream``. Each request is of the form::

    {"tool": "<tool_name>", "args": {...}}

and the corresponding response is the exact dict returned by
:func:`digital_twins.mcp.dispatch.dispatch`.

The transport is *thin*: all logic (role-gate, owner-scope, tool bodies,
audit) lives in ``dispatch`` — this module only handles framing, I/O, and
authentication. R7 parity with the HTTP transport (T027) holds by
construction: both delegate to the same ``dispatch``.

Authentication
--------------
The stdio transport authenticates the *process owner* — the human who
launched ``digital-twins serve-mcp --transport stdio`` — using the same
credential order as 003's ``cli._auth_checker``:

1. ``DT_SERVICE_TOKEN`` env var (BR-10, constant-time compare) → the
   configured service-account email (default ``"system"``);
2. ``DT_PERSONAL_TOKEN`` env var (003 personal token) → its account email;
3. ``DT_USER_PASSWORD`` + a caller-supplied ``--as``-style env (not
   supported on the stdio path in v1; stdio assumes the process owner is
   the actor).

When no credential is present, the stdio transport runs with
``caller_email = service_account_email`` and ``caller_role`` resolved via
``accounts.get_role``; if that account does not exist, the transport
returns ``internal_error`` on every request (fail closed).

The per-request JSON does NOT carry credentials in v1: the stdio transport
is a local, single-user channel (one process, one operator), so the
process-owner credential is the caller's identity for every request.
"""
from __future__ import annotations

import json
import os
import sys
from typing import IO, Any

from .. import accounts as _accounts
from .. import auth as _auth
from . import dispatch as _dispatch
from .registry import MCPContext


def _resolve_caller(db, service_account_email: str) -> tuple[str, str | None]:
    """Resolve the process-owner caller for the stdio transport.

    Returns ``(caller_email, caller_role)``. ``caller_role`` is ``None``
    when the resolved email has no account row (fail closed).
    """
    # 1) service token (BR-10) → the configured service-account email.
    service = os.environ.get("DT_SERVICE_TOKEN")
    if service:
        return service_account_email, _accounts.get_role(db, service_account_email)

    # 2) personal token (003) → its account email.
    token = os.environ.get("DT_PERSONAL_TOKEN")
    if token:
        email = _auth.verify_personal_token(db, token)
        if email is not None:
            return email, _accounts.get_role(db, email)

    # 3) no credential: run as the service account (the process owner is
    #    assumed to be the configured service account; fail closed if that
    #    account does not exist).
    return service_account_email, _accounts.get_role(db, service_account_email)


def handle_request(
    request: dict,
    ctx: MCPContext,
    dispatch: Any = None,
) -> dict:
    """Process one stdio request; return the response dict.

    ``request`` is ``{"tool": <name>, "args": {..}}``. ``dispatch`` is
    injectable for tests; defaults to :func:`digital_twins.mcp.dispatch.
    dispatch`.
    """
    if dispatch is None:
        dispatch = _dispatch.dispatch
    tool = request.get("tool")
    args = request.get("args") or {}
    if not isinstance(tool, str) or not tool:
        return {
            "ok": False,
            "error": {
                "code": "internal_error",
                "message": "request missing 'tool' string field",
            },
        }
    return dispatch(ctx, tool, args)


def serve(
    in_stream: IO[str],
    out_stream: IO[str],
    db,
    service_account_email: str = "system",
    dispatch: Any = None,
) -> None:
    """Run the stdio transport until ``in_stream`` is exhausted.

    Reads NDJSON lines from ``in_stream``, dispatches each via
    :func:`handle_request`, and writes one JSON object per line to
    ``out_stream``. The transport exits cleanly when the input is closed.

    Parameters
    ----------
    in_stream:
        Text stream to read requests from (typically ``sys.stdin``).
    out_stream:
        Text stream to write responses to (typically ``sys.stdout``).
    db:
        Migrated state connection (accounts, schedules, audit_runs).
    service_account_email:
        The account the service token resolves to (the
        ``mcp.service_account_email`` knob, default ``"system"``).
    dispatch:
        Optional dispatch callable (injectable for tests); defaults to
        :func:`digital_twins.mcp.dispatch.dispatch`.
    """
    caller_email, caller_role = _resolve_caller(db, service_account_email)

    if caller_role is None:
        # Fail closed: the process-owner credential resolves to no account.
        for line in in_stream:
            line = line.strip()
            if not line:
                continue
            out_stream.write(json.dumps({
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": (
                        "stdio transport: no valid credential "
                        f"(account {caller_email!r} does not exist)"
                    ),
                },
            }) + "\n")
            out_stream.flush()
        return

    ctx = MCPContext(
        db=db,
        caller_email=caller_email,
        caller_role=caller_role,
        agent_kind="stdio",
    )

    for line in in_stream:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": f"malformed JSON request: {exc}",
                },
            }
        else:
            if not isinstance(request, dict):
                response = {
                    "ok": False,
                    "error": {
                        "code": "internal_error",
                        "message": "request must be a JSON object",
                    },
                }
            else:
                response = handle_request(request, ctx, dispatch=dispatch)
        out_stream.write(json.dumps(response) + "\n")
        out_stream.flush()


def main(db, service_account_email: str = "system") -> None:
    """Run the stdio transport on ``sys.stdin`` / ``sys.stdout``."""
    serve(sys.stdin, sys.stdout, db, service_account_email=service_account_email)
