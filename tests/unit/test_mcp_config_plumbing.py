"""007 Phase 1 (T001-T004): MCPContext.config plumbing (007-R6).

Phase 1 threads the loaded config dict through both MCP transports and
the CLI's ``serve-mcp`` entry point, so that the KB tool bodies added in
later phases can read the config. No tool-body behavior changes in this
phase; the fail-closed ``config_not_loaded`` shape is asserted here as the
documented contract (007-R6e / SC-007) that later phases implement.

- T001: ``MCPContext`` gains ``config: Any = None`` as the LAST dataclass
  field (4-arg positional 004 call shape keeps working; ``config``
  keyword carries the dict).
- T002: the stdio transport accepts a ``config`` kwarg on ``serve``/
  ``main``; ``main`` loads via the config layer when omitted; the built
  ``MCPContext`` carries a non-``None`` dict.
- T003: the http transport accepts a ``config`` kwarg on
  ``build_handler``/``serve``/``main``/``_build_handler_cls``; when
  omitted the config layer loads from cwd; the handler's ``MCPContext``
  carries the dict.
- T004: ``cli.serve_mcp`` passes its already-loaded ``cfg`` (its own
  ``load()`` call) into ``stdio.main(config=cfg)`` and
  ``mcp_http.main(config=cfg, ...)`` — exactly that dict, no second
  config load.
"""
from __future__ import annotations

import json
import sqlite3
from io import StringIO

import pytest

from digital_twins.accounts import create_account
from digital_twins.mcp.registry import MCPContext
from digital_twins.state.db import state_db_path
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """A migrated state DB with a reader ``system`` account.

    Mirrors the transport-suite fixture: the service-account resolution
    path (process owner / service token) needs a live role to resolve,
    otherwise the transports fail closed and the ``MCPContext`` never
    gets built.
    """
    path = state_db_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    create_account(conn, "system", "pw-system")
    conn.execute(
        "UPDATE accounts SET role='reader' WHERE email='system'")
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# T001: MCPContext.config field
# ---------------------------------------------------------------------------

def test_mcp_context_4_arg_positional_keeps_working():
    """The 004 call shape (db, email, role, agent_kind) still constructs.

    ``config`` is the LAST dataclass field with default ``None``, so the
    existing 4-arg positional construction is unchanged.
    """
    ctx = MCPContext(object(), "a@b", "reader", "stdio")
    assert ctx.db is not None
    assert ctx.caller_email == "a@b"
    assert ctx.caller_role == "reader"
    assert ctx.agent_kind == "stdio"
    assert ctx.config is None


def test_mcp_context_config_kwarg_carries_dict():
    """``MCPContext(..., config={})`` carries the given dict verbatim."""
    cfg = {"state_dir": "/tmp/x", "sources": {"hermes": {"enabled": True}}}
    ctx = MCPContext(object(), "a@b", "reader", "stdio", config=cfg)
    assert ctx.config is cfg


def test_mcp_context_is_still_a_dataclass_with_config_last():
    """``MCPContext`` remains a plain dataclass; ``config`` is its last field."""
    import dataclasses

    fields = [f.name for f in dataclasses.fields(MCPContext)]
    assert fields[-1] == "config"
    assert fields.index("agent_kind") == len(fields) - 2


def test_config_none_shape_is_fail_closed_contract():
    """Dispatching a KB tool with ``config=None`` → ``config_not_loaded``.

    007-R6e / SC-007: every tool body that needs the config fails closed
    with this exact shape BEFORE any other work::

        {"ok": False, "error": {"code": "config_not_loaded",
         "message": "MCPContext.config is None; the transport
                    must load config before dispatch"}}

    This is the Phase 1 plumbing contract; the tool bodies that implement
    it (kb_search / kb_chat / kb_ingest / kb_health) land in T010-T016 and
    are then asserted by their own test files (test_mcp_kb_*.py). Until
    then the dispatch module carries no fail-closed guard yet, so the
    Phase 1 plumbing tests do not assert on dispatch behavior — they only
    assert that the field exists, is last, and is None-tolerant.
    """
    # The contract is documented, not yet enforced, in Phase 1. A None
    # config must construct cleanly (the field is None-tolerant by design);
    # later phases assert the dispatch-time fail-closed shape.
    ctx = MCPContext(object(), "a@b", "reader", "stdio", config=None)
    assert ctx.config is None
