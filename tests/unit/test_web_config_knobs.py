"""T001 [006-web-app] RED: web.bind / web.port / web.base_url knobs.

Asserts the three new web.* knobs in the machine-readable KNOBS registry
(``digital_twins/config/knobs.py``) and their lock-step documentation in
the three shipped surfaces (constitution IV): ``config.example.yml``,
``.env.example``, and ``docs/configuration.md``.

Mirrors the parsing approach of ``tests/unit/test_knob_docs.py`` (the
standing lock-step guard) so the surfaces stay in sync. These tests are
RED until T002 lands the GROUP_WEB entry and the matching doc entries.
"""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    from digital_twins.config.knobs import KNOBS  # type: ignore[no-redef]
except ImportError:  # pragma: no cover - registry always present
    KNOBS = None

# --- paths (mirror test_knob_docs.py) --------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_EXAMPLE = _REPO_ROOT / "config.example.yml"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_CONFIGURATION_DOC = _REPO_ROOT / "docs" / "configuration.md"

# --- spec (the three knobs, exactly as the 006 plan locks them) -------------

_WEB_SPEC: dict[str, dict] = {
    "web.bind": {
        "type": "str",
        "default": "127.0.0.1",
        "env": "KB_WEB__BIND",
    },
    "web.port": {
        "type": "int",
        "default": 8767,
        "env": "KB_WEB__PORT",
    },
    "web.base_url": {
        "type": "str",
        "default": "http://localhost:8767",
        "env": "KB_WEB__BASE_URL",
    },
}


# --- parsers (mirror test_knob_docs.py exactly) -----------------------------

import re

# config.example.yml: `# env: KB_FOO` (bare, at end of line or before value)
_ENV_COMMENT_BARE = re.compile(r"#\s*env:\s*(KB_[A-Z0-9_]+)")
# config.example.yml: `(env: KB_FOO)` (parenthesized form)
_ENV_COMMENT_PAREN = re.compile(r"\(env:\s*(KB_[A-Z0-9_]+)\)")
# config.example.yml active knob line:  `    key: value  # env: KB_FOO`
_KNOB_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
# config.example.yml commented-out knob line: `# key: value  # env: KB_FOO`
_KNOB_LINE_COMMENTED = re.compile(r"^#\s*(\s*)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
# .env.example: lines like `#KB_FOO=`, `KB_FOO=`, `MYTOOL_TOKEN=`
_ENV_ENTRY = re.compile(r"^#?([A-Z][A-Z0-9_]+)=")


def _parse_config_example(path: Path) -> dict[str, dict]:
    """Extract documented knobs from config.example.yml.

    Same algorithm as test_knob_docs.py._parse_config_example: active and
    commented-out knob lines are both treated as documented; commented-out
    section headers are skipped; commented-out leaf lines are recorded only
    when they carry an env annotation.
    """
    text = path.read_text(encoding="utf-8")
    knobs: dict[str, dict] = {}
    stack: list[tuple[int, str]] = []  # active-only stack: (indent, key)

    for raw in text.splitlines():
        env_match = _ENV_COMMENT_BARE.search(raw) or _ENV_COMMENT_PAREN.search(raw)
        env = env_match.group(1) if env_match else None

        is_commented = False
        m_active = _KNOB_LINE.match(raw)
        if m_active:
            m = m_active
        else:
            m_commented = _KNOB_LINE_COMMENTED.match(raw)
            if not m_commented:
                continue
            m = m_commented
            is_commented = True

        indent = len(m.group(1))
        key, rest = m.group(2), m.group(3).strip()

        if not is_commented:
            while stack and indent <= stack[-1][0]:
                stack.pop()

        val_m = re.match(r"^([^#]*)", rest)
        value = val_m.group(1).strip() if val_m else ""

        is_section = (value == "" or rest in ("[]", "{}") or rest.endswith(":"))

        if not is_commented and is_section:
            stack.append((indent, key))
            continue

        if is_commented:
            if is_section:
                continue
            if not env:
                continue
            parts = [k for _, k in stack]
            knobs[".".join(parts + [key])] = {"value": value, "env": env}
            continue

        parts = [k for _, k in stack]
        knobs[".".join(parts + [key])] = {"value": value, "env": env}

    return knobs


def _parse_env_example(path: Path) -> list[str]:
    """Extract documented env var names from .env.example."""
    envs: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _ENV_ENTRY.match(line.strip())
        if m:
            envs.append(m.group(1))
    return envs


def _literalize_default(raw: str):
    """Turn a docs cell into the value type the registry stores."""
    text = raw.strip()
    if text in ("~", "(none)", "(no default)", "—"):
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _parse_configuration_doc(path: Path) -> dict[str, dict]:
    """Extract documented knobs from docs/configuration.md.

    Same algorithm as test_knob_docs.py._parse_configuration_doc: every
    table with a ``| knob | type | default | env var | ... |`` header row
    yields ``{dotted.knob: {"type", "default", "env"}}``.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    knobs: dict[str, dict] = {}
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not (line.startswith("|") and line.endswith("|")):
            i += 1
            continue
        header_cells = [c.strip() for c in line[1:-1].split("|")]
        cols = {name: idx for idx, name in enumerate(header_cells)}
        if "knob" not in cols:
            i += 1
            continue
        j = i + 1
        if j < len(lines) and re.match(r"^\|[\s:\-|]+\|$", lines[j].strip()):
            j += 1
        while j < len(lines):
            row = lines[j].strip()
            if not row.startswith("|") or not row.endswith("|"):
                break
            cells = [c.strip() for c in row[1:-1].split("|")]
            if len(cells) != len(header_cells):
                raise AssertionError(
                    f"docs/configuration.md line {j + 1}: knob table row has "
                    f"{len(cells)} cells, header has {len(header_cells)}: "
                    f"{row!r}"
                )
            knob = cells[cols["knob"]]
            knobs[knob] = {
                "type": cells[cols["type"]],
                "default": _literalize_default(cells[cols["default"]]),
                "env": cells[cols["env var"]] if "env var" in cols else None,
            }
            j += 1
        i = j
    return knobs


# --- fixtures (mirror test_knob_docs.py) ------------------------------------


@pytest.fixture(scope="module")
def config_knobs() -> dict[str, dict]:
    """Parsed config.example.yml knobs."""
    return _parse_config_example(_CONFIG_EXAMPLE)


@pytest.fixture(scope="module")
def env_vars() -> list[str]:
    """Parsed .env.example env var names."""
    return _parse_env_example(_ENV_EXAMPLE)


@pytest.fixture(scope="module")
def configuration_doc() -> dict[str, dict]:
    """Parsed docs/configuration.md knob rows.

    Fails when the doc is missing (RED state).
    """
    assert _CONFIGURATION_DOC.is_file(), (
        f"docs/configuration.md is missing at {_CONFIGURATION_DOC}"
    )
    return _parse_configuration_doc(_CONFIGURATION_DOC)


# --- tests -----------------------------------------------------------------


class TestWebKnobRegistry:
    """The three web.* knobs must exist in KNOBS with the exact spec.

    RED until T002 adds GROUP_WEB + the registry entries:
    "web.bind" / "web.port" / "web.base_url" are not in the registry yet.
    """

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created")
    def test_knobs_registered(self):
        for path in _WEB_SPEC:
            assert path in KNOBS, (
                f"{path} is not in the KNOBS registry (T002 must add GROUP_WEB)"
            )

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created")
    def test_knobs_have_required_fields(self):
        for path in _WEB_SPEC:
            entry = KNOBS.get(path)
            assert entry is not None, f"{path} not in KNOBS registry"
            for field in ("type", "default", "env", "group"):
                assert field in entry, f"{path}: missing field {field!r}"

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created")
    def test_knobs_match_spec(self):
        """type, default, and env must match the 006 spec exactly."""
        for path, spec in _WEB_SPEC.items():
            entry = KNOBS.get(path)
            if entry is None:
                continue  # reported by test_knobs_registered
            for field, expected in spec.items():
                assert entry.get(field) == expected, (
                    f"{path}.{field}: registry has "
                    f"{entry.get(field)!r}, spec requires {expected!r}"
                )


class TestWebPortDistinctness:
    """web.port must be 8767 and distinct from the other server ports."""

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created")
    def test_web_port_is_8767(self):
        entry = KNOBS.get("web.port")
        assert entry is not None, "web.port not in KNOBS registry"
        assert entry["default"] == 8767, (
            f"web.port default is {entry['default']!r}, expected 8767"
        )

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created")
    def test_web_port_distinct_from_scheduler_status_port(self):
        """web.port (8767) must not collide with scheduler.status_port (8765)."""
        entry = KNOBS.get("web.port")
        scheduler = KNOBS.get("scheduler.status_port")
        if entry is None or scheduler is None:
            return  # missing entries reported by the registry tests
        assert entry["default"] != scheduler["default"], (
            "web.port must be distinct from scheduler.status_port"
        )

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created")
    def test_web_port_distinct_from_mcp_port(self):
        """web.port (8767) must not collide with mcp.port (8770)."""
        entry = KNOBS.get("web.port")
        mcp = KNOBS.get("mcp.port")
        if entry is None or mcp is None:
            return  # missing entries reported by the registry tests
        assert entry["default"] != mcp["default"], (
            "web.port must be distinct from mcp.port"
        )


class TestWebKnobsDocumentedInConfigExample:
    """Each web knob must be documented in config.example.yml.

    RED: there is no `web:` section yet. The knobs parser records active
    leaf lines and commented-out leaf lines (with an env annotation), so
    T002 may land the section as commented-out defaults.
    """

    @pytest.mark.parametrize("path", sorted(_WEB_SPEC))
    def test_knob_documented(self, path, config_knobs):
        assert path in config_knobs, (
            f"{path} is not documented in config.example.yml "
            "(T002 must add a `web:` section)"
        )

    @pytest.mark.parametrize("path", sorted(_WEB_SPEC))
    def test_knob_documentation_matches_spec(self, path, config_knobs):
        for p in _WEB_SPEC:
            if p not in config_knobs:
                continue  # reported by test_knob_documented
            doc = config_knobs[p]
            expected_env = _WEB_SPEC[p]["env"]
            assert doc.get("env") == expected_env, (
                f"{p}: config.example.yml env annotation "
                f"{doc.get('env')!r} != {expected_env!r}"
            )


class TestWebKnobsDocumentedInEnvExample:
    """Each web env var must be documented in .env.example.

    RED: KB_WEB__BIND / KB_WEB__PORT / KB_WEB__BASE_URL are absent.
    """

    @pytest.mark.parametrize("path", sorted(_WEB_SPEC))
    def test_env_var_documented(self, path, env_vars):
        expected_env = _WEB_SPEC[path]["env"]
        assert expected_env in env_vars, (
            f"{expected_env} (for {path}) is not in .env.example "
            "(T002 must add it, commented-out with a description)"
        )


class TestWebKnobsDocumentedInConfigurationDoc:
    """Each web knob must have a row in docs/configuration.md.

    RED: the `## Web` section does not exist yet.
    """

    @pytest.mark.parametrize("path", sorted(_WEB_SPEC))
    def test_knob_documented(self, path, configuration_doc):
        assert path in configuration_doc, (
            f"{path} is not documented in docs/configuration.md "
            "(T002 must add a `## Web` section table)"
        )

    @pytest.mark.parametrize("path", sorted(_WEB_SPEC))
    def test_knob_row_matches_spec(self, path, configuration_doc):
        for p in _WEB_SPEC:
            doc = configuration_doc.get(p)
            if doc is None:
                continue  # reported by test_knob_documented
            spec = _WEB_SPEC[p]
            assert doc.get("type") == spec["type"], (
                f"{p}: docs type {doc.get('type')!r} != registry {spec['type']!r}"
            )
            assert doc.get("default") == spec["default"], (
                f"{p}: docs default {doc.get('default')!r} != "
                f"registry {spec['default']!r}"
            )
            assert doc.get("env") == spec["env"], (
                f"{p}: docs env {doc.get('env')!r} != registry {spec['env']!r}"
            )
