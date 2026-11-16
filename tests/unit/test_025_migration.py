"""Red tests for 025 multi-tenant: apply_v6 migration (task-9, B1–B4).

Covers ``digital_twins.state.models.apply_v6`` + the ``(6, apply_v6)``
registry entry in ``digital_twins.state.migrations``:

- A v4 state DB (``user_version`` 4) migrates to 6 in one step;
  ``migrate()`` is idempotent on an already-6 DB.
- ``tenants`` table created; the install tenant is seeded by the
  migration itself (id=1, slug="install", status=active).
- ``accounts`` gains ``tenant_id`` NOT NULL DEFAULT 1 → legacy rows
  carry the install tenant.
- ``highwater`` / ``ingest_locks`` gain the tenant-partitioned primary
  key via in-place table swap; legacy rows copied with ``tenant_id=1``;
  a duplicate ``(source, item_key)`` within one tenant is rejected,
  while the same key under a second tenant is allowed.
- ``audit_runs`` / ``audit_items`` gain ``tenant_id`` (additive).
- ``tenant_service_tokens`` created (per-tenant analogue of the shared
  system token; FK cascade to ``tenants``).
- ``tenant_config`` is NOT created (T4: no per-tenant endpoint
  overrides in v1).
- All legacy rows survive the migration (additive-only, NFR-15).
"""

import pytest
import sqlite3

from digital_twins.state.migrations import SCHEMA_VERSION, migrate
from digital_twins.state.models import apply_v6


@pytest.fixture()
def db_v4(tmp_path):
    """A state DB at user_version 4 with legacy rows in every affected
    table — the 025 pre-migration state of a live install."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    # Build the v4 schema directly (no migrate() call — we want v4,
    # not SCHEMA_VERSION=6, so seed rows don't hit the v6 PK).
    import digital_twins.state.models as models
    for version in ("DDL_V1", "DDL_V2", "DDL_V3", "DDL_V4"):
        conn.executescript(getattr(models, version))
    conn.execute("PRAGMA user_version=4")
    # Seed legacy rows in every table apply_v6 must preserve.
    conn.executescript(
        """
        INSERT INTO highwater (source, item_key, last_key, updated_at)
        VALUES ('fs', 'item-1', 'item-1', '2026-01-01T00:00:00');
        INSERT INTO ingest_locks (source, run_id, acquired_at)
        VALUES ('fs', 'run-x', '2026-01-01T00:00:00');
        INSERT INTO audit_runs (run_id, started_at, trigger, status,
                                completed_at, scheduled_by)
        VALUES ('run-1', '2026-01-01T00:00:00', 'manual', 'ok',
                '2026-01-01T00:00:00', 'system');
        INSERT INTO audit_items (run_id, source, item_key, status, ts)
        VALUES ('run-1', 'fs', 'item-1', 'ok', '2026-01-01T00:00:00');
        INSERT INTO accounts (email, role, password_hash,
                              created_at, last_active)
        VALUES ('admin@example.com', 'admin', 'h', '2026-01-01T00:00:00',
                '2026-01-01T00:00:00');
        """
    )
    conn.commit()
    yield conn
    conn.close()


# --- registry -----------------------------------------------------------------

def test_schema_version_is_6():
    assert SCHEMA_VERSION == 6


def test_migrate_brings_v4_to_v6(db_v4):
    migrate(db_v4)
    assert db_v4.execute("PRAGMA user_version").fetchone()[0] == 6
    # Idempotent on an already-6 DB.
    migrate(db_v4)
    assert db_v4.execute("PRAGMA user_version").fetchone()[0] == 6


# --- tenants ------------------------------------------------------------------

def test_tenants_table_created_with_check(db_v4):
    migrate(db_v4)
    cols = {r[1] for r in db_v4.execute("PRAGMA table_info(tenants)")}
    assert {"id", "slug", "name", "status", "qdrant_collection",
            "neo4j_database", "created_at", "deleted_at"} <= cols
    # status is CHECK-constrained to the three lifecycle states.
    with pytest.raises(Exception):
        db_v4.execute(
            "INSERT INTO tenants (slug, name, status) "
            "VALUES ('x', 'X', 'bogus')"
        )


def test_install_tenant_seeded(db_v4):
    migrate(db_v4)
    row = db_v4.execute(
        "SELECT id, slug, status FROM tenants WHERE id=1"
    ).fetchone()
    assert row == (1, "install", "active")


def test_slug_charset_rejected(db_v4):
    """Slug GLOB '[a-z0-9-]*' rejects uppercase / spaces / dots."""
    migrate(db_v4)
    with pytest.raises(sqlite3.IntegrityError):
        db_v4.execute(
            "INSERT INTO tenants (id, slug, name, status) "
            "VALUES (2, 'Bad Slug', 'X', 'active')"
        )


def test_accounts_gain_tenant_id_defaulting_to_install(db_v4):
    migrate(db_v4)
    legacy = db_v4.execute(
        "SELECT tenant_id FROM accounts WHERE email='admin@example.com'"
    ).fetchone()
    assert legacy == (1,)
    # New rows default to install tenant unless explicitly set.
    db_v4.execute(
        "INSERT INTO accounts (email, role, password_hash) "
        "VALUES ('new@example.com', 'user', 'h')"
    )
    new = db_v4.execute(
        "SELECT tenant_id FROM accounts WHERE email='new@example.com'"
    ).fetchone()
    assert new == (1,)


# --- table swaps --------------------------------------------------------------

def _pk_cols(db_v4, table):
    rows = db_v4.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()
    return [r[1] for r in rows if r[5] > 0]


def test_highwater_pk_is_tenant_partitioned(db_v4):
    migrate(db_v4)
    assert _pk_cols(db_v4, "highwater") == ["tenant_id", "source", "item_key"]


def test_highwater_legacy_rows_copied(db_v4):
    migrate(db_v4)
    row = db_v4.execute(
        "SELECT tenant_id, source, item_key, last_key FROM highwater "
        "WHERE source='fs' AND item_key='item-1'"
    ).fetchone()
    assert row == (1, "fs", "item-1", "item-1")


def test_highwater_same_key_allowed_across_tenants(db_v4):
    migrate(db_v4)
    db_v4.execute(
        "INSERT INTO tenants (id, slug, name, status, qdrant_collection,"
        " neo4j_database, created_at) VALUES (2, 'acme', 'Acme','active',"
        " 'kb-acme', 'acme', '2026-01-01T00:00:00')"
    )
    db_v4.commit()
    # Same (source, item_key) under tenant 2 is a distinct PK — allowed.
    db_v4.execute(
        "INSERT INTO highwater (tenant_id, source, item_key, last_key,"
        " updated_at) VALUES (2, 'fs', 'item-1', 'item-1',"
        " '2026-01-01T00:00:00')"
    )
    db_v4.commit()
    n = db_v4.execute(
        "SELECT COUNT(*) FROM highwater "
        "WHERE source='fs' AND item_key='item-1'"
    ).fetchone()[0]
    assert n == 2


def test_ingest_locks_pk_is_tenant_partitioned(db_v4):
    migrate(db_v4)
    assert _pk_cols(db_v4, "ingest_locks") == ["tenant_id", "source"]


# --- audit --------------------------------------------------------------------

def test_audit_runs_gain_tenant_id(db_v4):
    migrate(db_v4)
    row = db_v4.execute(
        "SELECT tenant_id FROM audit_runs WHERE run_id='run-1'"
    ).fetchone()
    assert row == (1,)


def test_audit_items_gain_tenant_id(db_v4):
    migrate(db_v4)
    row = db_v4.execute(
        "SELECT tenant_id FROM audit_items WHERE run_id='run-1'"
    ).fetchone()
    assert row == (1,)


# --- tenant_service_tokens ----------------------------------------------------

def test_tenant_service_tokens_created(db_v4):
    migrate(db_v4)
    cols = {r[1] for r in db_v4.execute(
        "PRAGMA table_info(tenant_service_tokens)")}
    assert {"id", "tenant_id", "token_hash", "created_at", "revoked"} <= cols


def test_tenant_service_tokens_cascade_on_tenant_delete(db_v4):
    migrate(db_v4)
    # Add a second tenant + its service token.
    db_v4.execute(
        "INSERT INTO tenants (id, slug, name, status, qdrant_collection,"
        " neo4j_database, created_at) VALUES (2, 'acme', 'Acme','active',"
        " 'kb-acme', 'acme', '2026-01-01T00:00:00')"
    )
    db_v4.execute(
        "INSERT INTO tenant_service_tokens (tenant_id, token_hash,"
        " created_at) VALUES (2, 'pbkdf2$xx$yy', '2026-01-01T00:00:00')"
    )
    db_v4.commit()
    # Delete the tenant → cascade removes its service tokens.
    db_v4.execute("DELETE FROM tenants WHERE id=2")
    db_v4.commit()
    n = db_v4.execute(
        "SELECT COUNT(*) FROM tenant_service_tokens WHERE tenant_id=2"
    ).fetchone()[0]
    assert n == 0


def test_tenant_config_not_created(db_v4):
    """T4: no per-tenant endpoint overrides in v1 — no table."""
    migrate(db_v4)
    tables = {
        r[0] for r in db_v4.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "tenant_config" not in tables


# --- direct apply_v6 ------------------------------------------------------------

def test_apply_v6_is_idempotent_on_fresh_v4(db_v4):
    """apply_v6 runs directly on a v4 DB (registry-independent)."""
    apply_v6(db_v4)
    db_v4.commit()
    apply_v6(db_v4)
    db_v4.commit()
