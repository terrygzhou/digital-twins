# 011 — Web UI Service Credential Fields (write-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The web admin Services panel can write service credentials (`qdrant.api_key`, `neo4j.user`, `neo4j.password`, `llm.api_key`, `embedding.api_key`) — write-only, never pre-filled, empty-on-save omits — closing the BR-12.2.1 loop 008 (API) and 009 (URL panel) left open.

**Architecture:** UI-only delta in `digital_twins/web/static/index.html` inline JS (+ the panel's CSS in `digital_twins/web/static/style.css`): a `SERVICE_CREDENTIALS` map (service → credential knob list), a `renderCredInputs(service, row)` helper that builds empty write-only inputs (`type="password"` except `neo4j.user`), and `saveService(service, input, inputs)` that folds non-empty credential values into the existing `POST /api/config/services` body. The API (`web/app.py`), config schema, and persistence (`merge_write` → `kb.local.yml`) are untouched (FR-004).

**Tech Stack:** Python 3.12, stdlib `http.server` web app, plain JS (no framework) inline in `index.html`; pytest + `http.client` for API round-trips; Node.js (`node --eval`, host has v22) to evaluate the extracted `saveService` function for JS-level behavior (omission on save, never pre-filled).

**Spec:** `specs/011-config-credentials/spec.md` (authority; supersedes 009 FR-010 — the supersession note already lives in `specs/009-admin-config-panel/spec.md`).

## Global Constraints

(verbatim requirements from the 011 spec — every task implicitly includes these)

- **FR-001**: The panel MUST render one write-only input per credential knob: `qdrant.api_key`, `neo4j.user`, `neo4j.password`, `llm.api_key`, `embedding.api_key` (neo4j's row carries both; the other three carry `api_key` only — no input may appear for a knob the service does not have).
- **FR-002**: Credential inputs MUST NOT be pre-filled from the GET response (which cannot carry values anyway) and MUST use `type="password"` — except `neo4j.user`, which is not a secret and uses a plain text input.
- **FR-003**: Save MUST include only non-empty credential inputs in the `POST /api/config/services` body; empty inputs are omitted (overwrite-only; no clear semantics).
- **FR-004**: No API changes: `GET /api/config/services`, `POST /api/config/services`, and `POST /api/config/services/probe` keep their 008/009 contracts, error shapes, and admin gating verbatim. No new config knobs; persistence remains `merge_write` → `kb.local.yml`.
- **FR-005**: Supersedes 009 FR-010: the panel is no longer URL-only; the credential fields above are editable in the panel, write-only.
- **SC-002**: Zero credential values in any web response body or server log across the changed surface (verified by extending the existing secret-hygiene tests — the new round-trip tests assert the fake values never appear in any response `raw` or in server logs).
- **SC-003**: The `GET /api/config/services` response shape is byte-identical to 0.9.0 — existing 008/009 contract tests stay green unchanged (this slice must not touch `web/app.py`).
- NFR-13: no host path, username, or install location in shipped code (`index.html` stays generic).
- Standing guards must stay green: `tests/integration/test_portability.py` (T006), `tests/unit/test_knob_docs.py` (T027 — vacuous here: no new knobs).
- Test-first (constitution III): Task 1 lands the failing tests BEFORE any `index.html` change.

---

## Context (zero-knowledge engineer's orientation)

- `digital_twins/web/static/index.html` is the entire web UI: one inline `<script>` (vanilla JS, `"use strict"`, `var`-style, no ES modules — deliberately evaluable in Node). The Services panel lives in `renderServices(view)` (~line 405) and `saveService(service, input)` (~line 497); the static panel skeleton is the `<section ... id="services-panel" hidden>` at ~line 97.
- `view` is the masked effective view from `GET /api/config/services`: `{services: {qdrant: {url, api_key_set}, neo4j: {url, user_set, password_set}, llm: {url, endpoint→url, api_key_set}, embedding: {url, api_key_set}}, env_overrides: [KB_...]}`. **It can never contain credential values** — only `*_set` booleans.
- `SERVICE_KNOB` (line ~392) maps service → URL knob: `qdrant/neo4j → "url"`, `llm/embedding → "endpoint"`. `saveService` builds `body[service][SERVICE_KNOB[service]] = urlValue` and POSTs it. On 200 the response is the post-write masked view and the panel re-renders via `renderServices(data)`.
- Existing 009 static-contract tests live in `tests/integration/test_web_config_probe.py` (`_get_index_html` helper + marker assertions on the served HTML). 008 API tests live in `tests/integration/test_web_config_api.py` — reuse its `web_config_app` fixture (yields `(app, db, host, port, admin_token, reader_token, config_dir)`; pre-seeds `kb.local.yml` with fake credentials `QDRANT_KEY`/`NEO4J_PASSWORD`/`LLM_KEY`) and `_http_get`/`_http_post`.
- Tests are run from the repo root with `.venv/bin/python -m pytest`. **HTTP/socket tests must run escalated** (sandbox blocks sockets).
- `renderServices` appends to each row, in order: name span, URL input (`service-url`), badges span (`service-badges`), status pill, Save button (→ `saveService(service, input)`), Test button, remediation `<p>`. Credential inputs go between the URL input and the badges.

---

## Task 1 — RED: 011 credential-UI tests (fail on the 0.9.0 panel)

**Files:**
- Create: `tests/integration/test_web_config_credentials_ui.py`

**Step 1.1 — Write the test file** with exactly this content:

```python
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
function renderServices() {}
var SERVICE_CREDENTIALS = {
  qdrant: [{ knob: "api_key" }],
  neo4j: [{ knob: "user", plain: "plain" }, { knob: "password" }],
  llm: [{ knob: "api_key" }],
  embedding: [{ knob: "api_key" }]
};
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
```

**Step 1.2 — Run the new tests; confirm they fail for the right reasons:**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest \
  tests/integration/test_web_config_credentials_ui.py -v
```

Expected RED shape (run escalated — sockets): `test_index_html_credential_inputs_static_contract` and `test_credential_inputs_never_prefilled` fail (markers/function absent); `test_save_service_includes_non_empty_credentials` fails on the body assert (the 0.9.0 two-parameter `saveService` ignores credential inputs); `test_save_service_omits_empty_credentials` + `test_save_service_whitespace_only_credentials_omitted` PASS at RED (trivially true with a URL-only body) and become locks; the three API round-trip tests already PASS (the API half shipped in 008) — expected and correct: they pin 011's save-body shape end-to-end and must stay green both RED and GREEN. Observed RED: 3 failed, 5 passed.

- [ ] Step 1.1 done
- [ ] Step 1.2 RED confirmed (record which tests fail and why)

**Step 1.3 — Commit:**

```bash
git add tests/integration/test_web_config_credentials_ui.py
git commit -m "test(011): T1 RED — credential-UI static + Node save + round-trip tests"
```

- [ ] Step 1.3 committed

---

## Task 2 — GREEN: index.html credential inputs + save-omission

**Files:**
- Modify: `digital_twins/web/static/index.html` (panel JS) and
  `digital_twins/web/static/style.css` (panel CSS) — ONLY these two
  files in this slice (FR-004: `web/app.py` is NOT touched)

**Step 2.1 — Add the credential map + build helper.** Immediately after the existing `SERVICE_KNOB` definition (~line 397), insert:

```js
    // 011: write-only credential inputs (supersedes 009 FR-010's
    // URL-only v1).  One input per credential knob a service has
    // (FR-001): qdrant.api_key, neo4j.user, neo4j.password,
    // llm.api_key, embedding.api_key.  Every one is type="password"
    // except neo4j.user ("plain" — not a secret, FR-002).  Inputs are
    // ALWAYS created empty: the GET view carries only *_set booleans,
    // never values.
    var SERVICE_CREDENTIALS = {
      qdrant: [{ knob: "api_key" }],
      neo4j: [{ knob: "user", plain: "plain" },
              { knob: "password" }],
      llm: [{ knob: "api_key" }],
      embedding: [{ knob: "api_key" }]
    };

    function renderCredInputs(service, row) {
      // Returns the credential input elements in render order — the
      // same order saveService receives them (saveCredValues maps by
      // index against SERVICE_CREDENTIALS[service]).
      var inputs = [];
      (SERVICE_CREDENTIALS[service] || []).forEach(function (spec) {
        var inp = document.createElement("input");
        inp.className = "service-cred";
        // "plain" (neo4j.user) is the one non-password credential.
        inp.type = "password";
        if (spec.plain === "plain") {
          inp.type = "text";
        }
        inp.value = "";  // write-only: never pre-filled (FR-002)
        inp.setAttribute("aria-label",
          service + "." + spec.knob + " (leave blank to keep current)");
        row.appendChild(inp);
        inputs.push(inp);
      });
      return inputs;
    }

    function saveCredValues(service, inputs) {
      // FR-003: only NON-EMPTY (trimmed) credential inputs join the
      // save body; blank inputs omit the knob entirely (overwrite-only,
      // no clear semantics).
      var section = {};
      (SERVICE_CREDENTIALS[service] || []).forEach(function (spec, i) {
        var value = inputs[i] ? String(inputs[i].value).trim() : "";
        if (value !== "") section[spec.knob] = value;
      });
      return section;
    }
```

**Step 2.2 — Render the credential inputs per row.** In `renderServices` (the URL input ~line 419), wrap the URL input and the credential inputs in ONE flex cell so the row's 6-column grid (`.service-row` in `style.css`) holds regardless of credential count (neo4j has two). Replace the URL-input block + `row.appendChild(input);`:

```js
        // 011: the URL input and the write-only credential inputs
        // share one grid cell (flex-wrapped) — rows keep their
        // 6-column layout no matter how many credentials a service
        // has (neo4j has two).
        var urlCell = document.createElement("div");
        urlCell.className = "service-url-cell";

        var input = document.createElement("input");
        input.type = "text";
        input.className = "service-url";
        input.value = s.url || "";
        input.setAttribute("aria-label", service + " endpoint");
        urlCell.appendChild(input);

        // 011: write-only credential inputs (order fixed by
        // SERVICE_CREDENTIALS; blank on save keeps the current value).
        var credInputs = renderCredInputs(service, urlCell);
        row.appendChild(urlCell);
```

**Step 2.3 — Change the Save wiring + saveService.** Replace the Save button's listener:

```js
        save.addEventListener("click", function () {
          saveService(service, input, credInputs);
        });
```

and replace the whole `saveService` function with:

```js
    function saveService(service, input, credInputs) {
      var value = input.value.trim();
      showError("services-error", null);
      var section = {};
      section[SERVICE_KNOB[service]] = value;
      // 011 FR-003: fold in the non-empty credential inputs only.
      var creds = saveCredValues(service, credInputs || []);
      Object.keys(creds).forEach(function (k) { section[k] = creds[k]; });
      var body = {};
      body[service] = section;
      fetch("/api/config/services", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(body)
      }).then(function (r) {
        return r.json().catch(function () { return null; });
      }).then(function (data) {
        if (!data || data.error) {
          // Keep the input value on failure (FR-008: no lost edits).
          showError("services-error", data);
          return;
        }
        // The 200 body is the post-write masked view (FR-002).
        renderServices(data);
      });
    }
```

**Step 2.4 — Style the credential inputs.** In `digital_twins/web/static/style.css` (there is no inline `<style>` block — the panel CSS lives there), after the `.service-row .service-url` rule add:

```css
/* 011: URL + write-only credential inputs share one flex cell so the
   row's 6-column grid holds no matter how many credentials a service
   has (neo4j has two). */
.service-row .service-url-cell {
  display: flex;
  gap: 0.5rem;
  min-width: 0;
  flex-wrap: wrap;
}

.service-row .service-cred {
  font-family: ui-monospace, monospace;
  font-size: 0.85rem;
  width: 14rem;
}
```

**Step 2.5 — Update the panel hint.** The section's hint line (line ~100) reads "Service endpoints (admin only). …" — extend it so the owner sees the credential behavior:

```html
        Service endpoints and credentials (admin only). Save writes to
        <code>kb.local.yml</code>; credential fields are write-only
        (leave a field blank to keep its current value). An active
        <code>KB_*</code> environment variable overrides the saved
        value until it is unset.
```

**Step 2.6 — Run the 011 tests; confirm GREEN:**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest \
  tests/integration/test_web_config_credentials_ui.py -v
```

All 8 tests must pass (run escalated — sockets).

- [ ] Steps 2.1–2.5 applied
- [ ] Step 2.6 GREEN confirmed (record the 8/8 output)

**Step 2.7 — Run the 009/008 regressions** (same files, must stay green — SC-003):

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest \
  tests/integration/test_web_config_probe.py \
  tests/integration/test_web_config_api.py -v
```

- [ ] Step 2.7 regressions green (record the output)

**Step 2.8 — Commit:**

```bash
git add digital_twins/web/static/index.html digital_twins/web/static/style.css tests/integration/test_web_config_credentials_ui.py
git commit -m "feat(011): T2 GREEN — write-only credential inputs in the Services panel"
```

- [ ] Step 2.8 committed

---

## Task 3 — Verify: guards, full suite, live redeploy for owner

**Files:** none (verification only; `tasks.md` checkboxes stay in sync per AGENTS.md).

**Step 3.1 — Standing guards:**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest \
  tests/integration/test_portability.py tests/unit/test_knob_docs.py -v
```

Both green (portability: index.html gains no host paths; knob docs: no new knobs).

- [ ] Step 3.1 guards green

**Step 3.2 — Full suite (expect 968 + 8 = 976):**

```bash
cd /home/terry/projects/digital-twins && .venv/bin/python -m pytest -q
```

- [ ] Step 3.2 full suite green (record the count)

**Step 3.3 — Live redeploy for owner testing** (the local :8767 server must serve the new panel):

```bash
pkill -f "dtkb-pypi/bin/digital-twins w[e]b" || true
sleep 1
cd /home/terry/projects/digital-twins && /tmp/dtkb-pypi/bin/pip install . --no-deps -q
KB_CONFIG_DIR=/tmp/dtkb-deploy/config KB_STATE_DIR=/tmp/dtkb-deploy/state \
  KB_WEB__BIND=0.0.0.0 setsid nohup /tmp/dtkb-pypi/bin/digital-twins web \
  >> /tmp/dtkb-deploy/server.log 2>&1 </dev/null &
sleep 2
curl -s http://127.0.0.1:8767/api/v1/health || curl -s http://127.0.0.1:8767/health
curl -s http://127.0.0.1:8767/ | grep -c "SERVICE_CREDENTIALS"
```

The last command must print `1` (the new panel is live). NEVER kill the process holding the ~30GB GPU (the embedding model) — only the `digital-twins web` process.

- [ ] Step 3.3 redeployed; live index.html carries SERVICE_CREDENTIALS

**Step 3.4 — Owner re-test instructions (surface in the final report):** sign in as `terry.zhou@ymail.com` / `terry` at `http://pop-os:8767/`, open the Services panel, fill `neo4j.user` + `neo4j.password` (and/or `qdrant.api_key` / `llm.api_key` / `embedding.api_key`), Save, then Test — neo4j should flip from `auth-failed` toward `ok` (SC-001).

- [ ] Step 3.4 surfaced
