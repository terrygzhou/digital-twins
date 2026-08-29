"""run --once --as merges user config + passes owner (C-3 / US3 S1).

C-3 (specs/003-multi-user/spec.md) explicitly names "CLI `run --as`" as a
config-merge site.  US3 S1: "Given alice's override `max_items: 50` on
hermes, When bob's hermes schedule fires, Then bob's configured cap applies
(not alice's)."  The serve-tick path implements this; the manual
`run --once --as` path must also merge the caller's per-user overrides into
a copy of the global config and stamp the owner on ingested points.

Red test first: `run --once --as` currently ignores user_config overrides
and does not pass `owner=` to `run_pipeline`.
"""

import os
import yaml
import pytest
from click.testing import CliRunner

from digital_twins.cli import cli
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


def _seed_account(db, email, role, password="pw123"):
    from digital_twins.auth import hash_password
    db.execute(
        "INSERT INTO accounts (email, role, password_hash, created_at, last_active) "
        "VALUES (?, ?, ?, '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')",
        (email, role, hash_password(password)),
    )
    db.commit()


def _seed_user_config(db, email, source, key, value):
    from digital_twins.user_config import set_override
    set_override(db, email, source, key, value)


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    # Global config: hermes enabled, max_items=100
    cfg_dict = {
        "sources": {"hermes": {"enabled": True, "max_items": 100}},
        "qdrant": {"url": "http://localhost:6333"},
    }
    (config_dir / "kb.yml").write_text(yaml.safe_dump(cfg_dict), encoding="utf-8")

    # Local override
    (config_dir / "kb.local.yml").write_text(yaml.safe_dump(
        {"state_dir": str(state_dir)},
    ), encoding="utf-8")

    monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("DT_USER_PASSWORD", "pw123")
    # No personal token (password path)
    monkeypatch.delenv("DT_PERSONAL_TOKEN", raising=False)
    return tmp_path


def _open_db(tmp_path):
    state_dir = tmp_path / "state"
    db = connect(state_dir)
    migrate(db)
    return db


class TestRunOnceAsMergesUserConfig:
    """run --once --as USER merges USER's user_config overrides."""

    def test_owner_tag_stamped_on_points(self, env_dirs, monkeypatch):
        """A manual run with --as stamps owner=<user> on ingested points."""
        db = _open_db(env_dirs)
        _seed_account(db, "alice@example.com", "scheduler")
        db.close()

        # Patch run_pipeline to capture the owner kwarg
        import digital_twins.ingest.pipeline as pipe
        captured = {}

        def fake_run_pipeline(cfg, db, qdrant, embedder, **kw):
            captured.update(kw)
            from digital_twins.ingest.pipeline import RunSummary
            return RunSummary(run_id="test-id", points=0, counts={})

        monkeypatch.setattr(pipe, "run_pipeline", fake_run_pipeline)

        # Also need to patch the source build to avoid real source
        import digital_twins.sources as src
        monkeypatch.setattr(src, "build", lambda name, entry: _FakeSource())

        runner = CliRunner()
        result = runner.invoke(cli, [
            "run", "--once", "--as", "alice@example.com",
        ])
        assert result.exit_code == 0, result.output
        assert captured.get("owner") == "alice@example.com", (
            f"owner kwarg not passed to run_pipeline: {captured.get('owner')}")

    def test_user_config_overrides_applied(self, env_dirs, monkeypatch):
        """run --once --as merges the caller's user_config into the config."""
        db = _open_db(env_dirs)
        _seed_account(db, "alice@example.com", "scheduler")
        # Alice overrides hermes max_items to 50
        _seed_user_config(db, "alice@example.com", "hermes", "max_items", "50")
        db.close()

        import digital_twins.ingest.pipeline as pipe
        captured_cfg = {}

        def fake_run_pipeline(cfg, db, qdrant, embedder, **kw):
            captured_cfg["hermes"] = cfg["sources"].get("hermes", {})
            from digital_twins.ingest.pipeline import RunSummary
            return RunSummary(run_id="test-id", points=0, counts={})

        monkeypatch.setattr(pipe, "run_pipeline", fake_run_pipeline)
        import digital_twins.sources as src
        monkeypatch.setattr(src, "build", lambda name, entry: _FakeSource())

        runner = CliRunner()
        result = runner.invoke(cli, [
            "run", "--once", "--as", "alice@example.com",
        ])
        assert result.exit_code == 0, result.output
        # Alice's override should be merged: max_items=50, not the global 100
        assert captured_cfg["hermes"]["max_items"] == 50, (
            f"user_config override not merged: {captured_cfg['hermes']}")

    def test_no_as_no_merge(self, env_dirs, monkeypatch):
        """Without --as, the global config is used as-is (001 behavior)."""
        db = _open_db(env_dirs)
        _seed_account(db, "alice@example.com", "scheduler")
        _seed_user_config(db, "alice@example.com", "hermes", "max_items", "50")
        db.close()

        import digital_twins.ingest.pipeline as pipe
        captured_cfg = {}

        def fake_run_pipeline(cfg, db, qdrant, embedder, **kw):
            captured_cfg["hermes"] = cfg["sources"].get("hermes", {})
            from digital_twins.ingest.pipeline import RunSummary
            return RunSummary(run_id="test-id", points=0, counts={})

        monkeypatch.setattr(pipe, "run_pipeline", fake_run_pipeline)
        import digital_twins.sources as src
        monkeypatch.setattr(src, "build", lambda name, entry: _FakeSource())

        runner = CliRunner()
        # No --as: global config, no merge
        result = runner.invoke(cli, [
            "run", "--once",
        ])
        assert result.exit_code == 0, result.output
        # Global max_items=100, not alice's override 50
        assert captured_cfg["hermes"]["max_items"] == 100, (
            f"global config should be unchanged without --as: {captured_cfg['hermes']}")

    def test_global_config_not_mutated(self, env_dirs, monkeypatch):
        """The merge produces a copy; the global config dict is never mutated."""
        db = _open_db(env_dirs)
        _seed_account(db, "alice@example.com", "scheduler")
        _seed_user_config(db, "alice@example.com", "hermes", "max_items", "50")
        db.close()

        from digital_twins.config.loader import load
        import digital_twins.ingest.pipeline as pipe

        # Capture the config dict that load() returns (pre-merge)
        pre_cfg = load()
        pre_max = pre_cfg["sources"]["hermes"]["max_items"]
        assert pre_max == 100  # global default

        def fake_run_pipeline(cfg, db, qdrant, embedder, **kw):
            # The cfg passed to run_pipeline should have the override
            assert cfg["sources"]["hermes"]["max_items"] == 50
            from digital_twins.ingest.pipeline import RunSummary
            return RunSummary(run_id="test-id", points=0, counts={})

        monkeypatch.setattr(pipe, "run_pipeline", fake_run_pipeline)
        import digital_twins.sources as src
        monkeypatch.setattr(src, "build", lambda name, entry: _FakeSource())

        runner = CliRunner()
        result = runner.invoke(cli, [
            "run", "--once", "--as", "alice@example.com",
        ])
        assert result.exit_code == 0, result.output
        # The global config (re-loaded) must still be 100 — not mutated
        post_cfg = load()
        assert post_cfg["sources"]["hermes"]["max_items"] == 100, (
            "global config was mutated by the user_config merge")


class _FakeSource:
    """Minimal source stub for tests that patch build()."""
    def prerequisites(self):
        return []
    def close(self):
        pass
    def read(self, *a, **kw):
        return iter([])


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
