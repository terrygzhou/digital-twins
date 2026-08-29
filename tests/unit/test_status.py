"""T017 — /status endpoint: status_payload + ThreadingHTTPServer handler.

RED-first per the task brief. These tests pin the 002 contract shape for
``status_payload`` (contracts/scheduler.md) and the HTTP surface
(GET /status -> 200 JSON, 404 otherwise, Content-Type application/json,
SC-004 wall-clock < 500 ms on a real request).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from digital_twins.scheduler import status
from digital_twins.scheduler.status import StatusServer, status_payload
from digital_twins.state import migrations


# --- helpers ---------------------------------------------------------------


def _make_db(tmp_path, *, check_same_thread: bool = False):
    """Migrated db (v2) ready for seeding schedules + audit rows.

    ``check_same_thread=False`` (the default) lets the StatusServer's handler
    thread — a different thread from the test's main thread — use the
    connection, which the HTTP tests require. Payload-builder tests run in a
    single thread, so the default is harmless there too.
    """
    path = tmp_path / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrations.migrate(conn)
    return conn


def _seed_schedule(db, owner="alice@example.com", source="fs",
                   preset="daily", param=None, fire_time="03:00"):
    db.execute(
        "INSERT INTO schedules "
        "(owner, source, preset, param, fire_time, enabled, next_fire_at, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (owner, source, preset, param, fire_time,
         "2026-01-02T03:00:00+00:00", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00"),
    )
    db.commit()


def _seed_audit_run(db, run_id, started_at, completed_at, status_value,
                    trigger="manual", scheduled_by="system",
                    per_source_counts=None):
    db.execute(
        "INSERT INTO audit_runs "
        "(run_id, started_at, completed_at, status, trigger, scheduled_by, "
        " per_source_counts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, started_at, completed_at, status_value, trigger,
         scheduled_by,
         json.dumps(per_source_counts) if per_source_counts is not None
         else None),
    )
    db.commit()


_CONFIG = {"state_dir": "/tmp/unused", "sources": {}}


# 1 — payload shape (migrated db + seeded schedule + seeded audit row) ------


def test_status_payload_shape(tmp_path):
    """Five top-level keys; schedules carries the schedule's fields;
    last_run matches the seeded audit row; queue_depth == 0."""
    db = _make_db(tmp_path)
    _seed_schedule(db, owner="alice@example.com", source="fs", preset="daily",
                   fire_time="03:00")
    _seed_audit_run(db, "run-1", "2026-01-01T04:00:00+00:00",
                    "2026-01-01T04:01:00+00:00", "ok", trigger="schedule",
                    scheduled_by="alice@example.com",
                    per_source_counts={"fs": 3})

    payload = status_payload(db, _CONFIG, start_time=1000.0)

    assert isinstance(payload, dict)
    assert set(payload) == {
        "uptime_s", "schedules", "last_run", "queue_depth",
    }, f"top-level keys: {sorted(payload)}"

    # uptime_s: time.time() - start_time (float, non-negative)
    assert isinstance(payload["uptime_s"], float)
    assert payload["uptime_s"] >= 0.0

    # schedules: one row, mapped to the payload subset
    assert isinstance(payload["schedules"], list) and len(payload["schedules"]) == 1
    sch = payload["schedules"][0]
    expected_keys = {"id", "owner", "source", "preset", "fire_time",
                     "next_fire_at", "enabled"}
    assert expected_keys.issubset(set(sch)), f"schedule keys: {sorted(sch)}"
    assert sch["owner"] == "alice@example.com"
    assert sch["source"] == "fs"
    assert sch["preset"] == "daily"
    assert sch["fire_time"] == "03:00"
    assert sch["next_fire_at"] == "2026-01-02T03:00:00+00:00"
    assert sch["enabled"] in (0, 1) and sch["enabled"] == 1

    # last_run: matches the seeded audit row
    last = payload["last_run"]
    assert last is not None
    expected_last_keys = {"run_id", "started_at", "completed_at", "status",
                          "trigger", "scheduled_by", "source",
                          "per_source_counts"}
    assert expected_last_keys.issubset(set(last)), f"last_run keys: {sorted(last)}"
    assert last["run_id"] == "run-1"
    assert last["started_at"] == "2026-01-01T04:00:00+00:00"
    assert last["completed_at"] == "2026-01-01T04:01:00+00:00"
    assert last["status"] == "ok"
    assert last["trigger"] == "schedule"
    assert last["scheduled_by"] == "alice@example.com"
    # per_source_counts is stored as a JSON TEXT column; the payload decodes it.
    assert last["per_source_counts"] == {"fs": 3}

    # queue_depth: 0 in v1 (single-fire-per-tick-per-schedule)
    assert payload["queue_depth"] == 0
    db.close()


# 2 — empty db: no schedules, no runs ----------------------------------------


def test_status_payload_no_schedules_no_runs(tmp_path):
    """Migrated but empty db -> schedules == [], last_run is None,
    queue_depth == 0."""
    db = _make_db(tmp_path)
    payload = status_payload(db, _CONFIG, start_time=1000.0)
    assert payload["schedules"] == []
    assert payload["last_run"] is None
    assert payload["queue_depth"] == 0
    assert isinstance(payload["uptime_s"], float) and payload["uptime_s"] >= 0.0
    db.close()


# 3 — last_run is the most recent by started_at --------------------------------


def test_status_payload_last_run_most_recent(tmp_path):
    """Two audit rows -> last_run is the one with the later started_at."""
    db = _make_db(tmp_path)
    _seed_audit_run(db, "run-older", "2026-01-01T03:00:00+00:00",
                    "2026-01-01T03:01:00+00:00", "ok")
    _seed_audit_run(db, "run-newer", "2026-01-01T05:00:00+00:00",
                    "2026-01-01T05:01:00+00:00", "partial")
    payload = status_payload(db, _CONFIG, start_time=1000.0)
    assert payload["last_run"] is not None
    assert payload["last_run"]["run_id"] == "run-newer", (
        f"expected the later row, got {payload['last_run']['run_id']!r}")
    db.close()


# 4 — HTTP handler: real GET /status (SC-004) ---------------------------------


def _wait_server_ready(server, timeout=5.0):
    """Poll /status until the server accepts connections."""
    host, port = server.server_address[:2]
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(
                f"http://{host}:{port}/status", timeout=1.0
            ) as resp:
                resp.read()
            return
        except urllib.error.HTTPError:
            return  # server answered; any HTTP code means it is up
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_http_handler_serves_status(tmp_path):
    """Real ThreadingHTTPServer on port 0 (ephemeral): GET /status -> 200,
    Content-Type application/json, body parses to the payload shape, and the
    wall-clock elapsed time is < 0.5 s (SC-004)."""
    db = _make_db(tmp_path)
    _seed_schedule(db, owner="bob@example.com", source="hermes", preset="hourly")
    _seed_audit_run(db, "run-srv", "2026-01-02T01:00:00+00:00",
                    "2026-01-02T01:00:30+00:00", "ok", trigger="schedule",
                    scheduled_by="bob@example.com",
                    per_source_counts={"hermes": 1})

    server = StatusServer(("127.0.0.1", 0), db, _CONFIG)
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        host, port = server.server_address[:2]
        start = time.monotonic()
        with urllib.request.urlopen(
            f"http://{host}:{port}/status", timeout=5.0
        ) as resp:
            elapsed = time.monotonic() - start
            assert resp.status == 200
            content_type = resp.headers.get("Content-Type", "")
            assert content_type.startswith("application/json"), content_type
            body = json.loads(resp.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        db.close()

    assert set(body) == {"uptime_s", "schedules", "last_run", "queue_depth"}
    assert len(body["schedules"]) == 1
    assert body["schedules"][0]["source"] == "hermes"
    assert body["last_run"]["run_id"] == "run-srv"
    assert body["queue_depth"] == 0
    # SC-004: wall-clock < 500 ms
    assert elapsed < 0.5, f"SC-004 violated: /status took {elapsed * 1000:.1f} ms"


# 5 — unknown path -> 404 ------------------------------------------------------


def test_http_handler_unknown_path_404(tmp_path):
    """GET /nonexistent -> HTTP 404."""
    db = _make_db(tmp_path)
    server = StatusServer(("127.0.0.1", 0), db, _CONFIG)
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _wait_server_ready(server)

        host, port = server.server_address[:2]
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(
                f"http://{host}:{port}/nonexistent", timeout=5.0)
        assert exc_info.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        db.close()
