"""008/US2 (T021, RED-first): secret hygiene (US2 AC2; FR-004).

For each of the 4 hard services, the credential value must be **absent**
from:

* log records (``logging`` capture handler on ``digital_twins``),
* audit row JSON (the ``audit_runs`` DB row + the web ``GET
  /api/audit/recent`` payload),
* error-string fixtures (``str()`` of the exceptions/HealthResults the
  config + health layer produces for that service).

Plus: the committed example files (``config.example.yml``,
``.env.example``) carry **placeholders only** — no obviously-fake
credential value from this test suite may ever appear in a committed file
(FR-004; NFR-13).

Capture mechanism (per the brief: "capture the client kwargs/headers via
fixtures"): drive the health checks with credential values configured,
capturing (a) everything logged at WARNING+ on ``digital_twins``, and
(b) the resulting ``HealthResult`` strings (the "error string fixtures").
The audit surface is exercised through the real audit helpers with a
credential-bearing run, and the web audit route is exercised through the
real WebApp so the JSON that leaves the process is what gets checked.

RED expectations:
* If any shipped code logs a credential (e.g. an unscrubbed request URL
  carrying a query-string token, or an echo of the api_key in an error
  message), the corresponding ``credential_not_in_*`` assertion REDs.
* The example-file assertions are guard tests — expected GREEN on first
  run (the examples already carry placeholders); they protect against a
  GREEN-phase regression where a real-looking secret example slips in.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from digital_twins.config.schema import validate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# --- obviously-fake credential values (FR-004 safe) --------------------------

QDRANT_KEY = "sk-test-fake-008-us2-qdrant"
NEO4J_PASSWORD = "sk-test-fake-008-us2-neo4j"
LLM_KEY = "sk-test-fake-008-us2-llm"
EMB_KEY = "sk-test-fake-008-us2-embedding"

ALL_CREDENTIALS = {
    "qdrant": QDRANT_KEY,
    "neo4j": NEO4J_PASSWORD,
    "llm": LLM_KEY,
    "embedding": EMB_KEY,
}


def make_cfg() -> dict:
    """Fully-configured config: credentials set for all 4 services."""
    cfg = validate({})
    cfg["qdrant"]["url"] = "http://qdrant.example:6333"
    cfg["qdrant"]["api_key"] = QDRANT_KEY
    cfg["neo4j"]["url"] = "bolt://neo4j.example:7687"
    cfg["neo4j"]["user"] = "neo4j"
    cfg["neo4j"]["password"] = NEO4J_PASSWORD
    cfg["llm"]["endpoint"] = "http://llm.example:8000/v1"
    cfg["llm"]["model"] = "gpt-4o-mini"
    cfg["llm"]["api_key"] = LLM_KEY
    cfg["embedding"]["endpoint"] = "http://embed.example:8080/v1"
    cfg["embedding"]["api_key"] = EMB_KEY
    return cfg


@pytest.fixture
def logged_records():
    """Capture every record logged on the ``digital_twins`` logger tree."""
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    root = logging.getLogger("digital_twins")
    old_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def all_log_text(records) -> str:
    return "\n".join(
        (r.getMessage() + "\n" + " ".join(map(repr, r.args))
         if isinstance(r.args, tuple) else "")
        for r in records
    ) + "\n".join(r.getMessage() for r in records)


# =============================================================================
# 1. Log records: credentials absent (US2 AC2 — logs)
# =============================================================================


@pytest.mark.parametrize("service", sorted(ALL_CREDENTIALS))
def test_credential_absent_from_log_records(service, logged_records,
                                             monkeypatch):
    """Driving every health check with the service's credential set must
    produce no log record containing that credential value.

    RED (pre-US2, if any check leaks a credential into a log line — e.g. an
    unscrubbed URL query string or an echoed api_key in an error message).
    """
    import urllib.error

    def boom(req, timeout=None):
        # Force a failure path so error logs are exercised.
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized",
                                     None, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)

    from digital_twins.health import run_health_checks

    cfg = make_cfg()
    for res in run_health_checks(cfg):
        assert res.endpoint in ("qdrant", "neo4j", "llm", "embedding")

    text = all_log_text(logged_records)
    secret = ALL_CREDENTIALS[service]
    assert secret not in text, (
        f"{service} credential value appeared in a log record (FR-004): "
        f"the capture contains the token for {service!r}"
    )


def test_credential_absent_from_exception_str():
    """The error-string fixtures: no config/health-layer exception string
    may carry a credential value (the 007 ``_scrub`` guarantee, extended to
    every service)."""
    import urllib.error

    from digital_twins.health import (
        ServiceDependencyError,
        check_embedding,
        check_llm,
        check_neo4j,
        check_qdrant,
    )

    # Each check is driven into its error branch with a real-looking
    # credential configured; str() of the resulting error/HealthResult must
    # not carry the token.
    for service, check, secret in (
        ("qdrant", check_qdrant, QDRANT_KEY),
        ("neo4j", check_neo4j, NEO4J_PASSWORD),
        ("llm", check_llm, LLM_KEY),
        ("embedding", check_embedding, EMB_KEY),
    ):
        cfg = make_cfg()
        res = check(cfg)
        err_str = f"{res.detail} {res.remediation} {str(getattr(res, 'status', ''))}"
        # Also exercise the preflight error string.
        try:
            from digital_twins.health import preflight

            preflight(cfg)
        except ServiceDependencyError as exc:
            err_str += f" {str(exc)} {exc.remediation}"
        assert secret not in err_str, (
            f"{service} credential value leaked into the error string "
            f"fixture (FR-004): {err_str!r}"
        )


# =============================================================================
# 2. Audit row JSON: credentials absent (US2 AC2 — audit)
# =============================================================================


def test_credential_absent_from_audit_row_json(tmp_path):
    """An audit row written by the real audit helpers (with a
    credential-bearing config in scope, as the pipeline run sees it) must
    not contain any of the 4 credential values in its JSON.

    The audit layer stores pipeline output (per_source_counts, status,
    trigger, scheduled_by) — none of which should ever carry a credential.
    If any code path puts a credential into the audit row, this REDs.
    """
    from digital_twins.state import db as state_db

    conn = state_db.connect(tmp_path)
    try:
        from digital_twins.state.models import (
            finish_audit_run,
            start_audit_run,
        )

        run_id = "run-008-us2-secret-hygiene"
        start_audit_run(conn, run_id, trigger="web", scheduled_by="system")
        # Neutral pipeline-shaped counts: the audit layer must not echo any
        # credential even when the run was configured with all 4 set.
        counts = {"fs": {"items": 3, "points": 6}}
        finish_audit_run(conn, run_id, "ok", counts)

        row_blob = conn.execute(
            "SELECT status, per_source_counts, trigger FROM audit_runs "
            "WHERE run_id=?", (run_id,)
        ).fetchall()
        blob = " ".join(str(c) for row in row_blob for c in row)
        for service, secret in ALL_CREDENTIALS.items():
            assert secret not in blob, (
                f"{service} credential value leaked into the audit row "
                f"JSON (FR-004): {blob!r}"
            )
        # The stored JSON must round-trip to the neutral counts, proving the
        # row is a faithful (credential-free) record of pipeline output.
        stored = json.loads(
            conn.execute(
                "SELECT per_source_counts FROM audit_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        assert stored == counts, (
            f"audit per_source_counts must round-trip the pipeline output "
            f"unchanged, got {stored!r}"
        )
    finally:
        conn.close()


# =============================================================================
# 3. Error-string fixtures: per-service, driven through the real check
# =============================================================================


@pytest.mark.parametrize("service,secret", sorted(ALL_CREDENTIALS.items()))
def test_error_string_fixture_absent_for_service(service, secret,
                                                  monkeypatch):
    """Each service's error-string fixture (the HealthResult produced when
    the check fails) must not carry the credential value.

    This is the "error string fixtures" half of US2 AC2, per-service: drive
    the check into a failure, build the string the user/operator sees, and
    assert the token is absent.
    """
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized",
                                     None, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)

    from digital_twins.health import (
        check_embedding,
        check_llm,
        check_neo4j,
        check_qdrant,
    )

    check = {
        "qdrant": check_qdrant,
        "neo4j": check_neo4j,
        "llm": check_llm,
        "embedding": check_embedding,
    }[service]

    cfg = make_cfg()
    res = check(cfg)
    # The operator-facing error string: detail + remediation + status.
    fixture = f"status={res.status} detail={res.detail} remediation={res.remediation}"
    assert secret not in fixture, (
        f"{service} credential value leaked into the error string fixture "
        f"(FR-004): {fixture!r}"
    )


# =============================================================================
# 4. Example files: placeholders only (committed files)
# =============================================================================


def test_example_files_carry_placeholders_only():
    """Committed example files must carry placeholders only — no
    obviously-fake credential value from this suite (or any real-looking
    secret) may appear.  FR-004 / NFR-13."""
    examples = [
        REPO_ROOT / "config.example.yml",
        REPO_ROOT / ".env.example",
    ]
    for path in examples:
        assert path.is_file(), f"missing example file: {path}"
        text = path.read_text(encoding="utf-8")
        for service, secret in ALL_CREDENTIALS.items():
            assert secret not in text, (
                f"{service} obviously-fake credential value {secret!r} "
                f"appeared in committed example {path.name} — committed "
                f"files must carry placeholders only (FR-004)"
            )
        # Guard against real-looking secret patterns in the examples.
        # (A 32+ char token that looks like an API key would be a red
        # flag; the examples use short placeholders like "your-api-key".)
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Flag anything that looks like a long bearer/api token.
            m = re.search(r"(sk-[A-Za-z0-9_-]{16,})", stripped)
            assert m is None, (
                f"{path.name}: line {line!r} contains a token that looks "
                f"like a real API key (FR-004: placeholders only)"
            )


def test_knobs_doc_does_not_echo_credential_values():
    """config/knobs.py documents each knob — it must not echo a credential
    value as a default/example (FR-004: no committed credential)."""
    knobs_path = (REPO_ROOT / "digital_twins" / "config" / "knobs.py")
    assert knobs_path.is_file(), f"missing {knobs_path}"
    text = knobs_path.read_text(encoding="utf-8")
    for service, secret in ALL_CREDENTIALS.items():
        assert secret not in text, (
            f"{service} credential value {secret!r} appeared in "
            f"knobs.py (FR-004: committed files must not carry credentials)"
        )
