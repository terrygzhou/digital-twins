"""Knob-doc sync guard (SC-002): zero undocumented knobs.

Verifies that every knob in the machine-readable registry (knobs.py) is
documented in the shipped example files (config.example.yml, .env.example),
and vice versa.  Written TDD-first (T027); will pass when T028 lands.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# --- paths ---------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_EXAMPLE = _REPO_ROOT / "config.example.yml"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# --- parsers ---------------------------------------------------------------

# config.example.yml: `# env: KB_FOO` (bare, at end of line or before value)
_ENV_COMMENT_BARE = re.compile(r"#\s*env:\s*(KB_[A-Z0-9_]+)")
# config.example.yml: `(env: KB_FOO)` (parenthesized, e.g. embedding.device)
_ENV_COMMENT_PAREN = re.compile(r"\(env:\s*(KB_[A-Z0-9_]+)\)")
# .env.example: lines like `#KB_FOO=` or `KB_FOO=`
_ENV_ENTRY = re.compile(r"^#?(KB_[A-Z0-9_]+)=")


def _parse_config_example(path: Path) -> dict[str, dict]:
    """Extract documented knobs from config.example.yml.

    Returns {dotted.path: {"value": str, "env": str | None}}.
    """
    text = path.read_text(encoding="utf-8")
    knobs: dict[str, dict] = {}
    stack: list[tuple[int, str]] = []  # (indent, key)

    for raw in text.splitlines():
        # Try both env-comment forms on the same line
        env_match = _ENV_COMMENT_BARE.search(raw) or _ENV_COMMENT_PAREN.search(raw)
        env = env_match.group(1) if env_match else None

        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", raw)
        if not m:
            continue
        indent = len(m.group(1))
        key, rest = m.group(2), m.group(3).strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()

        # Strip trailing comment to get the raw value
        val_m = re.match(r"^([^#]*)", rest)
        value = val_m.group(1).strip() if val_m else ""

        # Section header (no value) — push onto stack
        if value == "" or rest in ("[]", "{}") or rest.endswith(":"):
            stack.append((indent, key))
            continue

        # Leaf — record the knob
        parts = [k for _, k in stack]
        knob_path = ".".join(parts + [key])
        entry: dict = {"value": value}
        if env:
            entry["env"] = env
        knobs[knob_path] = entry

    return knobs


def _parse_env_example(path: Path) -> list[str]:
    """Extract documented env var names from .env.example."""
    envs: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _ENV_ENTRY.match(line.strip())
        if m:
            envs.append(m.group(1))
    return envs


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def config_knobs() -> dict[str, dict]:
    """Parsed config.example.yml knobs."""
    return _parse_config_example(_CONFIG_EXAMPLE)


@pytest.fixture(scope="module")
def env_vars() -> list[str]:
    """Parsed .env.example env var names."""
    return _parse_env_example(_ENV_EXAMPLE)


# --- knob registry import (T028 creates this) -------------------------------

try:
    from digital_twins.config.knobs import KNOBS  # type: ignore[no-redef]
except ImportError:
    KNOBS = None


# --- tests -----------------------------------------------------------------


class TestKnobRegistryStructure:
    """KNOBS registry must be a well-formed dict."""

    def test_knobs_exists(self):
        assert KNOBS is not None, (
            "KNOBS registry not found — T028 (knobs.py) has not landed yet"
        )

    def test_knobs_is_dict(self):
        assert isinstance(KNOBS, dict)

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created (T028)")
    def test_every_knob_has_required_fields(self):
        for path, entry in KNOBS.items():
            assert isinstance(entry, dict), f"{path}: entry is not a dict"
            for field in ("type", "default", "env", "group"):
                assert field in entry, (
                    f"{path}: missing field {field!r}"
                )

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created (T028)")
    def test_env_var_names_are_valid(self):
        """Every env var in KNOBS must start with KB_ or be a credential var."""
        for path, entry in KNOBS.items():
            env = entry.get("env")
            if env is None:
                continue
            # KB_ prefix knobs, or credential env vars (YMAIL_, GMAIL_, MYTOOL_)
            assert env.startswith("KB_") or env in (
                "YMAIL_APP_PASSWORD", "GMAIL_APP_PASSWORD",
            ), (
                f"{path}: env var {env!r} is not a KB_ var or known credential"
            )


class TestKnobsDocumentedInConfigExample:
    """Every knob in KNOBS must appear in config.example.yml."""

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created (T028)")
    def test_every_knob_in_config_example(self, config_knobs):
        """KNOBS key must be a documented knob path in config.example.yml.

        Note: config.example.yml only documents knobs that are *user-visible*.
        Source knobs (sources.<name>.enabled, etc.) are documented via the
        sources section, so we check that each KNOBS path either appears
        directly in config.example.yml OR is a source knob under a
        documented source section.
        """
        documented_paths = set(config_knobs.keys())
        # Build the set of documented source paths from the example
        documented_sources: set[str] = set()
        for path in documented_paths:
            if path.startswith("sources."):
                # sources.<name> — collect the source name
                parts = path.split(".")
                if len(parts) == 2:
                    documented_sources.add(parts[1])
                # Also collect sources.<name>.<field>
                elif len(parts) >= 3:
                    documented_sources.add(parts[1])

        missing: list[str] = []
        for knob_path in KNOBS:
            if knob_path in documented_paths:
                continue
            # Source knob: sources.<name>.<field>
            if knob_path.startswith("sources."):
                parts = knob_path.split(".")
                source_name = parts[1] if len(parts) > 1 else ""
                # Check if the source section is documented
                source_doc = f"sources.{source_name}"
                if source_doc in documented_paths or source_name in documented_sources:
                    continue
            missing.append(knob_path)

        assert not missing, (
            f"Knobs in KNOBS but not in config.example.yml: {missing}"
        )


class TestKnobsDocumentedInEnvExample:
    """Every env var in KNOBS must appear in .env.example."""

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created (T028)")
    def test_every_env_var_in_env_example(self, env_vars):
        documented_envs = set(env_vars)
        missing: list[str] = []
        for knob_path, entry in KNOBS.items():
            env = entry.get("env")
            if env is None:
                continue
            if env not in documented_envs:
                missing.append(f"{knob_path} ({env})")
        assert not missing, (
            f"Env vars in KNOBS but not in .env.example: {missing}"
        )


class TestConfigExampleKnobsInRegistry:
    """Every knob in config.example.yml must exist in KNOBS."""

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created (T028)")
    def test_config_example_knobs_in_registry(self, config_knobs):
        missing: list[str] = []
        for knob_path in config_knobs:
            if knob_path not in KNOBS:
                missing.append(knob_path)
        assert not missing, (
            f"Knobs in config.example.yml but not in KNOBS: {missing}"
        )


class TestEnvExampleVarsInRegistry:
    """Every env var in .env.example must exist in KNOBS."""

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created (T028)")
    def test_env_example_vars_in_registry(self, env_vars):
        # Build set of env vars declared in KNOBS
        registry_envs: set[str] = set()
        for entry in KNOBS.values():
            env = entry.get("env")
            if env is not None:
                registry_envs.add(env)

        missing: list[str] = []
        for var in env_vars:
            if var not in registry_envs:
                missing.append(var)
        assert not missing, (
            f"Env vars in .env.example but not in KNOBS: {missing}"
        )


class TestEnvVarConsistency:
    """KNOBS env vars must match the config.example.yml annotations."""

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created (T028)")
    def test_env_vars_match_config_annotations(self, config_knobs):
        mismatches: list[str] = []
        for knob_path, entry in KNOBS.items():
            env = entry.get("env")
            if env is None:
                continue
            if knob_path in config_knobs:
                documented_env = config_knobs[knob_path].get("env")
                if documented_env is not None and documented_env != env:
                    mismatches.append(
                        f"{knob_path}: KNOBS says {env!r}, "
                        f"config.example.yml says {documented_env!r}"
                    )
        assert not mismatches, (
            f"Env var mismatches between KNOBS and config.example.yml: {mismatches}"
        )
