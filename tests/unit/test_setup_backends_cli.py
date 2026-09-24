"""RED-first tests for the per-service backend CLI wiring
(install-setup-separation T1.2 / T2).

Covers:
  - ``setup --local``: forces the local-stack path (no docker probe, no
    cloud prompt); the resolved all-local map (gpu-dependent llm/embedding)
    drives ``run_local_stack``.
  - ``setup --backends qdrant=local,llm=<url>``: a mixed choice starts the
    local stack for the local services AND writes the external URL for the
    rest — one ``run_local_stack`` call, no cloud path.
  - ``setup --backends qdrant=<url>,neo4j=<url>,llm=<url>`` (all external):
    no docker, no local stack — the cloud path runs instead.
  - ``--backends`` parsing: ``qdrant=local,llm=https://h/v1`` ->
    ``{"qdrant": "local", "llm": "https://h/v1"}``.

All external effects are monkeypatched (docker, compose, health, admin,
kb.local writes): hermetic, no containers / network.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import yaml

import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
    for v in ("KB_QDRANT__URL", "KB_NEO4J__URL", "KB_LLM__ENDPOINT",
              "KB_EMBEDDING__ENDPOINT", "INIT_ADMIN_EMAIL"):
        monkeypatch.delenv(v, raising=False)
    return config_dir, state_dir


def _fake_connect(state_dir):
    import sqlite3
    conn = sqlite3.connect(str(state_dir / "state.db"))
    conn.execute("CREATE TABLE IF NOT EXISTS accounts ("
                 "id INTEGER PRIMARY KEY, email TEXT UNIQUE, role TEXT, "
                 "password_hash TEXT, created_at TEXT DEFAULT '', "
                 "last_active TEXT DEFAULT '')")
    return conn


def _ok_checks():
    from digital_twins.health import HealthResult
    return [HealthResult(n, True, "ok", "", status="ok")
            for n in ("qdrant", "neo4j", "llm", "embedding")]


def _run_setup_cli(monkeypatch, *args, gpu=False, docker=False,
                   config_dir=None, state_dir=None):
    """Invoke ``digital-twins setup <args>`` with every external effect
    stubbed; return (result, captured) where captured exposes what the
    backend path called."""
    import digital_twins.setup as setup_mod
    from digital_twins.cli import cli

    captured = {"local": [], "cloud": [], "cloud_env": [], "docker": 0,
                "kb_local": None, "connect": 0}

    if state_dir is None:
        state_dir = Path(os.environ["KB_STATE_DIR"])

    def _connect(d):
        captured["connect"] += 1
        c = _fake_connect(d)
        return c

    monkeypatch.setattr(setup_mod, "connect", _connect)
    monkeypatch.setattr(setup_mod, "run_health_checks", lambda cfg: _ok_checks())

    def _fake_local(prompt_text, echo, resolved=None, **kw):
        captured["local"].append(resolved)
        # write a kb.local.yml from the resolved map so the health path works
        if resolved is not None and config_dir is not None:
            captured["kb_local"] = resolved
        return True

    def _fake_cloud(prompt_text, echo, **kw):
        captured["cloud"].append(True)
        return True

    def _fake_cloud_env(echo, **kw):
        captured["cloud_env"].append(True)
        return True

    def _fake_write_kb_local(data):
        captured["kb_local"] = data
        import digital_twins.setup as sm
        return sm.local_config_path()

    monkeypatch.setattr(setup_mod, "write_kb_local", _fake_write_kb_local)
    monkeypatch.setattr(setup_mod, "run_local_stack",
                        lambda p, e, resolved=None: _fake_local(p, e, resolved))
    monkeypatch.setattr(setup_mod, "run_cloud_stack",
                        lambda p, e: _fake_cloud(p, e))
    monkeypatch.setattr(setup_mod, "run_cloud_env_stack",
                        lambda e: _fake_cloud_env(e))
    monkeypatch.setattr(setup_mod, "docker_available", lambda: docker)
    monkeypatch.setattr(setup_mod, "_gpu_present", lambda: gpu)
    monkeypatch.setattr(setup_mod, "has_valid_local_config", lambda: False)

    runner = CliRunner()
    result = runner.invoke(cli, ["setup", *args])
    return result, captured


def test_local_flag_forces_local_stack(_isolate, monkeypatch):
    config_dir, state_dir = _isolate
    res, cap = _run_setup_cli(monkeypatch, "--local", gpu=True, docker=True,
                              config_dir=config_dir, state_dir=state_dir)
    assert res.exit_code == 0, res.output
    # The local-stack path ran; cloud never ran; no docker probe was needed.
    assert len(cap["local"]) == 1
    assert cap["cloud"] == []
    # The resolved map is all-local (GPU host).
    resolved = cap["local"][0]
    assert resolved["qdrant"]["mode"] == "local"
    assert resolved["llm"]["mode"] == "local"


def test_backends_mixed_starts_local_stack(_isolate, monkeypatch):
    config_dir, state_dir = _isolate
    res, cap = _run_setup_cli(
        monkeypatch, "--backends", "qdrant=local,llm=https://llm.example/v1",
        gpu=False, docker=True, config_dir=config_dir, state_dir=state_dir)
    assert res.exit_code == 0, res.output
    assert len(cap["local"]) == 1
    assert cap["cloud"] == []
    resolved = cap["local"][0]
    assert resolved["qdrant"]["mode"] == "local"
    assert resolved["llm"]["mode"] == "external"
    assert resolved["llm"]["url"] == "https://llm.example/v1"


def test_backends_all_external_writes_supplied_urls(_isolate, monkeypatch):
    """All-external --backends writes the supplied URLs straight into
    kb.local.yml — it does NOT fall into the cloud prompt path
    (the flag already supplied every required URL, so re-prompting for
    them would be wrong)."""
    import digital_twins.setup as setup_mod
    config_dir, state_dir = _isolate
    res, cap = _run_setup_cli(
        monkeypatch, "--backends",
        "qdrant=https://q,neo4j=bolt://n,llm=https://l/v1,embedding=https://e/v1",
        gpu=False, docker=False, config_dir=config_dir, state_dir=state_dir)
    assert res.exit_code == 0, res.output
    # No local stack, and no cloud-prompt path either: the URLs came
    # from the flag.
    assert cap["local"] == []
    assert cap["cloud"] == []
    # kb.local.yml was written from the resolved map with exactly the
    # supplied external URLs.
    assert cap["kb_local"] is not None
    content = cap["kb_local"]
    assert content["qdrant"]["url"] == "https://q"
    assert content["neo4j"]["url"] == "bolt://n"
    assert content["llm"]["endpoint"] == "https://l/v1"
    assert content["embedding"]["endpoint"] == "https://e/v1"


def test_backends_parsing(_isolate, monkeypatch):
    """--backends is parsed into a {service: value} map before resolution."""
    import digital_twins.setup as setup_mod
    m = setup_mod.parse_backends("qdrant=local,llm=https://h/v1,neo4j=local")
    assert m == {"qdrant": "local", "llm": "https://h/v1", "neo4j": "local"}
    # unknown service -> ValueError (fail fast, no silent typo)
    with pytest.raises(ValueError):
        setup_mod.parse_backends("redis=local")
    # empty value is allowed (means "required external, missing URL")
    m2 = setup_mod.parse_backends("qdrant=")
    assert m2 == {"qdrant": ""}
