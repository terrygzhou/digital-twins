"""channels-config T1: channel view + write helpers (config layer).

Test-First (RED-first) for ``digital_twins/config/channels.channel_view``
and ``digital_twins/config/local_io.channel_write``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from digital_twins.config import local_io
from digital_twins.config.channels import channel_view
from digital_twins.config.loader import load
from digital_twins.config.schema import BUILTIN_SOURCES, SchemaError

_CREDENTIAL_VARS = ("YMAIL_APP_PASSWORD", "GMAIL_APP_PASSWORD",
                    "MYTOOL_TOKEN")


def _cleared_creds(monkeypatch):
    for var in _CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)


def _cfgdir(tmp_path: Path) -> Path:
    return tmp_path / "cfg"


def _env(cfgdir: Path) -> dict:
    return {"KB_CONFIG_DIR": str(cfgdir)}


def _write_kb_yml(cfgdir: Path, doc: dict) -> None:
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "kb.yml").write_text(yaml.safe_dump(doc))


def _view(tmp_path, kb_doc=None, env_extra=None, monkeypatch=None):
    cfgdir = _cfgdir(tmp_path)
    if kb_doc is not None:
        _write_kb_yml(cfgdir, kb_doc)
    env = _env(cfgdir)
    if env_extra:
        env.update(env_extra)
    return channel_view(str(cfgdir), env=env)


# --- 1.1 channel_view --------------------------------------------------------


def test_fresh_install_all_builtins_disabled(tmp_path, monkeypatch):
    _cleared_creds(monkeypatch)
    view = _view(tmp_path, monkeypatch=monkeypatch)
    assert set(view) == set(BUILTIN_SOURCES)
    for name, row in view.items():
        assert row["enabled"] is False, name
        assert row["max_items"] == 200, name
        assert row["timeout_s"] == 1500, name
        assert isinstance(row["prerequisites"], list), name
        assert row["credential_set"] in (True, False), name
    for no_cred in ("hermes", "pi", "dsh", "paperclip", "fs"):
        assert view[no_cred]["credential_set"] is True, no_cred
    assert view["yahoo"]["credential_set"] is False
    assert view["gmail"]["credential_set"] is False


def test_fresh_install_no_prereqs_leak(tmp_path, monkeypatch):
    _cleared_creds(monkeypatch)
    view = _view(tmp_path, monkeypatch=monkeypatch)
    # pi/dsh/paperclip have unset session-store paths on a fresh install
    assert view["pi"]["prerequisites"], "pi without sessions_dir must not be ready"
    assert view["dsh"]["prerequisites"]
    assert view["paperclip"]["prerequisites"]


def test_prerequisites_ready_when_extra_set(tmp_path, monkeypatch):
    _cleared_creds(monkeypatch)
    files = tmp_path / "files"
    files.mkdir()
    view = _view(tmp_path, kb_doc={"sources": {"fs": {"extra": {"dir": str(files)}}}},
                 monkeypatch=monkeypatch)
    assert view["fs"]["prerequisites"] == []


def test_credential_set_flips_with_env(tmp_path, monkeypatch):
    _cleared_creds(monkeypatch)
    assert _view(tmp_path, monkeypatch=monkeypatch)["yahoo"]["credential_set"] is False
    monkeypatch.setenv("YMAIL_APP_PASSWORD", "s3cret-value")
    view = _view(tmp_path, monkeypatch=monkeypatch)
    assert view["yahoo"]["credential_set"] is True
    # the credential value must never appear anywhere in the view
    assert "s3cret-value" not in yaml.safe_dump(view)


def test_env_layer_wins_over_kb_yml(tmp_path, monkeypatch):
    _cleared_creds(monkeypatch)
    view = _view(tmp_path,
                  kb_doc={"sources": {"fs": {"enabled": True, "max_items": 7}}},
                  env_extra={"KB_SOURCES__FS__MAX_ITEMS": "3"},
                  monkeypatch=monkeypatch)
    assert view["fs"]["enabled"] is True
    assert view["fs"]["max_items"] == 3


def test_custom_source_broken_entrypoint(tmp_path, monkeypatch):
    _cleared_creds(monkeypatch)
    view = _view(tmp_path,
                  kb_doc={"sources": {"mytool": {
                      "enabled": False,
                      "entrypoint": "no_such_module_zz:make"}}},
                  monkeypatch=monkeypatch)
    assert "mytool" in view
    row = view["mytool"]
    assert row["enabled"] is False
    assert row["prerequisites"], "broken entrypoint must surface as a prerequisite"
    assert "no_such_module_zz" in row["prerequisites"][0]
    # no entrypoint key -> not a channel
    view2 = _view(tmp_path,
                  kb_doc={"sources": {"plain": {"enabled": False}}},
                  monkeypatch=monkeypatch)
    assert "plain" not in view2
    assert set(view2) == set(BUILTIN_SOURCES)


def test_custom_source_working_entrypoint(tmp_path, monkeypatch):
    _cleared_creds(monkeypatch)
    files = tmp_path / "files"
    files.mkdir()
    view = _view(tmp_path,
                  kb_doc={"sources": {"mytool": {
                      "entrypoint": "digital_twins.sources.fs:factory",
                      "extra": {"dir": str(files)}}}},
                  monkeypatch=monkeypatch)
    row = view["mytool"]
    assert row["prerequisites"] == []
    assert row["credential_set"] is True


def test_custom_credential_env_var(tmp_path, monkeypatch):
    _cleared_creds(monkeypatch)
    files = tmp_path / "files"
    files.mkdir()
    doc = {"sources": {"mytool": {
        "entrypoint": "digital_twins.sources.fs:factory",
        "credential": "MYTOOL_TOKEN",
        "extra": {"dir": str(files)}}}}
    view = _view(tmp_path, kb_doc=doc, monkeypatch=monkeypatch)
    assert view["mytool"]["credential_set"] is False
    monkeypatch.setenv("MYTOOL_TOKEN", "tok")
    assert _view(tmp_path, kb_doc=doc, monkeypatch=monkeypatch)["mytool"]["credential_set"] is True


# --- 1.2 channel_write -------------------------------------------------------


def test_channel_write_preserves_existing_keys(tmp_path):
    cfgdir = _cfgdir(tmp_path)
    env = _env(cfgdir)
    local_io.merge_write({"llm": {"endpoint": "http://one/v1"}}, env=env)
    p = local_io.channel_write({"sources": {"fs": {"enabled": True}}}, env=env)
    assert p == cfgdir / "kb.local.yml"
    cfg = load(cwd=tmp_path, env=env)
    assert cfg["llm"]["endpoint"] == "http://one/v1"
    assert cfg["sources"]["fs"]["enabled"] is True
    # defaults of untouched sources survive
    assert cfg["sources"]["hermes"]["enabled"] is False


def test_channel_write_unknown_source_raises(tmp_path):
    cfgdir = _cfgdir(tmp_path)
    env = _env(cfgdir)
    with pytest.raises(SchemaError, match="nope"):
        local_io.channel_write({"sources": {"nope": {"enabled": True}}}, env=env)
    assert not (cfgdir / "kb.local.yml").exists()


def test_channel_write_registered_custom_source_ok(tmp_path):
    cfgdir = _cfgdir(tmp_path)
    _write_kb_yml(cfgdir, {"sources": {"mytool": {
        "entrypoint": "digital_twins.sources.fs:factory"}}})
    env = _env(cfgdir)
    local_io.channel_write({"sources": {"mytool": {"enabled": True}}}, env=env)
    cfg = load(cwd=tmp_path, env=env)
    assert cfg["sources"]["mytool"]["enabled"] is True


def test_channel_write_invalid_key_raises(tmp_path):
    cfgdir = _cfgdir(tmp_path)
    env = _env(cfgdir)
    with pytest.raises(SchemaError, match="bogus"):
        local_io.channel_write({"sources": {"fs": {"bogus": 1}}}, env=env)
    # built-in: custom-only knobs are not allowed
    with pytest.raises(SchemaError, match="entrypoint"):
        local_io.channel_write({"sources": {"fs": {"entrypoint": "x:y"}}}, env=env)


def test_channel_write_credential_key_allowed_for_builtin(tmp_path):
    # schema._validate_source allows "credential" for every source type
    cfgdir = _cfgdir(tmp_path)
    env = _env(cfgdir)
    local_io.channel_write(
        {"sources": {"fs": {"credential": "MY_TOKEN"}}}, env=env)
    cfg = load(cwd=tmp_path, env=env)
    assert cfg["sources"]["fs"]["credential"] == "MY_TOKEN"


def test_channel_write_requires_sources_mapping(tmp_path):
    cfgdir = _cfgdir(tmp_path)
    env = _env(cfgdir)
    with pytest.raises(SchemaError):
        local_io.channel_write({"qdrant": {"url": "http://x"}}, env=env)
    with pytest.raises(SchemaError):
        local_io.channel_write({"sources": {}}, env=env)
    with pytest.raises(SchemaError):
        local_io.channel_write({"sources": {"fs": "enabled"}}, env=env)
