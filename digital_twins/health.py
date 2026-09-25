"""Endpoint health checks with remediation hints (US1; contracts/cli.md)."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from digital_twins.config.schema import get
from digital_twins.ingest.embedding import DEFAULT_MODEL, model_dimension

# Qdrant collection name, pinned in code like the embedding model: the
# contract's knob surface (BR-11.6.4) has no collection knob, and baseline
# parity keeps the name "personal_kb".
QDRANT_COLLECTION = "personal_kb"


def qdrant_collection(cfg) -> str:
    """Resolved Qdrant collection name (014 follow-up / independence knob).

    Reads `qdrant.collection` from the config layer; falls back to the
    pinned `QDRANT_COLLECTION` constant when the knob is unset or empty.
    The pinned constant remains the default so existing installs (and
    the baseline-parity `personal_kb` contract) are unchanged; a
    non-default value overrides it for all Qdrant ops in this process.
    """
    from digital_twins.config.schema import get
    val = get(cfg, "qdrant.collection")
    return val if val else QDRANT_COLLECTION

HTTP_TIMEOUT_S = 10

VALID_STATUSES = frozenset({"ok", "unconfigured", "unreachable", "auth-failed"})

logger = logging.getLogger(__name__)


class ServiceDependencyError(Exception):
    """Raised by preflight when a hard service dependency is not ok."""

    def __init__(self, service, status, remediation):
        super().__init__(f"{service}: {status} — {remediation}")
        self.service = service
        self.status = status
        self.remediation = remediation


_URL_QUERY_RE = re.compile(r'(https?://\S+?)\?[^"\s]*')


def _scrub(text: str, limit: int = 120) -> str:
    # NFR-13 / FR-004: exception text can echo request URLs; drop any
    # query string (credentials-in-URL) before it reaches validate output
    # or log records.
    def _strip(m):
        return m.group(1)
    return _URL_QUERY_RE.sub(_strip, text)[:limit]


def _classify(exc: BaseException) -> str:
    # ponytail: heuristic name/message match; add exact exception classes
    # (qdrant_client UnexpectedStatusCode etc.) if false positives appear
    name = type(exc).__name__
    msg = str(exc)
    if "Auth" in name or "Unauthorized" in msg or "Forbidden" in msg \
            or "401" in msg or "403" in msg:
        return "auth-failed"
    return "unreachable"


@dataclass
class HealthResult:
    endpoint: str
    ok: bool
    detail: str
    remediation: str = ""
    status: str = ""


def check_qdrant(cfg) -> HealthResult:
    url = get(cfg, "qdrant.url")
    if not url:
        return HealthResult(
            "qdrant", False, "qdrant.url is not configured",
            "set qdrant.url in kb.local.yml (env: KB_QDRANT__URL), then re-run setup/validate",
            status="unconfigured")
    coll = qdrant_collection(cfg)
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=url, api_key=get(cfg, "qdrant.api_key") or None)
        names = {c.name for c in client.get_collections().collections}
        if coll not in names:
            return HealthResult(
                "qdrant", True,
                f"reachable; collection {coll!r} will be created on first run",
            )
        dim = _collection_dim(client, coll)
        expected = model_dimension(get(cfg, "embedding.model") or DEFAULT_MODEL)
        if dim != expected:
            return HealthResult(
                "qdrant", False,
                f"collection {coll} is {dim}-dim but the pinned model "
                f"produces {expected}-dim vectors",
                "recreate the collection at the pinned dimension or re-embed (FR-010)",
            )
        return HealthResult(
            "qdrant", True, f"reachable; {coll} is {dim}-dim")
    except Exception as exc:
        status = _classify(exc)
        if status == "auth-failed":
            detail = f"auth failed: {exc.__class__.__name__}: {_scrub(str(exc))}"
            remediation = ("check qdrant.api_key / qdrant.url "
                           "(env: KB_QDRANT__API_KEY / KB_QDRANT__URL)")
        else:
            detail = f"unreachable: {exc.__class__.__name__}: {_scrub(str(exc))}"
            remediation = ("check qdrant.url (env: KB_QDRANT__URL) points at "
                           "a live Qdrant host:port")
        return HealthResult("qdrant", False, detail, remediation,
                            status=status)


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
            "set neo4j.url in kb.local.yml (env: KB_NEO4J__URL), then re-run setup/validate",
            status="unconfigured")
    user = get(cfg, "neo4j.user")
    password = get(cfg, "neo4j.password")
    if not user or not password:
        return HealthResult(
            "neo4j", False, "neo4j.user/neo4j.password are not configured",
            "set neo4j.user and neo4j.password "
            "(env: KB_NEO4J__USER / KB_NEO4J__PASSWORD)",
            status="unconfigured")
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(url, auth=(user, password))
        try:
            driver.verify_connectivity()
            with driver.session() as session:
                session.run("RETURN 1")
                # Graph shape probe: run a COUNT over the labels the
                # read path relies on.  On a fresh instance the labels
                # do not exist yet — the probe simply returns 0 (or the
                # query succeeds with an empty result).  A non-zero count
                # means the write path has run at least once.  This is a
                # soft diagnostic: a healthy-but-empty graph is not an
                # error, so the check stays ok=True; the count is
                # surfaced in the detail string for operators.
                # S4 graph shape: probe the label the pipeline writes
                # (openspec change s4-graph-alignment — SourceItem
                # replaces KbItem/KbChunk).
                count_result = session.run(
                    "MATCH (si:SourceItem) RETURN count(si) AS n").single()
                n_items = count_result["n"] if count_result else 0
                if n_items:
                    detail = (f"reachable; auth ok at {url}; "
                              f"{n_items} SourceItem node(s) in graph")
                else:
                    detail = f"reachable; auth ok at {url} (graph empty)"
            return HealthResult("neo4j", True, detail)
        finally:
            driver.close()
    except Exception as exc:
        status = "auth-failed" if "Auth" in type(exc).__name__ else "unreachable"
        if status == "auth-failed":
            remediation = (
                "correct neo4j.user / neo4j.password "
                "(env: KB_NEO4J__USER / KB_NEO4J__PASSWORD)")
        else:
            remediation = (
                "check neo4j.url (env: KB_NEO4J__URL) points at a live "
                "Neo4j bolt endpoint")
        return HealthResult(
            "neo4j", False,
            f"{type(exc).__name__}: {_scrub(str(exc))}", remediation,
            status=status)


def check_llm(cfg) -> HealthResult:
    endpoint = get(cfg, "llm.endpoint")
    if not endpoint:
        return HealthResult(
            "llm", False, "llm.endpoint is not configured",
            "set llm.endpoint in kb.local.yml (env: KB_LLM__ENDPOINT) to an "
            "OpenAI-compatible base URL",
            status="unconfigured")
    api_key = get(cfg, "llm.api_key")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    target = endpoint.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(target, headers=headers)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            detail = f"reachable ({target} -> HTTP {resp.status})"
            ctx = _llm_context_window(endpoint, headers)
            if ctx is not None:
                detail += f"; context window {ctx} (SGLang /get_model_info)"
            else:
                detail += "; context window unknown (no SGLang /get_model_info)"
            return HealthResult("llm", True, detail, status="ok")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # server answered but rejected credentials — not ok (FR-001)
            return HealthResult(
                "llm", False,
                f"auth failed ({target} -> HTTP {exc.code}); endpoint answered",
                "check llm.api_key / llm.endpoint "
                "(env: KB_LLM__API_KEY / KB_LLM__ENDPOINT)",
                status="auth-failed")
        # Non-auth HTTP error: the endpoint answered but /models is not
        # healthy (wrong base path 404, failing server 5xx) — not ok
        # (diff-review P2: a misconfigured endpoint must fail fast).
        return HealthResult(
            "llm", False,
            f"endpoint misconfigured ({target} -> HTTP {exc.code})",
            "check llm.endpoint (env: KB_LLM__ENDPOINT) is a live "
            "OpenAI-compatible URL whose /models route answers 2xx",
            status="unreachable")
    except Exception as exc:
        return HealthResult(
            "llm", False,
            f"unreachable: {type(exc).__name__}: {_scrub(str(exc))}",
            "check llm.endpoint (env: KB_LLM__ENDPOINT) is a live "
            "OpenAI-compatible URL",
            status="unreachable")


def _llm_context_window(base: str, headers: dict) -> int | None:
    """Probe an SGLang server root for its model context window.

    SGLang serves GET /get_model_info at the server root (outside /v1) and
    reports the model's ``context_len``. Non-SGLang OpenAI-compatible
    endpoints lack the route, so a failed probe reads as "unknown" rather
    than an error (reachability already established).
    """
    roots = [base]
    if base.endswith("/v1"):
        roots.insert(0, base[: -len("/v1")])
    for root in roots:
        try:
            req = urllib.request.Request(
                root.rstrip("/") + "/get_model_info", headers=headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            continue
        ctx = data.get("context_len") if isinstance(data, dict) else None
        if isinstance(ctx, int) and ctx > 0:
            return ctx
    return None

def check_embedding(cfg) -> HealthResult:
    endpoint = get(cfg, "embedding.endpoint")
    if not endpoint:
        model = get(cfg, "embedding.model") or DEFAULT_MODEL
        try:
            dim = model_dimension(model)
        except Exception as exc:
            return HealthResult(
                "embedding", False,
                f"in-process check failed: {exc.__class__.__name__}: "
                f"{_scrub(str(exc))}",
                "check embedding.model (env: KB_EMBEDDING__MODEL)",
                status="unreachable")
        return HealthResult(
            "embedding", True,
            f"in-process embedding ok ({model}, {dim}-dim)",
            status="ok")
    api_key = get(cfg, "embedding.api_key")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    target = endpoint.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(target, headers=headers)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return HealthResult(
                "embedding", True,
                f"reachable ({target} -> HTTP {resp.status})",
                status="ok")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return HealthResult(
                "embedding", False,
                f"auth failed ({target} -> HTTP {exc.code}); endpoint answered",
                "check embedding.api_key / embedding.endpoint "
                "(env: KB_EMBEDDING__API_KEY / KB_EMBEDDING__ENDPOINT)",
                status="auth-failed")
        return HealthResult(
            "embedding", False,
            f"endpoint misconfigured ({target} -> HTTP {exc.code})",
            "check embedding.endpoint (env: KB_EMBEDDING__ENDPOINT) is a "
            "live OpenAI-compatible URL whose /models route answers 2xx",
            status="unreachable")
    except Exception as exc:
        return HealthResult(
            "embedding", False,
            f"unreachable: {type(exc).__name__}: {_scrub(str(exc))}",
            "check embedding.endpoint (env: KB_EMBEDDING__ENDPOINT) is a "
            "live OpenAI-compatible URL",
            status="unreachable")


def run_health_checks(cfg) -> list:
    return [check_qdrant(cfg), check_neo4j(cfg), check_llm(cfg),
            check_embedding(cfg)]


def preflight(cfg) -> list:
    """Gate: every service in run_health_checks is a hard dependency (US1;
    T013 shape — all four, none optional).

    Raises ServiceDependencyError on the first non-ok check, before the
    pipeline touches any store (no audit row, no upserts). Returns the
    ordered list of ok service names when all pass.
    """
    ok_services = []
    for res in run_health_checks(cfg):
        if not res.ok:
            logger.warning(
                "preflight: service %s is %s: %s", res.endpoint, res.status,
                res.remediation)
            raise ServiceDependencyError(res.endpoint, res.status,
                                         res.remediation)
        ok_services.append(res.endpoint)
    return ok_services
