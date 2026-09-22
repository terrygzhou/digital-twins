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


def test_skip_services_precedence_over_valid_config(_isolate_config,
                                                    monkeypatch):
    """B1 regression: --skip-services must win even when kb.local.yml is
    valid — no docker probe, no cloud prompt, no service startup."""
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    # A valid local config: without skip_services the wizard would skip
    # service startup anyway, but it must NOT probe docker. With
    # skip_services the probe must not happen regardless.
    (config_dir / "kb.local.yml").parent.mkdir(parents=True)
    (config_dir / "kb.local.yml").write_text(
        yaml.safe_dump({"qdrant": {"url": "http://q:6333"}}),
        encoding="utf-8")
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


def test_skip_services_with_cloud_envs_still_no_service(_isolate_config,
                                                        monkeypatch):
    """--skip-services takes precedence over force_cloud + env vars:
    even with KB_QDRANT__URL set, no cloud stack runs."""
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))
    monkeypatch.setattr(setup_mod, "run_health_checks",
                        lambda cfg: [_ok_check(n) for n in
                                     ("qdrant", "neo4j", "llm", "embedding")])
    monkeypatch.setenv("KB_QDRANT__URL", "http://cloud-q:6333")
    monkeypatch.setenv("KB_NEO4J__URL", "bolt://cloud-n:7687")
    monkeypatch.setenv("KB_LLM__ENDPOINT", "http://cloud-llm/v1")

    def _no_probe(*a, **kw):
        raise AssertionError("skip_services must win over force_cloud")
    monkeypatch.setattr(setup_mod, "docker_available", _no_probe)
    monkeypatch.setattr(setup_mod, "run_local_stack", _no_probe)
    monkeypatch.setattr(setup_mod, "run_cloud_stack", _no_probe)

    rc = setup_mod.run_setup(
        prompt=lambda q: "unused",
        confirm=lambda q: True,
        echo=lambda msg: None,
        force_cloud=True,
        skip_services=True)
    assert rc == 0
    # No kb.local.yml should have been written by the cloud path.
    assert not (config_dir / "kb.local.yml").is_file()


def test_cloud_empty_env_vars_fail_with_named_vars(_isolate_config,
                                                    monkeypatch):
    """M1 regression: env vars set to empty string are honored as
    'set but empty' (no prompt fallthrough) and the gate names the exact
    vars that are empty."""
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    prompts = []
    monkeypatch.setenv("KB_QDRANT__URL", "http://q:6333")
    monkeypatch.setenv("KB_NEO4J__URL", "")  # explicitly empty
    monkeypatch.delenv("KB_LLM__ENDPOINT", raising=False)

    # Call the real run_cloud_stack: qdrant from env, neo4j empty (no
    # prompt), llm unset (prompt). The gate must fail on neo4j and name
    # it; the prompt for llm must NOT have been reached.
    prompt_calls = []
    def fake_prompt(label):
        prompt_calls.append(label)
        return "http://llm-answered:8000/v1"
    monkeypatch.setenv("KB_LLM__ENDPOINT", "")  # also empty
    ok = setup_mod.run_cloud_stack(prompt_text=fake_prompt,
                                    echo=lambda m: None)
    assert ok is False
    # No prompt should have been shown: all three vars were set (even if
    # two are empty), so the gate runs against the empty values.
    assert prompt_calls == []


def test_cloud_unset_env_vars_prompt_and_write(_isolate_config,
                                               monkeypatch):
    """When the 3 required vars are unset, the prompts fire; the answered
    values land in kb.local.yml (plus optional keys when set)."""
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    monkeypatch.delenv("KB_QDRANT__URL", raising=False)
    monkeypatch.delenv("KB_NEO4J__URL", raising=False)
    monkeypatch.delenv("KB_LLM__ENDPOINT", raising=False)
    monkeypatch.delenv("KB_EMBEDDING__ENDPOINT", raising=False)
    monkeypatch.delenv("KB_NEO4J__USER", raising=False)
    monkeypatch.delenv("KB_NEO4J__PASSWORD", raising=False)
    monkeypatch.setenv("KB_EMBEDDING__ENDPOINT", "http://embed:8080/v1")
    monkeypatch.setenv("KB_NEO4J__USER", "neo4j-user")
    monkeypatch.setenv("KB_NEO4J__PASSWORD", "neo4j-pw")

    answers = iter([
        "http://cloud-q:6333",
        "bolt://cloud-n:7687",
        "http://cloud-llm:8000/v1",
    ])
    ok = setup_mod.run_cloud_stack(prompt_text=lambda label: next(answers),
                                   echo=lambda m: None)
    assert ok is True
    data = yaml.safe_load((config_dir / "kb.local.yml").read_text())
    assert data["qdrant"]["url"] == "http://cloud-q:6333"
    assert data["neo4j"]["url"] == "bolt://cloud-n:7687"
    assert data["neo4j"]["user"] == "neo4j-user"
    assert data["neo4j"]["password"] == "neo4j-pw"
    assert data["llm"]["endpoint"] == "http://cloud-llm:8000/v1"
    assert data["embedding"]["endpoint"] == "http://embed:8080/v1"


def test_local_stack_no_gpu_omits_llm_and_embedding(_isolate_config,
                                                    monkeypatch):
    """No-GPU path: _gpu_present() False -> llm excluded from up_services,
    written kb.local.yml omits llm.endpoint + embedding.endpoint."""
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    # No nvidia-smi binary: the _gpu_present probe reports False.
    monkeypatch.setattr(setup_mod, "_gpu_present", lambda: False)
    # Capture what up_services would be by looking at the compose args.
    compose_args = []
    def fake_compose(args, **kw):
        compose_args.append(list(args))
        return 0
    monkeypatch.setattr(setup_mod, "_docker_compose", fake_compose)

    ok = setup_mod.run_local_stack(prompt=lambda q: "unused",
                                   echo=lambda m: None)
    assert ok is True
    # The up call's args should not include 'llm'.
    up_call = [a for a in compose_args if a[0] == "up"]
    assert up_call, "expected a 'docker compose up' call"
    up_services = up_call[0][2:]
    assert "llm" not in up_services
    # kb.local.yml omits llm + embedding.
    data = yaml.safe_load((config_dir / "kb.local.yml").read_text())
    assert "llm" not in data
    assert "embedding" not in data
    assert "qdrant" in data
    assert "neo4j" in data


def test_local_stack_half_up_polls_survivors(_isolate_config, monkeypatch):
    """M3 regression: when `docker compose up` fails (rc!=0) the
    wizard still health-polls the mandatory services and, when they
    are healthy, writes kb.local.yml and returns True (not False/exit 3)."""
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_gpu_present", lambda: False)

    def fake_compose(args, **kw):
        # `up` fails; everything else succeeds.
        return 1 if args[0] == "up" else 0
    monkeypatch.setattr(setup_mod, "_docker_compose", fake_compose)
    monkeypatch.setattr(setup_mod, "_poll_url", lambda url, **kw: True)

    ok = setup_mod.run_local_stack(prompt=lambda q: "unused",
                                   echo=lambda m: None)
    assert ok is True
    # kb.local.yml was written despite up failing.
    data = yaml.safe_load((config_dir / "kb.local.yml").read_text())
    assert data["qdrant"]["url"] == "http://localhost:6333"


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


def test_admin_created_when_first_row_is_reader(_isolate_config, monkeypatch):
    """M5 regression: a pre-existing non-admin row (reader) does NOT count
    as 'admin exists' — the wizard creates an admin alongside it."""
    config_dir, state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    from digital_twins.accounts import create_account
    create_account(db, "reader@example.com", "pw", role="reader")
    db.close()
    monkeypatch.setattr(setup_mod, "connect", lambda d: _fake_connect(d))

    email, pw = setup_mod.create_admin_account(state_dir)
    assert pw, "a fresh admin password must be generated even when a reader exists"
    assert email != "reader@example.com"
    # The reader row is untouched; an admin row now exists.
    db = _fake_connect(state_dir)
    rows = db.execute(
        "SELECT email, role FROM accounts ORDER BY rowid").fetchall()
    db.close()
    by_email = {r[0]: r[1] for r in rows}
    assert by_email["reader@example.com"] == "reader"
    assert by_email[email] == "admin"
    # The credentials file points at the new admin, not the reader.
    cred = (state_dir / "admin-credentials.txt").read_text(encoding="utf-8")
    assert f"email: {email}" in cred
    assert "reader@example.com" not in cred


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

# ---------------------------------------------------------------------------
# 0.11.2 wizard fixes: neo4j credential re-prompt + endpoint whitespace strip
# ---------------------------------------------------------------------------


def test_cloud_whitespace_in_endpoints_is_stripped(_isolate_config,
                                                    monkeypatch):
    """A pasted URL with a trailing/leading space must be stripped before
    being written to kb.local.yml (the opaque 'InvalidURL: control
    characters' health-check failure)."""
    config_dir, _state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    for var in ("KB_NEO4J__USER", "KB_NEO4J__PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    prompts = iter([
        "http://cloud-q:6333 ",
        " bolt://cloud-n:7687 ",
        "http://cloud-llm:8000/v1 ",
        "neo4j-user",
        "neo4j-pw",
    ])
    ok = setup_mod.run_cloud_stack(prompt_text=lambda label: next(prompts),
                                   echo=lambda m: None)
    assert ok is True
    data = yaml.safe_load((config_dir / "kb.local.yml").read_text())
    assert data["qdrant"]["url"] == "http://cloud-q:6333"
    assert data["neo4j"]["url"] == "bolt://cloud-n:7687"
    assert data["llm"]["endpoint"] == "http://cloud-llm:8000/v1"


def test_cloud_neo4j_credentials_reprompt_when_empty(_isolate_config,
                                                      monkeypatch):
    """When the Neo4j URL is answered but user+password are left empty,
    the wizard re-prompts; credentials entered on the re-prompt are
    written to kb.local.yml."""
    config_dir, _state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    monkeypatch.delenv("KB_NEO4J__USER", raising=False)
    monkeypatch.delenv("KB_NEO4J__PASSWORD", raising=False)

    prompts = iter([
        "http://cloud-q:6333",
        "bolt://cloud-n:7687",
        "http://cloud-llm:8000/v1",
        "",            # neo4j user: first answer empty -> re-prompt
        "",            # neo4j password: first answer empty -> re-prompt
        "neo4j-user",  # re-prompt user
        "neo4j-pw",    # re-prompt password
    ])
    ok = setup_mod.run_cloud_stack(prompt_text=lambda label: next(prompts),
                                   echo=lambda m: None)
    assert ok is True
    data = yaml.safe_load((config_dir / "kb.local.yml").read_text())
    assert data["neo4j"]["user"] == "neo4j-user"
    assert data["neo4j"]["password"] == "neo4j-pw"


def test_cloud_neo4j_auth_disabled_stays_clean(_isolate_config,
                                                monkeypatch):
    """Auth-disabled Neo4j: every re-prompt answered empty -> kb.local.yml
    carries no user/password keys (the health check tolerates that)."""
    config_dir, _state_dir = _isolate_config
    import digital_twins.setup as setup_mod

    monkeypatch.delenv("KB_NEO4J__USER", raising=False)
    monkeypatch.delenv("KB_NEO4J__PASSWORD", raising=False)

    prompts = iter([
        "http://cloud-q:6333",
        "bolt://cloud-n:7687",
        "http://cloud-llm:8000/v1",
    ])
    ok = setup_mod.run_cloud_stack(
        prompt_text=lambda label: next(prompts, ""),
        echo=lambda m: None)
    assert ok is True
    data = yaml.safe_load((config_dir / "kb.local.yml").read_text())
    assert "user" not in data["neo4j"]
    assert "password" not in data["neo4j"]
