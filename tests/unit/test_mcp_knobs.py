"""T001 (RED): mcp.port + mcp.service_account_email knobs (feature 004, R12).

Asserts that the two MCP config knobs are registered in the machine-readable
KNOBS registry (digital_twins/config/knobs.py) with the correct type,
default, env var, and group — AND that both are documented in the shipped
example files (config.example.yml `mcp:` section, .env.example `KB_MCP__*`).

Constitution IV (config-first): a knob with no doc is a defect. This test is
red until T002 lands the knobs + docs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from digital_twins.config.knobs import KNOBS

# --- paths ---------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_EXAMPLE = _REPO_ROOT / "config.example.yml"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# config.example.yml: `# env: KB_FOO` (bare) / `(env: KB_FOO)` (parenthesized)
_ENV_COMMENT_BARE = re.compile(r"#\s*env:\s*(KB_[A-Z0-9_]+)")
_ENV_COMMENT_PAREN = re.compile(r"\(env:\s*(KB_[A-Z0-9_]+)\)")
# active knob line:  `    key: value  # env: KB_FOO`
_KNOB_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")


def _parse_config_example(path: Path) -> dict[str, dict]:
    """Extract documented knobs from config.example.yml.

    Returns {dotted.path: {"value": str, "env": str | None}}.
    Mirrors the parser shape in tests/unit/test_knob_docs.py.
    """
    text = path.read_text(encoding="utf-8")
    knobs: dict[str, dict] = {}
    stack: list[tuple[int, str]] = []  # active-only stack: (indent, key)

    for raw in text.splitlines():
        env_match = _ENV_COMMENT_BARE.search(raw) or _ENV_COMMENT_PAREN.search(raw)
        env = env_match.group(1) if env_match else None

        m = _KNOB_LINE.match(raw)
        if not m:
            continue

        indent = len(m.group(1))
        key, rest = m.group(2), m.group(3).strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()

        val_m = re.match(r"^([^#]*)", rest)
        value = val_m.group(1).strip() if val_m else ""

        is_section = (value == "" or rest in ("[]", "{}") or rest.endswith(":"))
        if is_section:
            stack.append((indent, key))
            continue

        knob_path = ".".join([k for _, k in stack] + [key])
        entry: dict = {"value": value}
        if env:
            entry["env"] = env
        knobs[knob_path] = entry

    return knobs


def _parse_env_example(path: Path) -> list[str]:
    """Extract documented env var names from .env.example."""
    envs: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#?([A-Z][A-Z0-9_]+)=", line.strip())
        if m:
            envs.append(m.group(1))
    return envs


@pytest.fixture(scope="module")
def config_knobs() -> dict[str, dict]:
    """Parsed config.example.yml knobs."""
    return _parse_config_example(_CONFIG_EXAMPLE)


@pytest.fixture(scope="module")
def env_vars() -> list[str]:
    """Parsed .env.example env var names."""
    return _parse_env_example(_ENV_EXAMPLE)


# --- expected knob specs (R12) --------------------------------------------

EXPECTED_MCP_KNOBS: dict[str, dict] = {
    "mcp.port": {
        "type": "int",
        "default": 8770,
        "env": "KB_MCP__PORT",
        "group": "MCP",
    },
    "mcp.service_account_email": {
        "type": "str",
        "default": "system",
        "env": "KB_MCP__SERVICE_ACCOUNT_EMAIL",
        "group": "MCP",
    },
}


# --- registry assertions ---------------------------------------------------


class TestMcpKnobsInRegistry:
    """Both mcp.* knobs must exist in KNOBS with the correct entry."""

    @pytest.mark.parametrize("knob_path", sorted(EXPECTED_MCP_KNOBS))
    def test_knob_registered(self, knob_path):
        assert knob_path in KNOBS, (
            f"{knob_path} is not in the KNOBS registry (R12; T002 must land it)"
        )

    @pytest.mark.parametrize("knob_path", sorted(EXPECTED_MCP_KNOBS))
    def test_knob_entry_matches_spec(self, knob_path):
        expected = EXPECTED_MCP_KNOBS[knob_path]
        entry = KNOBS.get(knob_path)
        assert entry is not None, f"{knob_path} is not in the KNOBS registry"
        for field in ("type", "default", "env", "group"):
            assert field in entry, (
                f"{knob_path}: registry entry missing field {field!r}"
            )
        for field, expected_value in expected.items():
            assert entry[field] == expected_value, (
                f"{knob_path}.{field}: registry says {entry[field]!r}, "
                f"expected {expected_value!r}"
            )


# --- config.example.yml assertions -----------------------------------------


class TestMcpKnobsDocumentedInConfigExample:
    """Both mcp.* knobs must be documented under an `mcp:` section."""

    @pytest.mark.parametrize("knob_path", sorted(EXPECTED_MCP_KNOBS))
    def test_knob_in_config_example(self, knob_path, config_knobs):
        assert knob_path in config_knobs, (
            f"{knob_path} is not documented in config.example.yml — "
            "an mcp: section with the knob is required (constitution IV)"
        )

    @pytest.mark.parametrize("knob_path", sorted(EXPECTED_MCP_KNOBS))
    def test_knob_documented_value_and_env(self, knob_path, config_knobs):
        entry = config_knobs.get(knob_path)
        assert entry is not None, f"{knob_path} is not documented in config.example.yml"
        expected = EXPECTED_MCP_KNOBS[knob_path]
        assert entry.get("value") == str(expected["default"]), (
            f"{knob_path}: config.example.yml documents value "
            f"{entry.get('value')!r}, expected {str(expected['default'])!r}"
        )
        assert entry.get("env") == expected["env"], (
            f"{knob_path}: config.example.yml env annotation "
            f"{entry.get('env')!r} != {expected['env']!r}"
        )


# --- .env.example assertions ------------------------------------------------


class TestMcpEnvVarsInEnvExample:
    """Both KB_MCP__* env vars must appear in .env.example."""

    @pytest.mark.parametrize("knob_path", sorted(EXPECTED_MCP_KNOBS))
    def test_env_var_in_env_example(self, knob_path, env_vars):
        expected_env = EXPECTED_MCP_KNOBS[knob_path]["env"]
        assert expected_env in env_vars, (
            f"{knob_path}: env var {expected_env!r} is not in .env.example"
        )
