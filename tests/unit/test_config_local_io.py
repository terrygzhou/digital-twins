"""008/US2 (T005, RED-first): machine-local config writer (kb.local.yml).

FR-010/FR-003 need a persistence path the web admin UI can write through;
neither the loader (read-only) nor user_config (per-user DB overrides)
provides it. T006 implements local_io.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from digital_twins.config import local_io
from digital_twins.config import loader


def _cfgdir(tmp_path: Path) -> str:
    return str(tmp_path / "cfg")


def test_local_config_path_resolves_config_dir(tmp_path):
    p = local_io.local_config_path(env={"KB_CONFIG_DIR": _cfgdir(tmp_path)})
    assert p == Path(_cfgdir(tmp_path)) / "kb.local.yml"
    assert local_io.local_config_path(env={"KB_CONFIG_DIR": _cfgdir(tmp_path)}) \
        == loader.local_config_path(env={"KB_CONFIG_DIR": _cfgdir(tmp_path)})


def test_local_config_path_default_home():
    p = local_io.local_config_path(env={})
    assert p.name == "kb.local.yml"


def test_merge_write_creates_file(tmp_path):
    p = local_io.merge_write({"llm": {"endpoint": "http://ext/v1"}},
                             env={"KB_CONFIG_DIR": _cfgdir(tmp_path)})
    assert p.exists()
    from digital_twins.config.loader import load
    cfg = load(cwd=tmp_path, env={"KB_CONFIG_DIR": _cfgdir(tmp_path)})
    assert cfg["llm"]["endpoint"] == "http://ext/v1"


def test_merge_write_preserves_unrelated_keys(tmp_path):
    p = local_io.merge_write({"qdrant": {"url": "http://a:6333"}},
                             env={"KB_CONFIG_DIR": _cfgdir(tmp_path)})
    local_io.merge_write({"llm": {"endpoint": "http://b/v1"}},
                         env={"KB_CONFIG_DIR": _cfgdir(tmp_path)})
    text = p.read_text()
    assert "qdrant" in text and "http://a:6333" in text
    assert "llm" in text and "http://b/v1" in text


def test_merge_write_abort_unparseable_yaml_no_write(tmp_path):
    d = Path(_cfgdir(tmp_path))
    d.mkdir(parents=True)
    p = d / "kb.local.yml"
    p.write_text("llm: [unclosed\n")
    with pytest.raises(Exception):
        local_io.merge_write({"neo4j": {"url": "bolt://x"}},
                             env={"KB_CONFIG_DIR": _cfgdir(tmp_path)})
    assert p.read_text() == "llm: [unclosed\n"


def test_merge_write_atomic_no_partial_on_replace_failure(tmp_path, monkeypatch):
    import os as _os
    d = Path(_cfgdir(tmp_path))
    p = local_io.merge_write({"llm": {"endpoint": "http://one"}},
                             env={"KB_CONFIG_DIR": _cfgdir(tmp_path)})
    real_replace = _os.replace

    def boom(*a, **k):
        raise PermissionError("disk full (simulated)")

    monkeypatch.setattr(_os, "replace", boom)
    try:
        with pytest.raises(PermissionError):
            local_io.merge_write({"llm": {"endpoint": "http://two"}},
                                 env={"KB_CONFIG_DIR": _cfgdir(tmp_path)})
    finally:
        monkeypatch.undo()
    assert p.read_text().count("http://one") == 1
    leftovers = [f for f in d.iterdir() if f.name != "kb.local.yml"]
    assert leftovers == [], f"partial temp files left behind: {leftovers}"
    assert real_replace is not None


def test_merge_write_refuses_out_of_dir_target(tmp_path):
    d = Path(_cfgdir(tmp_path))
    outsider = tmp_path / "elsewhere.yml"
    with pytest.raises(Exception):
        local_io.merge_write({"llm": {"endpoint": "x"}}, target=outsider,
                             env={"KB_CONFIG_DIR": _cfgdir(tmp_path)})
    assert not outsider.exists()
