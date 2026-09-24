"""Tests for the interactive per-service second pass
(install-setup-separation T2.1 / T2.2).

Covers:
  - a mixed choice (qdrant=local, llm=external+URL, neo4j=local,
    embedding=external+URL) on the default-interactive path produces the
    right resolved map AND ``run_local_stack`` is called with
    ``resolved=`` containing exactly the local-mode services;
  - on a no-GPU host the llm/embedding prompts default to external;
  - services named in ``--backends`` are NOT re-prompted on any path
    (the flag's resolution carries through, untouched).

All external effects are monkeypatched (docker, compose, health, admin,
kb.local writes): hermetic, no containers / network.  Prompts are
captured the way ``tests/unit/test_setup.py`` does — a monkeypatched
prompt returning scripted answers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


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


def _stub(monkeypatch, *, gpu=False, docker=False,
          config_dir=None, state_dir=None):
    """Stub every external effect of ``digital-twins setup``; return a
    ``captured`` dict exposing what the backend path called."""
    import digital_twins.setup as setup_mod

    captured = {"local": [], "cloud": [], "cloud_env": [],
                "kb_local": None, "connect": 0}

    if state_dir is None:
        state_dir = Path(os.environ["KB_STATE_DIR"])

    def _connect(d):
        captured["connect"] += 1
        return _fake_connect(d)

    monkeypatch.setattr(setup_mod, "connect", _connect)
    monkeypatch.setattr(setup_mod, "run_health_checks",
                        lambda cfg: _ok_checks())

    def _fake_local(prompt_text, echo, resolved=None, **kw):
        captured["local"].append(resolved)
        if resolved is not None and config_dir is not None:
            captured["kb_local"] = resolved
        return True

    def _fake_cloud(prompt_text, echo, **kw):
        captured["cloud"].append(True)
        return True

    def _fake_cloud_env(echo, **kw):
        captured["cloud_env"].append(True)
        return True

    monkeypatch.setattr(setup_mod, "run_local_stack",
                        lambda p, e, resolved=None: _fake_local(p, e, resolved))
    monkeypatch.setattr(setup_mod, "run_cloud_stack",
                        lambda p, e: _fake_cloud(p, e))
    monkeypatch.setattr(setup_mod, "run_cloud_env_stack",
                        lambda e: _fake_cloud_env(e))
    monkeypatch.setattr(setup_mod, "docker_available", lambda: docker)
    monkeypatch.setattr(setup_mod, "_gpu_present", lambda: gpu)
    monkeypatch.setattr(setup_mod, "has_valid_local_config", lambda: False)
    return captured


def test_mixed_choice_resolved_map_and_up_services(_isolate, monkeypatch):
    """(a) The default-interactive path, mixed answers
    (qdrant=local, llm=external+URL, neo4j=local, embedding=external+URL):
    the resolved map passed to run_local_stack has exactly qdrant + neo4j
    in local mode, and the external services carry the prompted URLs."""
    import digital_twins.setup as setup_mod
    config_dir, state_dir = _isolate
    cap = _stub(monkeypatch, gpu=False, docker=True,
                config_dir=config_dir, state_dir=state_dir)

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()

    # Scripted answers, in prompt order (qdrant, neo4j, llm, embedding,
    # then URL prompts for the two external answers):
    answers = iter([
        "local",                       # qdrant: local
        "local",                       # neo4j: local
        "external",                    # llm: external
        "http://llm.example/v1",       # llm URL prompt
        "external",                    # embedding: external
        "http://embed.example/v1",     # embedding URL prompt
    ])
    prompt_calls = []

    def fake_prompt(label):
        prompt_calls.append(label)
        return next(answers, "")

    rc = setup_mod.run_setup(
        prompt=fake_prompt,
        confirm=lambda q: True,
        echo=lambda msg: None)
    assert rc == 0
    # One run_local_stack call; its resolved map is the per-service mix.
    assert len(cap["local"]) == 1
    assert cap["cloud"] == []
    resolved = cap["local"][0]
    assert resolved["qdrant"]["mode"] == "local"
    assert resolved["neo4j"]["mode"] == "local"
    assert resolved["llm"]["mode"] == "external"
    assert resolved["llm"]["url"] == "http://llm.example/v1"
    assert resolved["embedding"]["mode"] == "external"
    assert resolved["embedding"]["url"] == "http://embed.example/v1"
    # Exactly the local-mode services would start in Docker (up_services):
    local_svcs = [s for s in resolved if resolved[s]["mode"] == "local"]
    assert sorted(local_svcs) == ["neo4j", "qdrant"]
    # The second pass fired: all four per-service prompts were shown
    # (each named service starts with "svc: run locally") plus the two
    # external URL prompts ("External svc URL ...").
    choice_prompts = [p for p in prompt_calls
                      if "run locally" in p]
    url_prompts = [p for p in prompt_calls
                   if p.startswith("External ") and " URL " in p]
    assert len(choice_prompts) == 4
    assert len(url_prompts) == 2
    assert len(prompt_calls) == 6


def test_no_gpu_defaults_external_for_llm_embedding(_isolate, monkeypatch):
    """(b) No-GPU host: the llm/embedding prompts show the external
    default, and pressing Enter (empty answer) on every prompt resolves
    llm + embedding to external (with unavailable=True), while
    qdrant/neo4j stay local."""
    import digital_twins.setup as setup_mod
    config_dir, state_dir = _isolate
    cap = _stub(monkeypatch, gpu=False, docker=True,
                config_dir=config_dir, state_dir=state_dir)

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()

    prompt_calls = []

    def fake_prompt(label):
        prompt_calls.append(label)
        return ""  # press Enter everywhere -> keep the shown default

    rc = setup_mod.run_setup(
        prompt=fake_prompt,
        confirm=lambda q: True,
        echo=lambda msg: None)
    assert rc == 0
    resolved = cap["local"][0]
    assert resolved["qdrant"]["mode"] == "local"
    assert resolved["neo4j"]["mode"] == "local"
    assert resolved["llm"]["mode"] == "external"
    assert resolved["llm"].get("unavailable") is True
    assert resolved["embedding"]["mode"] == "external"
    assert resolved["embedding"].get("unavailable") is True
    # The prompts themselves show the external default on no-GPU hosts:
    llm_prompt = next(p for p in prompt_calls if p.startswith("llm"))
    embed_prompt = next(p for p in prompt_calls
                        if p.startswith("embedding"))
    assert "default: external" in llm_prompt
    assert "default: external" in embed_prompt
    qdrant_prompt = next(p for p in prompt_calls
                         if p.startswith("qdrant"))
    neo4j_prompt = next(p for p in prompt_calls
                        if p.startswith("neo4j"))
    assert "default: local" in qdrant_prompt
    assert "default: local" in neo4j_prompt


def test_backends_named_services_are_not_reprompted(_isolate, monkeypatch):
    """(c) Services named in --backends are resolved from the flag and
    never re-prompted (regression guard: the T2.1 second pass must not
    accidentally re-prompt services that the flag already resolved)."""
    import digital_twins.setup as setup_mod
    config_dir, state_dir = _isolate
    cap = _stub(monkeypatch, gpu=False, docker=True,
                config_dir=config_dir, state_dir=state_dir)

    state_dir.mkdir(parents=True)
    db = _fake_connect(state_dir)
    db.close()

    # qdrant + llm named in the flag; the flag values must land in the
    # resolved map untouched, and no prompt may fire for either service.
    backends_spec = {"qdrant": "local", "llm": "https://llm.example/v1"}
    prompt_calls = []

    def fake_prompt(label):
        prompt_calls.append(label)
        # If a prompt fires for a flag-named service, fail loudly:
        if label.startswith("qdrant") or label.startswith("llm"):
            raise AssertionError(
                f"--backends named this service — it must not be "
                f"re-prompted: {label!r}")
        return "local"  # unnamed services default to local (no prompt
                        # expected on the --backends path)

    rc = setup_mod.run_setup(
        prompt=fake_prompt,
        confirm=lambda q: True,
        echo=lambda msg: None,
        backends=backends_spec)
    assert rc == 0
    assert cap["cloud"] == []
    resolved = cap["local"][0]
    # Flag-named services resolved from the flag, values untouched:
    assert resolved["qdrant"]["mode"] == "local"
    assert resolved["llm"]["mode"] == "external"
    assert resolved["llm"]["url"] == "https://llm.example/v1"
    # Unnamed services fall to the resolve_backends default: neo4j ->
    # local; on a no-GPU host the llm/embedding local default
    # auto-flips to external (unavailable). No second-pass prompt:
    assert resolved["neo4j"]["mode"] == "local"
    assert resolved["embedding"]["mode"] == "external"
    assert resolved["embedding"]["unavailable"] is True
    # No prompt fired for any service on the --backends path:
    assert prompt_calls == []
