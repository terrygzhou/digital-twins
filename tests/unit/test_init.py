"""init behavior tests (T013, US1)."""

import pytest
import yaml
from click.testing import CliRunner

from digital_twins import health
from digital_twins.cli import cli
from digital_twins.config.schema import BUILTIN_SOURCES


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
    # 003 first-admin step (T008/C-4): init reads the first-admin
    # credentials from these auth-only env vars (never argv/prompt when
    # set).  Every init test in this file supplies them so the tests
    # stay deterministic and non-interactive.
    monkeypatch.setenv("INIT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "admin-pw")
    monkeypatch.chdir(tmp_path)  # keep .env resolution away from the repo
    return config_dir, state_dir


def _stub_health(monkeypatch, ok=True):
    def _run(cfg):
        return [health.HealthResult(ep, ok, "ok" if ok else "down", "")
                for ep in ("qdrant", "neo4j", "llm")]
    monkeypatch.setattr(health, "run_health_checks", _run)


def _read(config_dir):
    return yaml.safe_load((config_dir / "kb.local.yml").read_text(encoding="utf-8"))


def test_init_creates_starter_and_state(env_dirs, monkeypatch):
    config_dir, state_dir = env_dirs
    _stub_health(monkeypatch)
    result = CliRunner().invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    data = _read(config_dir)
    assert set(data["sources"]) == set(BUILTIN_SOURCES)
    assert all(s["enabled"] is False for s in data["sources"].values())
    assert (state_dir / "state.db").exists()


def test_init_prompts_for_missing_endpoints(env_dirs, monkeypatch):
    config_dir, _ = env_dirs
    _stub_health(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(cli, ["init"], input=(
        "https://q:6333\nbolt://n:7687\nneo4j_user\ns3cr3t\n"
        "https://llm.example/v1\ngpt-test\n"))
    assert result.exit_code == 0, result.output
    data = _read(config_dir)
    assert data["qdrant"]["url"] == "https://q:6333"
    assert data["neo4j"]["password"] == "s3cr3t"
    assert data["llm"]["model"] == "gpt-test"


def test_init_keeps_existing_values(env_dirs, monkeypatch):
    config_dir, _ = env_dirs
    config_dir.mkdir()
    (config_dir / "kb.local.yml").write_text(
        "qdrant:\n  url: https://old.example\n"
        "sources:\n  fs:\n    enabled: true\n", encoding="utf-8")
    _stub_health(monkeypatch)
    result = CliRunner().invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    data = _read(config_dir)
    assert data["qdrant"]["url"] == "https://old.example"
    assert data["sources"]["fs"]["enabled"] is True
    # missing built-ins were added, still disabled
    assert data["sources"]["gmail"]["enabled"] is False


def test_init_interrupted_resumes(env_dirs, monkeypatch):
    config_dir, state_dir = env_dirs
    config_dir.mkdir()  # dir exists but no kb.local.yml: an interrupted init
    _stub_health(monkeypatch)
    result = CliRunner().invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    assert (config_dir / "kb.local.yml").is_file()
    assert (state_dir / "state.db").exists()


def test_init_unhealthy_exits_1(env_dirs, monkeypatch):
    config_dir, state_dir = env_dirs
    _stub_health(monkeypatch, ok=False)
    result = CliRunner().invoke(cli, ["init", "--yes"])
    assert result.exit_code == 1
    assert "FAIL" in result.output
    # setup still happened even though health failed
    assert (config_dir / "kb.local.yml").is_file()
    assert (state_dir / "state.db").exists()
