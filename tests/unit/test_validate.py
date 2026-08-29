"""validate command tests (T014, US1)."""

import pytest
from click.testing import CliRunner

from digital_twins import health
from digital_twins.cli import cli


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)


def test_validate_healthy_exits_0(env_dirs, monkeypatch):
    monkeypatch.setattr(health, "run_health_checks", lambda cfg: [
        health.HealthResult(ep, True, "ok")
        for ep in ("qdrant", "neo4j", "llm")])
    result = CliRunner().invoke(cli, ["validate"])
    assert result.exit_code == 0, result.output
    for ep in ("qdrant", "neo4j", "llm"):
        assert ep in result.output
    assert "FAIL" not in result.output


def test_validate_failure_exits_1_with_remediation(env_dirs, monkeypatch):
    monkeypatch.setattr(health, "run_health_checks", lambda cfg: [
        health.HealthResult("qdrant", False, "qdrant.url is not configured",
                            "set qdrant.url in kb.local.yml (env: KB_QDRANT__URL), "
                            "then re-run init/validate"),
        health.HealthResult("neo4j", True, "ok"),
        health.HealthResult("llm", True, "ok"),
    ])
    result = CliRunner().invoke(cli, ["validate"])
    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "KB_QDRANT__URL" in result.output
