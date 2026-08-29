"""T009: `run --once [--source]` — one-shot host-cron run.

Contracts (specs/002-scheduled-runs/contracts/cli.md):
- --once marks the run as one-shot: no schedule advance, no pidfile.
- trigger='manual' for run --once (vs 'schedule' for serve fires).
- scheduled_by='system' without --as (T011 sets the owner user later).
- Exit codes: 0 ok/partial, 1 failed, 2 auth/config error.
- 001's prerequisite gating is PRESERVED (exit 2 + audit row on failure).
"""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from qdrant_client import QdrantClient

from digital_twins import cli as cli_mod
from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_config(config_dir: Path, fs_dir: Path,
                  enable_pi: bool = False, pi_dir: str | None = None) -> None:
    """Write a minimal kb.local.yml with an enabled fs source (and optionally pi)."""
    lines = ["sources:"]
    lines.append("  fs:")
    lines.append("    enabled: true")
    lines.append("    max_items: 200")
    lines.append("    timeout_s: 1500")
    lines.append("    extra:")
    lines.append(f"      dir: {fs_dir}")
    if enable_pi:
        lines.append("  pi:")
        lines.append("    enabled: true")
        lines.append("    max_items: 200")
        lines.append("    timeout_s: 1500")
        lines.append("    extra:")
        lines.append(f"      sessions_dir: {pi_dir}")
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "kb.local.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_schedule(db, source: str = "fs", next_fire_at: str = "2025-07-15T03:00:00") -> None:
    """Pre-seed a schedules row so we can verify run --once does NOT advance it."""
    db.execute(
        "INSERT INTO schedules (owner, source, preset, param, fire_time, "
        "enabled, next_fire_at, acl, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("system", source, "daily", None, "03:00", 1,
         next_fire_at, "owner", "2025-07-01T00:00:00", "2025-07-01T00:00:00"),
    )
    db.commit()


def _audit_rows(db) -> list:
    return db.execute(
        "SELECT status, trigger, scheduled_by, per_source_counts "
        "FROM audit_runs"
    ).fetchall()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_run_once_ingests_new_items_and_writes_audit(tmp_path, monkeypatch):
    """run --once over an enabled fs source: exit 0, qdrant has points,
    audit row has trigger='manual', scheduled_by='system'."""
    fs_dir = tmp_path / "data"
    fs_dir.mkdir()
    (fs_dir / "note1.txt").write_text(
        "hello world " * 20, encoding="utf-8")

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

    # stub QdrantClient and the embedder so no real network / model is needed
    in_memory = QdrantClient(":memory:")
    captured = {}

    def fake_qdrant_factory():
        captured["qdrant"] = in_memory
        return in_memory

    def fake_embed(texts):
        return [[0.5] * 384 for _ in texts]

    monkeypatch.setattr(cli_mod, "_make_embedder", lambda cfg: fake_embed)
    # patch the qdrant factory inside run: the factory is defined inline, so
    # patch QdrantClient in the module namespace that run_pipeline resolves.
    # Simpler: monkeypatch the QdrantClient import that qdrant_factory uses.
    monkeypatch.setattr("qdrant_client.QdrantClient",
                        lambda *a, **kw: in_memory)

    result = CliRunner().invoke(cli_mod.cli, ["run", "--once"])
    assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"

    # qdrant has points
    count = in_memory.count(QDRANT_COLLECTION).count
    assert count >= 1, f"expected >=1 point, got {count}"

    # audit row
    state_dir = Path(os.environ["KB_STATE_DIR"])
    db = connect(state_dir)
    try:
        rows = _audit_rows(db)
        assert len(rows) == 1, f"expected 1 audit row, got {len(rows)}: {rows}"
        status, trigger, scheduled_by, counts_json = rows[0]
        assert trigger == "manual"
        assert scheduled_by == "system"
        assert status in ("ok", "partial")
    finally:
        db.close()


def test_run_once_no_schedule_advance(tmp_path, monkeypatch):
    """After run --once, a pre-seeded due schedule keeps its next_fire_at —
    one-shot runs never advance schedules."""
    fs_dir = tmp_path / "data"
    fs_dir.mkdir()
    (fs_dir / "note1.txt").write_text("hello", encoding="utf-8")

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
    monkeypatch.setattr("qdrant_client.QdrantClient",
                        lambda *a, **kw: in_memory)
    monkeypatch.setattr(cli_mod, "_make_embedder",
                        lambda cfg: (lambda texts: [[0.5] * 384 for _ in texts]))

    # pre-seed a schedule row; it must survive run --once unchanged
    state_dir = Path(os.environ["KB_STATE_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    db = connect(state_dir)
    migrate(db)
    _seed_schedule(db, source="fs", next_fire_at="2025-07-15T03:00:00")
    db.close()

    result = CliRunner().invoke(cli_mod.cli, ["run", "--once"])
    assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"

    db = connect(state_dir)
    try:
        row = db.execute(
            "SELECT next_fire_at FROM schedules WHERE source='fs'"
        ).fetchone()
        assert row is not None, "schedule row disappeared"
        assert row[0] == "2025-07-15T03:00:00", \
            f"schedule was advanced: {row[0]}"
    finally:
        db.close()


def test_run_once_source_filter(tmp_path, monkeypatch):
    """run --once --source fs with a second enabled source (pi, stubbed) →
    only fs items ingested; audit per_source_counts reflects only fs."""
    fs_dir = tmp_path / "data"
    fs_dir.mkdir()
    (fs_dir / "note1.txt").write_text("hello fs", encoding="utf-8")

    pi_dir = tmp_path / "pi_sessions"
    pi_dir.mkdir()

    config_dir = tmp_path / "config"
    _write_config(config_dir, fs_dir, enable_pi=True, pi_dir=str(pi_dir))

    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KB_QDRANT__URL", "https://q.example:6333")
    monkeypatch.setenv("KB_EMBEDDING__MODEL", "BAAI/bge-small-en-v1.5")
    monkeypatch.setenv("KB_EMBEDDING__DEVICE", "cpu")
    monkeypatch.setenv("KB_CHUNKING__MAX_CHARS", "200")
    monkeypatch.setenv("KB_CHUNKING__OVERLAP", "20")
    monkeypatch.chdir(tmp_path)

    in_memory = QdrantClient(":memory:")
    monkeypatch.setattr("qdrant_client.QdrantClient",
                        lambda *a, **kw: in_memory)
    monkeypatch.setattr(cli_mod, "_make_embedder",
                        lambda cfg: (lambda texts: [[0.5] * 384 for _ in texts]))

    result = CliRunner().invoke(cli_mod.cli, ["run", "--once", "--source", "fs"])
    assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"

    state_dir = Path(os.environ["KB_STATE_DIR"])
    db = connect(state_dir)
    try:
        rows = _audit_rows(db)
        assert len(rows) == 1
        _status, _trigger, _sb, counts_json = rows[0]
        counts = json.loads(counts_json)
        # only fs should appear — pi was enabled but filtered out
        assert "fs" in counts, f"fs missing from counts: {counts}"
        assert "pi" not in counts, f"pi leaked into counts: {counts}"
    finally:
        db.close()


def test_run_once_prerequisite_failure_exit_2(tmp_path, monkeypatch):
    """Enabled source with a broken prerequisite → exit 2, stderr names the
    source + missing prerequisite, audit row `failed` written (001 T019/T025
    behavior PRESERVED)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "kb.local.yml").write_text(
        "sources:\n"
        "  fs:\n"
        "    enabled: true\n"
        "    extra:\n"
        f"      dir: {tmp_path / 'nope'}\n",
        encoding="utf-8")

    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli_mod.cli, ["run", "--once"])
    assert result.exit_code == 2, f"exit={result.exit_code} output={result.output}"
    assert "fs" in result.output, f"source not named: {result.output}"
    assert "dir" in result.output, f"prerequisite not named: {result.output}"

    state_dir = Path(os.environ["KB_STATE_DIR"])
    db = connect(state_dir)
    try:
        rows = db.execute(
            "SELECT status FROM audit_runs"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "failed"
    finally:
        db.close()


def test_run_once_default_no_flag_unchanged(tmp_path, monkeypatch):
    """Invoking `run` WITHOUT --once behaves as 001 (same exit code as before;
    default path is identical — verify against the 001 command body)."""
    fs_dir = tmp_path / "data"
    fs_dir.mkdir()
    (fs_dir / "note1.txt").write_text("hello default", encoding="utf-8")

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
    monkeypatch.setattr("qdrant_client.QdrantClient",
                        lambda *a, **kw: in_memory)
    monkeypatch.setattr(cli_mod, "_make_embedder",
                        lambda cfg: (lambda texts: [[0.5] * 384 for _ in texts]))

    # without --once: 001 behavior — the run command should still work
    result = CliRunner().invoke(cli_mod.cli, ["run"])
    assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"

    # qdrant has points (same as --once)
    count = in_memory.count(QDRANT_COLLECTION).count
    assert count >= 1, f"expected >=1 point, got {count}"

    # audit row exists (001 behavior)
    state_dir = Path(os.environ["KB_STATE_DIR"])
    db = connect(state_dir)
    try:
        rows = db.execute(
            "SELECT status, trigger, scheduled_by FROM audit_runs"
        ).fetchall()
        assert len(rows) == 1
        status, trigger, scheduled_by = rows[0]
        # 001's run_pipeline defaults: trigger="manual", scheduled_by="system"
        assert trigger == "manual"
        assert scheduled_by == "system"
    finally:
        db.close()


def test_run_pipeline_plumbs_scheduled_by(qdrant, tmp_path):
    """run_pipeline accepts `scheduled_by` and records it on the audit row.

    The 002 `run --once` command passes trigger='manual', scheduled_by='system'
    explicitly; this unit test proves the param is plumbed through to the audit
    row (a non-default value proves it is not silently dropped), and that the
    additive default keeps 001 callers (who pass neither) on 'system'.
    """
    fs_dir = tmp_path / "data"
    fs_dir.mkdir()
    (fs_dir / "note1.txt").write_text("hello world " * 20, encoding="utf-8")

    cfg = {
        "state_dir": str(tmp_path / "state"),
        "config_dir": str(tmp_path / "config"),
        "qdrant": {"url": "https://q.example:6333", "api_key": None},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "sources": {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
                    for n in ("hermes", "pi", "dsh", "paperclip", "yahoo", "gmail", "fs")},
    }
    cfg["sources"]["fs"] = {"enabled": True, "max_items": 200, "timeout_s": 1500,
                           "extra": {"dir": str(fs_dir)}}
    db = connect(tmp_path / "state")
    migrate(db)

    def _embedder(texts):
        return [[0.5] * 384 for _ in texts]

    # non-default scheduled_by proves the param reaches the audit row
    run_pipeline(cfg, db, qdrant, _embedder,
                 trigger="manual", scheduled_by="alice@example.com")
    rows = db.execute(
        "SELECT trigger, scheduled_by FROM audit_runs").fetchall()
    assert len(rows) == 1
    assert rows[0] == ("manual", "alice@example.com"), (
        f"scheduled_by not plumbed: {rows[0]}")

    # default (001 callers pass neither) stays 'system'. rowid preserves
    # insertion order (both rows share a second-precision started_at, so
    # sorting by started_at, run_id would tie and fall back to random UUIDs).
    run_pipeline(cfg, db, qdrant, _embedder)
    rows = db.execute(
        "SELECT trigger, scheduled_by FROM audit_runs ORDER BY rowid"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0] == ("manual", "alice@example.com")
    assert rows[1] == ("manual", "system"), (
        f"default scheduled_by must be 'system': {rows[1]}")
    db.close()
