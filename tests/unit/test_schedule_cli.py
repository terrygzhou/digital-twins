"""schedule add|list|remove CLI tests (T012, US3 — FR-3 v1 CRUD).

003 post-auth role check (C-2): every mutating schedule subcommand
(add, remove) authenticates first via DT_PERSONAL_TOKEN or
DT_USER_PASSWORD + --as, then checks the caller's role against R3
(schedule_crud: admin/scheduler yes, reader no). On denial: exit 2,
named reason, no schedule row written.

``schedule list`` is filter-only (no auth).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from digital_twins.cli import cli
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate

PRESETS = ("daily", "hourly", "weekly", "monthly", "every-N-hours")


# --- harness ---------------------------------------------------------------


def _seed_account(db, email, role, password="pw123"):
    """Insert a minimal accounts row with a known password."""
    from digital_twins.auth import hash_password
    db.execute(
        "INSERT INTO accounts (email, role, password_hash, created_at, last_active) "
        "VALUES (?, ?, ?, '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')",
        (email, role, hash_password(password)),
    )
    db.commit()


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    """Isolated config + state dirs with seeded accounts."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
    monkeypatch.chdir(tmp_path)

    # Create a minimal kb.local.yml so load() works
    from digital_twins.config.schema import get
    import yaml
    starter = {
        "state_dir": str(state_dir),
        "config_dir": str(config_dir),
    }
    (config_dir / "kb.local.yml").write_text(
        yaml.safe_dump(starter), encoding="utf-8")

    # Migrate the state DB + seed accounts
    db = connect(state_dir)
    migrate(db)
    _seed_account(db, "admin@example.com", "admin", "admin-pw")
    _seed_account(db, "alice@example.com", "scheduler", "alice-pw")
    _seed_account(db, "bob@example.com", "scheduler", "bob-pw")
    _seed_account(db, "reader@example.com", "reader", "reader-pw")
    db.close()

    return config_dir, state_dir


def _schedule_rows(state_dir: Path, owner: str | None = None):
    """Fetch schedule rows directly from sqlite (bypass the CLI for
    ground-truth assertions)."""
    db = connect(state_dir)
    try:
        if owner is None:
            rows = db.execute(
                "SELECT id, owner, source, preset, param, fire_time, "
                "enabled, next_fire_at, acl FROM schedules ORDER BY id ASC"
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, owner, source, preset, param, fire_time, "
                "enabled, next_fire_at, acl FROM schedules "
                "WHERE owner = ? ORDER BY id ASC",
                (owner,),
            ).fetchall()
    finally:
        db.close()
    return rows


def _add(args: list[str], env: dict | None = None):
    """Invoke `schedule add` with the given args + env."""
    runner = CliRunner()
    return runner.invoke(cli, ["schedule", "add"] + args, env=env or {})


# 1 — add with each preset stores the correct row ---------------------------


@pytest.mark.parametrize("preset", list(PRESETS))
def test_add_with_each_preset_stores_row(env_dirs, preset, monkeypatch):
    """For each of the five presets, `schedule add` stores a row with the
    correct owner / source / preset / param / fire_time / enabled / acl /
    next_fire_at.

    - param is set ONLY for every-N-hours (None for the others).
    - fire_time defaults to 03:00.
    - enabled defaults to 1.
    - acl defaults to 'owner'.
    - next_fire_at is set (non-None, non-empty).
    """
    _config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    param_arg = ["--param", "2"] if preset == "every-N-hours" else []
    result = _add([
        "--as", "alice@example.com",
        "--source", "fs",
        "--preset", preset,
        *param_arg,
    ], env={"DT_USER_PASSWORD": "alice-pw"})
    assert result.exit_code == 0, result.output

    rows = _schedule_rows(state_dir, owner="alice@example.com")
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    row = rows[0]
    # row = (id, owner, source, preset, param, fire_time, enabled, next_fire_at, acl)
    assert row[1] == "alice@example.com"
    assert row[2] == "fs"
    assert row[3] == preset
    if preset == "every-N-hours":
        assert row[4] == 2  # param
    else:
        assert row[4] is None  # param is NULL
    assert row[5] == "03:00"  # fire_time default
    assert row[6] == 1  # enabled
    assert row[7] is not None and row[7] != ""  # next_fire_at set
    assert row[8] == "owner"  # acl


# 2 — invalid preset lists the five valid ones ------------------------------


def test_add_invalid_preset_lists_valid(env_dirs, monkeypatch):
    """`--preset cron` → exit 2, stderr names the five valid presets."""
    _config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    result = _add(
        ["--as", "alice@example.com", "--source", "fs", "--preset", "cron"],
        env={"DT_USER_PASSWORD": "alice-pw"})
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}: {result.output}"
    )
    # stderr (in result.output for CliRunner) must name all five valid presets
    for p in PRESETS:
        assert p in result.output, (
            f"invalid-preset error must name {p!r}; got: {result.output}"
        )


# 3 — param validation -------------------------------------------------------


def test_add_param_with_non_every_n_hours_preset_rejected(env_dirs, monkeypatch):
    """`--preset daily --param 5` → exit 2 with a clear error (param is only
    for every-N-hours)."""
    _config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    result = _add([
        "--as", "alice@example.com",
        "--source", "fs",
        "--preset", "daily",
        "--param", "5",
    ], env={"DT_USER_PASSWORD": "alice-pw"})
    # Exit 2 AND a real error message (not just "No such command").
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}: {result.output}"
    )
    # The error must be about the preset/param, not a missing command.
    assert "No such command" not in result.output, (
        f"the schedule command does not exist: {result.output}"
    )
    # And no schedule row should have been created.
    rows = _schedule_rows(state_dir)
    assert len(rows) == 0, (
        f"no row should be stored on param validation failure, "
        f"got {len(rows)}"
    )


def test_add_every_n_hours_without_param_rejected(env_dirs, monkeypatch):
    """`--preset every-N-hours` without `--param` → exit 2 (param required
    for every-N-hours)."""
    _config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    result = _add([
        "--as", "alice@example.com",
        "--source", "fs",
        "--preset", "every-N-hours",
    ], env={"DT_USER_PASSWORD": "alice-pw"})
    # Exit 2 AND a real error message (not just "No such command").
    assert result.exit_code == 2, (
        f"expected exit 2, got {result.exit_code}: {result.output}"
    )
    assert "No such command" not in result.output, (
        f"the schedule command does not exist: {result.output}"
    )
    # And no schedule row should have been created.
    rows = _schedule_rows(state_dir)
    assert len(rows) == 0, (
        f"no row should be stored when every-N-hours lacks --param, "
        f"got {len(rows)}"
    )


# 4 — list round-trip --------------------------------------------------------


def test_list_round_trip(env_dirs, monkeypatch):
    """Add two schedules with different owners; `schedule list` shows both;
    `schedule list --as alice` shows only alice's.

    `schedule list` is filter-only (no auth required).
    """
    _config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    # Add two schedules with different owners.
    r1 = _add(
        ["--as", "alice@example.com", "--source", "fs", "--preset", "daily"],
        env={"DT_USER_PASSWORD": "alice-pw"})
    assert r1.exit_code == 0, r1.output
    r2 = _add(
        ["--as", "bob@example.com", "--source", "fs", "--preset", "hourly"],
        env={"DT_USER_PASSWORD": "bob-pw"})
    assert r2.exit_code == 0, r2.output

    # List all: both rows present.
    result = CliRunner().invoke(cli, ["schedule", "list"])
    assert result.exit_code == 0, result.output
    assert "alice@example.com" in result.output
    assert "bob@example.com" in result.output
    assert "daily" in result.output
    assert "hourly" in result.output

    # List filtered by owner: only alice's.
    result = CliRunner().invoke(cli, ["schedule", "list", "--as", "alice@example.com"])
    assert result.exit_code == 0, result.output
    assert "alice@example.com" in result.output
    assert "bob@example.com" not in result.output


# 5 — remove round-trip ------------------------------------------------------


def test_remove_round_trip(env_dirs, monkeypatch):
    """Add a schedule, then `schedule remove --id <id>` → row gone, exit 0.
    `schedule remove --id 9999` (missing) → exit 1 with a clear message.

    `schedule remove` requires authentication (003 post-auth role check).
    """
    _config_dir, state_dir = env_dirs
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)

    # Add a schedule.
    r = _add(
        ["--as", "alice@example.com", "--source", "fs", "--preset", "daily"],
        env={"DT_USER_PASSWORD": "alice-pw"})
    assert r.exit_code == 0, r.output

    rows = _schedule_rows(state_dir)
    assert len(rows) == 1
    schedule_id = rows[0][0]  # id column

    # Remove it (authenticated as alice).
    result = CliRunner().invoke(
        cli, ["schedule", "remove", "--id", str(schedule_id),
              "--as", "alice@example.com"],
        env={"DT_USER_PASSWORD": "alice-pw"})
    assert result.exit_code == 0, result.output

    # Row is gone.
    rows = _schedule_rows(state_dir)
    assert len(rows) == 0, f"expected 0 rows after remove, got {len(rows)}"

    # Remove a missing id → exit 1 with a clear message.
    result = CliRunner().invoke(
        cli, ["schedule", "remove", "--id", "9999",
              "--as", "alice@example.com"],
        env={"DT_USER_PASSWORD": "alice-pw"})
    assert result.exit_code == 1, (
        f"expected exit 1 for missing id, got {result.exit_code}: "
        f"{result.output}"
    )
    assert "9999" in result.output or "not found" in result.output.lower()
