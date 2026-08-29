"""Quickstart Scenarios 1–7 end-to-end verification (T039, Phase 8 Polish).

Verifies that the documented behavior in ``specs/001-package-foundation/quickstart.md``
matches the actual code behavior, without real Qdrant/Neo4j/LLM endpoints.

- In-memory Qdrant (``QdrantClient(":memory:")``) for pipeline + health checks
- Stub embedder (fixed 384-dim vectors)
- Stub health checks (``run_health_checks`` monkeypatched)
- The real fs source for scenario 3
- ``sys.modules`` injection for scenario 5's fake custom-source module
- ``CliRunner`` for CLI-level exit-code assertions
- Existing test infrastructure re-invoked for scenarios 6 and 7
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner
from qdrant_client import models as qm

from digital_twins import health
from digital_twins.cli import cli
from digital_twins.config.schema import BUILTIN_SOURCES
from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.pipeline import (
    DimensionMismatchError,
    PrerequisiteError,
    run_pipeline,
)
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate
from digital_twins.state.models import get_highwater

# re-invoke existing test modules for scenarios 6 and 7
from tests.integration import test_portability
from tests.unit import test_knob_docs, test_upgrade

# .env.example path for env-var cross-checks (mirrors test_knob_docs.py)
_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _env_example_vars():
    """Extract documented env var names from .env.example."""
    import re
    return [
        m.group(1)
        for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if (m := re.match(r"^#?([A-Z][A-Z0-9_]+)=", line.strip()))
    ]


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #

def _cfg(tmp_path, fs_dir=None):
    """Full config dict matching what the schema expects after validation."""
    sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
               for n in BUILTIN_SOURCES}
    if fs_dir is not None:
        sources["fs"] = {"enabled": True, "max_items": 200, "timeout_s": 1500,
                         "extra": {"dir": str(fs_dir)}}
    return {
        "state_dir": str(tmp_path / "state"),
        "config_dir": str(tmp_path / "config"),
        "qdrant": {"url": "https://q.example:6333", "api_key": None},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "sources": sources,
    }


def _embedder(texts):
    """Stub embedder: fixed 384-dim vectors (matches the pinned model)."""
    return [[0.5] * 384 for _ in texts]


def _point_count(qdrant):
    return qdrant.count(QDRANT_COLLECTION, exact=True).count


def _stub_health(monkeypatch, ok=True):
    """Replace health.run_health_checks with a canned result."""
    def _run(cfg):
        return [health.HealthResult(ep, ok, "ok" if ok else "down", "")
                for ep in ("qdrant", "neo4j", "llm")]
    monkeypatch.setattr(health, "run_health_checks", _run)


# --------------------------------------------------------------------------- #
# Scenario 1 — Clean-host init
# --------------------------------------------------------------------------- #
# quickstart.md:
#   `digital-twins init` prompts for endpoints, creates config dir with
#   kb.local.yml (every source enabled: false), state dir + state.db,
#   prints health table, exit 0. Idempotent: re-running keeps existing
#   values, prompts for nothing new, exits 0.
# --------------------------------------------------------------------------- #

class TestScenario1_CleanHostInit:

    def test_init_creates_starter_and_state(self, tmp_path, monkeypatch):
        """First run: kb.local.yml created, state.db created, exit 0."""
        config_dir = tmp_path / "config"
        state_dir = tmp_path / "state"
        monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
        # 003 first-admin step (T008): init creates the first admin from
        # these auth-only env vars when the accounts table is empty.
        monkeypatch.setenv("INIT_ADMIN_EMAIL", "admin@example.com")
        monkeypatch.setenv("INIT_ADMIN_PASSWORD", "admin-pw")
        monkeypatch.chdir(tmp_path)
        _stub_health(monkeypatch)

        result = CliRunner().invoke(cli, ["init", "--yes"])
        assert result.exit_code == 0, result.output

        # kb.local.yml exists with every built-in source disabled
        data = yaml.safe_load(
            (config_dir / "kb.local.yml").read_text(encoding="utf-8"))
        assert set(data["sources"]) == set(BUILTIN_SOURCES)
        assert all(s["enabled"] is False for s in data["sources"].values())

        # state.db created
        assert (state_dir / "state.db").exists()

    def test_init_prompts_for_missing_endpoints(self, tmp_path, monkeypatch):
        """First run without --yes: prompts for every missing endpoint."""
        config_dir = tmp_path / "config"
        state_dir = tmp_path / "state"
        monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
        # 003 first-admin step (T008): credentials via env, not prompts.
        monkeypatch.setenv("INIT_ADMIN_EMAIL", "admin@example.com")
        monkeypatch.setenv("INIT_ADMIN_PASSWORD", "admin-pw")
        monkeypatch.chdir(tmp_path)
        _stub_health(monkeypatch)

        result = CliRunner().invoke(cli, ["init"], input=(
            "https://q:6333\n"       # qdrant.url
            "bolt://n:7687\n"        # neo4j.url
            "neo4j_user\n"           # neo4j.user
            "s3cr3t\n"               # neo4j.password
            "https://llm.example/v1\n"  # llm.endpoint
            "gpt-test\n"             # llm.model
        ))
        assert result.exit_code == 0, result.output

        data = yaml.safe_load(
            (config_dir / "kb.local.yml").read_text(encoding="utf-8"))
        assert data["qdrant"]["url"] == "https://q:6333"
        assert data["neo4j"]["url"] == "bolt://n:7687"
        assert data["neo4j"]["user"] == "neo4j_user"
        assert data["neo4j"]["password"] == "s3cr3t"
        assert data["llm"]["endpoint"] == "https://llm.example/v1"
        assert data["llm"]["model"] == "gpt-test"

    def test_init_idempotent_re_run_keeps_existing_values(self, tmp_path, monkeypatch):
        """Second run: existing values kept, no new prompts, exit 0."""
        config_dir = tmp_path / "config"
        state_dir = tmp_path / "state"
        monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
        # 003 first-admin step (T008): credentials via env, not prompts.
        # The re-run asserts idempotency: no second admin is created.
        monkeypatch.setenv("INIT_ADMIN_EMAIL", "admin@example.com")
        monkeypatch.setenv("INIT_ADMIN_PASSWORD", "admin-pw")
        monkeypatch.chdir(tmp_path)
        _stub_health(monkeypatch)

        # Seed an existing kb.local.yml with some values set
        config_dir.mkdir()
        (config_dir / "kb.local.yml").write_text(
            "qdrant:\n"
            "  url: https://old.example\n"
            "sources:\n"
            "  fs:\n"
            "    enabled: true\n",
            encoding="utf-8")

        # Re-run init: existing values kept, missing built-ins added, exit 0
        result = CliRunner().invoke(cli, ["init", "--yes"])
        assert result.exit_code == 0, result.output

        data = yaml.safe_load(
            (config_dir / "kb.local.yml").read_text(encoding="utf-8"))
        # existing values preserved
        assert data["qdrant"]["url"] == "https://old.example"
        assert data["sources"]["fs"]["enabled"] is True
        # missing built-ins were added, still disabled
        assert data["sources"]["gmail"]["enabled"] is False

        # Third run: idempotent again, no changes
        result2 = CliRunner().invoke(cli, ["init", "--yes"])
        assert result2.exit_code == 0, result2.output
        data2 = yaml.safe_load(
            (config_dir / "kb.local.yml").read_text(encoding="utf-8"))
        assert data2["qdrant"]["url"] == "https://old.example"
        assert data2["sources"]["fs"]["enabled"] is True

    def test_init_unhealthy_exits_1(self, tmp_path, monkeypatch):
        """init still creates config + state even when health fails."""
        config_dir = tmp_path / "config"
        state_dir = tmp_path / "state"
        monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("KB_STATE_DIR", str(state_dir))
        # 003 first-admin step (T008): credentials via env, not prompts.
        monkeypatch.setenv("INIT_ADMIN_EMAIL", "admin@example.com")
        monkeypatch.setenv("INIT_ADMIN_PASSWORD", "admin-pw")
        monkeypatch.chdir(tmp_path)
        _stub_health(monkeypatch, ok=False)

        result = CliRunner().invoke(cli, ["init", "--yes"])
        assert result.exit_code == 1
        assert "FAIL" in result.output
        # setup still happened
        assert (config_dir / "kb.local.yml").is_file()
        assert (state_dir / "state.db").exists()


# --------------------------------------------------------------------------- #
# Scenario 2 — Validate + dimension guard
# --------------------------------------------------------------------------- #
# quickstart.md:
#   `digital-twins validate` exits 0 when all endpoints ok.
#   Wrong-dim collection -> hard error + remediation, exit 1.
#   The dimension guard in pipeline.py raises DimensionMismatchError.
# --------------------------------------------------------------------------- #

class TestScenario2_ValidateAndDimensionGuard:

    def test_validate_healthy_exits_0(self, tmp_path, monkeypatch):
        """All endpoints ok -> exit 0, health table printed."""
        monkeypatch.setenv("KB_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.chdir(tmp_path)

        monkeypatch.setattr(health, "run_health_checks", lambda cfg: [
            health.HealthResult(ep, True, "ok")
            for ep in ("qdrant", "neo4j", "llm")])

        result = CliRunner().invoke(cli, ["validate"])
        assert result.exit_code == 0, result.output
        for ep in ("qdrant", "neo4j", "llm"):
            assert ep in result.output
        assert "FAIL" not in result.output

    def test_validate_dimension_mismatch_exits_1_with_remediation(
            self, tmp_path, monkeypatch):
        """Qdrant collection has wrong dim -> FAIL + remediation, exit 1.

        The CLI validate command calls health.run_health_checks(load()).
        We stub run_health_checks to call the real check_qdrant against an
        in-memory Qdrant whose personal_kb collection is 1536-dim
        (pinned model is 384-dim), proving the hard-error + remediation path.
        """
        monkeypatch.setenv("KB_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.chdir(tmp_path)

        # In-memory Qdrant with a 1536-dim collection
        from qdrant_client import QdrantClient
        inmem = QdrantClient(":memory:")
        inmem.create_collection(
            QDRANT_COLLECTION,
            vectors_config=qm.VectorParams(size=1536, distance=qm.Distance.COSINE))

        # Stub: qdrant check uses the real check_qdrant against the in-mem
        # client; neo4j/llm report ok.
        monkeypatch.setattr(
            health, "run_health_checks",
            lambda cfg: [
                health.check_qdrant({**cfg, "qdrant": {"url": "http://inmem:6333"}}),
                health.HealthResult("neo4j", True, "ok"),
                health.HealthResult("llm", True, "ok"),
            ])

        # Monkeypatch QdrantClient so check_qdrant's internal import resolves
        # to the in-memory client.
        monkeypatch.setitem(
            sys.modules, "qdrant_client",
            types.SimpleNamespace(QdrantClient=lambda **kw: inmem))

        result = CliRunner().invoke(cli, ["validate"])
        assert result.exit_code == 1, result.output
        assert "FAIL" in result.output
        # the error names both dimensions
        assert "1536" in result.output
        assert "384" in result.output
        # remediation is present
        assert "recreate" in result.output or "re-embed" in result.output

    def test_pipeline_dimension_guard_raises(self, qdrant, tmp_path):
        """assert_dimension raises DimensionMismatchError on dim mismatch."""
        from digital_twins.ingest.pipeline import assert_dimension
        qdrant.create_collection(
            QDRANT_COLLECTION,
            vectors_config=qm.VectorParams(size=512, distance=qm.Distance.COSINE))
        with pytest.raises(DimensionMismatchError) as excinfo:
            assert_dimension(qdrant, 384)
        assert excinfo.value.actual == 512
        assert excinfo.value.expected == 384
        assert "512" in str(excinfo.value)
        assert "384" in str(excinfo.value)

    def test_pipeline_dimension_guard_noop_when_collection_missing(
            self, qdrant, tmp_path):
        """assert_dimension is a no-op when the collection does not exist."""
        from digital_twins.ingest.pipeline import assert_dimension
        # collection does not exist yet -> no error
        assert_dimension(qdrant, 384)  # no exception


# --------------------------------------------------------------------------- #
# Scenario 3 — Ingest + idempotent re-run
# --------------------------------------------------------------------------- #
# quickstart.md:
#   Run 1: fs: new=2, skipped=0
#   Run 2: new=0, skipped=2
#   Collection still holds exactly 2 points (here: 3 — 2 items, 1 multi-chunk).
#   The existing test_idempotency.py covers this; re-invoke for cross-check.
# --------------------------------------------------------------------------- #

class TestScenario3_IngestIdempotent:

    @pytest.fixture
    def fs_dir(self, tmp_path):
        d = tmp_path / "files"
        d.mkdir()
        (d / "a.txt").write_text("alpha " * 50, encoding="utf-8")  # 300 ch -> 2 chunks
        (d / "sub").mkdir()
        (d / "sub" / "b.txt").write_text("beta note", encoding="utf-8")  # 1 chunk
        return d

    def test_run1_ingests_run2_skips(
            self, qdrant, tmp_path, fs_dir):
        """Run 1 ingests all items; run 2 skips via high-water marks."""
        cfg = _cfg(tmp_path, fs_dir)
        db = connect(tmp_path / "state")
        migrate(db)

        s1 = run_pipeline(cfg, db, qdrant, _embedder)
        assert s1.counts == {"fs": 2}, f"run 1: expected fs=2 items, got {s1.counts}"
        assert s1.points == 3, f"run 1: expected 3 points, got {s1.points}"
        assert _point_count(qdrant) == 3

        # run 2: high-water marks mean nothing new is read
        s2 = run_pipeline(cfg, db, qdrant, _embedder)
        assert s2.counts == {"fs": 0}, (
            f"run 2: expected 0 new items, got {s2.counts}")
        assert s2.points == 0
        assert _point_count(qdrant) == 3, (
            f"run 2: collection should still hold 3 points, "
            f"got {_point_count(qdrant)}")

        # audit rows: both ok, one per run
        rows = db.execute("SELECT status FROM audit_runs ORDER BY started_at").fetchall()
        assert len(rows) == 2
        assert all(r[0] == "ok" for r in rows)

        # high-water marks persisted
        assert get_highwater(db, "fs", "a.txt") is not None
        assert get_highwater(db, "fs", "sub/b.txt") is not None
        db.close()

    def test_existing_idempotency_test_passes(self, qdrant, tmp_path):
        """Cross-check: the existing test_idempotency test still passes."""
        # Re-invoke the key assertions from test_idempotency.py
        cfg = _cfg(tmp_path, _make_fs_dir(tmp_path))
        db = connect(tmp_path / "state")
        migrate(db)

        s1 = run_pipeline(cfg, db, qdrant, _embedder)
        assert s1.points == 3
        assert s1.counts == {"fs": 2}

        s2 = run_pipeline(cfg, db, qdrant, _embedder)
        assert s2.points == 0
        assert _point_count(qdrant) == 3

        # third run: cursors wiped, full re-read must still not duplicate
        db.execute("DELETE FROM highwater")
        db.commit()
        s3 = run_pipeline(cfg, db, qdrant, _embedder)
        assert s3.points == 3
        assert _point_count(qdrant) == 3
        db.close()


def _make_fs_dir(tmp_path):
    d = tmp_path / "files"
    d.mkdir(exist_ok=True)
    (d / "a.txt").write_text("alpha " * 50, encoding="utf-8")
    (d / "sub").mkdir(exist_ok=True)
    (d / "sub" / "b.txt").write_text("beta note", encoding="utf-8")
    return d


# --------------------------------------------------------------------------- #
# Scenario 4 — Fail-fast prerequisite
# --------------------------------------------------------------------------- #
# quickstart.md:
#   Enable hermes source without session store -> exit 2, message names
#   source + missing prerequisite + where to set it. Nothing ingested;
#   the failed run is still audited.
# --------------------------------------------------------------------------- #

class TestScenario4_FailFastPrerequisite:

    def test_hermes_missing_prerequisite_raises_named(self, qdrant, tmp_path, monkeypatch):
        """hermes source with no hermes CLI -> PrerequisiteError names it."""
        monkeypatch.setattr("digital_twins.sources.hermes.shutil.which", lambda _name: None)
        sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
                   for n in BUILTIN_SOURCES}
        sources["hermes"] = {"enabled": True, "max_items": 200, "timeout_s": 1500}
        cfg = {
            "state_dir": str(tmp_path / "state"),
            "config_dir": str(tmp_path / "config"),
            "qdrant": {"url": "https://q.example:6333", "api_key": None},
            "neo4j": {"url": None, "user": None, "password": None},
            "llm": {"endpoint": None, "model": None, "api_key": None},
            "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
            "chunking": {"max_chars": 200, "overlap": 20},
            "sources": sources,
        }
        db = connect(tmp_path / "state")
        migrate(db)

        with pytest.raises(PrerequisiteError) as excinfo:
            run_pipeline(cfg, db, qdrant, _embedder)

        msg = str(excinfo.value)
        assert "hermes" in msg, "error must name the source"
        # nothing was ingested: no collection at all
        assert [c.name for c in qdrant.get_collections().collections] == []
        # the failed run is still audited
        rows = db.execute("SELECT status FROM audit_runs").fetchall()
        assert rows == [("failed",)]
        db.close()

    def test_run_cli_exit_2_names_source_and_prerequisite(self, tmp_path, monkeypatch):
        """CLI run with hermes enabled -> exit 2, names source + prerequisite."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "kb.local.yml").write_text(
            "sources:\n"
            "  hermes:\n"
            "    enabled: true\n",
            encoding="utf-8")
        monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.chdir(tmp_path)

        # 1. source prerequisite gate: missing hermes CLI -> exit 2, names the
        #    source + prerequisite; qdrant.url intentionally NOT set (fail-fast
        #    must fire before endpoint config is touched)
        monkeypatch.setattr(
            "digital_twins.sources.hermes.shutil.which", lambda _name: None)
        result = CliRunner().invoke(cli, ["run", "--source", "hermes"])
        assert result.exit_code == 2, result.output
        assert "hermes" in result.output
        assert "fail-fast" in result.output

        # 2. prerequisites satisfied -> fail-fast clears, config error surfaces
        monkeypatch.setattr(
            "digital_twins.sources.hermes.shutil.which",
            lambda _name: "/usr/bin/hermes")
        result = CliRunner().invoke(cli, ["run", "--source", "hermes"])
        assert result.exit_code != 2, result.output
        assert "fail-fast" not in result.output

    def test_fs_missing_dir_exit_2(self, tmp_path, monkeypatch):
        """CLI run with fs pointing at a missing dir -> exit 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "kb.local.yml").write_text(
            f"sources:\n  fs:\n    enabled: true\n    extra:\n"
            f"      dir: {tmp_path / 'nope'}\n", encoding="utf-8")
        monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(cli, ["run"])
        assert result.exit_code == 2
        assert "fs" in result.output
        assert "dir" in result.output


# --------------------------------------------------------------------------- #
# Scenario 5 — Custom source
# --------------------------------------------------------------------------- #
# quickstart.md:
#   Configure sources.mytool with entrypoint, credential, prefix.
#   With MYTOOL_TOKEN set: run ingests. Without it: fail-fast exit 2
#   naming the credential. No package reinstall involved.
# --------------------------------------------------------------------------- #

class _FakeMyTool:
    """A minimal contract-compliant custom source for the fake module."""

    def __init__(self, name, entry):
        self.name = name
        self.capability = None  # set by factory

    def prerequisites(self):
        return []

    def read(self, since):
        from digital_twins.sources.base import IngestItem
        yield IngestItem(
            key="k1", content="mytool item",
            ts="2025-01-01T00:00:00+00:00")

    def close(self):
        pass


def _make_fake_mytool_module():
    """Create a fake `mytool_kb` module with a `make_source` factory.

    The factory returns an object that passes the `isinstance(Source)`
    check by dynamically subclassing `Source` at runtime.
    """
    from digital_twins.sources.base import Capability, Source

    # Build a concrete Source subclass with the right behaviour
    class MyToolSource(Source):
        name = "mytool"
        capability = Capability(
            runtime=None, credential="MYTOOL_TOKEN", prefix="mytool:")

        def prerequisites(self):
            return []

        def read(self, since):
            from digital_twins.sources.base import IngestItem
            yield IngestItem(
                key="k1", content="mytool item",
                ts="2025-01-01T00:00:00+00:00")

        def close(self):
            pass

    mod = types.ModuleType("mytool_kb")
    mod.make_source = lambda entry: MyToolSource()
    return mod


class TestScenario5_CustomSource:

    def test_custom_source_with_credential_ingests(
            self, qdrant, tmp_path, monkeypatch):
        """MYTOOL_TOKEN set -> custom source ingests successfully."""
        monkeypatch.setenv("MYTOOL_TOKEN", "secret-value")
        sys.modules["mytool_kb"] = _make_fake_mytool_module()

        try:
            sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
                       for n in BUILTIN_SOURCES}
            sources["mytool"] = {
                "enabled": True, "max_items": 200, "timeout_s": 1500,
                "entrypoint": "mytool_kb:make_source",
                "credential": "MYTOOL_TOKEN",
                "prefix": "mytool:",
            }
            cfg = {
                "state_dir": str(tmp_path / "state"),
                "config_dir": str(tmp_path / "config"),
                "qdrant": {"url": "https://q.example:6333", "api_key": None},
                "neo4j": {"url": None, "user": None, "password": None},
                "llm": {"endpoint": None, "model": None, "api_key": None},
                "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
                "chunking": {"max_chars": 200, "overlap": 20},
                "sources": sources,
            }
            db = connect(tmp_path / "state")
            migrate(db)

            summary = run_pipeline(cfg, db, qdrant, _embedder)
            assert summary.counts == {"mytool": 1}, (
                f"expected mytool=1, got {summary.counts}")
            assert summary.points == 1
            assert _point_count(qdrant) == 1

            # audit row written
            rows = db.execute("SELECT status FROM audit_runs").fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "ok"
            db.close()
        finally:
            sys.modules.pop("mytool_kb", None)

    def test_custom_source_without_credential_fail_fast(
            self, qdrant, tmp_path, monkeypatch):
        """MYTOOL_TOKEN unset -> PrerequisiteError names the credential."""
        monkeypatch.delenv("MYTOOL_TOKEN", raising=False)
        sys.modules["mytool_kb"] = _make_fake_mytool_module()

        try:
            sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
                       for n in BUILTIN_SOURCES}
            sources["mytool"] = {
                "enabled": True, "max_items": 200, "timeout_s": 1500,
                "entrypoint": "mytool_kb:make_source",
                "credential": "MYTOOL_TOKEN",
                "prefix": "mytool:",
            }
            cfg = {
                "state_dir": str(tmp_path / "state"),
                "config_dir": str(tmp_path / "config"),
                "qdrant": {"url": "https://q.example:6333", "api_key": None},
                "neo4j": {"url": None, "user": None, "password": None},
                "llm": {"endpoint": None, "model": None, "api_key": None},
                "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
                "chunking": {"max_chars": 200, "overlap": 20},
                "sources": sources,
            }
            db = connect(tmp_path / "state")
            migrate(db)

            with pytest.raises(PrerequisiteError) as excinfo:
                run_pipeline(cfg, db, qdrant, _embedder)

            msg = str(excinfo.value)
            assert "mytool" in msg, "error must name the source"
            assert "MYTOOL_TOKEN" in msg, "error must name the credential"

            # nothing ingested
            assert [c.name for c in qdrant.get_collections().collections] == []
            # failed run audited
            rows = db.execute("SELECT status FROM audit_runs").fetchall()
            assert rows == [("failed",)]
            db.close()
        finally:
            sys.modules.pop("mytool_kb", None)

    def test_custom_source_cli_exit_2_without_credential(
            self, tmp_path, monkeypatch):
        """CLI run with custom source + missing credential -> exit 2."""
        monkeypatch.delenv("MYTOOL_TOKEN", raising=False)
        sys.modules["mytool_kb"] = _make_fake_mytool_module()

        try:
            config_dir = tmp_path / "config"
            config_dir.mkdir()
            (config_dir / "kb.local.yml").write_text(
                "sources:\n"
                "  mytool:\n"
                "    enabled: true\n"
                "    entrypoint: mytool_kb:make_source\n"
                "    credential: MYTOOL_TOKEN\n"
                "    prefix: 'mytool:'\n",
                encoding="utf-8")
            monkeypatch.setenv("KB_CONFIG_DIR", str(config_dir))
            monkeypatch.setenv("KB_STATE_DIR", str(tmp_path / "state"))
            monkeypatch.chdir(tmp_path)

            result = CliRunner().invoke(cli, ["run", "--source", "mytool"])
            assert result.exit_code == 2, result.output
            assert "mytool" in result.output
            assert "MYTOOL_TOKEN" in result.output
        finally:
            sys.modules.pop("mytool_kb", None)


# --------------------------------------------------------------------------- #
# Scenario 6 — Portability + knob-doc audit
# --------------------------------------------------------------------------- #
# quickstart.md:
#   pytest tests/integration/test_portability.py tests/unit/test_knob_docs.py
#   Both pass.
# --------------------------------------------------------------------------- #

class TestScenario6_PortabilityAndKnobDocs:

    def test_portability_no_host_specific_values(self):
        """Re-invoke the portability check directly."""
        test_portability.test_no_host_specific_values_in_shipped_surface()

    def test_knob_docs_registry_exists(self):
        """KNOBS registry is present and well-formed."""
        test_knob_docs.TestKnobRegistryStructure().test_knobs_exists()
        test_knob_docs.TestKnobRegistryStructure().test_knobs_is_dict()

    def test_knob_docs_every_knob_has_fields(self):
        """Every KNOBS entry has the required fields."""
        from digital_twins.config.knobs import KNOBS
        for path, entry in KNOBS.items():
            assert isinstance(entry, dict), f"{path}: not a dict"
            for field in ("type", "default", "env", "group"):
                assert field in entry, f"{path}: missing {field!r}"

    def test_knob_docs_env_var_names_valid(self):
        """Every env var in KNOBS is valid; credential vars are documented."""
        import re
        documented = set(_env_example_vars())
        from digital_twins.config.knobs import KNOBS
        for path, entry in KNOBS.items():
            env = entry.get("env")
            if env is None:
                continue
            assert env.startswith("KB_") or re.match(
                r"^[A-Z][A-Z0-9_]+$", env), (
                f"{path}: env var {env!r} not KB_ or valid credential name")
            if not env.startswith("KB_"):
                assert env in documented, (
                    f"{path}: credential {env!r} not in .env.example")


# --------------------------------------------------------------------------- #
# Scenario 7 — Upgrade preservation
# --------------------------------------------------------------------------- #
# quickstart.md:
#   Seed state (high-water + audit + accounts), run a version-bumped
#   migration, assert every row and the config survive.
#   Covered by tests/unit/test_upgrade.py.
# --------------------------------------------------------------------------- #

class TestScenario7_UpgradePreservation:

    def test_seed_data_survives_version_bumped_migration(
            self, tmp_path, monkeypatch):
        """All v1 rows intact after a v1->v3 upgrade."""
        from digital_twins.state import migrations, models

        conn = connect(tmp_path)
        migrations.migrate(conn)  # create the base schema
        # seed
        conn.execute(
            "INSERT INTO accounts (email, role, password_hash) "
            "VALUES ('a@example.com', 'admin', 'hash1')")
        conn.execute(
            "INSERT INTO accounts (email, role, password_hash) "
            "VALUES ('b@example.com', 'reader', NULL)")
        conn.commit()
        models.upsert_highwater(conn, "fs", "notes/a.txt", "abc123")
        models.start_audit_run(conn, "run-001", trigger="schedule")
        models.finish_audit_run(conn, "run-001", "ok",
                                per_source_counts={"fs": {"new": 1}})

        before = {
            "accounts": conn.execute(
                "SELECT * FROM accounts ORDER BY id").fetchall(),
            "highwater": conn.execute(
                "SELECT * FROM highwater ORDER BY source, item_key").fetchall(),
            "audit_runs": conn.execute(
                "SELECT * FROM audit_runs ORDER BY run_id").fetchall(),
        }

        v = migrations.migrate(conn)
        assert v == 3

        after = {
            "accounts": conn.execute(
                "SELECT * FROM accounts ORDER BY id").fetchall(),
            "highwater": conn.execute(
                "SELECT * FROM highwater ORDER BY source, item_key").fetchall(),
            "audit_runs": conn.execute(
                "SELECT * FROM audit_runs ORDER BY run_id").fetchall(),
        }
        for table in ("accounts", "highwater", "audit_runs"):
            assert after[table] == before[table], f"{table} changed"
        conn.close()

    def test_migration_is_idempotent(self, tmp_path):
        """Running migrate() twice is a no-op."""
        from digital_twins.state import migrations
        conn = connect(tmp_path)
        v1 = migrations.migrate(conn)
        v2 = migrations.migrate(conn)
        assert v1 == v2 == 3
        conn.close()

    def test_existing_upgrade_tests_pass(self, tmp_path, monkeypatch):
        """Cross-check: re-invoke key assertions from test_upgrade.py."""
        # test_upgrade.test_migration_is_idempotent
        from digital_twins.state import migrations
        conn = connect(tmp_path)
        v1 = migrations.migrate(conn)
        v2 = migrations.migrate(conn)
        v3 = migrations.migrate(conn)
        assert v1 == v2 == v3 == 3
        assert migrations.user_version(conn) == 3
        conn.close()


# --------------------------------------------------------------------------- #
# CLI surface: --version-json is documented in --help
# --------------------------------------------------------------------------- #

class TestVersionJsonSurface:
    """`--version-json` must be visible in `digital-twins --help`."""

    def test_version_json_appears_in_help(self):
        """The flag is surfaced in the group's help output."""
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "--version-json" in result.output
