"""CLI dimension guard (T033, S2-mismatch, NFR-2).

`digital-twins run` must exit 1 (config/validate failure) when the Qdrant
collection's dimension does not match the pinned embedding model — the
error names both dimensions and the remediation.
"""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from digital_twins import __version__, cli as cli_mod
from digital_twins.health import QDRANT_COLLECTION


def _install_qdrant(monkeypatch, dim):
    """Install a fake qdrant_client whose personal_kb collection is `dim`-dim."""
    import sys
    import types

    class FakeQdrant:
        def __init__(self, url=None, api_key=None):
            pass

        def collection_exists(self, name):
            return name == QDRANT_COLLECTION

        def get_collection(self, name):
            return SimpleNamespace(config=SimpleNamespace(
                params=SimpleNamespace(vectors=SimpleNamespace(size=dim))))

        def upsert(self, *a, **kw):
            pass

    mod = types.ModuleType("qdrant_client")
    mod.QdrantClient = FakeQdrant
    monkeypatch.setitem(sys.modules, "qdrant_client", mod)


def test_run_dimension_mismatch_exits_1(tmp_path, monkeypatch):
    """run with a 1536-dim collection against the pinned 384-dim model ->
    exit 1, no collection writes, error names both dims + remediation."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "kb.local.yml").write_text(
        "qdrant:\n"
        "  url: http://fake:6333\n"
        "sources:\n"
        "  fs:\n"
        "    enabled: true\n"
        "    extra:\n"
        "      dir: " + str(tmp_path / "files") + "\n",
        encoding="utf-8")
    (tmp_path / "files").mkdir()
    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    _install_qdrant(monkeypatch, 1536)

    result = CliRunner().invoke(cli_mod.cli, ["run"])
    assert result.exit_code == 1, result.output
    out = result.output.lower()
    assert "1536" in out and "384" in out
    assert "dimension mismatch" in out


# --- T034: machine-readable --version ---------------------------------------

def test_version_plain_stdout_only(tmp_path, monkeypatch):
    """--version prints exactly the bare version string, exit 0."""
    monkeypatch.chdir(tmp_path)  # no config required for version
    result = CliRunner().invoke(cli_mod.cli, ["--version"])
    line = result.output.splitlines()
    assert len(line) == 1
    # the version number is the parseable token on the single output line
    assert __version__ in line[0]


def test_version_json_machine_readable(tmp_path, monkeypatch):
    """--version-json prints a JSON object with the version (machine-readable)."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli_mod.cli, ["--version-json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["name"] == "digital-twins"
    assert data["version"] == __version__
