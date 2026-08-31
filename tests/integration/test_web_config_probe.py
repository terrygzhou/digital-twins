"""009 (T002–T013, RED-first): web admin UI — Services panel + probe.

Contract: ``specs/009-admin-config-panel/contracts/web-config-api.md``
(extends the 008 config surface; the two 008 routes
``GET/POST /api/config/services`` are unchanged — the new admin-gated
``POST /api/config/services/probe`` is covered by T006–T008 / T012).

This module hosts the 009 integration tests:

* **T002** (static contract, RED): two functions against ``GET /`` —
  the US1 panel test (panel section hidden by default, row container,
  error line, per-row Save button marker, admin gate
  ``me.role === "admin"``, the four service names, the 008 fetch
  ``/api/config/services``; goes green with T004) and the US2
  probe-wiring test (Test-all + per-row Test button markers, the 009
  probe fetch ``/api/config/services/probe``, initial ``not tested``
  pill; RED until T011 wires the probe).
* **T003** (US1 save mapping, expected GREEN on first run — mapping
  guard like the 008 T024 409 ruling): the UI display→knob mapping
  (``qdrant.url`` / ``neo4j.url`` / ``llm.endpoint`` /
  ``embedding.endpoint``) must be accepted by the existing 008
  ``POST /api/config/services`` and visible in the post-write and the
  subsequent masked views.
* **T006** (probe happy path, RED): monkeypatched ``digital_twins.health.check_*`` fakes covering all four statuses → ``POST /api/config/services/probe`` with ``{}`` → 200, canonical order, exact ``status|detail|remediation`` fields; a subset request is honored in request order; and one UN-mocked embedding probe against a real local ``http.server`` answering ``/models`` → ``ok`` (FR-003).
* **T007** (probe errors, RED): a non-object JSON body → 400 ``invalid JSON body``; a non-list/empty ``services`` → 400 ``services must be a non-empty list``; an unknown name → 404 ``unknown service "x"`` (the 008 shapes, pinned for the probe).
* **T008** (SC-002 budget, RED): all four checks sleeping past the per-service deadline → the full four-service response in ≤ 5.0 s, every entry ``unreachable`` with the timeout line.

Harness: reuses the 008 fixture + http helpers from
``test_web_config_api`` (same directory; no ``tests/`` package, so a
plain module import).
"""
from __future__ import annotations

import http.client
import json
import time

import pytest

from test_web_config_api import _http_get, _http_post, web_config_app


# =============================================================================
# T002 — static contract: index.html carries the Services panel markup
# =============================================================================


def _get_index_html(web_config_app):
    """GET / as the app serves it; return the decoded HTML (asserts 200)."""
    _app, _db, host, port, _at, _rt, _cd = web_config_app
    code, _parsed, raw = _http_get(host, port, "/")
    assert code == 200, f"GET / expected 200, got {code}: {raw[:200]!r}"
    return raw.decode("utf-8")


def test_index_html_services_panel_static_contract(web_config_app):
    """T002 (US1): GET / must serve index.html containing the US1
    Services panel markup (research D5): the panel section hidden by
    default, the row container, the error line, the per-row Save
    button marker, the admin gate, all four service names, and the
    008 masked-view fetch.

    RED: the panel does not exist yet.  GREEN with T004.
    """
    html = _get_index_html(web_config_app)

    # Panel container — present and hidden by default (SC-004: the
    # config surface must not be reachable before the role check).
    assert 'id="services-panel" hidden' in html, (
        "index.html must contain a section with id=\"services-panel\" "
        "and the hidden attribute (panel hidden by default)"
    )

    # Row container / error line (static markers; rows are
    # JS-rendered into the container).
    assert "services-rows" in html, (
        "index.html must contain the services row container "
        "(services-rows)"
    )
    assert "services-error" in html, (
        "index.html must contain the panel error line (services-error)"
    )

    # Per-row Save button (rendered by the panel JS — locked as a
    # marker).
    assert "services-save-btn" in html, (
        "index.html must wire a per-row Save button (services-save-btn)"
    )

    # Admin gate: the panel is un-hidden ONLY behind this check.
    assert 'me.role === "admin"' in html, (
        'index.html must gate the panel on me.role === "admin"'
    )

    # All four hard services wired (canonical order).
    for name in ("qdrant", "neo4j", "llm", "embedding"):
        assert name in html, f"index.html must wire service {name!r}"

    # The 008 masked-view fetch.
    assert "/api/config/services" in html, (
        "index.html must fetch GET /api/config/services"
    )


def test_index_html_probe_wiring_static_contract(web_config_app):
    """T002 (US2 half): index.html must carry the probe-wiring
    markers — Test-all toolbar button, per-row Test button marker,
    the 009 probe fetch, and the initial ``not tested`` pill value.

    RED until T011 wires the probe fetch + Test buttons.
    """
    html = _get_index_html(web_config_app)

    assert "services-test-all-btn" in html, (
        "index.html must contain the Test-all button "
        "(services-test-all-btn)"
    )
    assert "services-test-btn" in html, (
        "index.html must wire a per-row Test button (services-test-btn)"
    )
    assert "/api/config/services/probe" in html, (
        "index.html must fetch POST /api/config/services/probe"
    )
    assert "not tested" in html, (
        "index.html must render the initial status pill 'not tested'"
    )


# =============================================================================
# T003 — save mapping: the UI display→knob mapping must be a valid 008 POST
# =============================================================================

UI_KNOB_MAP = [
    ("qdrant", "url"),
    ("neo4j", "url"),
    ("llm", "endpoint"),
    ("embedding", "endpoint"),
]

SAVE_URLS = {
    "qdrant": "http://127.0.0.1:16333",
    "neo4j": "bolt://127.0.0.1:17687",
    "llm": "http://127.0.0.1:18000/v1",
    "embedding": "http://127.0.0.1:18080/v1",
}


@pytest.mark.parametrize("service,knob", UI_KNOB_MAP)
def test_save_mapping_round_trip(web_config_app, service, knob):
    """POST ``{"<service>": {"<knob>": value}}`` with the panel's
    display→knob mapping → 200, and the new effective url is visible in
    the post-write view AND the subsequent GET view (the masked view
    flattens ``*.endpoint`` to the UI key ``url``).

    Expected GREEN on first run (mapping guard, like the 008 T024 409
    ruling): the 008 POST route already accepts these knob bodies —
    this locks the mapping the panel posts, so a future knob rename
    cannot silently turn the panel's Save into a 422 or a no-op write.
    """
    _app, _db, host, port, admin_token, _rt, _cd = web_config_app
    new_url = SAVE_URLS[service]
    headers = {"Authorization": f"Bearer {admin_token}"}

    code, parsed, raw = _http_post(
        host, port, "/api/config/services",
        {service: {knob: new_url}},
        headers=headers,
    )
    assert code == 200, (
        f"POST /api/config/services with {{{service!r}: {{{knob!r}: …}}}} "
        f"expected 200, got {code}: {raw[:300]!r}"
    )
    assert parsed is not None, f"post-write body must be JSON: {raw[:300]!r}"

    row = parsed.get("services", {}).get(service, {})
    assert row.get("url") == new_url, (
        f"post-write view must show services.{service}.url == "
        f"{new_url!r}, got {row!r}"
    )

    code2, parsed2, raw2 = _http_get(
        host, port, "/api/config/services", headers=headers,
    )
    assert code2 == 200, (
        f"GET /api/config/services after save expected 200, got {code2}: "
        f"{raw2[:300]!r}"
    )
    row2 = parsed2.get("services", {}).get(service, {})
    assert row2.get("url") == new_url, (
        f"GET /api/config/services after save must show "
        f"services.{service}.url == {new_url!r}, got {row2!r}"
    )


# =============================================================================
# T006 - probe happy path (admin, mocked checks + one real-network probe)
# =============================================================================


def _http_post_raw(host, port, path, raw_body, headers=None, timeout=5.0):
    """POST a pre-serialized body (for malformed-JSON + long-poll tests).
    Returns ``(status, parsed, raw)`` like ``_http_post``."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        conn.request("POST", path, body=raw_body, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            parsed = None
        return resp.status, parsed, raw
    finally:
        conn.close()


def _fake_check(service, status, detail=None, remediation=None):
    """Build a fake ``check_<service>`` that ignores its config and returns
    a pinned ``HealthResult`` (the T010 handler maps the fields verbatim)."""
    from digital_twins.health import HealthResult

    def fake(cfg):
        return HealthResult(
            endpoint=service,
            ok=(status == "ok"),
            detail=detail if detail is not None else f"fake-{status}-detail",
            remediation=(
                remediation
                if remediation is not None
                else f"fake-{status}-remediation"
            ),
            status=status,
        )

    return fake


def test_probe_all_four_200_canonical_order(web_config_app, monkeypatch):
    """T006: POST /api/config/services/probe with ``{}`` as admin -> 200,
    all four services in canonical order, each entry exactly
    ``{status, detail, remediation}`` with the mocked statuses exact.

    RED: the route does not exist yet (404 not_found).
    """
    import digital_twins.health as health

    _app, _db, host, port, admin_token, _rt, _cd = web_config_app
    monkeypatch.setattr(health, "check_qdrant",
                        _fake_check("qdrant", "ok"))
    monkeypatch.setattr(health, "check_neo4j",
                        _fake_check("neo4j", "unconfigured"))
    monkeypatch.setattr(health, "check_llm",
                        _fake_check("llm", "unreachable"))
    monkeypatch.setattr(health, "check_embedding",
                        _fake_check("embedding", "auth-failed"))

    code, parsed, raw = _http_post(
        host, port, "/api/config/services/probe", {},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, f"probe expected 200, got {code}: {raw[:300]!r}"
    assert isinstance(parsed, dict), f"probe body must be JSON: {raw[:300]!r}"
    services = parsed.get("services", {})
    assert list(services) == ["qdrant", "neo4j", "llm", "embedding"], (
        f"probe must return all four services in canonical order, got "
        f"{list(services)!r}"
    )
    expected_statuses = {
        "qdrant": "ok",
        "neo4j": "unconfigured",
        "llm": "unreachable",
        "embedding": "auth-failed",
    }
    for name, entry in services.items():
        assert set(entry) == {"status", "detail", "remediation"}, (
            f"services.{name} must be exactly "
            f"{{status, detail, remediation}}, got {sorted(entry)!r}"
        )
        assert entry["status"] == expected_statuses[name], (
            f"services.{name}.status must be {expected_statuses[name]!r}, "
            f"got {entry['status']!r}"
        )
        assert entry["detail"] == f"fake-{expected_statuses[name]}-detail"
        assert entry["remediation"] == f"fake-{expected_statuses[name]}-remediation"


def test_probe_subset_request_order(web_config_app, monkeypatch):
    """T006: ``{"services": ["llm", "qdrant"]}`` probes exactly those two,
    in request order."""
    import digital_twins.health as health

    _app, _db, host, port, admin_token, _rt, _cd = web_config_app
    for name in ("qdrant", "neo4j", "llm", "embedding"):
        monkeypatch.setattr(health, f"check_{name}",
                            _fake_check(name, "ok"))

    code, parsed, raw = _http_post(
        host, port, "/api/config/services/probe",
        {"services": ["llm", "qdrant"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, f"subset probe expected 200, got {code}: {raw[:300]!r}"
    services = parsed.get("services", {})
    assert list(services) == ["llm", "qdrant"], (
        f"subset probe must return exactly the requested services in "
        f"request order, got {list(services)!r}"
    )


def test_probe_embedding_real_local_server(web_config_app, monkeypatch):
    """T006 (FR-003): one probe is NOT mocked - ``embedding.endpoint``
    points at a real local http.server answering ``/models`` and the
    un-mocked ``check_embedding`` must read it as ``ok``."""
    import threading

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _ModelsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith("/models"):
                body = b'{"data": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
    # serve_forever() blocks the calling thread - run it in a daemon
    # thread so the test thread can drive the probe request.
    server_thread = threading.Thread(
        target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        srv_port = server.server_address[1]
        _app, _db, host, port, admin_token, _rt, _cd = web_config_app
        monkeypatch.setenv(
            "KB_EMBEDDING__ENDPOINT", f"http://127.0.0.1:{srv_port}/v1")

        code, parsed, raw = _http_post(
            host, port, "/api/config/services/probe",
            {"services": ["embedding"]},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert code == 200, (
            f"real-network embedding probe expected 200, got {code}: "
            f"{raw[:300]!r}"
        )
        entry = parsed.get("services", {}).get("embedding", {})
        assert entry.get("status") == "ok", (
            f"un-mocked check_embedding against the local /models server "
            f"must be ok, got {entry!r}"
        )
    finally:
        server.shutdown()
        server.server_close()


# =============================================================================
# T007 - probe error shapes (malformed body, bad services list, unknown name)
# =============================================================================


@pytest.mark.parametrize("raw_body", ["[1, 2]", "not json at all"])
def test_probe_malformed_body_400(web_config_app, raw_body):
    """T007: a body that is not a JSON object -> 400
    ``{"error": "invalid JSON body"}`` (the 008 shape, pinned)."""
    _app, _db, host, port, admin_token, _rt, _cd = web_config_app
    code, parsed, raw = _http_post_raw(
        host, port, "/api/config/services/probe", raw_body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 400, (
        f"probe with body {raw_body!r} expected 400, got {code}: "
        f"{raw[:300]!r}"
    )
    assert parsed == {"error": "invalid JSON body"}, (
        f"malformed probe body must yield the pinned 008 400 shape, got "
        f"{parsed!r}"
    )


@pytest.mark.parametrize("bad", ["qdrant", [], 7])
def test_probe_services_must_be_non_empty_list(web_config_app, bad):
    """T007: ``services`` present but not a non-empty list -> 400 with the
    pinned error string (D4 refinement)."""
    _app, _db, host, port, admin_token, _rt, _cd = web_config_app
    code, parsed, raw = _http_post(
        host, port, "/api/config/services/probe", {"services": bad},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 400, (
        f"probe with services={bad!r} expected 400, got {code}: {raw[:300]!r}"
    )
    assert parsed == {"error": "services must be a non-empty list"}, (
        f"non-list/empty services must yield the pinned 400 shape, got "
        f"{parsed!r}"
    )


def test_probe_unknown_service_404(web_config_app):
    """T007: an unknown name inside ``services`` -> 404 with the service
    name quoted (the 008 unknown-service shape, pinned for the probe)."""
    _app, _db, host, port, admin_token, _rt, _cd = web_config_app
    code, parsed, raw = _http_post(
        host, port, "/api/config/services/probe", {"services": ["x"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 404, (
        f"probe with unknown service expected 404, got {code}: {raw[:300]!r}"
    )
    assert parsed == {"error": 'unknown service "x"'}, (
        f"unknown probe service must yield the pinned 404 shape, got "
        f"{parsed!r}"
    )


# =============================================================================
# T008 - SC-002 probe latency budget (parallel per-service deadline)
# =============================================================================


def test_probe_deadline_budget_sc002(web_config_app, monkeypatch):
    """T008 (SC-002): every check sleeps past the per-service deadline
    (``PROBE_PER_SERVICE_DEADLINE_S = 4.5``) -> the response still arrives
    within the 5.0 s budget with all four entries present, each
    ``status=="unreachable"`` carrying the timeout line.

    RED: the route does not exist yet; GREEN only with T010's parallel
    deadline (a sequential 4 x 6 s implementation would take > 24 s).
    """
    import digital_twins.health as health

    def _slow(cfg):
        time.sleep(6)
        raise AssertionError("check outlived the probe deadline")

    _app, _db, host, port, admin_token, _rt, _cd = web_config_app
    for name in ("check_qdrant", "check_neo4j", "check_llm",
                 "check_embedding"):
        monkeypatch.setattr(health, name, _slow)

    t0 = time.monotonic()
    code, parsed, raw = _http_post_raw(
        host, port, "/api/config/services/probe", "{}",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=10.0,
    )
    elapsed = time.monotonic() - t0

    assert code == 200, (
        f"probe expected 200 within the SC-002 budget, got {code} after "
        f"{elapsed:.2f}s: {raw[:300]!r}"
    )
    assert elapsed <= 5.0, (
        f"probe took {elapsed:.2f}s; SC-002 requires the full four-service "
        f"probe to return within 5.0 s"
    )
    services = parsed.get("services", {})
    assert list(services) == ["qdrant", "neo4j", "llm", "embedding"], (
        f"all four services must be present after a timeout, got "
        f"{list(services)!r}"
    )
    for name, entry in services.items():
        assert entry["status"] == "unreachable", (
            f"services.{name} must be unreachable on timeout, got "
            f"{entry!r}"
        )
        assert "timed out" in entry["detail"], (
            f"services.{name}.detail must carry the timeout line, got "
            f"{entry['detail']!r}"
        )
