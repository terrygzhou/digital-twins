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
    """Minimal stand-in for the official neo4j Driver (unit tests only).

    Mirrors the two surfaces the package uses:

    * the driver-level ``.run(query, **params)`` (the pipeline's
      ``neo4j`` parameter contract — 002 US2), and
    * the ``.session()`` context manager the graph read path
      (:mod:`digital_twins.ingest.graph_query`) uses for retrieval
      queries.
    """

    def __init__(self) -> None:
        self.run_calls: list[tuple] = []
        self.session_calls: list[tuple] = []

    def session(self, **kwargs):  # noqa: D401 - interface parity stub
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        self.run_calls.append((query, params))
        self.session_calls.append((query, params))

        class _Result:
            """Mimics neo4j Result: .data() returns list[dict] of records."""
            def __init__(self, rows):
                self._rows = rows
            def data(self):
                return self._rows
            def single(self):
                return self._rows[0] if self._rows else None

        # Default: empty result.  Tests can set .stub_rows to override.
        return _Result(getattr(self, "stub_rows", []) or [])


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
