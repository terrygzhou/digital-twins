"""dashboard-left-column T2: web dashboard left-column static contract.

The static UI assets are embedded/served by the Python web app (no
bundler), so this task's contract — like T002's services panel
contract (tests/integration/test_web_config_probe.py) and the
channels panel contract (tests/unit/test_web_channels_ui.py) — is a
static assertion test: it reads the shipped assets directly and pins
the left-column layout, the four panel ids (hidden by default, FR-D4),
the panel fetch endpoints, the admin-gate co-location in showKbView(),
the two-column CSS grid, and the FR-D3 no-credential-value discipline.

No socket / web-harness fixture: the test only reads files.
"""
from __future__ import annotations

import re
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parents[2] / "digital_twins" / "web" / "static"

_PANELS = (
    "dashboard-account-panel",
    "dashboard-channels-panel",
    "dashboard-jobs-panel",
    "dashboard-models-panel",
)


def _index_html() -> str:
    return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _style_css() -> str:
    return (_STATIC_DIR / "style.css").read_text(encoding="utf-8")


# --- 2.1 layout markup ---------------------------------------------------------


def test_dashboard_side_column_markup():
    """The #kb-view main grid carries a #dashboard-side left column that
    holds all four dashboard panel ids, each hidden by default (FR-D4)."""
    html = _index_html()
    assert 'id="dashboard-side"' in html, (
        "index.html must carry the dashboard left column "
        '(id="dashboard-side")'
    )
    for pid in _PANELS:
        assert f'id="{pid}"' in html, (
            f"index.html must carry the {pid!r} panel id"
        )
        assert f'id="{pid}" hidden' in html, (
            f"the {pid!r} panel must be hidden by default (FR-D4)"
        )
        # Each panel is a card section in the side column.
        assert re.search(
            r'<section class="card ' + re.escape(pid) + r'" id="' +
            re.escape(pid) + r'" hidden',
            html,
        ), (
            f"the {pid!r} panel must be a <section class=\"card {pid}\">"
        )


def test_dashboard_side_column_precedes_main_content():
    """The side column sits before the knowledge base panel (left column,
    reads first in DOM order)."""
    html = _index_html()
    assert html.find('id="dashboard-side"') < html.find('id="kb-panel"'), (
        "the dashboard side column must precede the knowledge base panel "
        "(left column reads first)"
    )


# --- 2.1 panel fetch endpoints -------------------------------------------------


def test_dashboard_panels_fetch_endpoints():
    """Each dashboard panel fetches its read-only overview endpoint via
    the shared authHeaders() helper (FR-D1 routes)."""
    html = _index_html()
    for endpoint in (
        "/api/dashboard/accounts",
        "/api/dashboard/jobs",
        "/api/dashboard/models",
        "/api/config/channels",
    ):
        assert f'fetch("{endpoint}"' in html, (
            f"index.html must fetch \"{endpoint}\" for the dashboard panels"
        )
    # The four loader functions are wired.
    for loader in (
        "loadDashboardAccounts",
        "loadDashboardJobs",
        "loadDashboardModels",
    ):
        assert f"function {loader}()" in html, (
            f"index.html must define the {loader}() loader"
        )


# --- 2.1 admin-gate co-location (FR-D4) ---------------------------------------


def test_dashboard_panels_admin_gate_in_showkbview():
    """The showKbView() block un-hides the four dashboard panels ONLY
    behind me.role === "admin" (SC-004 discipline, co-located with the
    services panel gate)."""
    html = _index_html()
    # The single admin branch that already gates services-panel must now
    # also un-hide the four dashboard panels.  The un-hide of each panel
    # sits AFTER the admin check opens.
    gate = 'if (me && me.role === "admin")'
    unhide_services = 'el("services-panel").hidden = false;'
    assert html.find(gate) < html.find(unhide_services), (
        "the services-panel un-hide must occur after the admin gate "
        "(SC-004, regression)"
    )
    for pid in _PANELS:
        unhide = f'el("{pid}").hidden = false;'
        assert unhide in html, (
            f"index.html must un-hide the {pid!r} panel in showKbView()"
        )
        assert html.find(gate) < html.find(unhide), (
            f"the {pid!r} un-hide must occur after the admin gate opens "
            "(SC-004 / FR-D4)"
        )


# --- 2.1 FR-D3: no credential values in the dashboard UI ----------------------


def test_dashboard_js_never_renders_credential_values():
    """FR-D3: the dashboard models panel renders the *_set booleans and
    effective endpoint/model values only — no credential value
    interpolation or pre-fill anywhere in the dashboard JS."""
    html = _index_html()
    # The masked-view booleans are the only credential surface.
    for flag in ("api_key_set", "user_set", "password_set"):
        assert flag in html, (
            f"index.html must render the {flag!r} boolean for the "
            "models panel"
        )
    # No credential value field access in the dashboard JS region.
    js = html.split("---- dashboard left column", 1)[1]
    assert not re.search(r"\w+\.(api_key|user|password)\b(?!_set)", js), (
        "the dashboard JS must not read a credential value field "
        "(only the *_set booleans are allowed)"
    )


# --- 2.2 CSS two-column grid ---------------------------------------------------


def test_dashboard_css_two_column_grid():
    """style.css turns #kb-view into a two-column grid (side + main) and
    the four dashboard panels carry side-panel rules."""
    css = _style_css()
    # The kb-view main grid becomes two columns (side + content).
    assert re.search(
        r'#kb-view\s*\{[^}]*grid-template-columns', css, re.DOTALL
    ), (
        "style.css must give #kb-view a two-column grid-template-columns"
    )
    # The side column is a grid area / track.
    assert ".dashboard-side" in css or "#dashboard-side" in css, (
        "style.css must carry a rule for the #dashboard-side column"
    )
    # Each dashboard panel gets a card rule in the side column.
    for pid in _PANELS:
        assert re.search(r"\." + re.escape(pid) + r"\b", css), (
            f"style.css must carry a rule for the .{pid} panel"
        )
