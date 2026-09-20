"""Tests for digital_twins.setup (the `digital-twins setup` wizard, 015-lite).

Branches covered:
  - a valid local config skips service startup;
  - Docker available + confirm yes -> local stack path (kb.local.yml written,
    admin credentials file created with chmod 600);
  - Docker available + confirm no -> cloud fallback;
  - no Docker -> cloud fallback; cloud success / failure (exit 5);
  - --force-cloud skips Docker detection entirely;
  - --skip-services skips service startup;
  - an existing admin account -> no credentials file;
  - a failing health check -> exit 1.

All external effects are monkeypatched (docker, HTTP health-poll, health
checks, account creation): no containers, no network, hermetic.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Point KB_CONFIG_DIR / KB_STATE_DIR at fresh tmp dirs so no test
    touches the host's real config/state."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
    monkeypatch.delenv("KB_QDRANT__URL", raising=False)
    monkeypatch.delenv("KB_NEO4J__URL", raising=False)
    monkeypatch.delenv("KB_LLM__ENDPOINT", raising=False)
    monkeypatch.delenv("INIT_ADMIN_EMAIL", raising=False)
    return config_dir, state_dir


def _fake_connect(state_dir):
    """A sqlite-backed fake that mimics state.db.connect for create_admin."""
    import sqlite3
    conn = sqlite3.connect(str(state_dir / "state.db"))
    conn.execute("CREATE TABLE IF NOT EXISTS accounts ("
                 "id INTEGER PRIMARY KEY, email TEXT UNIQUE, "
                 "role TEXT, password_hash TEXT, created_at TEXT DEFAULT '', "
                 "last_active TEXT DEFAULT '')")
    return conn


def _admin_rows(conn):
    return conn.execute(
        "SELECT email, password_hash FROM accounts").fetchall()


def _ok_check(name):
    from digital_twins.health import HealthResult
    return HealthResult(name, True, "ok", "", status="ok")


def _fail_check(name, detail="down"):
    from digital_twins.health import HealthResult
    return HealthResult(name, False, detail, "remediate " + name,
                        status="unreachable")


def test_valid_local_config_skips_service_startup(_isolate_config,
                                                  monkeypatch):
    """A kb.local.yml with qdrant.url present: no docker, no cloud prompts,
    init + admin + validate still run; all-ok health -> exit 0."""
    import digital_twins.setup as setup_mod
    config_dir, state_dir = _isolate_config
    (config_dir / "kb.local.yml").parent.mkdir(parents=True)
    (config_dir / "kb.local.yml").write_text(
        yaml.safe_dump({"qdrant": {"url": "http://q:6333"}}),
        encoding="utf-8")

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))
    monkeypatch.setattr(setup_mod, "run_health_checks",
                        lambda cfg: [_ok_check("qdrant"), _ok_check("neo4j"),
                                     _ok_check("llm"),
                                     _ok_check("embedding")])

    def _no_service_call(*a, **kw):
        raise AssertionError("service startup should not have been attempted")
    monkeypatch.setattr(setup_mod, "run_local_stack", _no_service_call)
    monkeypatch.setattr(setup_mod, "run_cloud_stack", _no_service_call)
    monkeypatch.setattr(setup_mod, "docker_available", _no_service_call)

    rc = setup_mod.run_setup(
        prompt=lambda q: "unused",
        confirm=lambda q: True,
        echo=lambda msg: None)
    assert rc == 0
    cred = state_dir / "admin-credentials.txt"
    assert cred.is_file()
    text = cred.read_text(encoding="utf-8")
    assert "password:" in text
    mode = stat.S_IMODE(cred.stat().st_mode)
    assert mode == 0o600, f"credentials file must be chmod 600, got {oct(mode)}"
    # The local config was left untouched (still points at http://q:6333).
    data = yaml.safe_load((config_dir / "kb.local.yml").read_text())
    assert data["qdrant"]["url"] == "http://q:6333"


def test_docker_available_confirm_yes_runs_local_stack(_isolate_config,
                                                       monkeypatch):
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))
    monkeypatch.setattr(setup_mod, "run_health_checks",
                        lambda cfg: [_ok_check(n) for n in
                                     ("qdrant", "neo4j", "llm", "embedding")])

    called = {"local": 0, "cloud": 0}

    def fake_local_stack(prompt, echo):
        called["local"] += 1
        return True
    monkeypatch.setattr(setup_mod, "run_local_stack", fake_local_stack)
    monkeypatch.setattr(setup_mod, "run_cloud_stack",
                        lambda p, e: (_ for _ in ()).throw(
                            AssertionError("cloud path must not run")))
    monkeypatch.setattr(setup_mod, "docker_available", lambda: True)

    rc = setup_mod.run_setup(
        prompt=lambda q: "unused",
        confirm=lambda q: True,
        echo=lambda msg: None)
    assert rc == 0
    assert called["local"] == 1


def test_docker_available_confirm_no_falls_back_to_cloud(_isolate_config,
                                                         monkeypatch):
    """Declining the local stack falls back to cloud; with the real
    run_cloud_stack (env vars set) it writes kb.local.yml."""
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))
    monkeypatch.setattr(setup_mod, "run_health_checks",
                        lambda cfg: [_ok_check(n) for n in
                                     ("qdrant", "neo4j", "llm", "embedding")])
    monkeypatch.setattr(setup_mod, "docker_available", lambda: True)
    monkeypatch.setenv("KB_QDRANT__URL", "http://cloud-q:6333")
    monkeypatch.setenv("KB_NEO4J__URL", "bolt://cloud-n:7687")
    monkeypatch.setenv("KB_LLM__ENDPOINT", "http://cloud-llm/v1")

    calls = []
    def fake_local(prompt, echo):
        calls.append("local")
        return True
    monkeypatch.setattr(setup_mod, "run_local_stack", fake_local)

    rc = setup_mod.run_setup(
        prompt=lambda q: "unused",
        confirm=lambda q: False,  # decline the local stack
        echo=lambda msg: None)
    assert rc == 0
    # The local stack was NOT invoked; cloud wrote kb.local.yml.
    assert "local" not in calls
    data = yaml.safe_load((config_dir / "kb.local.yml").read_text())
    assert data["qdrant"]["url"] == "http://cloud-q:6333"
    assert data["neo4j"]["url"] == "bolt://cloud-n:7687"
    assert data["llm"]["endpoint"] == "http://cloud-llm/v1"


def test_no_docker_falls_back_to_cloud_success(_isolate_config, monkeypatch):
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))
    monkeypatch.setattr(setup_mod, "run_health_checks",
                        lambda cfg: [_ok_check(n) for n in
                                     ("qdrant", "neo4j", "llm", "embedding")])
    monkeypatch.setattr(setup_mod, "docker_available", lambda: False)
    monkeypatch.setenv("KB_QDRANT__URL", "http://cloud-q:6333")
    monkeypatch.setenv("KB_NEO4J__URL", "bolt://cloud-n:7687")
    monkeypatch.setenv("KB_LLM__ENDPOINT", "http://cloud-llm/v1")
    monkeypatch.setattr(setup_mod, "run_cloud_stack",
                        lambda p, e: True)

    rc = setup_mod.run_setup(
        prompt=lambda q: "unused",
        confirm=lambda q: True,
        echo=lambda msg: None)
    assert rc == 0


def test_cloud_failure_returns_exit_code_5(_isolate_config, monkeypatch):
    """run_cloud_stack returning False (no resolvable endpoints) -> exit 5."""
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))
    monkeypatch.setattr(setup_mod, "docker_available", lambda: False)
    monkeypatch.setattr(setup_mod, "run_cloud_stack", lambda p, e: False)

    rc = setup_mod.run_setup(
        prompt=lambda q: "",  # user answers empty -> required vars missing
        confirm=lambda q: True,
        echo=lambda msg: None)
    assert rc == 5


def test_local_stack_failure_returns_exit_code_3(_isolate_config, monkeypatch):
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))
    monkeypatch.setattr(setup_mod, "docker_available", lambda: True)
    monkeypatch.setattr(setup_mod, "run_local_stack", lambda p, e: False)

    rc = setup_mod.run_setup(
        prompt=lambda q: "unused",
        confirm=lambda q: True,
        echo=lambda msg: None)
    assert rc == 3


def test_force_cloud_skips_docker_detection(_isolate_config, monkeypatch):
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))
    monkeypatch.setattr(setup_mod, "run_health_checks",
                        lambda cfg: [_ok_check(n) for n in
                                     ("qdrant", "neo4j", "llm", "embedding")])

    def _no_docker_probe(*a, **kw):
        raise AssertionError("force_cloud must not probe docker")
    monkeypatch.setattr(setup_mod, "docker_available", _no_docker_probe)

    monkeypatch.setenv("KB_QDRANT__URL", "http://cloud-q:6333")
    monkeypatch.setenv("KB_NEO4J__URL", "bolt://cloud-n:7687")
    monkeypatch.setenv("KB_LLM__ENDPOINT", "http://cloud-llm/v1")
    monkeypatch.setattr(setup_mod, "run_cloud_stack", lambda p, e: True)

    rc = setup_mod.run_setup(
        prompt=lambda q: "unused",
        confirm=lambda q: True,
        echo=lambda msg: None,
        force_cloud=True)
    assert rc == 0


def test_skip_services_skips_backend_startup(_isolate_config, monkeypatch):
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))
    monkeypatch.setattr(setup_mod, "run_health_checks",
                        lambda cfg: [_ok_check(n) for n in
                                     ("qdrant", "neo4j", "llm", "embedding")])

    def _no_probe(*a, **kw):
        raise AssertionError("skip_services must not probe docker or cloud")
    monkeypatch.setattr(setup_mod, "docker_available", _no_probe)
    monkeypatch.setattr(setup_mod, "run_local_stack", _no_probe)
    monkeypatch.setattr(setup_mod, "run_cloud_stack", _no_probe)

    rc = setup_mod.run_setup(
        prompt=lambda q: "unused",
        confirm=lambda q: True,
        echo=lambda msg: None,
        skip_services=True)
    assert rc == 0


def test_existing_admin_account_creates_no_credentials_file(
        _isolate_config, monkeypatch):
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    # Pre-populate an admin row: create_admin_account must read it back,
    # not create a second account, and must not write a credentials file.
    from digital_twins.auth import hash_password
    from digital_twins.accounts import create_account
    create_account(db, "existing@example.com", "pw", role="admin")
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))
    monkeypatch.setattr(setup_mod, "run_health_checks",
                        lambda cfg: [_ok_check(n) for n in
                                     ("qdrant", "neo4j", "llm", "embedding")])
    monkeypatch.setattr(setup_mod, "docker_available", lambda: True)
    monkeypatch.setattr(setup_mod, "run_local_stack", lambda p, e: True)

    rc = setup_mod.run_setup(
        prompt=lambda q: "unused",
        confirm=lambda q: True,
        echo=lambda msg: None)
    assert rc == 0
    assert not (state_dir / "admin-credentials.txt").exists()


def test_health_check_failure_returns_exit_code_1(_isolate_config,
                                                   monkeypatch):
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))
    monkeypatch.setattr(setup_mod, "docker_available", lambda: True)
    monkeypatch.setattr(setup_mod, "run_local_stack", lambda p, e: True)
    monkeypatch.setattr(setup_mod, "run_health_checks", lambda cfg: [
        _ok_check("qdrant"), _fail_check("neo4j"),
        _ok_check("llm"), _ok_check("embedding")])

    rc = setup_mod.run_setup(
        prompt=lambda q: "unused",
        confirm=lambda q: True,
        echo=lambda msg: None)
    assert rc == 1


def test_create_admin_account_idempotent(_isolate_config, monkeypatch):
    """Two consecutive calls: first creates (returns the password), the
    second reads the existing row back (empty password, no second row)."""
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))

    email, pw = setup_mod.create_admin_account(state_dir,
                                                email="first@example.com")
    assert pw
    email2, pw2 = setup_mod.create_admin_account(state_dir,
                                                  email="second@example.com")
    assert email2 == "first@example.com"
    assert pw2 == ""
    db = _fake_connect(state_dir)
    rows = _admin_rows(db)
    db.close()
    assert [r[0] for r in rows] == ["first@example.com"]


def test_write_kb_local_leaves_existing_file_alone(_isolate_config):
    """write_kb_local is no-op when kb.local.yml already exists."""
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    existing = {"qdrant": {"url": "http://existing:6333"}}
    (config_dir / "kb.local.yml").parent.mkdir(parents=True)
    path = config_dir / "kb.local.yml"
    path.write_text(yaml.safe_dump(existing), encoding="utf-8")

    result = setup_mod.write_kb_local({"qdrant": {"url": "http://new:6333"}})
    assert result == path
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["qdrant"]["url"] == "http://existing:6333"


def test_write_kb_local_writes_when_absent(_isolate_config):
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    path = setup_mod.write_kb_local({"qdrant": {"url": "http://new:6333"}})
    assert path.is_file()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["qdrant"]["url"] == "http://new:6333"


def test_has_valid_local_config(_isolate_config, monkeypatch):
    import digital_twins.setup as setup_mod
    config_dir, state_dir = _isolate_config

    assert not setup_mod.has_valid_local_config()
    (config_dir / "kb.local.yml").parent.mkdir(parents=True)
    path = config_dir / "kb.local.yml"
    # no qdrant key -> not valid
    path.write_text(yaml.safe_dump({"neo4j": {"url": "bolt://x"}}),
                    encoding="utf-8")
    assert not setup_mod.has_valid_local_config()
    # qdrant present but empty -> not valid
    path.write_text(yaml.safe_dump({"qdrant": {"url": ""}}),
                    encoding="utf-8")
    assert not setup_mod.has_valid_local_config()
    # qdrant.url set -> valid
    path.write_text(yaml.safe_dump({"qdrant": {"url": "http://x:6333"}}),
                    encoding="utf-8")
    assert setup_mod.has_valid_local_config()
