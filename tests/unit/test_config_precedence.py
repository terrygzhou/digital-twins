"""Four-layer config precedence (the T026 invariant, pulled forward).

env (incl. .env) > kb.local.yml > kb.yml > built-in defaults — and the
winner must be provable layer by layer.
"""
import os
import textwrap

import pytest

from digital_twins.config import ConfigError, SchemaError, env_var_for, get, load




KB_YAML = """
qdrant:
  url: https://from-kb:6333
chunking:
  max_chars: 1111
"""

LOCAL_YAML = """
qdrant:
  url: https://from-kb-local:6333
"""


def write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def test_env_beats_local_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("KB_QDRANT__URL", "https://from-env:6333")
    write(tmp_path / "kb.local.yml", LOCAL_YAML)
    cfg = load(cwd=tmp_path, config_dir=tmp_path)
    assert get(cfg, "qdrant.url") == "https://from-env:6333"


def test_local_yaml_beats_yaml(tmp_path):
    write(tmp_path / "kb.yml", KB_YAML)
    write(tmp_path / "kb.local.yml", LOCAL_YAML)
    cfg = load(cwd=tmp_path, config_dir=tmp_path)
    assert get(cfg, "qdrant.url") == "https://from-kb-local:6333"
    assert get(cfg, "chunking.max_chars") == 1111  # untouched layer survives


def test_yaml_beats_defaults(tmp_path):
    write(tmp_path / "kb.yml", KB_YAML)
    cfg = load(cwd=tmp_path, config_dir=tmp_path)
    assert get(cfg, "qdrant.url") == "https://from-kb:6333"
    assert get(cfg, "chunking.max_chars") == 1111


def test_defaults_when_nothing_else(tmp_path):
    cfg = load(cwd=tmp_path, config_dir=tmp_path)
    assert get(cfg, "embedding.model") == "BAAI/bge-small-en-v1.5"
    assert get(cfg, "embedding.device") == "auto"
    assert get(cfg, "chunking.max_chars") == 800
    assert get(cfg, "chunking.overlap") == 100
    assert get(cfg, "qdrant.url") is None
    assert get(cfg, "state_dir") == os.path.expanduser("~/.digital-twins")
    assert get(cfg, "config_dir") == str(tmp_path)


def test_env_beats_dotenv_beats_local_yaml(monkeypatch, tmp_path):
    """Real env beats .env beats kb.local.yml for the same knob."""
    monkeypatch.setenv("KB_QDRANT__URL", "https://from-env:6333")
    write(tmp_path / ".env", "KB_CHUNKING__MAX_CHARS=555\n")
    write(tmp_path / "kb.local.yml", "chunking:\n  max_chars: 2222\n")
    cfg = load(cwd=tmp_path, config_dir=tmp_path)
    assert get(cfg, "qdrant.url") == "https://from-env:6333"
    assert get(cfg, "chunking.max_chars") == 555


def test_real_env_beats_dotenv(monkeypatch, tmp_path):
    monkeypatch.setenv("KB_CHUNKING__OVERLAP", "7")
    write(tmp_path / ".env", "KB_CHUNKING__OVERLAP=99\n")
    cfg = load(cwd=tmp_path, config_dir=tmp_path)
    assert get(cfg, "chunking.overlap") == 7


def test_env_values_are_type_coerced(monkeypatch, tmp_path):
    monkeypatch.setenv("KB_CHUNKING__MAX_CHARS", "900")
    monkeypatch.setenv("KB_SOURCES__FS__ENABLED", "true")
    cfg = load(cwd=tmp_path, config_dir=tmp_path)
    assert get(cfg, "chunking.max_chars") == 900
    assert get(cfg, "sources.fs.enabled") is True


def test_file_values_are_type_coerced(tmp_path):
    write(tmp_path / "kb.yml", "chunking:\n  max_chars: '640'\n")
    cfg = load(cwd=tmp_path, config_dir=tmp_path)
    assert get(cfg, "chunking.max_chars") == 640


def test_bad_env_int_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("KB_CHUNKING__MAX_CHARS", "abc")
    with pytest.raises(SchemaError):
        load(cwd=tmp_path, config_dir=tmp_path)


def test_fresh_config_has_all_builtin_sources_disabled(tmp_path):
    cfg = load(cwd=tmp_path, config_dir=tmp_path)
    names = ["hermes", "pi", "dsh", "paperclip", "yahoo", "gmail", "fs"]
    for name in names:
        assert get(cfg, f"sources.{name}.enabled") is False
        assert get(cfg, f"sources.{name}.max_items") == 200
        assert get(cfg, f"sources.{name}.timeout_s") == 1500


def test_unknown_source_knob_rejected(tmp_path):
    write(tmp_path / "kb.yml", "sources:\n  fs:\n    nonsense: 1\n")
    with pytest.raises(SchemaError, match="nonsense"):
        load(cwd=tmp_path, config_dir=tmp_path)


def test_source_extra_is_source_specific_and_allowed(tmp_path):
    write(tmp_path / "kb.yml",
          "sources:\n  fs:\n    extra:\n      dir: /somewhere\n")
    cfg = load(cwd=tmp_path, config_dir=tmp_path)
    assert get(cfg, "sources.fs.extra.dir") == "/somewhere"


def test_custom_source_defaults(tmp_path):
    write(tmp_path / "kb.yml", "sources:\n  mytool:\n    enabled: true\n")
    cfg = load(cwd=tmp_path, config_dir=tmp_path)
    assert get(cfg, "sources.mytool.enabled") is True
    assert get(cfg, "sources.mytool.max_items") == 200
    assert get(cfg, "sources.mytool.prefix") == "mytool:"


def test_cwd_kbyl_is_used_when_configdir_empty(tmp_path):
    write(tmp_path / "kb.yml", KB_YAML)
    cfg = load(cwd=tmp_path, config_dir=tmp_path / "empty-user-dir")
    assert get(cfg, "qdrant.url") == "https://from-kb:6333"


def test_configdir_kbyl_beats_cwd_kbyl(tmp_path):
    write(tmp_path / "kb.yml", "qdrant:\n  url: https://from-cwd:6333\n")
    user = tmp_path / "user"
    user.mkdir()
    write(user / "kb.yml", "qdrant:\n  url: https://from-user:6333\n")
    cfg = load(cwd=tmp_path, config_dir=user)
    assert get(cfg, "qdrant.url") == "https://from-user:6333"


def test_env_var_mapping():
    assert env_var_for("qdrant.url") == "KB_QDRANT__URL"
    assert env_var_for("state_dir") == "KB_STATE_DIR"
    assert env_var_for("embedding.device") == "KB_EMBEDDING__DEVICE"


def test_bootstrap_uses_env_config_dir(monkeypatch, tmp_path):
    """When config_dir is not passed, KB_CONFIG_DIR must be honored."""
    user = tmp_path / "user"
    user.mkdir()
    write(user / "kb.local.yml", LOCAL_YAML)
    monkeypatch.setenv("KB_CONFIG_DIR", str(user))
    cfg = load(cwd=tmp_path)
    assert get(cfg, "qdrant.url") == "https://from-kb-local:6333"
