"""Shared test fixtures: in-memory Qdrant, stubbed Neo4j + LLM transports."""

import pytest
from qdrant_client import QdrantClient


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
