"""MCP HTTP transport (feature 004, R7, T027).

A minimal HTTP server that accepts ``POST /mcp`` with a JSON body::

    {"tool": "<tool_name>", "args": {...}}

and returns the exact dict returned by
:func:`digital_twins.mcp.dispatch.dispatch` as the JSON response.

The transport is *thin*: all logic (role-gate, owner-scope, tool bodies,
audit) lives in ``dispatch`` — this module only handles HTTP framing,
authentication, and I/O. R7 parity with the stdio transport (T026) holds
by construction: both delegate to the same ``dispatch``.

Authentication
--------------
Per-request, via the ``Authorization: Bearer <token>`` header, using
:func:`digital_twins.mcp.auth.mcp_authenticator` (the same credential
order as 003's ``cli._auth_checker``): service token → personal token →
session token. An unknown / missing credential → HTTP 401; a
known-but-dead credential → HTTP 403 (fail closed, mirroring the
``(False, <reason>)`` authenticator return).

The ``MCPContext.agent_kind`` is set to ``"http"`` so the audit row's
``per_source_counts`` can attribute the request to the HTTP transport.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .. import accounts as _accounts
from . import dispatch as _dispatch
from .auth import mcp_authenticator
from .registry import MCPContext


# ---------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------

def _build_handler_cls(db, service_account_email: str,
                       dispatch: Any = None) -> type:
    """Build a ``BaseHTTPRequestHandler`` subclass wired to ``db`` + ``dispatch``.

    ``dispatch`` is injectable for tests; defaults to
    :func:`digital_twins.mcp.dispatch.dispatch`. The returned class is
    suitable for ``ThreadingHTTPServer``.
    """
    if dispatch is None:
        dispatch = _dispatch.dispatch
    authenticator = mcp_authenticator(db, service_account_email)

    class Handler(BaseHTTPRequestHandler):
        # Silence the default access log (tests do not want it on stderr).
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            pass

        # --- HTTP methods ------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            """GET → 405 (this server only accepts POST /mcp in v1).

            SSE / streaming is out of scope for the 004 transport slice
            (research D2: v1 is request/response JSON; a follow-up slice
            adds the SSE endpoint).
            """
            self._send(405, {"ok": False,
                             "error": {"code": "method_not_allowed",
                                       "message": "use POST /mcp"}})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/mcp":
                self._send(404, {"ok": False,
                                 "error": {"code": "not_found",
                                           "message": f"unknown path: {self.path}"}})
                return

            # --- auth gate (mcp_authenticator) ---------------------------
            ok, email_or_reason = authenticator(self.headers)
            if not ok:
                # 401 for an unknown / missing credential (email_or_reason
                # is None); 403 for a known-but-dead credential (a reason
                # string is present).
                status = 401 if email_or_reason is None else 403
                reason = email_or_reason or "authentication failed"
                self._send(status, {"ok": False,
                                    "error": {"code": "unauthorized" if status == 401
                                              else "forbidden",
                                              "message": reason}})
                return

            # --- read the JSON body --------------------------------------
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                request = json.loads(raw.decode("utf-8")) if raw else None
            except json.JSONDecodeError as exc:
                self._send(400, {"ok": False,
                                 "error": {"code": "bad_request",
                                           "message": f"malformed JSON body: {exc}"}})
                return
            if not isinstance(request, dict):
                self._send(400, {"ok": False,
                                 "error": {"code": "bad_request",
                                           "message": "body must be a JSON object"}})
                return

            # --- resolve the caller's role + dispatch ---------------------
            caller_role = _accounts.get_role(db, email_or_reason)
            if caller_role is None:
                # The authenticator said the credential was accepted but the
                # account no longer exists (a race: account deleted between
                # token verify and role resolve). Fail closed → 403.
                self._send(403, {"ok": False,
                                 "error": {"code": "forbidden",
                                           "message": "account no longer exists"}})
                return

            ctx = MCPContext(
                db=db,
                caller_email=email_or_reason,
                caller_role=caller_role,
                agent_kind="http",
            )
            response = dispatch(ctx, request.get("tool", ""),
                                request.get("args") or {})
            self._send(200, response)

        # --- response writer --------------------------------------------
        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def build_handler(db, service_account_email: str = "system",
                  dispatch: Any = None) -> type:
    """Public builder: return the request-handler class for ``db``.

    This is the seam the tests + the CLI use: the handler is constructed
    against the open state DB and the dispatch callable, and ready to be
    handed to a ``ThreadingHTTPServer``.
    """
    return _build_handler_cls(db, service_account_email, dispatch)


# ---------------------------------------------------------------------------
# server entry point
# ---------------------------------------------------------------------------

def serve(db, host: str = "127.0.0.1", port: int = 8770,
          service_account_email: str = "system",
          dispatch: Any = None) -> ThreadingHTTPServer:
    """Start the HTTP MCP server on ``(host, port)``.

    Returns the ``ThreadingHTTPServer`` (call ``.serve_forever()`` to run;
    the CLI's ``serve-mcp`` does this). ``dispatch`` is injectable for
    tests; defaults to :func:`digital_twins.mcp.dispatch.dispatch`.
    """
    handler = _build_handler_cls(db, service_account_email, dispatch)
    server = ThreadingHTTPServer((host, port), handler)
    return server


def main(db, service_account_email: str = "system", port: int = 8770) -> None:
    """Run the HTTP MCP server until interrupted."""
    server = serve(db, port=port,
                   service_account_email=service_account_email)
    try:
        server.serve_forever()
    finally:
        server.server_close()
