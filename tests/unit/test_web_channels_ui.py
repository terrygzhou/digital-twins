"""channels-config T4: web admin UI channels panel static contract.

The static UI assets are embedded/served by the Python web app (no
bundler), so this task's contract — like T002's services panel
contract (tests/integration/test_web_config_probe.py) and T008's
``GET /`` contract (tests/unit/test_web_app_kb.py) — is a static
assertion test: it reads the shipped assets directly and pins the
panel markup, the admin-gate wiring, the T3 endpoint fetch, and the
FR-004 discipline (no credential value ever reaches the UI).

No socket / web-harness fixture: the test only reads files.
"""
from __future__ import annotations

import re
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parents[2] / "digital_twins" / "web" / "static"


def _index_html() -> str:
    return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _style_css() -> str:
    return (_STATIC_DIR / "style.css").read_text(encoding="utf-8")


# --- 4.1 panel markup --------------------------------------------------------


def test_channels_panel_markup_ids():
    """The panel section ships with the brief's static ids: the hidden
    section, the row container, the refresh button, and the error line."""
    html = _index_html()
    assert 'id="channels-panel" hidden' in html, (
        'index.html must contain a section with id="channels-panel" '
        "and the hidden attribute (hidden by default, SC-004)"
    )
    assert "channels-rows" in html, (
        "index.html must contain the channel row container (channels-rows)"
    )
    assert "channels-refresh-btn" in html, (
        "index.html must carry the channel Refresh button (channels-refresh-btn)"
    )
    assert "channels-error" in html, (
        "index.html must carry the panel error line (channels-error)"
    )
    assert 'class="card channels-panel"' in html, (
        "the panel section must carry the channels-panel card class"
    )
    # The hint pins the kb.local.yml write target + the KB_SOURCES__
    # env-shadowing rule.
    assert "kb.local.yml" in html
    assert "KB_SOURCES__" in html


def test_channels_style_css_rows_rule():
    """.channels-rows mirrors .services-rows in style.css."""
    css = _style_css()
    assert re.search(r"\.channels-panel\s+\.channels-rows\s*\{", css), (
        "style.css must carry a .channels-rows rule mirroring .services-rows"
    )


# --- 4.2 admin-gate wiring ----------------------------------------------------


def test_channels_panel_admin_gate_co_located_with_services():
    """The showKbView() block that gates the services panel gates the
    channels panel in the same place: un-hidden ONLY behind
    me.role === "admin" (SC-004 discipline, T013-style static gate)."""
    html = _index_html()

    # Both panels must be gated on me.role === "admin" within showKbView().
    # The services panel is gated first; the channels panel gate is
    # co-located immediately after (with optional comment lines between).
    # We verify each panel's admin-gate independently, then confirm
    # co-location by checking that the channels gate appears after the
    # services gate within the same showKbView() function body.
    show_kb = re.search(
        r'function\s+showKbView\(\)\s*\{(.*)', html, re.DOTALL
    )
    assert show_kb is not None, "showKbView() must exist in index.html"
    kb_body = show_kb.group(1)

    # Services panel admin gate
    svc = re.search(
        r'if\s*\(\s*me\s*&&\s*me\.role\s*===\s*"admin"\s*\)\s*\{\s*'
        r'el\("services-panel"\)\.hidden\s*=\s*false;\s*'
        r'loadServices\(\);',
        kb_body,
    )
    assert svc is not None, (
        'index.html must gate services-panel on me.role === "admin" '
        "in showKbView()"
    )

    # Channels panel admin gate (co-located: appears after services gate)
    ch = re.search(
        r'el\("channels-panel"\)\.hidden\s*=\s*false;\s*loadChannels\(\);',
        kb_body[svc.end():],
    )
    assert ch is not None, (
        'index.html must gate channels-panel on me.role === "admin", '
        "co-located with the services-panel gate in showKbView()"
    )

    # The channels gate must also have the admin check immediately before it
    ch_gate = re.search(
        r'if\s*\(\s*me\s*&&\s*me\.role\s*===\s*"admin"\s*\)\s*\{\s*'
        r'el\("channels-panel"\)\.hidden\s*=\s*false;\s*'
        r'loadChannels\(\);',
        kb_body[svc.end():],
    )
    assert ch_gate is not None, (
        'the channels-panel un-hide must sit inside an '
        'if (me && me.role === "admin") block co-located with the '
        "services gate"
    )

# --- 4.2 fetch / save patterns ------------------------------------------------


def test_channels_js_fetches_t3_endpoint():
    """The panel JS fetches the T3 endpoint with the shared authHeaders()
    helper, and the refresh button is wired to the loader."""
    html = _index_html()
    assert 'fetch("/api/config/channels"' in html, (
        'index.html must fetch "/api/config/channels"'
    )
    assert re.search(
        r'fetch\("/api/config/channels",\s*\{\s*method:\s*"POST"', html
    ), "index.html must POST to /api/config/channels for the per-row Save"
    assert 'el("channels-refresh-btn").addEventListener("click", loadChannels)' in html, (
        "the Refresh button must re-fetch the panel (loadChannels)"
    )
    # Per-source row id scheme mirrors the services-…-<name> scheme.
    assert re.search(r'id\s*=\s*"channels-row-"', html)
    for suffix in ("enabled-", "max-items-", "timeout-s-"):
        assert re.search(
            r'id\s*=\s*"channels-' + re.escape(suffix) + '"', html
        ), f"index.html must carry a channels-{suffix}<{name}> input id"
    # Per-row Save button marker (mirrors services-save-btn's contract).
    assert "channels-save-btn" in html, (
        "index.html must wire a per-row Save button (channels-save-btn)"
    )


def test_channels_js_error_handling_shape():
    """POST failures surface in channels-error via the shared showError
    helper without re-rendering; a 200 view re-renders the rows."""
    html = _index_html()
    assert html.count('showError("channels-error"') >= 3, (
        "the channels JS must route GET failures, POST failures "
        "(404/422/409/500), and network errors through "
        "showError(\"channels-error\")"
    )
    assert "renderChannels(data)" in html, (
        "a 200 POST body (the post-write view) must re-render the panel"
    )
    # The env-override list renders once, above the rows.
    assert "env vars shadowing saved values:" in html, (
        "env_overrides must render as a one-line list above the rows"
    )
    assert "prerequisites:" in html and "ready" in html, (
        "each row must show a prerequisites pill (names joined) or "
        '"ready" when prerequisites is empty'
    )


# --- FR-004: no credential values in the UI ------------------------------------


def test_channels_js_never_renders_credential_values():
    """FR-004: the panel renders the credential_set boolean only — no
    credential value input, interpolation, or pre-fill anywhere in the
    channels JS or panel markup."""
    html = _index_html()

    # The masked-view boolean is the only credential surface.
    assert "credential_set" in html
    assert "credential configured" in html

    # No value interpolation of credential fields: the only credential
    # token a source row could carry is the `credential_set` key.  A
    # `s.credential` / `row.credential` style value access would leak
    # the env var name or value into the UI — it must not exist.
    assert not re.search(r"\w+\.credential\b(?!_set)", html), (
        "index.html must not read a credential value field "
        "(only the credential_set boolean is allowed)"
    )
    # No credential value input in the channels panel markup (the
    # services panel's write-only inputs are a different section; the
    # channels markup region carries no inputs at all — rows are
    # JS-rendered).
    panel = html.split('id="channels-panel"', 1)[1].split("</section>", 1)[0]
    assert "input" not in panel, (
        "the channels panel markup must not carry static inputs — "
        "credential values are never part of this surface"
    )
    # The channels JS region: no pre-filled value from the view (the
    # row values it sets are the three OVERRIDABLE knobs only).
    js = html.split("---- channels panel", 1)[1]
    assert not re.search(r"value\s*=\s*s\.credential", js), (
        "the channels JS must not pre-fill any credential value"
    )


# --- regression: services panel contract intact -------------------------------


def test_services_panel_block_still_present():
    """Adding the channels panel must not disturb the T002 services
    panel static contract (admin-gate co-location means both panels
    live in the same showKbView() block)."""
    html = _index_html()
    assert 'id="services-panel" hidden' in html
    assert "services-rows" in html
    assert "services-error" in html
    assert "services-save-btn" in html
    assert '/api/config/services' in html
    assert 'me.role === "admin"' in html
    # Both panels coexist behind the same gate.
    assert 'id="channels-panel" hidden' in html
