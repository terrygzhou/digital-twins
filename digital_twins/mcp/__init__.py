"""MCP (Model Context Protocol) scheduler tools (feature 004).

Phase 1 (T003/T004) introduces the credential gate
:func:`digital_twins.mcp.auth.mcp_authenticator` — the MCP analogue of
003's ``cli._auth_checker``, returning ``(bool, email | None)`` so the
MCP executor (T010) can resolve the caller's role via
``accounts.get_role``.  Later phases add the tool registry, stdio /
HTTP transports, and the ``serve-mcp`` CLI surface.
"""
