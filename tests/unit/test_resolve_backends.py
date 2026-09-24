"""Unit tests for digital_twins.setup.resolve_backends (install-setup-separation T1.1).

Pure function, Test-First: resolve_backends(flags, env, gpu, docker) ->
dict[service, {"mode": "local"|"external", "url": str}] for
qdrant/neo4j/llm/embedding.

Covers:
  - all-local shorthand (--local): four local URLs (llm/embedding only when
    a GPU is present; on a no-GPU host they resolve to external+empty and
    carry a flag so the caller knows a local LLM was requested but is
    unavailable).
  - all-external shorthand (--cloud): all four external.
  - explicit --backends qdrant=local,llm=https://h/v1: mixed; unlisted
    services default to local.
  - external value = user URL; env-var precedence in --cloud-env mode.
  - llm local on no-GPU host -> "unavailable" marker (not silently dropped).
"""

from __future__ import annotations

import digital_twins.setup as setup_mod
from digital_twins.setup import resolve_backends


def test_all_local_with_gpu():
    out = resolve_backends(local=True, gpu=True, docker=True)
    assert out["qdrant"]["mode"] == "local"
    assert out["neo4j"]["mode"] == "local"
    assert out["llm"]["mode"] == "local"
    assert out["embedding"]["mode"] == "local"


def test_all_local_no_gpu_flips_llm_embedding_external():
    out = resolve_backends(local=True, gpu=False, docker=True)
    assert out["qdrant"]["mode"] == "local"
    assert out["neo4j"]["mode"] == "local"
    assert out["llm"]["mode"] == "external"
    assert out["llm"].get("unavailable") is True
    assert out["embedding"]["mode"] == "external"
    assert out["embedding"].get("unavailable") is True


def test_cloud_shorthand_all_external():
    out = resolve_backends(cloud=True)
    for svc in ("qdrant", "neo4j", "llm", "embedding"):
        assert out[svc]["mode"] == "external"


def test_backends_mixed_local_and_external():
    out = resolve_backends(
        backends={"qdrant": "local", "llm": "https://h/v1"},
        gpu=True, docker=True)
    assert out["qdrant"]["mode"] == "local"
    assert out["qdrant"]["url"] == setup_mod._QDRANT_EP
    assert out["llm"]["mode"] == "external"
    assert out["llm"]["url"] == "https://h/v1"
    # unlisted services default to local (interactive default)
    assert out["neo4j"]["mode"] == "local"
    assert out["embedding"]["mode"] == "local"


def test_backends_explicit_empty_value_is_required_external():
    out = resolve_backends(backends={"qdrant": ""}, gpu=True, docker=False)
    assert out["qdrant"]["mode"] == "external"
    assert out["qdrant"]["url"] == ""
    assert out["qdrant"]["required"] is True


def test_cloud_env_reads_urls_from_env():
    env = {"KB_QDRANT__URL": "https://q.example"}
    out = resolve_backends(cloud_env=True, env=env)
    assert out["qdrant"]["mode"] == "external"
    assert out["qdrant"]["url"] == "https://q.example"
    # missing var on a required service stays empty + required (the gate)
    assert out["neo4j"]["url"] == ""
    assert out["neo4j"]["required"] is True


def test_neo4j_credentials_carried():
    out = resolve_backends(local=True, gpu=False, docker=True,
                          neo4j_user="u1", neo4j_password="p1")
    assert out["neo4j"]["user"] == "u1"
    assert out["neo4j"]["password"] == "p1"
