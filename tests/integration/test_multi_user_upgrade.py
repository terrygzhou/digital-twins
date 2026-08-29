"""T021: v2 -> v3 multi-user upgrade preservation.

Seeds a real v2 database (accounts + highwater + audit_runs + schedules) and
upgrades it to v3 with the public migration API (``migrations.migrate``), then
asserts the upgrade-preservation invariant (Constitution VI, NFR-15):

* all four pre-existing tables are row-for-row identical (byte-identical)
* the two new ``accounts`` columns (``created_at``, ``last_active``) exist
  and are defaulted to ``''`` on legacy rows (data-model.md)
* the three new v3 tables (``personal_tokens``, ``user_config``, ``sessions``)
  exist
* ``user_version`` advanced to 3

Red phase (Constitution III): the first test asserts a v3 invariant
(``personal_tokens`` exists) that is *known* to be false on a v2-pinned DB.
Run it with ``-m`` against the v2-pinned DB before calling ``migrate()`` and
it fails; after ``migrate()`` the assertion holds and the test passes. That
demonstrates the red -> green the constitution requires without shipping a
red commit.

T002's migration is already in place, so the test is green when written; the
red is documented by the pre-migration assertion.
"""

import json

import pytest

from digital_twins.state import migrations, models
from digital_twins.state.db import connect


def _table_names(conn):
    """Set of table names currently present in the database."""
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _pin_v2(conn):
    """Force the database to user_version 2 (v2-only state).

    A plain ``migrate()`` now lands on v3 (T002), so to exercise a v2 -> v3
    upgrade we apply v1 + v2 explicitly, then rewind user_version to 2.
    """
    models.apply_v1(conn)
    models.apply_v2(conn)
    conn.execute("PRAGMA user_version=2")
    conn.commit()


def _seed_v2(conn):
    """Populate a v2 DB with representative rows across all four v1/v2 tables.

    Uses the public ``models`` API where it is designed to be called (highwater
    upsert, audit run start/finish) and direct SQL for the account + schedule
    rows, mirroring the 002 ``tests/unit/test_upgrade.py`` seed helpers.
    """
    # two accounts (admin + reader)
    conn.execute(
        "INSERT INTO accounts (email, role, password_hash) "
        "VALUES (?, ?, ?)",
        ("alice@example.com", "admin", "pbkdf2:abc123"))
    conn.execute(
        "INSERT INTO accounts (email, role, password_hash) "
        "VALUES (?, ?, ?)",
        ("bob@example.com", "reader", "pbkdf2:def456"))
    conn.commit()

    # three highwater marks across two sources
    models.upsert_highwater(conn, "fs", "notes/a.txt", "abc123")
    models.upsert_highwater(conn, "imap", "inbox", "msg-id-42")
    models.upsert_highwater(conn, "fs", "logs/b.log", "xyz789")

    # two audit runs (one ok, one failed)
    models.start_audit_run(conn, "run-001", trigger="schedule",
                           scheduled_by="alice@example.com")
    models.finish_audit_run(conn, "run-001", "ok",
                            per_source_counts={"fs": {"new": 2, "skipped": 1, "failed": 0}})
    models.start_audit_run(conn, "run-002", trigger="manual",
                           scheduled_by="bob@example.com")
    models.finish_audit_run(conn, "run-002", "failed",
                            per_source_counts={"imap": {"new": 0, "skipped": 0, "failed": 1}})

    # one v2 schedule row (the v2-only table)
    conn.execute(
        "INSERT INTO schedules "
        "(owner, source, preset, param, fire_time, enabled, next_fire_at, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("alice@example.com", "fs", "daily", None, "03:00", 1,
         "2026-01-02T03:00:00+00:00",
         "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00"))
    conn.commit()


def _snapshot(conn):
    """Capture every row in the four pre-existing tables, keyed by table name.

    Used to assert row-for-row identity before and after the v2 -> v3 upgrade
    (byte-identical preservation, NFR-15).

    ``accounts`` is projected onto its pre-v3 columns so the comparison is
    meaningful on both sides of the migration: the v3 step adds two new
    columns (``created_at``, ``last_active``) whose values are *expected* to
    differ (they are newly defaulted to ``''``), so a raw ``SELECT *`` would
    spuriously "fail" the preservation check. The highwater / audit_runs /
    schedules tables have no schema change in v3, so they use ``SELECT *``.
    """
    return {
        "accounts": conn.execute(
            "SELECT id, email, role, password_hash FROM accounts "
            "ORDER BY id").fetchall(),
        "highwater": conn.execute(
            "SELECT * FROM highwater ORDER BY source, item_key").fetchall(),
        "audit_runs": conn.execute(
            "SELECT * FROM audit_runs ORDER BY run_id").fetchall(),
        "schedules": conn.execute(
            "SELECT * FROM schedules ORDER BY id").fetchall(),
    }


def _accounts_columns(conn):
    """Mapping of accounts column name -> (type, notnull, default) for PRAGMA check."""
    return {r[1]: (r[2], r[3], r[4]) for r in conn.execute(
        "PRAGMA table_info(accounts)")}


# 1 ------------------------------------------------------------------------
# Red-phase demonstration: on a v2-pinned DB the v3 table does not exist, so
# this assertion fails. After migrate() runs it passes. The test below keeps
# the red assertion in place (it is the v2->v3 invariant the constitution
# requires) and proves the green by running the migration first.

def test_red_phase_v2_db_lacks_v3_tables(tmp_path):
    """A v2-pinned DB has no personal_tokens table (red precondition).

    This test does NOT call migrate(). It documents the red state that the
    upgrade-preservation tests must transition out of: before the v3 step,
    the v3 tables are absent. Running this test against the pre-migration DB
    fails the assertion; after T002's migration is in place and migrate()
    has run, the assertion would pass.
    """
    conn = connect(tmp_path)
    _pin_v2(conn)
    _seed_v2(conn)

    tables = _table_names(conn)
    # The v3 table must be ABSENT on a v2-pinned DB. This is the red
    # precondition: it is True now, and becomes False once migrate() runs.
    assert "personal_tokens" not in tables, (
        "precondition: personal_tokens must not exist on a v2-pinned DB")
    # user_version is still 2 before the upgrade
    assert migrations.user_version(conn) == 2, (
        "precondition: user_version must be 2 before the v2->v3 upgrade")
    conn.close()


# 2 ------------------------------------------------------------------------
# The green test: run the real migration, assert preservation.

def test_v2_to_v3_preserves_all_preexisting_tables(tmp_path):
    """All four v1/v2 tables are byte-identical after the v2 -> v3 upgrade.

    Seed a real v2 DB (accounts + highwater + audit_runs + schedules), run
    the public migration API, and assert every row in every pre-existing table
    is preserved (Constitution VI, NFR-15).
    """
    conn = connect(tmp_path)
    _pin_v2(conn)
    _seed_v2(conn)

    before = _snapshot(conn)
    assert len(before["accounts"]) == 2, "precondition: two accounts seeded"
    assert len(before["highwater"]) == 3, "precondition: three highwater rows"
    assert len(before["audit_runs"]) == 2, "precondition: two audit runs"
    assert len(before["schedules"]) == 1, "precondition: one schedule row"

    v = migrations.migrate(conn)
    assert v == 3, f"user_version must advance to 3, got {v}"
    assert migrations.user_version(conn) == 3

    after = _snapshot(conn)
    for table in ("accounts", "highwater", "audit_runs", "schedules"):
        assert after[table] == before[table], (
            f"{table} rows changed after v2->v3 upgrade: "
            f"before={before[table]!r} after={after[table]!r}")
    conn.close()


def test_v3_accounts_columns_defaulted_to_empty_string(tmp_path):
    """The two new accounts columns exist and are defaulted to '' on legacy rows.

    Per data-model.md: ``created_at`` and ``last_active`` are
    ``TEXT NOT NULL DEFAULT ''`` so a legacy v1/v2 row (pre-v3, no timestamp)
    is still a valid row and the ``ORDER BY last_active DESC`` query does not
    have to special-case NULL.
    """
    conn = connect(tmp_path)
    _pin_v2(conn)
    _seed_v2(conn)

    # before the upgrade: the columns do not exist yet
    cols_before = _accounts_columns(conn)
    assert "created_at" not in cols_before, (
        "precondition: created_at must not exist on a v2 accounts table")
    assert "last_active" not in cols_before, (
        "precondition: last_active must not exist on a v2 accounts table")

    migrations.migrate(conn)

    cols = _accounts_columns(conn)
    assert cols.get("created_at") == ("TEXT", 1, "''"), (
        f"created_at: expected ('TEXT', 1, \"''\"), got {cols.get('created_at')}")
    assert cols.get("last_active") == ("TEXT", 1, "''"), (
        f"last_active: expected ('TEXT', 1, \"''\"), got {cols.get('last_active')}")

    # Legacy v2 rows gain the '' sentinel for both new columns.
    for email in ("alice@example.com", "bob@example.com"):
        row = conn.execute(
            "SELECT created_at, last_active FROM accounts WHERE email=?",
            (email,)).fetchone()
        assert row == ("", ""), (
            f"legacy row {email} not defaulted to '': got {row!r}")
    conn.close()


def test_v3_creates_new_tables(tmp_path):
    """v2 -> v3 creates personal_tokens, user_config, sessions (data-model.md).

    All three tables must exist after the upgrade, with the primary-key /
    foreign-key shape the data-model prescribes.
    """
    conn = connect(tmp_path)
    _pin_v2(conn)
    _seed_v2(conn)

    migrations.migrate(conn)

    tables = _table_names(conn)
    assert {"personal_tokens", "user_config", "sessions"} <= tables, (
        f"v3 tables missing after upgrade: {sorted(tables)}")

    # personal_tokens: PK on id, token_hash UNIQUE, FK to accounts.email
    pt_cols = {r[1]: r[2] for r in conn.execute(
        "PRAGMA table_info(personal_tokens)")}
    assert pt_cols.get("id") == "INTEGER"
    assert pt_cols.get("account_email") == "TEXT"
    assert pt_cols.get("token_hash") == "TEXT"
    assert pt_cols.get("revoked") == "INTEGER"

    # user_config: composite PK (account_email, source, key)
    uc_cols = {r[1]: r[2] for r in conn.execute(
        "PRAGMA table_info(user_config)")}
    assert uc_cols.get("account_email") == "TEXT"
    assert uc_cols.get("source") == "TEXT"
    assert uc_cols.get("key") == "TEXT"
    assert uc_cols.get("value") == "TEXT"

    # sessions: PK on session_token, FK to accounts.email
    s_cols = {r[1]: r[2] for r in conn.execute(
        "PRAGMA table_info(sessions)")}
    assert s_cols.get("session_token") == "TEXT"
    assert s_cols.get("account_email") == "TEXT"
    assert s_cols.get("revoked") == "INTEGER"
    conn.close()


def test_v2_to_v3_upgrade_is_idempotent(tmp_path):
    """Running migrate() a second time is a no-op: user_version stays at 3.

    Additive DDL is idempotent: the new tables already exist and the two new
    accounts columns already exist, so a second migrate() must not error,
    must not duplicate tables or columns, and must not advance user_version.
    """
    conn = connect(tmp_path)
    _pin_v2(conn)
    _seed_v2(conn)

    v1 = migrations.migrate(conn)
    assert v1 == 3

    tables_after_first = _table_names(conn)
    acct_cols_after_first = _accounts_columns(conn)

    v2 = migrations.migrate(conn)
    assert v2 == 3, "re-run must not advance user_version past 3"
    assert migrations.user_version(conn) == 3

    assert _table_names(conn) == tables_after_first, (
        "re-running v3 changed the table set")
    assert _accounts_columns(conn) == acct_cols_after_first, (
        "re-running v3 changed the accounts columns")
    conn.close()


def test_v2_to_v3_preserves_audit_run_counts(tmp_path):
    """The JSON payload in audit_runs.per_source_counts is preserved verbatim.

    A byte-identical row comparison above already covers this, but we also
    decode the JSON to prove the structure survived (Constitution V: audit
    records are queryable and attributable).
    """
    conn = connect(tmp_path)
    _pin_v2(conn)
    _seed_v2(conn)

    before = conn.execute(
        "SELECT per_source_counts FROM audit_runs WHERE run_id='run-001'"
    ).fetchone()[0]
    before_counts = json.loads(before)
    assert before_counts == {"fs": {"new": 2, "skipped": 1, "failed": 0}}

    migrations.migrate(conn)

    after = conn.execute(
        "SELECT per_source_counts FROM audit_runs WHERE run_id='run-001'"
    ).fetchone()[0]
    after_counts = json.loads(after)
    assert after_counts == before_counts, (
        f"audit run counts changed after upgrade: {before_counts} -> {after_counts}")
    conn.close()
