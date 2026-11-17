"""T021 (RED): ``digital-twins tenant`` CLI group (025 T022).

Contracts: specs/025-multi-tenant/contracts/cli.md —

- ``tenant list``            — slug, name, status, qdrant_collection,
  neo4j_database (install-admin only, T3).
- ``tenant create``          — row + Qdrant CREATE COLLECTION +
  Neo4j CREATE DATABASE (Enterprise) / channel partitioning
  (Community 5.26), at pinned dimension.
- ``tenant suspend``         — status=suspended; tenant auth tokens 403.
- ``tenant delete``          — status=deleted + DROP of storage;
  requires ``--confirm <slug>``; audit-recorded.
- ``tenant service-token create`` — plaintext shown once, hash
  stored at rest (C-1: ``pbkdf2$salt_hex$hash_hex``).

RED: the CLI group does not exist yet (task T022 lands the group in
``digital_twins/cli.py``). These tests exercise the *contract*; they
fail with ``SystemExit``/missing-command until T022.

Harness mirrors test_cli_channels.py: ``CliRunner`` against
``cli.cli`` with isolated config dir + tmp state dir; Qdrant/Neo4j
effects mocked at the client-factory level (no live service required
for unit tests). The live green gate (T008) runs against 6333/7687.
"""

import os
import re
import sqlite3
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from digital_twins import cli as cli_mod
from digital_twins.cli import cli


class FakeQdrant:
    """Minimal Qdrant client stub for unit tests that mock
    ``_qdrant_client`` at the factory level."""
    def create_collection(self, name, vectors_config=None, **kw):
        return True

    def delete_collection(self, name):
        return True


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def config_env(tmp_path, monkeypatch):
    """Isolated config dir + tmp state dir; qdrant/neo4j knobs set to
    dummy values (tests mock the Qdrant client; Neo4j is not touched
    by any command under test)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (config_dir / "kb.yml").write_text(yaml.safe_dump({
        "state_dir": str(state_dir),
        "qdrant": {"url": "http://127.0.0.1:6333"},
        "neo4j": {
            "url": "bolt://127.0.0.1:7687",
            "user": "neo4j",
            "password": "dummy",
        },
    }), encoding="utf-8")
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.chdir(tmp_path)

    # Seed an install-level admin account + personal token so the CLI
    # tenant auth gate (C-2: DT_PERSONAL_TOKEN → manage_accounts) passes
    # without weakening the production gate.  Mirrors test_schedule_cli.
    from digital_twins.auth import hash_password, create_personal_token
    from digital_twins.state.db import connect

    db = connect(state_dir)
    try:
        db.execute(
            "INSERT INTO accounts (email, role, password_hash, "
            "created_at, last_active) VALUES (?, ?, ?, ?, ?)",
            ("admin@example.com", "admin",
             hash_password("admin-pw-025"),
             "2025-01-01T00:00:00+00:00",
             "2025-01-01T00:00:00+00:00"),
        )
        db.commit()
        _token_id, token_plain = create_personal_token(
            db, "admin@example.com")
    finally:
        db.close()
    monkeypatch.setenv("DT_PERSONAL_TOKEN", token_plain)

    return config_dir


@pytest.fixture()
def runner():
    return CliRunner()


def invoke(runner, *args):
    return runner.invoke(cli, ["tenant", *args], catch_exceptions=False)


# ---------------------------------------------------------------------------
# tenant list
# ---------------------------------------------------------------------------

def test_tenant_list_shows_install_and_provisioned(config_env, runner):
    """The install tenant (id 1) is always present; a provisioned
    tenant shows its slug + resolved stores."""
    from digital_twins.tenant_service import provision_tenant
    from digital_twins.state.db import connect
    from digital_twins.state.migrations import migrate

    state_dir = Path(config_env / ".." / "state")
    db = connect(state_dir)
    try:
        migrate(db)
        provision_tenant(db, "kb-pk", name="Personal KB")
    finally:
        db.close()

    result = invoke(runner, "list")
    assert result.exit_code == 0, result.output
    assert "install" in result.output
    assert "kb-pk" in result.output
    assert "kb-kb-pk" in result.output
    assert "Personal KB" in result.output


def test_tenant_list_columns_match_contract(config_env, runner):
    """Header carries the contract columns: slug, name, status,
    qdrant_collection, neo4j_database."""
    result = invoke(runner, "list")
    assert result.exit_code == 0, result.output
    for col in ("slug", "name", "status", "qdrant_collection",
                "neo4j_database"):
        assert col in result.output


def test_tenant_list_empty_states_beyond_install(config_env, runner):
    """A fresh install lists the install tenant only (no data rows)."""
    result = invoke(runner, "list")
    assert result.exit_code == 0, result.output
    lines = [l for l in result.output.splitlines() if l.strip()]
    # header + install row
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# tenant create
# ---------------------------------------------------------------------------

def test_tenant_create_provisions_row_and_collection(config_env, runner,
                                                    monkeypatch):
    """create ⇒ row (kb-<slug> / <slug>) + Qdrant CREATE COLLECTION
    at the pinned dimension; prints slug + collection + DB name."""
    created = []

    class FakeQdrant:
        def create_collection(self, name, **kw):
            created.append((name, kw.get("vectors_config"), kw.get("vectors")))

    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())

    result = invoke(runner, "create", "--slug", "kb-pk",
                    "--name", "Personal KB")
    assert result.exit_code == 0, result.output
    assert "kb-pk" in result.output
    assert "kb-kb-pk" in result.output
    assert created and created[0][0] == "kb-kb-pk"

    # row landed with the T1 store names
    state_dir = Path(config_env / ".." / "state")
    db = sqlite3.connect(str(state_dir / "state.db"))
    try:
        row = db.execute(
            "SELECT slug, qdrant_collection, neo4j_database, status "
            "FROM tenants WHERE slug=?", ("kb-pk",)).fetchone()
    finally:
        db.close()
    assert row is not None
    slug, coll, db_name, status = row
    assert (slug, coll, db_name, status) == ("kb-pk", "kb-kb-pk", "kb-pk",
                                              "active")


def test_tenant_create_duplicate_slug_fails(config_env, runner, monkeypatch):
    """A second create with the same slug exits non-zero (DuplicateSlug)."""
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    r1 = invoke(runner, "create", "--slug", "kb-pk")
    assert r1.exit_code == 0, r1.output
    r2 = invoke(runner, "create", "--slug", "kb-pk")
    assert r2.exit_code != 0
    assert "already" in r2.output.lower() or "duplicate" in r2.output.lower()


def test_tenant_create_reserved_slug_rejected(config_env, runner, monkeypatch):
    """'install' is reserved — exit non-zero, no row written."""
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    result = invoke(runner, "create", "--slug", "install")
    assert result.exit_code != 0
    assert "reserved" in result.output.lower()


def test_tenant_create_bad_slug_rejected(config_env, runner, monkeypatch):
    """Slugs must match ^[a-z0-9-]+$ (no uppercase / no dots / no
    empty)."""
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    for bad in ("KB-PK", "kb_pk", "kb..pk", ""):
        result = invoke(runner, "create", "--slug", bad)
        assert result.exit_code != 0, f"slug {bad!r} should be rejected"


# ---------------------------------------------------------------------------
# tenant suspend
# ---------------------------------------------------------------------------

def test_tenant_suspend_sets_status(config_env, runner, monkeypatch):
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    invoke(runner, "create", "--slug", "kb-pk")
    result = invoke(runner, "suspend", "--slug", "kb-pk")
    assert result.exit_code == 0, result.output
    state_dir = Path(config_env / ".." / "state")
    db = sqlite3.connect(str(state_dir / "state.db"))
    try:
        status = db.execute(
            "SELECT status FROM tenants WHERE slug=?",
            ("kb-pk",)).fetchone()[0]
    finally:
        db.close()
    assert status == "suspended"


def test_tenant_suspend_install_tenant_rejected(config_env, runner):
    """The install tenant cannot be suspended (guards the legacy
    shared stores)."""
    result = invoke(runner, "suspend", "--slug", "install")
    assert result.exit_code != 0


def test_tenant_suspend_unknown_slug_exits_nonzero(config_env, runner):
    result = invoke(runner, "suspend", "--slug", "nope")
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# tenant delete
# ---------------------------------------------------------------------------

def test_tenant_delete_requires_slug_confirmation(config_env, runner,
                                                 monkeypatch):
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    invoke(runner, "create", "--slug", "kb-pk")
    # no --confirm → fail fast
    result = invoke(runner, "delete", "--slug", "kb-pk")
    assert result.exit_code != 0
    # confirm string must match the slug exactly
    result = invoke(runner, "delete", "--slug", "kb-pk",
                    "--confirm", "wrong")
    assert result.exit_code != 0
    # with the right confirm it proceeds
    result = invoke(runner, "delete", "--slug", "kb-pk",
                    "--confirm", "kb-pk")
    assert result.exit_code == 0, result.output
    state_dir = Path(config_env / ".." / "state")
    db = sqlite3.connect(str(state_dir / "state.db"))
    try:
        status = db.execute(
            "SELECT status FROM tenants WHERE slug=?",
            ("kb-pk",)).fetchone()[0]
    finally:
        db.close()
    assert status == "deleted"


def test_tenant_delete_drops_collection(config_env, runner, monkeypatch):
    """delete ⇒ DROP COLLECTION before the row is marked deleted
    (storage teardown precedes the audit trail row)."""
    calls = []

    class FakeQdrant:
        def delete_collection(self, name):
            calls.append(name)
            return True

    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    invoke(runner, "create", "--slug", "kb-pk")
    result = invoke(runner, "delete", "--slug", "kb-pk",
                    "--confirm", "kb-pk")
    assert result.exit_code == 0, result.output
    assert calls == ["kb-kb-pk"]


def test_tenant_delete_install_tenant_rejected(config_env, runner,
                                               monkeypatch):
    """The install tenant (id 1) is protected — InstallTenantProtected."""
    result = invoke(runner, "delete", "--slug", "install",
                    "--confirm", "install")
    assert result.exit_code != 0
    out = result.output.lower()
    assert "protected" in out or "cannot be deleted" in out


# ---------------------------------------------------------------------------
# tenant service-token create
# ---------------------------------------------------------------------------

def test_service_token_shown_once_and_hashed_at_rest(config_env, runner,
                                                     monkeypatch):
    """C-1 pattern: the plaintext is a 32-byte hex value, printed
    exactly once; only ``pbkdf2$salt_hex$hash_hex`` is stored."""
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    invoke(runner, "create", "--slug", "kb-pk")

    result = invoke(runner, "service-token", "create", "--slug", "kb-pk")
    assert result.exit_code == 0, result.output

    m = re.search(r"[0-9a-f]{64}", result.output)
    assert m, f"no 64-hex-char plaintext in output: {result.output!r}"
    plaintext = m.group(0)
    # shown exactly once
    assert result.output.count(plaintext) == 1

    state_dir = Path(config_env / ".." / "state")
    db = sqlite3.connect(str(state_dir / "state.db"))
    try:
        row = db.execute(
            "SELECT tenant_id, token_hash, revoked FROM "
            "tenant_service_tokens").fetchone()
    finally:
        db.close()
    assert row is not None
    tenant_id, token_hash, revoked = row
    assert row is not None
    assert tenant_id >= 2  # the kb-pk tenant, not the install tenant
    assert revoked == 0
    # hash format: pbkdf2$salt_hex$hash_hex — and NEVER the plaintext
    assert token_hash.startswith("pbkdf2$")
    assert token_hash.count("$") == 2
    assert plaintext not in token_hash
    import hashlib as _hashlib
    salt_hex, hash_hex = token_hash.split("$")[1], token_hash.split("$")[2]
    expected = _hashlib.pbkdf2_hmac(
        "sha256", plaintext.encode(), bytes.fromhex(salt_hex), 100_000)
    assert expected.hex() == hash_hex


def test_service_token_for_unknown_slug_fails(config_env, runner):
    result = invoke(runner, "service-token", "create",
                    "--slug", "nope")
    assert result.exit_code != 0


def test_service_token_suspended_tenant_fails(config_env, runner,
                                              monkeypatch):
    """A suspended tenant's tokens are dead (403 class) — issuing one
    fails closed."""
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    invoke(runner, "create", "--slug", "kb-pk")
    invoke(runner, "suspend", "--slug", "kb-pk")
    result = invoke(runner, "service-token", "create", "--slug", "kb-pk")
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# tenant service-token list
# ---------------------------------------------------------------------------

def test_service_token_list_empty(config_env, runner):
    """A fresh install with no tokens says 'no service tokens found'."""
    result = invoke(runner, "service-token", "list")
    assert result.exit_code == 0, result.output
    assert "no service tokens found" in result.output


def test_service_token_list_shows_metadata(config_env, runner, monkeypatch):
    """After creating a token, list shows id, tenant, created_at, revoked
    — and NEVER the pbkdf2 hash (C-1)."""
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    invoke(runner, "create", "--slug", "kb-pk")
    invoke(runner, "service-token", "create", "--slug", "kb-pk")

    list_result = invoke(runner, "service-token", "list")
    assert list_result.exit_code == 0, list_result.output
    # Header columns
    assert "id" in list_result.output
    assert "tenant" in list_result.output
    assert "created_at" in list_result.output
    assert "revoked" in list_result.output
    # Tenant slug present
    assert "kb-pk" in list_result.output
    # Hash must NOT appear (C-1)
    assert "pbkdf2$" not in list_result.output
    assert "sha256" not in list_result.output


def test_service_token_list_filtered_by_slug(config_env, runner,
                                             monkeypatch):
    """--slug filters to that tenant's tokens only."""
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    invoke(runner, "create", "--slug", "kb-pk")
    invoke(runner, "create", "--slug", "kb-other")
    invoke(runner, "service-token", "create", "--slug", "kb-pk")
    invoke(runner, "service-token", "create", "--slug", "kb-other")

    result = invoke(runner, "service-token", "list",
                    "--slug", "kb-pk")
    assert result.exit_code == 0, result.output
    assert "kb-pk" in result.output
    assert "kb-other" not in result.output


# ---------------------------------------------------------------------------
# tenant service-token revoke
# ---------------------------------------------------------------------------

def test_service_token_revoke_success(config_env, runner, monkeypatch):
    """Revoke flips the row; the token no longer verifies."""
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    invoke(runner, "create", "--slug", "kb-pk")
    create_result = invoke(runner, "service-token", "create",
                           "--slug", "kb-pk")
    assert create_result.exit_code == 0, create_result.output

    # Extract the plaintext token from the create output:
    # "service token for tenant `kb-pk` (shown once):"
    # <64-char hex plaintext>
    # "(store this now; ...)"
    lines = create_result.output.splitlines()
    # Find the line after the "shown once" header line
    token_plain = None
    for i, line in enumerate(lines):
        if "shown once" in line:
            token_plain = lines[i + 1].strip()
            break
    assert token_plain, f"could not find token in: {create_result.output}"

    result = invoke(runner, "service-token", "revoke",
                    "--slug", "kb-pk", "--token", token_plain)
    assert result.exit_code == 0, result.output
    assert "revoked" in result.output

    # Verify the token no longer works: it should appear revoked in list
    list_result = invoke(runner, "service-token", "list",
                         "--slug", "kb-pk")
    assert "yes" in list_result.output


def test_service_token_revoke_unknown_token_noop(config_env, runner,
                                                  monkeypatch):
    """Revoking a token that doesn't exist is a silent no-op (not an
    error)."""
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    invoke(runner, "create", "--slug", "kb-pk")
    invoke(runner, "service-token", "create", "--slug", "kb-pk")

    result = invoke(runner, "service-token", "revoke",
                    "--slug", "kb-pk",
                    "--token", "totally-fake-token-12345")
    assert result.exit_code == 0, result.output
    assert "no-op" in result.output or "no matching" in result.output


def test_service_token_revoke_suspended_tenant_fails(config_env, runner,
                                                      monkeypatch):
    """Revoking on a suspended tenant fails closed."""
    monkeypatch.setattr(cli_mod, "_qdrant_client",
                        lambda cfg: FakeQdrant())
    invoke(runner, "create", "--slug", "kb-pk")
    invoke(runner, "service-token", "create", "--slug", "kb-pk")
    invoke(runner, "suspend", "--slug", "kb-pk")

    result = invoke(runner, "service-token", "revoke",
                    "--slug", "kb-pk",
                    "--token", "dummy-token")
    assert result.exit_code != 0
