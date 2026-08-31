"""011 — web UI service credential fields (write-only), RED-first.

Spec: ``specs/011-config-credentials/spec.md`` (supersedes 009 FR-010).
UI-only slice: the 008/009 API is unchanged (FR-004 / SC-003) — these
tests lock the panel's credential inputs (FR-001/FR-002), the
save-omission behavior (FR-003) via the live ``saveService`` JS, and the
credential write round-trip (US1 AC2/AC3/AC4) through the real API.

RED: the 0.9.0 panel has no credential inputs and ``saveService`` posts
the URL knob only.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from test_web_config_api import (
    LLM_KEY,
    NEO4J_PASSWORD,
    QDRANT_KEY,
    _http_get,
    _http_post,
    web_config_app,
)

# Fresh credential values the POST must carry (distinct from the fixture's
# pre-seeded values so a test that merely re-reads the seed cannot pass).
NEO4J_USER_NEW = "twin-admin"
NEO4J_PASSWORD_NEW = "sk-test-fake-011-neo4j"
LLM_KEY_NEW = "sk-test-fake-011-llm"
EMB_KEY_NEW = "sk-test-fake-011-embedding"

_INDEX_HTML = (
    Path(__file__).resolve().parents[2]
    / "digital_twins" / "web" / "static" / "index.html"
)


# --- JS evaluation helpers (Node is available on the host) -------------------


def _index_js():
    """The inline script text from the shipped index.html."""
    text = _INDEX_HTML.read_text(encoding="utf-8")
    start = text.index("<script>") + len("<script>")
    end = text.index("</script>", start)
    return text[start:end]


def _extract_function(js, name):
    """Extract ``function <name> ... { ... }`` by brace matching."""
    i = js.index("function " + name + "(")
    j = js.index("{", i)
    depth = 0
    for k in range(j, len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[i:k + 1]
    raise ValueError(f"unbalanced braces extracting {name!r}")


_NODE_PRELUDE = """
var document = {
  getElementById: function (id) {
    if (id === "services-error") {
      return { textContent: "" };
    }
    return null;
  }
};
function el(id) { return document.getElementById(id); }
function showError() {}
function authHeaders() { return {}; }
var SERVICE_KNOB = { qdrant: "url", neo4j: "url", llm: "endpoint", embedding: "endpoint" };
var SERVICE_CREDENTIALS = {
  qdrant: [{ knob: "api_key" }],
  neo4j: [{ knob: "user", plain: "plain" }, { knob: "password" }],
  llm: [{ knob: "api_key" }],
  embedding: [{ knob: "api_key" }]
};
function renderServices() {}
var __captures = [];
var fetch = function (url, opts) {
  __captures.push({ url: url, method: opts && opts.method, body: opts && opts.body });
  return Promise.resolve({
    json: function () {
      return Promise.resolve({ services: {}, error: "fake-error-for-path-test" });
    }
  });
};
"""


def _eval_save_service(url_value, cred_values):
    """Evaluate the shipped ``saveService`` in Node and return
    ``(post_body, post_url)`` for a save with the given URL + credential
    input values (``cred_values``: list of value strings in the same
    order the panel renders the service's credential inputs)."""
    if shutil.which("node") is None:
        pytest.skip("node not available for the JS-level save test")
    fn = _extract_function(_index_js(), "saveService")
    cred_fn = _extract_function(_index_js(), "saveCredValues")
    payload = json.dumps({"url": url_value, "creds": cred_values})
    program = (
        "var p = JSON.parse(process.argv[1]);\n"
        + _NODE_PRELUDE
        + "\n"
        + cred_fn
        + "\n"
        + fn
        + "\n"
        + "var urlInput = { value: p.url };\n"
        + "var credInputs = p.creds.map(function (v) { return { value: v }; });\n"
        + "saveService('neo4j', urlInput, credInputs);\n"
        + "setTimeout(function () {\n"
        + "  process.stdout.write(JSON.stringify({\n"
        + "    url: __captures[0].url,\n"
        + "    body: JSON.parse(__captures[0].body)\n"
        + "  }));\n"
        + "}, 10);\n"
    )
    out = subprocess.run(
        ["node", "--eval", program, payload],
        capture_output=True, text=True, check=True,
    )
    result = json.loads(out.stdout)
    return result["body"], result["url"]


# =============================================================================
# T1.1 — static contract: the panel carries write-only credential inputs
# =============================================================================


def test_index_html_credential_inputs_static_contract(web_config_app):
    """FR-001/FR-002/FR-005: index.html carries the credential-input
    infrastructure — the SERVICE_CREDENTIALS knob map, the service-cred
    input class, the password-type marker, the plain-text exception
    marker, and the save-omission helper call.

    RED: the 0.9.0 panel is URL-only.
    """
    _app, _db, host, port, _at, _rt, _cd = web_config_app
    code, _parsed, raw = _http_get(host, port, "/")
    assert code == 200, f"GET / expected 200, got {code}"
    html = raw.decode("utf-8")

    assert "SERVICE_CREDENTIALS" in html, (
        "index.html must define the SERVICE_CREDENTIALS service→knob map"
    )
    for knob in ("qdrant.api_key", "neo4j.user", "neo4j.password",
                 "llm.api_key", "embedding.api_key"):
        assert knob in html, (
            f"index.html must name credential knob {knob!r}"
        )
    assert "service-cred" in html, (
        "index.html must use the service-cred class for credential inputs"
    )
    assert 'type = "password"' in html, (
        "credential inputs must use type=\"password\" (FR-002)"
    )
    assert '"plain"' in html or "plain" in html, (
        "neo4j.user must be marked as the plain (non-password) exception"
    )
    assert "saveCredValues" in html, (
        "index.html must build the save body via a saveCredValues helper "
        "(FR-003 omission lives in one testable place)"
    )


# =============================================================================
# T1.2 — save behavior (live JS in Node): omission + never pre-filled
# =============================================================================


def test_save_service_omits_empty_credentials():
    """FR-003: a save with a blank credential input omits the credential
    knob from the POST body; the URL knob is still present.

    Exercises the REAL shipped saveService (extracted from index.html,
    evaluated in Node with a stubbed fetch).
    """
    body, url = _eval_save_service("bolt://host:7687", ["", ""])
    assert url == "/api/config/services"
    assert body == {"neo4j": {"url": "bolt://host:7687"}}, (
        f"blank credential inputs must be omitted entirely, got {body!r}"
    )


def test_save_service_includes_non_empty_credentials():
    """FR-003: non-empty credential inputs land in the POST body under
    the section's credential knob names, alongside the URL knob.
    """
    body, url = _eval_save_service(
        "bolt://host:7687", [NEO4J_USER_NEW, NEO4J_PASSWORD_NEW])
    assert url == "/api/config/services"
    assert body == {
        "neo4j": {
            "url": "bolt://host:7687",
            "user": NEO4J_USER_NEW,
            "password": NEO4J_PASSWORD_NEW,
        }
    }, f"non-empty credentials must be included, got {body!r}"


def test_save_service_whitespace_only_credentials_omitted():
    """FR-003: whitespace-only credential input = empty = omitted
    (the panel trims, mirroring the URL input's behavior)."""
    body, _url = _eval_save_service("bolt://host:7687", ["   ", "\t"])
    assert body == {"neo4j": {"url": "bolt://host:7687"}}, (
        f"whitespace-only credentials must be omitted, got {body!r}"
    )


def test_credential_inputs_have_visible_labels():
    """Owner fix (post-release): credential inputs must be visually
    identifiable — a visible placeholder naming the field
    (password / username / API key) with the blank-means-keep hint,
    not only an aria-label. renderCredInputs ships the literals."""
    js = _index_js()
    build = _extract_function(js, "renderCredInputs")
    assert "placeholder" in build, (
        "credential inputs need a visible placeholder (owner: the panel "
        "showed unlabeled fields)"
    )
    for label in ("password (blank = keep current)",
                  "username (blank = keep current)",
                  "API key (blank = keep current)"):
        assert label in build, (
            f"renderCredInputs must carry the visible label {label!r}"
        )


def test_credential_inputs_never_prefilled():
    """FR-002: credential inputs are created empty and are never
    assigned the GET response (which cannot carry values anyway):
    the shipped render code must not write a GET-sourced value into a
    credential input."""
    js = _index_js()
    build = _extract_function(js, "renderCredInputs")
    assert "value" in build and ".value = s[" not in build and \
        ".value = s." not in build, (
        "renderCredInputs must not pre-fill from the GET view"
    )
    assert "aria-label" in build, (
        "credential inputs need an aria-label (a11y, mirroring the URL input)"
    )


# =============================================================================
# T1.3 — API round-trip (US1 AC2/AC3/AC4): the write path + gating
# =============================================================================


def test_post_credentials_round_trip_sets_flags(web_config_app):
    """US1 AC2: POST the neo4j credential pair → 200 post-write view with
    user_set/password_set true and NO credential value in the response;
    kb.local.yml gains the new values.

    FR-004: the API half already works (008) — this pins 011's save
    body shape end-to-end (the body the 011 panel produces is exactly
    this: section → URL knob + credential knobs).
    """
    _app, _db, host, port, admin_token, _rt, config_dir = web_config_app
    code, parsed, raw = _http_post(
        host, port, "/api/config/services",
        {"neo4j": {"url": "bolt://neo4j:7687",
                    "user": NEO4J_USER_NEW,
                    "password": NEO4J_PASSWORD_NEW}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, f"expected 200, got {code}: {raw[:300]!r}"
    svc = parsed["services"]["neo4j"]
    assert svc["user_set"] is True
    assert svc["password_set"] is True
    assert NEO4J_USER_NEW not in raw.decode("utf-8")
    assert NEO4J_PASSWORD_NEW not in raw.decode("utf-8")

    saved = yaml.safe_load(
        (Path(config_dir) / "kb.local.yml").read_text(encoding="utf-8"))
    assert saved["neo4j"]["user"] == NEO4J_USER_NEW
    assert saved["neo4j"]["password"] == NEO4J_PASSWORD_NEW


def test_post_url_only_preserves_saved_credentials(web_config_app):
    """US1 AC3: a save that omits credential knobs (blank inputs on the
    panel) leaves the saved credentials untouched — overwrite-only."""
    _app, _db, host, port, admin_token, _rt, config_dir = web_config_app
    code, parsed, raw = _http_post(
        host, port, "/api/config/services",
        {"neo4j": {"url": "bolt://new-host:7687"}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert code == 200, f"expected 200, got {code}: {raw[:300]!r}"
    svc = parsed["services"]["neo4j"]
    assert svc["url"] == "bolt://new-host:7687"
    assert svc["user_set"] is True      # pre-seeded "neo4j" survives
    assert svc["password_set"] is True  # pre-seeded password survives

    saved = yaml.safe_load(
        (Path(config_dir) / "kb.local.yml").read_text(encoding="utf-8"))
    assert saved["neo4j"]["password"] == NEO4J_PASSWORD
    assert saved["neo4j"]["user"] == "neo4j"


def test_post_credentials_as_reader_forbidden(web_config_app):
    """US1 AC4: admin gating is unchanged — a reader cannot POST
    credential writes."""
    _app, _db, host, port, _at, reader_token, _cd = web_config_app
    code, parsed, raw = _http_post(
        host, port, "/api/config/services",
        {"llm": {"api_key": LLM_KEY_NEW}},
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert code == 403, f"expected 403, got {code}: {raw[:300]!r}"
    assert LLM_KEY_NEW not in raw.decode("utf-8")
