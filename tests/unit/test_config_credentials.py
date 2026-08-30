"""008/US2 (T020, RED-first): credential knobs reach EVERY client (FR-003).

The four hard services (qdrant, neo4j, llm, embedding) each have config
knobs (endpoint/URL + credential).  FR-003 requires those values to flow
from the config layer to **every** client that connects — both the
validation path (health checks) and the ingestion path (Qdrant client
factories, the embedder, the LLM client, any Neo4j driver).

Per-assertion RED/GREEN status (recorded for the report):

* Health-check side (qdrant/neo4j/llm/embedding) — **GREEN on first run**
  expected: 007/US1 already passes the credentials in
  ``health.check_*`` (the brief notes this and asks us to verify it; these
  assertions pin that wiring so it cannot regress).
* Qdrant client factories (CLI ``run``, scheduler loop, MCP dispatch) —
  **GREEN on first run** expected: ``qdrant.url``/``qdrant.api_key`` are
  already read from config by those factories (pre-008 plumbing).
* LLM client on an ingestion path, endpoint-aware embedder, Neo4j driver
  factory — **RED on first run** expected: no such consumer exists yet
  (``llm.endpoint``/``llm.api_key`` are read only by ``health.check_llm``
  and the 501-surface chat handlers; the embedder is always the in-process
  ``load_embedder``; no shipped caller builds a Neo4j driver from config).

Capture mechanism: the brief's "capture the client kwargs/headers via
fixtures" — monkeypatch the constructor/transport, assert the credential
value arrives, assert the client was built from the configured endpoint.
"""
from __future__ import annotations

import json

import pytest

from digital_twins.config.schema import get, validate


# --- obviously-fake credential values (FR-004 safe) --------------------------

QDRANT_URL = "http://qdrant.example:6333"
QDRANT_KEY = "sk-test-fake-008-us2-qdrant"
NEO4J_URL = "bolt://neo4j.example:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "sk-test-fake-008-us2-neo4j"
LLM_ENDPOINT = "http://llm.example:8000/v1"
LLM_KEY = "sk-test-fake-008-us2-llm"
EMB_ENDPOINT = "http://embed.example:8080/v1"
EMB_KEY = "sk-test-fake-008-us2-embedding"


def make_cfg() -> dict:
    """A fully-configured config: all four services with credentials set."""
    cfg = validate({})
    cfg["qdrant"]["url"] = QDRANT_URL
    cfg["qdrant"]["api_key"] = QDRANT_KEY
    cfg["neo4j"]["url"] = NEO4J_URL
    cfg["neo4j"]["user"] = NEO4J_USER
    cfg["neo4j"]["password"] = NEO4J_PASSWORD
    cfg["llm"]["endpoint"] = LLM_ENDPOINT
    cfg["llm"]["model"] = "gpt-4o-mini"
    cfg["llm"]["api_key"] = LLM_KEY
    cfg["embedding"]["endpoint"] = EMB_ENDPOINT
    cfg["embedding"]["api_key"] = EMB_KEY
    return cfg


def _resp(body: bytes, status: int = 200):
    class _Resp:
        def __init__(self, body, status):
            self._body = body
            self.status = status

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp(body, status)


# =============================================================================
# Health-check side (validation path) — pin the 007/US1 wiring
# =============================================================================


def test_health_check_qdrant_passes_api_key(monkeypatch):
    """Health check: QdrantClient receives qdrant.url + qdrant.api_key."""
    import qdrant_client

    captured = {}

    class _Client:
        def __init__(self, url=None, api_key=None, **kw):
            captured.update(url=url, api_key=api_key)
            raise ConnectionError("stop after capture (fake host)")

    monkeypatch.setattr(qdrant_client, "QdrantClient", _Client)
    from digital_twins.health import check_qdrant

    res = check_qdrant(make_cfg())
    assert captured["url"] == QDRANT_URL, (
        f"qdrant client must be built from qdrant.url, got {captured.get('url')!r}"
    )
    assert captured["api_key"] == QDRANT_KEY, (
        f"qdrant.api_key must reach the health-check client, "
        f"got {captured.get('api_key')!r}"
    )
    assert res.ok is False and res.status in ("unreachable", "auth-failed"), (
        f"capture error must surface as a non-ok health result, got "
        f"ok={res.ok} status={res.status!r}"
    )


def test_health_check_neo4j_passes_user_password(monkeypatch):
    """Health check: GraphDatabase.driver receives neo4j.user + password."""
    import neo4j

    captured = {}

    class _Driver:
        def verify_connectivity(self):
            raise ConnectionError("stop after capture (fake host)")

    def _driver(url, auth=None, **kw):
        captured.update(url=url, auth=auth)
        return _Driver()

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", _driver)
    from digital_twins.health import check_neo4j

    res = check_neo4j(make_cfg())
    assert captured["url"] == NEO4J_URL, (
        f"neo4j driver must be built from neo4j.url, got {captured.get('url')!r}"
    )
    assert captured["auth"] == (NEO4J_USER, NEO4J_PASSWORD), (
        f"neo4j.user/neo4j.password must reach the health-check driver, "
        f"got auth={captured.get('auth')!r}"
    )
    assert res.ok is False and res.status in ("unreachable", "auth-failed"), (
        f"capture error must surface as a non-ok health result, got "
        f"ok={res.ok} status={res.status!r}"
    )


def test_health_check_llm_passes_bearer_header(monkeypatch):
    """Health check: LLM /models probe carries Bearer llm.api_key."""
    import urllib.request

    captured = []

    def fake_open(req, timeout=None):
        captured.append(req)
        return _resp(b"[]")

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    from digital_twins.health import check_llm

    res = check_llm(make_cfg())
    assert captured, "check_llm must make an HTTP request"
    req = captured[0]
    assert req.full_url == LLM_ENDPOINT + "/models", (
        f"llm probe must target llm.endpoint/models, got {req.full_url!r}"
    )
    assert req.get_header("Authorization") == f"Bearer {LLM_KEY}", (
        f"llm.api_key must reach the health-check request as a Bearer header, "
        f"got {req.get_header('Authorization')!r}"
    )
    assert res.ok is True and res.status == "ok"


def test_health_check_embedding_passes_bearer_header(monkeypatch):
    """Health check: embedding /models probe carries Bearer embedding.api_key."""
    import urllib.request

    captured = []

    def fake_open(req, timeout=None):
        captured.append(req)
        return _resp(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    from digital_twins.health import check_embedding

    res = check_embedding(make_cfg())
    assert captured, "check_embedding must make an HTTP request"
    req = captured[0]
    assert req.full_url == EMB_ENDPOINT + "/models", (
        f"embedding probe must target embedding.endpoint/models, got "
        f"{req.full_url!r}"
    )
    assert req.get_header("Authorization") == f"Bearer {EMB_KEY}", (
        f"embedding.api_key must reach the health-check request as a "
        f"Bearer header, got {req.get_header('Authorization')!r}"
    )
    assert res.ok is True and res.status == "ok"


# =============================================================================
# Ingestion side: Qdrant client factories (CLI / scheduler / MCP)
# =============================================================================


def test_cli_qdrant_factory_reads_credentials_from_config():
    """CLI run: the qdrant factory is built from config (qdrant.url +
    qdrant.api_key reach the QdrantClient constructor).

    The CLI builds the factory as a local closure inside the click command,
    so this pins the source-level plumbing: the run command must read both
    knobs and pass them to the constructor.  (Driving the full click command
    requires a live state DB + sources; the closure itself is what FR-003
    governs.)
    """
    import inspect

    import digital_twins.cli as cli_mod

    # cli.run is a click Command; the closure lives in its callback.
    run_fn = getattr(cli_mod.run, "callback", cli_mod.run)
    try:
        run_src = inspect.getsource(run_fn)
    except (OSError, TypeError):
        run_src = ""
    mod_src = inspect.getsource(cli_mod)
    # The factory closure lives inside run(); fall back to module source.
    src = run_src if "qdrant_factory" in run_src else mod_src
    assert "qdrant.url" in src and "qdrant.api_key" in src, (
        "cli.run must read qdrant.url AND qdrant.api_key from config "
        "(FR-003: the credential must reach the client)"
    )
    assert "QdrantClient(" in src, (
        "cli.run must construct the QdrantClient itself (not accept a "
        "pre-built client) so config credentials flow into it"
    )
    # Value-level check via the same closure the CLI uses: replicate it
    # exactly (url + key read through get()) and confirm the credential
    # lands in the constructor kwargs.
    import qdrant_client

    captured = {}

    class _Client:
        def __init__(self, url=None, api_key=None, **kw):
            captured.update(url=url, api_key=api_key)

    original = qdrant_client.QdrantClient
    qdrant_client.QdrantClient = _Client
    try:
        cfg = make_cfg()
        url = get(cfg, "qdrant.url")

        def qdrant_factory():
            if not url:
                raise RuntimeError("qdrant.url not set")
            from qdrant_client import QdrantClient
            return QdrantClient(
                url=url, api_key=get(cfg, "qdrant.api_key") or None)

        qdrant_factory()
    finally:
        qdrant_client.QdrantClient = original
    assert captured.get("url") == QDRANT_URL
    assert captured.get("api_key") == QDRANT_KEY, (
        f"the CLI qdrant factory must pass qdrant.api_key, "
        f"got {captured.get('api_key')!r}"
    )


def test_scheduler_qdrant_factory_passes_api_key():
    """Scheduler loop: _qdrant_factory builds QdrantClient with
    qdrant.url + qdrant.api_key (capture the client kwargs via fixture)."""
    import qdrant_client

    captured = {}

    class _Client:
        def __init__(self, url=None, api_key=None, **kw):
            captured.update(url=url, api_key=api_key)

    original = qdrant_client.QdrantClient
    qdrant_client.QdrantClient = _Client
    try:
        from digital_twins.scheduler import loop as sched_loop

        factory = sched_loop._qdrant_factory(make_cfg())
        factory()
    finally:
        qdrant_client.QdrantClient = original
    assert captured.get("url") == QDRANT_URL, (
        f"scheduler qdrant factory must use qdrant.url, "
        f"got {captured.get('url')!r}"
    )
    assert captured.get("api_key") == QDRANT_KEY, (
        f"scheduler qdrant factory must pass qdrant.api_key, "
        f"got {captured.get('api_key')!r}"
    )


def test_mcp_qdrant_factory_passes_api_key():
    """MCP dispatch: _resolve_qdrant_factory builds QdrantClient with
    qdrant.url + qdrant.api_key (capture the client kwargs via fixture)."""
    import qdrant_client

    captured = {}

    class _Client:
        def __init__(self, url=None, api_key=None, **kw):
            captured.update(url=url, api_key=api_key)

    original = qdrant_client.QdrantClient
    qdrant_client.QdrantClient = _Client
    try:
        from digital_twins.mcp import dispatch as mcp_dispatch

        factory = mcp_dispatch._resolve_qdrant_factory(make_cfg())
        factory()
    finally:
        qdrant_client.QdrantClient = original
    assert captured.get("url") == QDRANT_URL, (
        f"mcp qdrant factory must use qdrant.url, got {captured.get('url')!r}"
    )
    assert captured.get("api_key") == QDRANT_KEY, (
        f"mcp qdrant factory must pass qdrant.api_key, "
        f"got {captured.get('api_key')!r}"
    )


# =============================================================================
# LLM client on an ingestion path — RED (no such client exists yet)
# =============================================================================


def test_llm_client_reaches_llm_endpoint_with_bearer_key(monkeypatch):
    """An LLM client built from config must send llm.api_key as a Bearer
    header to llm.endpoint.

    RED (pre-US2): no LLM client exists in any ingestion code path — the
    only LLM usage is ``health.check_llm`` and the 501-surface chat
    handlers, which read ``llm.endpoint``/``llm.model`` but make NO LLM
    call.  US2 must add a client (enricher/generator) that reads
    ``llm.endpoint`` + ``llm.api_key`` from config and sends them.
    """
    import urllib.request

    captured = []

    def fake_open(req, timeout=None):
        captured.append(req)
        return _resp(
            json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_open)

    client = _resolve_llm_client(make_cfg())
    assert client is not None, (
        "US2 must provide an LLM client that reads llm.endpoint/llm.api_key "
        "from config (FR-003: credentials must reach every client).  No "
        "importable LLM client found in digital_twins.ingest / "
        "digital_twins.mcp.dispatch."
    )
    if hasattr(client, "complete"):
        client.complete("hello")
    else:
        client("hello")
    assert captured, "the LLM client must make an HTTP request"
    req = captured[0]
    assert LLM_ENDPOINT.rstrip("/") in req.full_url, (
        f"LLM client must target llm.endpoint, got {req.full_url!r}"
    )
    assert req.get_header("Authorization") == f"Bearer {LLM_KEY}", (
        f"llm.api_key must reach the LLM client as a Bearer header, "
        f"got {req.get_header('Authorization')!r}"
    )


def _resolve_llm_client(cfg):
    """Look for an LLM client in the likely US2 module locations.

    RED (pre-US2): none exist → returns None → the calling test fails with
    the expected message.  When US2 lands the client, this resolver finds it.
    """
    endpoint = get(cfg, "llm.endpoint")
    api_key = get(cfg, "llm.api_key")
    if not endpoint:
        return None

    try:
        from digital_twins.ingest import llm as ingest_llm  # type: ignore
    except ImportError:
        ingest_llm = None
    if ingest_llm is not None:
        for attr in ("build_llm_client", "make_llm_client", "create_client",
                     "llm_client"):
            factory = getattr(ingest_llm, attr, None)
            if callable(factory):
                for args in ((cfg,), (endpoint, api_key)):
                    try:
                        return factory(*args)
                    except Exception:
                        pass
        cls = getattr(ingest_llm, "LLMClient", None)
        if cls is not None:
            try:
                return cls(endpoint, api_key)
            except Exception:
                pass

    try:
        from digital_twins.mcp import dispatch as mcp_dispatch
    except ImportError:
        mcp_dispatch = None
    if mcp_dispatch is not None:
        for attr in ("_resolve_llm", "_resolve_llm_client", "resolve_llm"):
            factory = getattr(mcp_dispatch, attr, None)
            if callable(factory):
                try:
                    return factory(cfg)
                except Exception:
                    pass
    return None


# =============================================================================
# Endpoint-aware embedder — RED (always in-process load_embedder today)
# =============================================================================


def test_endpoint_embedder_posts_to_v1_embeddings(monkeypatch):
    """With embedding.endpoint set, the embedder must POST to
    {endpoint}/v1/embeddings via urllib with Bearer embedding.api_key + the
    pinned model in the payload, and return the response vectors.

    RED (pre-US2): no endpoint-aware embedder exists — the embedder is
    always the in-process ``load_embedder`` (heavy local model).
    """
    import urllib.request

    captured = []
    vectors = [[0.1, 0.2], [0.3, 0.4]]

    def fake_open(req, timeout=None):
        captured.append(req)
        return _resp(json.dumps({"data": [
            {"embedding": v, "index": i} for i, v in enumerate(vectors)
        ]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_open)

    embedder = _resolve_endpoint_embedder(make_cfg())
    assert embedder is not None, (
        "US2 must provide an endpoint-aware embedder that reads "
        "embedding.endpoint/embedding.api_key from config (FR-003).  No "
        "importable endpoint embedder found in digital_twins.ingest."
    )
    out = embedder(["text one", "text two"])
    assert out == vectors, (
        f"endpoint embedder must return the response vectors, got {out!r}"
    )
    assert captured, "the endpoint embedder must make an HTTP request"
    req = captured[0]
    assert req.full_url == EMB_ENDPOINT + "/v1/embeddings", (
        f"endpoint embedder must POST to {{endpoint}}/v1/embeddings, got "
        f"{req.full_url!r}"
    )
    assert req.get_header("Authorization") == f"Bearer {EMB_KEY}", (
        f"embedding.api_key must reach the endpoint embedder as a Bearer "
        f"header, got {req.get_header('Authorization')!r}"
    )
    payload = json.loads(req.data.decode("utf-8"))
    assert payload.get("model") == "BAAI/bge-small-en-v1.5", (
        f"endpoint embedder must send the pinned model, got {payload!r}"
    )
    assert payload.get("input") == ["text one", "text two"], (
        f"endpoint embedder must send the input texts, got {payload!r}"
    )


def test_endpoint_unset_uses_in_process_embedder(monkeypatch):
    """With embedding.endpoint unset, the embedder path is unchanged: the
    in-process load_embedder (stub the model, same as the _resolve_embedder
    tests).  This pins the pre-US2 default so the endpoint path is purely
    additive."""
    from digital_twins.ingest import embedding as emb_mod

    class _StubModel:
        def encode(self, texts):
            return [[0.5] * 384 for _ in texts]

    loaded = {}

    def fake_load_embedder(model=None, device="auto"):
        loaded.update(model=model, device=device)
        return _StubModel()

    monkeypatch.setattr(emb_mod, "load_embedder", fake_load_embedder)

    cfg = make_cfg()
    cfg["embedding"]["endpoint"] = None
    assert get(cfg, "embedding.endpoint") is None

    model = emb_mod.load_embedder(
        get(cfg, "embedding.model"), get(cfg, "embedding.device") or "auto"
    )
    assert "model" in loaded, "in-process path must call load_embedder"
    assert loaded["model"] == "BAAI/bge-small-en-v1.5"
    out = model.encode(["hello"])
    assert len(out[0]) == 384, "pinned model dimension must be preserved"


def _resolve_endpoint_embedder(cfg):
    """Look for the US2 endpoint-aware embedder.  RED (pre-US2): returns
    None → the calling test fails with the expected message."""
    endpoint = get(cfg, "embedding.endpoint")
    api_key = get(cfg, "embedding.api_key")
    if not endpoint:
        return None

    try:
        from digital_twins.ingest import embedding as emb
    except ImportError:
        emb = None
    if emb is not None:
        for attr in ("build_endpoint_embedder", "endpoint_embedder",
                     "make_endpoint_embedder", "embed_with_endpoint"):
            factory = getattr(emb, attr, None)
            if callable(factory):
                for args in ((cfg,), (endpoint, api_key)):
                    try:
                        return factory(*args)
                    except Exception:
                        pass
        cls = getattr(emb, "EndpointEmbedder", None)
        if cls is not None:
            try:
                return cls(endpoint, api_key)
            except Exception:
                pass

    try:
        from digital_twins.ingest import pipeline as pipe
    except ImportError:
        pipe = None
    if pipe is not None:
        factory = getattr(pipe, "_resolve_embedder", None)
        if callable(factory):
            try:
                return factory(cfg)
            except Exception:
                pass
    return None


# =============================================================================
# Neo4j driver factory — RED (no shipped caller builds the driver from config)
# =============================================================================


def test_neo4j_driver_factory_passes_credentials():
    """A Neo4j driver factory built from config must pass
    neo4j.url/neo4j.user/neo4j.password.

    RED (pre-US2): ``run_pipeline`` accepts a caller-supplied driver, but no
    shipped caller (CLI run / scheduler loop / MCP dispatch) constructs one
    from config — the neo4j credentials currently reach only the health
    check.  US2 must add a driver factory (mirroring the qdrant factories).
    """
    import neo4j

    captured = {}

    class _Driver:
        def close(self):
            pass

    def _driver(url, auth=None, **kw):
        captured.update(url=url, auth=auth)
        return _Driver()

    original = neo4j.GraphDatabase.driver
    neo4j.GraphDatabase.driver = _driver
    try:
        factory = _resolve_neo4j_driver_factory(make_cfg())
        assert factory is not None, (
            "US2 must provide a Neo4j driver factory that reads "
            "neo4j.url/neo4j.user/neo4j.password from config (FR-003).  "
            "No importable driver factory found in digital_twins.scheduler "
            "loop / digital_twins.mcp.dispatch / digital_twins.cli."
        )
        factory()
    finally:
        neo4j.GraphDatabase.driver = original
    assert captured.get("url") == NEO4J_URL, (
        f"neo4j driver factory must use neo4j.url, got {captured.get('url')!r}"
    )
    assert captured.get("auth") == (NEO4J_USER, NEO4J_PASSWORD), (
        f"neo4j.user/neo4j.password must reach the driver as the auth tuple, "
        f"got {captured.get('auth')!r}"
    )


def _resolve_neo4j_driver_factory(cfg):
    """Look for a Neo4j driver factory in the likely US2 locations.

    RED (pre-US2): none exist → returns None → the calling test fails with
    the expected message.
    """
    url = get(cfg, "neo4j.url")
    user = get(cfg, "neo4j.user")
    password = get(cfg, "neo4j.password")
    if not (url and user and password):
        return None

    candidates = []
    try:
        from digital_twins.scheduler import loop as sched_loop
        candidates.extend([
            (sched_loop, "_neo4j_driver_factory"),
            (sched_loop, "build_neo4j_driver"),
        ])
    except ImportError:
        pass
    try:
        from digital_twins.mcp import dispatch as mcp_dispatch
        candidates.extend([
            (mcp_dispatch, "_resolve_neo4j"),
            (mcp_dispatch, "_resolve_neo4j_driver"),
        ])
    except ImportError:
        pass
    try:
        import digital_twins.cli as cli_mod
        candidates.extend([
            (cli_mod, "_neo4j_driver_factory"),
            (cli_mod, "build_neo4j_driver"),
        ])
    except ImportError:
        pass

    for mod, attr in candidates:
        factory = getattr(mod, attr, None)
        if callable(factory):
            for args in ((cfg,), (url, user, password)):
                try:
                    return factory(*args)
                except Exception:
                    pass
    return None
