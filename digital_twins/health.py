"""Endpoint health checks with remediation hints (US1; contracts/cli.md)."""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass

from digital_twins.config.schema import get
from digital_twins.ingest.embedding import DEFAULT_MODEL, model_dimension

# Qdrant collection name, pinned in code like the embedding model: the
# contract's knob surface (BR-11.6.4) has no collection knob, and baseline
# parity keeps the name "personal_kb".
QDRANT_COLLECTION = "personal_kb"

HTTP_TIMEOUT_S = 10


@dataclass
class HealthResult:
    endpoint: str
    ok: bool
    detail: str
    remediation: str = ""


def check_qdrant(cfg) -> HealthResult:
    url = get(cfg, "qdrant.url")
    if not url:
        return HealthResult(
            "qdrant", False, "qdrant.url is not configured",
            "set qdrant.url in kb.local.yml (env: KB_QDRANT__URL), then re-run init/validate",
        )
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=url, api_key=get(cfg, "qdrant.api_key") or None)
        names = {c.name for c in client.get_collections().collections}
        if QDRANT_COLLECTION not in names:
            return HealthResult(
                "qdrant", True,
                f"reachable; collection {QDRANT_COLLECTION!r} will be created on first run",
            )
        dim = _collection_dim(client, QDRANT_COLLECTION)
        expected = model_dimension(get(cfg, "embedding.model") or DEFAULT_MODEL)
        if dim != expected:
            return HealthResult(
                "qdrant", False,
                f"collection {QDRANT_COLLECTION} is {dim}-dim but the pinned model "
                f"produces {expected}-dim vectors",
                "recreate the collection at the pinned dimension or re-embed (FR-010)",
            )
        return HealthResult(
            "qdrant", True, f"reachable; {QDRANT_COLLECTION} is {dim}-dim")
    except Exception as exc:  # any transport/auth failure reads as unreachable
        return HealthResult(
            "qdrant", False, f"unreachable: {exc.__class__.__name__}: {exc}",
            "check qdrant.url (env: KB_QDRANT__URL) points at a live Qdrant host:port",
        )


def _collection_dim(client, name):
    info = client.get_collection(name)
    vectors = info.config.params.vectors
    if hasattr(vectors, "size"):  # single-vector collection
        return vectors.size
    first = next(iter(vectors.values()), None)
    return first.size if first is not None else None


def check_neo4j(cfg) -> HealthResult:
    url = get(cfg, "neo4j.url")
    if not url:
        return HealthResult(
            "neo4j", False, "neo4j.url is not configured",
            "set neo4j.url in kb.local.yml (env: KB_NEO4J__URL), then re-run init/validate",
        )
    user = get(cfg, "neo4j.user")
    password = get(cfg, "neo4j.password")
    if not user or not password:
        return HealthResult(
            "neo4j", False, "neo4j.user/neo4j.password are not configured",
            "set neo4j.user and neo4j.password "
            "(env: KB_NEO4J__USER / KB_NEO4J__PASSWORD)",
        )
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(url, auth=(user, password))
        try:
            driver.verify_connectivity()
            with driver.session() as session:
                session.run("RETURN 1")
            return HealthResult("neo4j", True, f"reachable; auth ok at {url}")
        finally:
            driver.close()
    except Exception as exc:
        if "Auth" in type(exc).__name__:
            remediation = (
                "correct neo4j.user / neo4j.password "
                "(env: KB_NEO4J__USER / KB_NEO4J__PASSWORD)")
        else:
            remediation = (
                "check neo4j.url (env: KB_NEO4J__URL) points at a live "
                "Neo4j bolt endpoint")
        return HealthResult(
            "neo4j", False, f"{type(exc).__name__}: {exc}", remediation)


def check_llm(cfg) -> HealthResult:
    endpoint = get(cfg, "llm.endpoint")
    if not endpoint:
        return HealthResult(
            "llm", False, "llm.endpoint is not configured",
            "set llm.endpoint in kb.local.yml (env: KB_LLM__ENDPOINT) to an "
            "OpenAI-compatible base URL",
        )
    api_key = get(cfg, "llm.api_key")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    target = endpoint.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(target, headers=headers)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return HealthResult(
                "llm", True, f"reachable ({target} -> HTTP {resp.status})")
    except urllib.error.HTTPError as exc:
        # the server answered: it is reachable (auth/model config may still need care)
        return HealthResult(
            "llm", True,
            f"reachable ({target} -> HTTP {exc.code}); endpoint answered")
    except Exception as exc:
        return HealthResult(
            "llm", False, f"unreachable: {type(exc).__name__}: {exc}",
            "check llm.endpoint (env: KB_LLM__ENDPOINT) is a live "
            "OpenAI-compatible URL",
        )


def run_health_checks(cfg) -> list:
    return [check_qdrant(cfg), check_neo4j(cfg), check_llm(cfg)]
