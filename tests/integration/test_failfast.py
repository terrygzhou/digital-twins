"""Fail-fast prerequisites (T019): exit 2, source + prerequisite named.

cli.md: a missing prerequisite => exit 2, the error names the missing
prerequisite (runtime/credential/path) and where to set it; nothing is
ingested; the failed run is still audited.
"""

import pytest
from click.testing import CliRunner

from digital_twins import cli as cli_mod
from digital_twins.ingest.pipeline import PrerequisiteError, run_pipeline
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


def _cfg(tmp_path, fs_dir):
    sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
               for n in ("hermes", "pi", "dsh", "paperclip", "yahoo", "gmail", "fs")}
    sources["fs"] = {"enabled": True, "max_items": 200, "timeout_s": 1500,
                     "extra": {"dir": str(fs_dir)}}
    return {
        "state_dir": str(tmp_path / "state"),
        "config_dir": str(tmp_path / "config"),
        "qdrant": {"url": "https://q.example:6333", "api_key": None},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "sources": sources,
    }


def _embedder(texts):
    return [[0.5] * 384 for _ in texts]


def test_missing_prerequisite_raises_named(qdrant, tmp_path):
    cfg = _cfg(tmp_path, tmp_path / "does-not-exist")
    db = connect(tmp_path / "state")
    migrate(db)

    with pytest.raises(PrerequisiteError) as excinfo:
        run_pipeline(cfg, db, qdrant, _embedder)
    msg = str(excinfo.value)
    assert "fs" in msg            # names the source
    assert "dir" in msg           # names the missing prerequisite

    # nothing was ingested: no collection at all
    assert [c.name for c in qdrant.get_collections().collections] == []

    # the failed run is still audited
    rows = db.execute("SELECT status FROM audit_runs").fetchall()
    assert rows == [("failed",)]
    db.close()


def test_run_cli_exit_2_names_source_and_prerequisite(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "kb.local.yml").write_text(
        f"sources:\n  fs:\n    enabled: true\n    extra:\n"
        f"      dir: {tmp_path / 'nope'}\n", encoding="utf-8")
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli_mod.cli, ["run"])
    assert result.exit_code == 2
    assert "fs" in result.output
    assert "dir" in result.output
