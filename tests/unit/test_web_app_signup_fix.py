"""Web signup/sign-in fix: static-DB busy timeout + UI failure surfacing.

Root cause of "web sign up not working": a CLI command (setup's
docker-compose up + health checks, ``signup``, ``run``) can hold the
SQLite WAL write lock for tens of seconds; the web server's own state
DB connection used the stdlib default 5000 ms busy timeout, so a
signup / sign-in landing inside that window died with
``500 {"error": "internal_error"}`` ("database is locked").  The UI
then looked dead because ``doSignin`` / ``doSignup`` had no
``.catch`` — a fetch-level failure never reached the error element.

Fixes pinned here (no socket harness: the web-harness tests ERROR in
this sandbox because ``WebApp.__init__`` binds a real socket,
which the sandbox forbids):

1. ``web/app.py`` — ``_open_same_db`` sets
   ``PRAGMA busy_timeout=60000`` on the fresh file-backed
   connection so the web side queues behind a CLI lock holder
   instead of erroring after 5 s (module constant
   ``DB_BUSY_TIMEOUT_MS``; NOT a config knob — T027's knob surface
   is closed, and the guard battery pins that).
2. ``web/static/index.html`` — ``doSignin`` and ``doSignup`` chain a
   ``.catch`` that routes a fetch-level failure to the explicit
   ``showError(..., null)`` "could not reach the server" message, so
   the forms never look dead.

These are static-asset / module-level assertions, in the same
shape as ``test_web_channels_ui.py`` (read the shipped files
directly).
"""
from __future__ import annotations

import re
from pathlib import Path

import digital_twins.web.app as web_app

_STATIC_DIR = Path(__file__).resolve().parents[2] / "digital_twins" / "web" / "static"


def _index_html() -> str:
    return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


# --- 1. busy timeout on the web server's own state-DB connection -----------


def test_open_same_db_sets_long_busy_timeout(tmp_path):
    """The file-backed connection opened by _open_same_db queues behind a
    CLI lock holder (busy_timeout 60 s) instead of erroring after 5 s."""
    import sqlite3

    from digital_twins.state.db import connect
    from digital_twins.state.migrations import migrate

    db = connect(tmp_path)
    migrate(db)
    try:
        new = web_app._open_same_db(db, check_same_thread=False)
        assert new is not db
        busy = new.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy == web_app.DB_BUSY_TIMEOUT_MS, (
            f"web conn busy_timeout={busy}, expected {web_app.DB_BUSY_TIMEOUT_MS}"
        )
        # Generous: comfortably above any setup/docker-compose window
        # (tens of seconds) so a signup in that window queues, not 500s.
        assert web_app.DB_BUSY_TIMEOUT_MS >= 30000
        new.close()
    finally:
        db.close()


def test_open_same_db_memory_db_unaffected(tmp_path):
    """In-memory DBs (test doubles) have no cross-process lock: the
    original connection is returned as-is, no busy_timeout rewrite."""
    import sqlite3

    mem = sqlite3.connect(":memory:")
    memret = web_app._open_same_db(mem)
    assert memret is mem
    mem.close()


def test_busy_timeout_is_module_constant_not_a_knob():
    """T027: the knob surface is closed. The timeout must NOT be a
    config knob (would trip test_knob_docs)."""
    assert hasattr(web_app, "DB_BUSY_TIMEOUT_MS")
    # It must be a plain int constant, not something read from config.
    assert isinstance(web_app.DB_BUSY_TIMEOUT_MS, int)


# --- 2. UI: fetch-level failures surface, forms never look dead ------------


def test_do_signin_has_catch_surface():
    """doSignin chains a .catch that routes a fetch-level failure to
    the explicit error message (the form never looks dead)."""
    html = _index_html()
    m = re.search(r"function doSignin\(event\).*?\n    \}", html, re.S)
    assert m, "doSignin not found in index.html"
    body = m.group(0)
    # The .catch must be inside the doSignin fetch chain, routing to
    # the signin-error element via the explicit-message path (null res).
    assert ".catch(" in body, "doSignin has no .catch — form looks dead on network error"
    assert ".catch(function () {" in body, \
        "doSignin must chain a .catch(...) on the fetch chain"
    assert "showError(\"signin-error\", null)" in body, \
        "doSignin catch must route to the explicit 'cannot reach server' message"


def test_do_signup_has_catch_surface():
    """doSignup chains a .catch that routes a fetch-level failure to
    the signup-error element (the form never looks dead)."""
    html = _index_html()
    m = re.search(r"function doSignup\(event\).*?\n    \}", html, re.S)
    assert m, "doSignup not found in index.html"
    body = m.group(0)
    assert ".catch(" in body, "doSignup has no .catch — form looks dead on network error"
    assert ".catch(function () {" in body, \
        "doSignup must chain a .catch(...) on the fetch chain"
    assert "showError(\"signup-error\", null)" in body, \
        "doSignup catch must route to the explicit 'cannot reach server' message"
