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


# ---------------------------------------------------------------------------
# T002: stdio transport threads config (007-R6b)
# ---------------------------------------------------------------------------

def _run_stdio_once(db, config=None):
    """Run one stdio request through serve(); return the MCPContext seen
    by dispatch + whether the config layer's load() was called."""
    from digital_twins.mcp import stdio
    from digital_twins.mcp import dispatch as dispatch_module

    seen_ctx = []

    def fake_dispatch(ctx, tool_name, args):
        seen_ctx.append(ctx)
        return {"ok": True}

    monkeypatched = False
    import digital_twins.config.loader as loader
    real_load = loader.load
    loads = []

    def spy_load(*a, **kw):
        loads.append((a, kw))
        return {"state_dir": "/tmp/spy-cfg", "sources": {}}

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(dispatch_module, "dispatch", fake_dispatch, raising=False)
    mp.setattr(stdio, "dispatch", fake_dispatch, raising=False)
    mp.setattr(loader, "load", spy_load, raising=False)
    inp = StringIO(json.dumps({"tool": "kb_schedule_list", "args": {}}) + "\n")
    out = StringIO()
    try:
        if config is None:
            stdio.serve(inp, out, db, service_account_email="system")
        else:
            stdio.serve(inp, out, db, service_account_email="system",
                        config=config)
    finally:
        mp.undo()
    assert len(seen_ctx) == 1
    return seen_ctx[0], loads


def test_stdio_serve_threads_explicit_config(db):
    """stdio.serve(config=cfg) → the MCPContext carries that exact dict."""
    cfg = {"state_dir": "/tmp/explicit", "sources": {"hermes": {"enabled": True}}}
    ctx, loads = _run_stdio_once(db, config=cfg)
    assert ctx.config is cfg
    assert loads == []  # no load() when config is supplied


def test_stdio_serve_omitted_config_is_none(db):
    """stdio.serve() without config → MCPContext.config is None.

    The programmatic ``serve`` seam does not load config; the live
    transport (``main``) does. This is the documented 004 call shape
    for tests + the CLI.
    """
    ctx, loads = _run_stdio_once(db)
    assert ctx.config is None
    assert loads == []  # serve never loads; main does


def test_stdio_main_loads_config_when_omitted(db, monkeypatch):
    """stdio.main(db) loads config via digital_twins.config.loader.load().

    007-R6b: main() is the entry point that must load when the caller
    omits config. Assert via monkeypatched loader.load that main called
    it exactly once with no args (the cwd default).
    """
    from digital_twins.mcp import stdio
    import digital_twins.config.loader as loader

    loads = []
    monkeypatch.setattr(
        loader, "load",
        lambda *a, **kw: loads.append((a, kw)) or {"state_dir": "/tmp/x"},
        raising=False)
    # main() calls serve(sys.stdin, sys.stdout, ...); feed it an empty
    # stdin (immediate EOF) so serve returns without blocking.
    monkeypatch.setattr(stdio, "sys",
                        type("FakeIO", (), {"stdin": StringIO(""),
                                            "stdout": StringIO()}))
    stdio.main(db)
    assert len(loads) == 1
    a, kw = loads[0]
    assert a == () and kw == {}  # load() with the cwd default


def test_stdio_main_threads_explicit_config(db, monkeypatch):
    """stdio.main(db, config=cfg) → serve receives that dict; no load()."""
    from digital_twins.mcp import stdio
    import digital_twins.config.loader as loader

    loads = []
    monkeypatch.setattr(
        loader, "load",
        lambda *a, **kw: loads.append(1) or {"state_dir": "/tmp/x"},
        raising=False)
    seen = []

    def fake_serve(in_stream, out_stream, db_, *, service_account_email="system",
                   dispatch=None, config=None):
        seen.append(config)
        # Drain in_stream so the real loop shape is honored.
        for _ in in_stream:
            pass

    monkeypatch.setattr(stdio, "serve", fake_serve)
    monkeypatch.setattr(stdio, "sys",
                        type("FakeIO", (), {"stdin": StringIO(""),
                                            "stdout": StringIO()}))
    cfg = {"state_dir": "/tmp/main-cfg"}
    stdio.main(db, config=cfg)
    assert seen == [cfg]
    assert loads == []  # no load() when config is supplied


# ---------------------------------------------------------------------------
# T003: http transport threads config (007-R6c)
#
# Design (mirrors T002): the config load happens in ``main`` (the CLI entry
# point), not in the programmatic ``build_handler``/``serve``/``_build_handler_cls``
# seam. Those accept a ``config`` kwarg and thread it into the handler's
# ``MCPContext``. The live transport (``main``) always loads via the config
# layer first so a live server never dispatches with a ``None`` config.
# ---------------------------------------------------------------------------

def _http_post_to_handler(db, token, handler_builder):
    """POST one JSON request to a live ThreadingHTTPServer; return the
    parsed response body + the MCPContext seen by dispatch.

    ``handler_builder`` is a zero-arg callable that returns the handler
    class. It is called AFTER the dispatch monkeypatch is in place, so
    the Handler's closure captures the fake dispatch.
    """
    import threading
    import http.client
    from http.server import ThreadingHTTPServer
    from digital_twins.mcp import dispatch as dispatch_module

    seen_ctx = []

    def fake_dispatch(ctx, tool_name, args):
        seen_ctx.append(ctx)
        return {"ok": True}

    import digital_twins.mcp.http as http_mod
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(dispatch_module, "dispatch", fake_dispatch, raising=False)
    mp.setattr(http_mod, "dispatch", fake_dispatch, raising=False)
    handler_cls = handler_builder()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    host, port = server.server_address[:2]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        payload = json.dumps({"tool": "kb_schedule_list", "args": {}}).encode()
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/mcp", body=payload,
                     headers={"Content-Type": "application/json",
                              "Authorization": f"Bearer {token}"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2)
        mp.undo()
    assert len(seen_ctx) == 1
    return json.loads(body), seen_ctx[0]


def _service_token_for(db, monkeypatch):
    """Create a service account + return a usable Bearer token."""
    from digital_twins.accounts import create_account
    # Ensure a system account exists (the fixture already creates one as
    # reader, so the service-token path resolves).
    token = "test-http-service-token"
    monkeypatch.setenv("DT_SERVICE_TOKEN", token)
    return token


def test_http_build_handler_threads_explicit_config(db, monkeypatch):
    """http.build_handler(config=cfg) → the handler's MCPContext carries
    that exact dict; no config load in the builder."""
    from digital_twins.mcp import http
    cfg = {"state_dir": "/tmp/explicit",
           "sources": {"hermes": {"enabled": True}}}
    token = _service_token_for(db, monkeypatch)
    body, ctx = _http_post_to_handler(
        db, token,
        lambda: http.build_handler(db, service_account_email="system",
                                   config=cfg))
    assert body["ok"] is True
    assert ctx.config is cfg


def test_http_build_handler_omitted_config_is_none(db, monkeypatch):
    """http.build_handler() without config → MCPContext.config is None.

    The programmatic seam does not load config; the live transport
    (``main``) does.
    """
    from digital_twins.mcp import http
    token = _service_token_for(db, monkeypatch)
    body, ctx = _http_post_to_handler(
        db, token,
        lambda: http.build_handler(db, service_account_email="system"))
    assert body["ok"] is True
    assert ctx.config is None


def test_http_serve_threads_explicit_config(db, monkeypatch):
    """http.serve(config=cfg) → the handler is built with that config.

    Assert via monkeypatched ``_build_handler_cls`` that serve() passes
    the config through. (We do not run a live server here; the handler
    wiring is covered by the ``build_handler`` tests above.)
    """
    from digital_twins.mcp import http
    cfg = {"state_dir": "/tmp/serve-cfg"}
    seen = []
    real_builder = http._build_handler_cls

    def spy_builder(db_, service_account_email, dispatch=None, config=None):
        seen.append(config)
        return real_builder(db_, service_account_email, dispatch,
                            config=config)

    monkeypatch.setattr(http, "_build_handler_cls", spy_builder)
    server = http.serve(db, service_account_email="system", config=cfg)
    try:
        # The server is a ThreadingHTTPServer; we don't run it, we just
        # verify the handler was built with the right config.
        assert seen == [cfg]
    finally:
        server.server_close()


def test_http_main_loads_config_when_omitted(db, monkeypatch):
    """http.main(db) loads config via digital_twins.config.loader.load().

    007-R6c: main() is the entry point that must load when the caller
    omits config. Assert via monkeypatched loader.load that main called
    it exactly once with no args (the cwd default).
    """
    from digital_twins.mcp import http
    import digital_twins.config.loader as loader

    loads = []
    monkeypatch.setattr(
        loader, "load",
        lambda *a, **kw: loads.append((a, kw)) or {"state_dir": "/tmp/x"},
        raising=False)
    # main() calls serve(...).serve_forever(); monkeypatch serve_forever
    # to return immediately so main() completes without blocking.
    class _FakeServer:
        RequestHandlerClass = None
        def serve_forever(self):
            pass
        def server_close(self):
            pass
    built = []
    monkeypatch.setattr(
        http, "serve",
        lambda db_, **kw: built.append(kw) or _FakeServer())
    http.main(db)
    assert len(loads) == 1
    a, kw = loads[0]
    assert a == () and kw == {}  # load() with the cwd default
    assert built and built[0].get("config") == {"state_dir": "/tmp/x"}


def test_http_main_threads_explicit_config(db, monkeypatch):
    """http.main(db, config=cfg) → serve receives that dict; no load()."""
    from digital_twins.mcp import http
    import digital_twins.config.loader as loader

    loads = []
    monkeypatch.setattr(
        loader, "load",
        lambda *a, **kw: loads.append(1) or {"state_dir": "/tmp/x"},
        raising=False)
    class _FakeServer:
        RequestHandlerClass = None
        def serve_forever(self):
            pass
        def server_close(self):
            pass
    built = []
    monkeypatch.setattr(
        http, "serve",
        lambda db_, **kw: built.append(kw) or _FakeServer())
    cfg = {"state_dir": "/tmp/main-cfg"}
    http.main(db, config=cfg)
    assert built and built[0].get("config") is cfg
    assert loads == []  # no load() when config is supplied


def test_http_build_handler_cls_threads_explicit_config(db, monkeypatch):
    """http._build_handler_cls(config=cfg) → the handler's MCPContext
    carries that exact dict."""
    from digital_twins.mcp import http
    cfg = {"state_dir": "/tmp/cls-cfg"}
    token = _service_token_for(db, monkeypatch)
    body, ctx = _http_post_to_handler(
        db, token,
        lambda: http._build_handler_cls(db, "system", config=cfg))
    assert body["ok"] is True
    assert ctx.config is cfg


# ---------------------------------------------------------------------------
# T004: cli.serve_mcp passes its loaded cfg to both transports (007-R6d)
#
# Design: cli.serve_mcp loads the config once (line 596's ``load()``) and
# passes that SAME dict into both ``stdio.main(config=cfg)`` and
# ``mcp_http.main(config=cfg, ...)``. No second config load. The test
# asserts via monkeypatched ``stdio.main`` / ``mcp_http.main`` that the
# ``config`` kwarg is exactly the dict returned by the CLI's own
# ``load()`` call.
# ---------------------------------------------------------------------------

def _serve_mcp_invoke(monkeypatch, transport, captured):
    """Invoke cli.serve_mcp with the given transport; capture the config
    kwarg passed to the transport's main(). Return the captured config."""
    from click.testing import CliRunner
    from digital_twins.cli import cli as cli_group
    import digital_twins.config.loader as loader
    from digital_twins.mcp import stdio as stdio_mod
    from digital_twins.mcp import http as http_mod

    # Sentinel config dict that the CLI's load() will return. Enriched
    # with the keys serve_mcp actually reads (state_dir, mcp.port,
    # mcp.service_account_email) so the CLI path past connect/migrate
    # succeeds before reaching the (monkeypatched) transport main.
    sentinel_cfg = {"state_dir": "/tmp/cli-cfg",
                    "mcp": {"port": 8770,
                            "service_account_email": "system"},
                    "sources": {"hermes": {"enabled": True}}}
    monkeypatch.setattr(
        loader, "load", lambda *a, **kw: sentinel_cfg, raising=False)
    # cli.py imports ``load`` directly (from digital_twins.config import load),
    # so we also patch that binding.
    import digital_twins.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load", lambda: sentinel_cfg,
                        raising=False)

    if transport == "stdio":
        def fake_stdio_main(db_, service_account_email="system",
                            config=None):
            captured.append(("stdio", config))
        monkeypatch.setattr(stdio_mod, "main", fake_stdio_main,
                            raising=False)
    else:  # http
        def fake_http_main(db_, service_account_email="system",
                           port=8770, config=None):
            captured.append(("http", config, port))
        monkeypatch.setattr(http_mod, "main", fake_http_main,
                            raising=False)

    runner = CliRunner()
    res = runner.invoke(cli_group,
                        ["serve-mcp", "--transport", transport])
    return res, sentinel_cfg


def test_serve_mcp_stdio_passes_loaded_cfg(monkeypatch):
    """cli.serve_mcp --transport stdio passes its loaded cfg into
    stdio.main(config=cfg) — the SAME dict, no second load."""
    captured = []
    res, sentinel_cfg = _serve_mcp_invoke(monkeypatch, "stdio", captured)
    assert res.exit_code == 0, res.output
    assert len(captured) == 1
    kind, config = captured[0]
    assert kind == "stdio"
    assert config is sentinel_cfg  # identity, not equality


def test_serve_mcp_http_passes_loaded_cfg(monkeypatch):
    """cli.serve_mcp --transport http passes its loaded cfg into
    mcp_http.main(config=cfg, ...) — the SAME dict, no second load."""
    captured = []
    res, sentinel_cfg = _serve_mcp_invoke(monkeypatch, "http", captured)
    assert res.exit_code == 0, res.output
    assert len(captured) == 1
    kind, config, port = captured[0]
    assert kind == "http"
    assert config is sentinel_cfg  # identity, not equality
    assert port is not None
