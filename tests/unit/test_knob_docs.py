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
# config.example.yml active knob line:  `    key: value  # env: KB_FOO`
_KNOB_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
# config.example.yml commented-out knob line: `# key: value  # env: KB_FOO`
# Uses the brief's exact regex. Header lines like `# Precedence: ...` are
# matched but excluded by the is_section check (they have a value that is
# not a config value). The `# mytool:` block (no value) is also excluded.
#
# LIMITATION (deferred-minor, documented not fixed): when a commented-out
# knob line carries an *inline* comment — e.g. `# key: value  # note  # env: KB_FOO`
# — the value is truncated at the first `#` during parsing, and no test
# asserts that truncated value. The parser's value-stripping regex
# (`^([^#]*)`) cuts off everything from the first `#`, so the recorded value
# for such a knob is unreliable. This is intentional: the guard only needs
# the knob *path* and the env annotation (both captured before the strip),
# not the commented-out value. Do not "fix" the parser without re-evaluating
# whether any guard actually depends on the value of a commented-out knob.
_KNOB_LINE_COMMENTED = re.compile(r"^#\s*(\s*)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
# .env.example: lines like `#KB_FOO=`, `#YMAIL_APP_PASSWORD=`, `MYTOOL_TOKEN=`
# — matches any UPPER_CASE var, not just KB_-prefixed (catches credentials)
_ENV_ENTRY = re.compile(r"^#?([A-Z][A-Z0-9_]+)=")


def _parse_config_example(path: Path) -> dict[str, dict]:
    """Extract documented knobs from config.example.yml.

    Active and commented-out knob lines are both treated as documented.
    Commented-out lines are matched with the brief's regex
    `^#\\s*(\\s*)([A-Za-z_][A-Za-z0-9_]*):\\s*(.*)$`.

    Commented-out *section headers* (e.g. `# mytool:`) must not corrupt the
    active stack, so the parser tracks active and commented stacks separately:
    - active stack drives path construction for active leaves
    - commented leaves use the active stack at that line for their path
      (commented lines in the file always sit at the same nesting level as
       the section they belong to)

    Returns {dotted.path: {"value": str, "env": str | None}}.
    """
    text = path.read_text(encoding="utf-8")
    knobs: dict[str, dict] = {}
    stack: list[tuple[int, str]] = []  # active-only stack: (indent, key)

    for raw in text.splitlines():
        # Try both env-comment forms on the same line
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
            # Active line: maintain the active stack
            while stack and indent <= stack[-1][0]:
                stack.pop()

        # Strip trailing comment to get the raw value
        val_m = re.match(r"^([^#]*)", rest)
        value = val_m.group(1).strip() if val_m else ""

        is_section = (value == "" or rest in ("[]", "{}") or rest.endswith(":"))

        if not is_commented and is_section:
            # Active section header — push onto active stack
            stack.append((indent, key))
            continue

        if is_commented:
            if is_section:
                # Commented-out section header (`# mytool:`) — skip
                continue
            # Commented-out line: only record as a documented knob if it has
            # an env annotation. This excludes header prose like
            # `# Precedence: env (incl. .env) > ...` (no env: annotation).
            if not env:
                continue
            # Leaf — record the knob using the active stack for context
            parts = [k for _, k in stack]
            knob_path = ".".join(parts + [key])
            entry: dict = {"value": value}
            entry["env"] = env
            knobs[knob_path] = entry
            continue

        # Active leaf — record the knob
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
    def test_env_var_names_are_valid(self, env_vars):
        """Every env var in KNOBS must be a valid name, and credential vars
        (non-KB_) must be documented in .env.example."""
        documented_envs = set(env_vars)
        for path, entry in KNOBS.items():
            env = entry.get("env")
            if env is None:
                continue
            # KB_ prefix knobs, or UPPER_CASE credential env vars
            assert env.startswith("KB_") or re.match(r"^[A-Z][A-Z0-9_]+$", env), (
                f"{path}: env var {env!r} is not a KB_ var or valid credential name"
            )
            # Credential vars must be documented in .env.example
            if not env.startswith("KB_"):
                assert env in documented_envs, (
                    f"{path}: credential env var {env!r} not in .env.example"
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


class TestSchedulerStatusPort:
    """T002: scheduler.status_port knob (default 8765, 0 disables)."""

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created (T028)")
    def test_knob_registered(self):
        """scheduler.status_port must be a registered knob."""
        assert "scheduler.status_port" in KNOBS, (
            "scheduler.status_port is not in the KNOBS registry"
        )

    @pytest.mark.skipif(KNOBS is None, reason="KNOBS not yet created (T028)")
    def test_knob_has_required_fields(self):
        """The entry must carry type, default, env, and group."""
        entry = KNOBS.get("scheduler.status_port")
        assert entry is not None
        for field in ("type", "default", "env", "group"):
            assert field in entry, f"scheduler.status_port: missing field {field!r}"

    def test_default_is_8765(self):
        """The built-in default must be 8765."""
        from digital_twins.config.schema import DEFAULTS

        assert DEFAULTS.get("scheduler.status_port") == 8765, (
            "scheduler.status_port default must be 8765"
        )

    def test_zero_is_accepted(self):
        """0 is a valid value (means 'disabled'); coerce must accept it."""
        from digital_twins.config.schema import coerce

        assert coerce("scheduler.status_port", 0) == 0

    def test_negative_rejected(self):
        """Negative values must raise SchemaError naming the knob."""
        from digital_twins.config.schema import SchemaError, coerce

        with pytest.raises(SchemaError, match="scheduler.status_port"):
            coerce("scheduler.status_port", -1)

    def test_non_int_rejected(self):
        """Non-integer strings must raise SchemaError naming the knob."""
        from digital_twins.config.schema import SchemaError, coerce

        with pytest.raises(SchemaError, match="scheduler.status_port"):
            coerce("scheduler.status_port", "not-a-port")

    def test_documented_in_config_example(self, config_knobs):
        """The knob must be documented in config.example.yml with default + env."""
        assert "scheduler.status_port" in config_knobs, (
            "scheduler.status_port is not documented in config.example.yml"
        )
        doc = config_knobs["scheduler.status_port"]
        assert doc.get("value") == "8765", (
            f"scheduler.status_port: config.example.yml documents "
            f"value {doc.get('value')!r}, expected '8765'"
        )
        assert doc.get("env") == "KB_SCHEDULER__STATUS_PORT", (
            f"scheduler.status_port: config.example.yml env annotation "
            f"{doc.get('env')!r} != KB_SCHEDULER__STATUS_PORT"
        )

    def test_env_var_in_env_example(self, env_vars):
        """KB_SCHEDULER__STATUS_PORT must appear in .env.example."""
        assert "KB_SCHEDULER__STATUS_PORT" in env_vars, (
            "KB_SCHEDULER__STATUS_PORT is not in .env.example"
        )
