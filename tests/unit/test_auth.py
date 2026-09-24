"""T011: `run --once --as <user>` auth against the accounts store.

Contracts (specs/002-scheduled-runs/task-T011-brief.md, ruling R-04/R-06):
- Hash scheme: pbkdf2_hmac(sha256, password, 16-byte salt, 100_000 iters),
  stored as `pbkdf2$<salt_hex>$<hash_hex>` in accounts.password_hash.
- authenticate(db, owner, password) -> bool; missing owner / malformed hash
  / empty password -> False (no exception).
- `run --once --as USER` reads the password from the DT_USER_PASSWORD env
  var (auth-only, NOT a config knob, read from os.environ directly).
  Env unset -> exit 2 "DT_USER_PASSWORD not set" (no interactive prompt in
  v1 — 003 territory).
- Auth happens BEFORE any pipeline work: bad password exits 2 without
  touching any state (no audit row, no Qdrant write, no high-water advance).
  The stderr names the failure, never the user's password.
- Auth success -> scheduled_by=USER on the audit row.
- `run --once` without --as keeps scheduled_by='system' (T009 unchanged).
"""

import hashlib
import hmac
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from qdrant_client import QdrantClient

from digital_twins import cli as cli_mod
from digital_twins.health import QDRANT_COLLECTION
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_hash(password: str, salt: bytes) -> str:
    """pbkdf2_hmac(sha256, password, salt, 100_000) -> 'pbkdf2$salt_hex$hash_hex'."""
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def _seed_account(db, email: str, password: str, salt: bytes | None = None) -> None:
    """Seed an accounts row with a known pbkdf2 hash."""
    salt = salt if salt is not None else os.urandom(16)
    db.execute(
        "INSERT INTO accounts (email, role, password_hash) VALUES (?, ?, ?)",
        (email, "admin", _make_hash(password, salt)),
    )
    db.commit()


def _write_config(config_dir: Path, fs_dir: Path) -> None:
    lines = [
        "sources:",
        "  fs:",
        "    enabled: true",
        "    max_items: 200",
        "    timeout_s: 1500",
        "    extra:",
        f"      dir: {fs_dir}",
    ]
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "kb.local.yml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def _setup_env(tmp_path, monkeypatch, fs_dir):
    """Point config/state/qdrant/embedding at tmp_path; stub qdrant + embedder."""
    config_dir = tmp_path / "config"
    _write_config(config_dir, fs_dir)
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KB_QDRANT__URL", "https://q.example:6333")
    monkeypatch.setenv("KB_EMBEDDING__MODEL", "BAAI/bge-small-en-v1.5")
    monkeypatch.setenv("KB_EMBEDDING__DEVICE", "cpu")
    monkeypatch.setenv("KB_CHUNKING__MAX_CHARS", "200")
    monkeypatch.setenv("KB_CHUNKING__OVERLAP", "20")
    monkeypatch.chdir(tmp_path)

    in_memory = QdrantClient(":memory:")
    from digital_twins.health import QDRANT_COLLECTION as _COL
    from qdrant_client import models as _qm
    if not in_memory.collection_exists(_COL):
        in_memory.create_collection(
            _COL, vectors_config=_qm.VectorParams(
                size=384, distance=_qm.Distance.COSINE))
    monkeypatch.setattr("qdrant_client.QdrantClient",
                        lambda *a, **kw: in_memory)
    monkeypatch.setattr(cli_mod, "_make_embedder",
                        lambda cfg: (lambda texts: [[0.5] * 384 for _ in texts]))
    return in_memory


def _audit_rows(db) -> list:
    return db.execute(
        "SELECT status, trigger, scheduled_by, per_source_counts "
        "FROM audit_runs"
    ).fetchall()


def _state_db(tmp_path, state_dir: Path | None = None):
    if state_dir is None:
        state_dir = Path(os.environ.get(
            "KB_STATE_DIR", str(tmp_path / "state")))
    state_dir.mkdir(parents=True, exist_ok=True)
    db = connect(state_dir)
    migrate(db)
    return db


# ---------------------------------------------------------------------------
# authenticate / hash_password (unit)
# ---------------------------------------------------------------------------

def test_hash_password_format():
    """hash_password returns pbkdf2$<32 hex>$<64 hex>; re-derives the same
    digest; two calls differ (random 16-byte salt)."""
    from digital_twins.auth import hash_password

    stored = hash_password("s3cretpw")
    parts = stored.split("$")
    assert parts[0] == "pbkdf2"
    salt_hex, hash_hex = parts[1], parts[2]
    assert len(salt_hex) == 32, "salt must be 16 bytes (32 hex chars)"
    assert len(hash_hex) == 64, "sha256 digest is 32 bytes (64 hex chars)"
    assert bytes.fromhex(salt_hex)  # valid hex
    assert bytes.fromhex(hash_hex)  # valid hex

    # deterministic re-derivation with the same salt matches
    derived = hashlib.pbkdf2_hmac(
        "sha256", b"s3cretpw", bytes.fromhex(salt_hex), 100_000)
    assert derived.hex() == hash_hex

    # different salt -> different stored hash (no two hashes collide trivially)
    assert hash_password("s3cretpw") != stored


def test_authenticate_correct_password(tmp_path):
    db = _state_db(tmp_path)
    try:
        from digital_twins.auth import authenticate
        _seed_account(db, "alice@example.com", "correct-horse")
        assert authenticate(db, "alice@example.com", "correct-horse") is True
    finally:
        db.close()


def test_authenticate_wrong_password(tmp_path):
    db = _state_db(tmp_path)
    try:
        from digital_twins.auth import authenticate
        _seed_account(db, "alice@example.com", "correct-horse")
        assert authenticate(db, "alice@example.com", "wrong-password") is False
    finally:
        db.close()


def test_authenticate_missing_owner(tmp_path):
    db = _state_db(tmp_path)
    try:
        from digital_twins.auth import authenticate
        _seed_account(db, "alice@example.com", "correct-horse")
        # no row for bob -> False, no exception
        assert authenticate(db, "bob@example.com", "correct-horse") is False
    finally:
        db.close()


def test_authenticate_malformed_hash(tmp_path):
    """A malformed stored hash (or NULL) must return False, not crash."""
    db = _state_db(tmp_path)
    try:
        from digital_twins.auth import authenticate
        db.execute(
            "INSERT INTO accounts (email, role, password_hash) VALUES (?, ?, ?)",
            ("broken@example.com", "owner", "not-a-valid-hash"))
        db.execute(
            "INSERT INTO accounts (email, role, password_hash) VALUES (?, ?, ?)",
            ("null@example.com", "owner", None))
        db.commit()
        assert authenticate(db, "broken@example.com", "whatever") is False
        assert authenticate(db, "null@example.com", "whatever") is False
    finally:
        db.close()


def test_authenticate_empty_password(tmp_path):
    """An account whose hash was derived from an empty password still
    authenticates only against the empty password; a non-empty guess fails."""
    db = _state_db(tmp_path)
    try:
        from digital_twins.auth import authenticate
        _seed_account(db, "empty@example.com", "")
        assert authenticate(db, "empty@example.com", "") is True
        assert authenticate(db, "empty@example.com", "x") is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# run --once --as <user> (CLI)
# ---------------------------------------------------------------------------

def test_run_once_as_requires_once(tmp_path, monkeypatch):
    """--as without --once -> exit 2, names the conflict."""
    _setup_env(tmp_path, monkeypatch, tmp_path / "data")
    result = CliRunner().invoke(
        cli_mod.cli, ["run", "--as", "testuser"])
    assert result.exit_code == 2, f"exit={result.exit_code} out={result.output}"
    assert "--once" in result.output


def test_run_once_as_bad_password_exit_2_no_state(tmp_path, monkeypatch):
    """Bad password -> exit 2, stderr names the failure (never the password),
    and NOTHING was touched: zero audit rows, zero Qdrant points, no
    highwater, state db absent or empty."""
    fs_dir = tmp_path / "data"
    fs_dir.mkdir()
    (fs_dir / "note1.txt").write_text("hello world " * 20, encoding="utf-8")
    in_memory = _setup_env(tmp_path, monkeypatch, fs_dir)
    monkeypatch.setenv("DT_USER_PASSWORD", "totally-wrong")

    # seed the account with the CORRECT password so the failure is the
    # mismatch, not a missing account
    db = _state_db(tmp_path)
    _seed_account(db, "testuser", "correct-horse")
    db.close()

    result = CliRunner().invoke(
        cli_mod.cli, ["run", "--once", "--as", "testuser"])
    assert result.exit_code == 2, f"exit={result.exit_code} out={result.output}"
    assert "authentication failed" in result.output.lower()
    assert "testuser" in result.output
    assert "totally-wrong" not in result.output, "password must not leak"

    # NO state touched: audit_runs has zero rows, qdrant has zero points,
    # highwater has zero rows
    db = _state_db(tmp_path)
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 0, \
            "bad password must not write an audit row"
        assert db.execute(
            "SELECT COUNT(*) FROM highwater").fetchone()[0] == 0, \
            "bad password must not advance highwater"
    finally:
        db.close()
    assert in_memory.count(QDRANT_COLLECTION).count == 0, \
        "bad password must not write Qdrant points"


def test_run_once_as_no_env_var_exit_2(tmp_path, monkeypatch):
    """DT_USER_PASSWORD unset -> exit 2 with a 'DT_USER_PASSWORD' message.
    No hang on an interactive prompt (v1 has no prompt — 003 territory);
    no state touched."""
    _setup_env(tmp_path, monkeypatch, tmp_path / "data")
    monkeypatch.delenv("DT_USER_PASSWORD", raising=False)

    result = CliRunner().invoke(
        cli_mod.cli, ["run", "--once", "--as", "testuser"],
        input="")
    assert result.exit_code == 2, f"exit={result.exit_code} out={result.output}"
    assert "DT_USER_PASSWORD" in result.output

    db = _state_db(tmp_path)
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 0
    finally:
        db.close()


def test_run_once_as_correct_password_succeeds(tmp_path, monkeypatch):
    """Correct password -> exit 0, audit row has scheduled_by='testuser',
    Qdrant has points."""
    fs_dir = tmp_path / "data"
    fs_dir.mkdir()
    (fs_dir / "note1.txt").write_text("hello world " * 20, encoding="utf-8")
    in_memory = _setup_env(tmp_path, monkeypatch, fs_dir)
    monkeypatch.setenv("DT_USER_PASSWORD", "correct-horse")

    db = _state_db(tmp_path)
    _seed_account(db, "testuser", "correct-horse")
    db.close()

    result = CliRunner().invoke(
        cli_mod.cli, ["run", "--once", "--as", "testuser"])
    assert result.exit_code == 0, f"exit={result.exit_code} out={result.output}"

    assert in_memory.count(QDRANT_COLLECTION).count >= 1, \
        "expected >=1 point, got 0"

    db = _state_db(tmp_path)
    try:
        rows = _audit_rows(db)
        assert len(rows) == 1, f"expected 1 audit row, got {len(rows)}: {rows}"
        status, trigger, scheduled_by, _counts = rows[0]
        assert trigger == "manual"
        assert scheduled_by == "testuser"
        assert status in ("ok", "partial")
    finally:
        db.close()


def test_run_once_as_nonexistent_owner_exit_2(tmp_path, monkeypatch):
    """--as with a user that has no accounts row -> exit 2, named error,
    no state touched."""
    _setup_env(tmp_path, monkeypatch, tmp_path / "data")
    monkeypatch.setenv("DT_USER_PASSWORD", "whatever")

    db = _state_db(tmp_path)
    db.close()

    result = CliRunner().invoke(
        cli_mod.cli, ["run", "--once", "--as", "ghost"])
    assert result.exit_code == 2, f"exit={result.exit_code} out={result.output}"
    assert "authentication failed" in result.output.lower()
    assert "ghost" in result.output

    db = _state_db(tmp_path)
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 0
    finally:
        db.close()


def test_run_once_as_no_state_db_distinct_message(tmp_path, monkeypatch):
    """--as with the state db missing -> exit 2 with a message that names the
    setup gap (no state db, run init) and does NOT conflate it with a
    credential failure ('authentication failed'). T020 deferred-minor: the
    T011 review flagged the original message as ambiguous between 'no state
    db' and 'auth failed'; this pins the split.
    """
    # Point config at a FRESH tmp_path so the state dir does not yet exist.
    # (Unlike _setup_env's chdir + KB_STATE_DIR, we explicitly unset any
    # state dir the caller's env may carry, so the CLI genuinely sees a
    # missing state dir.)
    config_dir = tmp_path / "config"
    _write_config(config_dir, tmp_path / "data")
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state_does_not_exist"))
    monkeypatch.delenv("KB_STATE_DIR_FALLBACK", raising=False)
    monkeypatch.setenv("DT_USER_PASSWORD", "whatever")
    monkeypatch.chdir(tmp_path)

    # Make sure the state dir really is absent.
    state_dir = Path(os.environ["KB_STATE_DIR"])
    assert not state_dir.exists(), "test precondition: state dir must be absent"

    result = CliRunner().invoke(
        cli_mod.cli, ["run", "--once", "--as", "ghost"])
    assert result.exit_code == 2, f"exit={result.exit_code} out={result.output}"
    # The message names the setup gap, not a credential failure.
    assert "no state db" in result.output.lower()
    assert "setup" in result.output.lower()
    # It does NOT use the credential-failure phrase.
    assert "authentication failed" not in result.output.lower()


def test_run_once_without_as_unchanged(tmp_path, monkeypatch):
    """run --once WITHOUT --as keeps scheduled_by='system' (T009 behavior)."""
    fs_dir = tmp_path / "data"
    fs_dir.mkdir()
    (fs_dir / "note1.txt").write_text("hello system", encoding="utf-8")
    _setup_env(tmp_path, monkeypatch, fs_dir)
    monkeypatch.delenv("DT_USER_PASSWORD", raising=False)

    result = CliRunner().invoke(cli_mod.cli, ["run", "--once"])
    assert result.exit_code == 0, f"exit={result.exit_code} out={result.output}"

    db = _state_db(tmp_path)
    try:
        rows = _audit_rows(db)
        assert len(rows) == 1
        _status, trigger, scheduled_by, _counts = rows[0]
        assert trigger == "manual"
        assert scheduled_by == "system"
    finally:
        db.close()
