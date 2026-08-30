"""Shared test fixtures: in-memory Qdrant, stubbed Neo4j + LLM transports."""

import os

import pytest
from qdrant_client import QdrantClient


# captured once at import: what the environment actually had before tests ran
_KB_ENV_BASELINE = {k: v for k, v in os.environ.items() if k.startswith("KB_")}


@pytest.fixture(autouse=True)
def _isolate_kb_env(monkeypatch):
    """Enforce the session KB_* baseline at each test's start.

    `load()` may load .env files straight into os.environ (dotenv
    semantics); a per-test snapshot would treat a leaked var as baseline,
    so the baseline is captured once, at conftest import. monkeypatch
    undoes the enforcement at teardown.
    """
    for k in [k for k in os.environ
              if k.startswith("KB_") and k not in _KB_ENV_BASELINE]:
        monkeypatch.delenv(k, raising=False)
    for k, v in _KB_ENV_BASELINE.items():
        if os.environ.get(k) != v:
            monkeypatch.setenv(k, v)


@pytest.fixture
def qdrant():
    """In-memory Qdrant client (collections are 384-dim unless a test says otherwise)."""
    client = QdrantClient(":memory:")
    yield client


class StubNeo4j:
    """Minimal stand-in for the official neo4j Driver (unit tests only)."""

    def __init__(self) -> None:
        self.run_calls: list[tuple] = []

    def session(self, **kwargs):  # noqa: D401 - interface parity stub
        return self

    def run(self, query, **params):
        self.run_calls.append((query, params))
        return []


@pytest.fixture
def neo4j_stub():
    return StubNeo4j()


class StubLLM:
    """OpenAI-compatible endpoint stub (connectivity + fixed responses)."""

    reachable = True

    def complete(self, prompt: str) -> str:
        return "stub-llm-response"


@pytest.fixture
def llm_stub():
    return StubLLM()

@pytest.fixture(autouse=True)
def _preflight_bypass(request, monkeypatch):
    # 008 US1: pre-008 fixtures drive the pipeline on default config
    # (no service endpoints configured); the hard-dependency gate would
    # stop them before the behavior under test. Bypass the pipeline's
    # gate call only — modules marked preflight_real (008's own tests)
    # keep the real check.
    if request.node.get_closest_marker("preflight_real"):
        return
    import digital_twins.health as _health
    from digital_twins.ingest import pipeline as _pipeline
    monkeypatch.setattr(_pipeline, "preflight", lambda cfg: None)
    monkeypatch.setattr(_health, "preflight", lambda cfg: None)
