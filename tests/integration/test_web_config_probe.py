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

Harness: reuses the 008 fixture + http helpers from
``test_web_config_api`` (same directory; no ``tests/`` package, so a
plain module import).
"""
from __future__ import annotations

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
