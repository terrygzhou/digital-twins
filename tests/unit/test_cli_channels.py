"""channels-config T2: CLI ``channels`` group (RED-first).

Covers ``channels list`` / ``status`` / ``enable`` / ``disable`` / ``add``
on top of task 1's ``channel_view`` / ``channel_write``. CLI test pattern
matches 001's style: CliRunner against ``cli.cli`` with an isolated
config dir (KB_CONFIG_DIR) and chdir'd tmp cwd.

Credentials are secrets (BR-12.2.2): no credential value ever appears in
printed output — asserted directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from digital_twins.cli import cli


# --- harness ---------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Isolated config dir + cwd (001 CLI test pattern)."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.chdir(tmp_path)
    return config_dir


def _local(cfg_dir: Path) -> dict:
    p = cfg_dir / "kb.local.yml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _run(cfg_dir: Path, *args: str) -> "click.testing.Result":
    return CliRunner().invoke(cli, ["channels", *args])


# --- channels list ----------------------------------------------------------


def test_channels_list_fresh_all_builtins_disabled(cfg):
    r = _run(cfg, "list")
    assert r.exit_code == 0, r.output
    out = r.output
    for name in ("dsh", "fs", "gmail", "hermes", "paperclip", "pi", "yahoo"):
        line = next(
            (ln for ln in out.splitlines() if ln.split() and ln.split()[0] == name),
            "")
        assert line, f"row for {name!r} missing:\n{out}"
        assert "no" in line, f"{name} must show enabled=no:\n{line}"
        assert "yes" not in line.split(None, 2), f"{name} shows enabled:\n{line}"
    # rows sorted alphabetically (skip the header row)
    names = [ln.split()[0] for ln in out.splitlines()[1:] if ln.split()]
    assert names == sorted(names), names
    assert len(names) == 7, names
    # headers name the columns
    assert "credential" in out
    assert "prerequisites" in out


def test_channels_list_missing_prerequisite_shows(cfg):
    # pi has no sessions dir on a fresh install -> shows a prerequisite
    r = _run(cfg, "list")
    assert r.exit_code == 0, r.output
    line = next(ln for ln in r.output.splitlines()
                if ln.split() and ln.split()[0] == "pi")
    # unsatisfied prerequisites render as "BLOCKED: <names>" (spec: a
    # source with unsatisfied prerequisites is marked BLOCKED, not bare)
    cols = line.split()
    assert cols[0] == "pi"
    assert "BLOCKED:" in line, f"pi with no sessions dir must show BLOCKED: {line!r}"
    assert cols[-1] != "-", f"pi with no sessions dir must not be ready: {line!r}"


def test_channels_list_ready_shows_dash(cfg):
    # fs has no prerequisites on a fresh install -> the column renders "-"
    r = _run(cfg, "list")
    assert r.exit_code == 0, r.output
    line = next(ln for ln in r.output.splitlines()
                if ln.split() and ln.split()[0] == "fs")
    cols = line.split()
    assert cols[0] == "fs"
    assert "BLOCKED" not in line, f"fs has no prerequisites: {line!r}"
    assert cols[-1] == "-", f"fs ready row must end with '-': {line!r}"


def test_channels_list_enabled_shows_yes(cfg):
    local_io_local = cfg / "kb.local.yml"
    local_io_local.parent.mkdir(parents=True, exist_ok=True)
    local_io_local.write_text(yaml.safe_dump(
        {"sources": {"fs": {"enabled": True}}}))
    r = _run(cfg, "list")
    assert r.exit_code == 0, r.output
    line = next(ln for ln in r.output.splitlines()
                if ln.split() and ln.split()[0] == "fs")
    assert "yes" in line


def test_channels_list_never_prints_credential_value(cfg, monkeypatch):
    secret = "s3cret-value"
    monkeypatch.setenv("YMAIL_APP_PASSWORD", secret)
    r = _run(cfg, "list")
    assert r.exit_code == 0, r.output
    assert secret not in r.output


# --- channels status --------------------------------------------------------


def test_channels_status_single_source(cfg):
    r = _run(cfg, "status", "fs")
    assert r.exit_code == 0, r.output
    out = r.output
    assert "fs" in out
    assert "no" in out  # enabled=no on fresh install


def test_channels_status_unknown_exit1(cfg):
    r = _run(cfg, "status", "nope")
    assert r.exit_code == 1
    assert "unknown source: nope" in r.output


# --- channels enable / disable ---------------------------------------------


def test_channels_enable_writes_kb_local_only(cfg):
    r = _run(cfg, "enable", "fs")
    assert r.exit_code == 0, r.output
    local = _local(cfg)
    assert local["sources"]["fs"]["enabled"] is True
    # kb.yml must not be touched by the CLI write
    assert not (cfg / "kb.yml").exists()


def test_channels_enable_with_caps(cfg):
    r = _run(cfg, "enable", "fs", "--max-items", "42", "--timeout-s", "90")
    assert r.exit_code == 0, r.output
    local = _local(cfg)
    src = local["sources"]["fs"]
    assert src["enabled"] is True
    assert src["max_items"] == 42
    assert src["timeout_s"] == 90


def test_channels_disable_writes_false(cfg):
    (cfg / "kb.local.yml").parent.mkdir(parents=True, exist_ok=True)
    (cfg / "kb.local.yml").write_text(
        yaml.safe_dump({"sources": {"fs": {"enabled": True}}}))
    r = _run(cfg, "disable", "fs")
    assert r.exit_code == 0, r.output
    assert _local(cfg)["sources"]["fs"]["enabled"] is False


def test_channels_enable_unknown_source_exit1(cfg):
    r = _run(cfg, "enable", "nope")
    assert r.exit_code == 1
    assert "unknown source" in r.output
    assert not (cfg / "kb.local.yml").exists()


# --- channels add -----------------------------------------------------------


def test_channels_add_bad_entrypoint_no_write(cfg):
    r = _run(cfg, "add", "mytool", "--entrypoint", "no_such_module_zz:make")
    assert r.exit_code == 1
    assert "no_such_module_zz" in r.output
    assert not (cfg / "kb.local.yml").exists()


def test_channels_add_missing_factory_no_write(cfg):
    r = _run(cfg, "add", "mytool", "--entrypoint", "os:NoSuchFactory")
    assert r.exit_code == 1
    assert "NoSuchFactory" in r.output
    assert not (cfg / "kb.local.yml").exists()


def test_channels_add_good_entrypoint(cfg):
    r = _run(cfg, "add", "mytool",
             "--entrypoint", "digital_twins.sources.fs:factory")
    assert r.exit_code == 0, r.output
    local = _local(cfg)
    entry = local["sources"]["mytool"]
    assert entry["enabled"] is False
    assert entry["entrypoint"] == "digital_twins.sources.fs:factory"
    # a just-added channel shows up in the view -> list it
    r2 = _run(cfg, "list")
    assert r2.exit_code == 0, r2.output
    assert "mytool" in r2.output
    r3 = _run(cfg, "status", "mytool")
    assert r3.exit_code == 0, r3.output


def test_channels_add_with_credential_and_prefix(cfg):
    r = _run(cfg, "add", "mytool",
             "--entrypoint", "digital_twins.sources.fs:factory",
             "--credential", "MYTOOL_TOKEN",
             "--prefix", "mytool:")
    assert r.exit_code == 0, r.output
    entry = _local(cfg)["sources"]["mytool"]
    assert entry["credential"] == "MYTOOL_TOKEN"
    assert entry["prefix"] == "mytool:"
    # the credential env var *name* may appear; the value must not
    monkeypatch_free_check(cfg, "MYTOOL_TOKEN")


def monkeypatch_free_check(cfg_dir: Path, var: str) -> None:
    import os
    value = os.environ.get(var, "")
    r = _run(cfg_dir, "list")
    if value:
        assert value not in r.output
    # and the var name is fine to appear
    assert var in r.output
