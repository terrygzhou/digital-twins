"""T005 (RED): build_tool_registry — the MCP tool registry (feature 004, R2/R10).

Asserts that ``build_tool_registry()`` returns exactly 10 tools (6 real + 4
stubs), each carrying ``name``, ``description``, and ``inputSchema`` whose
field names mirror 003 ``contracts/scheduler.md`` exactly (R2).

The 6 real tools (R2):
    kb_schedule_list, kb_schedule_create, kb_schedule_update,
    kb_schedule_delete, kb_schedule_run, kb_run_history

The 4 BR-10 stubs (R10):
    kb_search, kb_chat, kb_ingest, kb_health

A missing tool, a renamed field, or an extra tool fails this test.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# expected tool names + inputSchema field sets (mirror 003 contracts)
# ---------------------------------------------------------------------------

REAL_TOOLS: set[str] = {
    "kb_schedule_list",
    "kb_schedule_create",
    "kb_schedule_update",
    "kb_schedule_delete",
    "kb_schedule_run",
    "kb_run_history",
}

STUB_TOOLS: set[str] = {
    "kb_search",
    "kb_chat",
    "kb_ingest",
    "kb_health",
}

ALL_TOOLS: set[str] = REAL_TOOLS | STUB_TOOLS

# Expected inputSchema field names per tool (the agent-facing contract, R2).
# agent_kind is optional on every tool (R6).
EXPECTED_FIELDS: dict[str, set[str]] = {
    # 1. kb_schedule_list — all_users (bool, optional), agent_kind
    "kb_schedule_list": {"all_users", "agent_kind"},
    # 2. kb_schedule_create — source, preset, param, fire_time, enabled,
    #    acl, agent_kind
    "kb_schedule_create": {
        "source", "preset", "param", "fire_time", "enabled", "acl",
        "agent_kind",
    },
    # 3. kb_schedule_update — schedule_id + any of preset/param/fire_time/
    #    enabled/acl, agent_kind
    "kb_schedule_update": {
        "schedule_id", "preset", "param", "fire_time", "enabled", "acl",
        "agent_kind",
    },
    # 4. kb_schedule_delete — schedule_id, agent_kind
    "kb_schedule_delete": {"schedule_id", "agent_kind"},
    # 5. kb_schedule_run — schedule_id, agent_kind
    "kb_schedule_run": {"schedule_id", "agent_kind"},
    # 6. kb_run_history — user, source, since, until, limit, agent_kind
    "kb_run_history": {
        "user", "source", "since", "until", "limit", "agent_kind",
    },
    # Stubs: minimal inputSchema (agent_kind only, or empty)
    "kb_search": {"agent_kind"},
    "kb_chat": {"agent_kind"},
    "kb_ingest": {"agent_kind"},
    "kb_health": {"agent_kind"},
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _registry() -> list[dict]:
    """Build the tool registry once per test (module-level import kept
    local so RED fails with a clean ImportError)."""
    from digital_twins.mcp.registry import build_tool_registry
    return build_tool_registry()


def _by_name(tools: list[dict]) -> dict[str, dict]:
    return {t["name"]: t for t in tools}


# ---------------------------------------------------------------------------
# T005: exactly 10 tools, 6 real + 4 stubs
# ---------------------------------------------------------------------------

def test_returns_exactly_10_tools():
    tools = _registry()
    assert len(tools) == 10, (
        f"build_tool_registry returned {len(tools)} tools, expected 10 "
        "(6 real + 4 stubs)"
    )


def test_real_tools_present():
    names = {t["name"] for t in _registry()}
    missing = REAL_TOOLS - names
    assert not missing, f"missing real tools: {missing}"


def test_stub_tools_present():
    names = {t["name"] for t in _registry()}
    missing = STUB_TOOLS - names
    assert not missing, f"missing stub tools: {missing}"


def test_no_extra_tools():
    names = {t["name"] for t in _registry()}
    extra = names - ALL_TOOLS
    assert not extra, f"unexpected extra tools: {extra}"


# ---------------------------------------------------------------------------
# T005: every tool has name, description, inputSchema
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", sorted(ALL_TOOLS))
def test_tool_has_required_keys(tool_name):
    tools = _by_name(_registry())
    assert tool_name in tools, f"{tool_name} not in registry"
    tool = tools[tool_name]
    for key in ("name", "description", "inputSchema"):
        assert key in tool, f"{tool_name} missing key {key!r}"
    assert tool["name"] == tool_name
    assert isinstance(tool["description"], str) and tool["description"]
    assert isinstance(tool["inputSchema"], dict)


# ---------------------------------------------------------------------------
# T005: inputSchema field names mirror 003 contracts exactly (R2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", sorted(REAL_TOOLS))
def test_real_tool_input_schema_fields(tool_name):
    tools = _by_name(_registry())
    schema = tools[tool_name]["inputSchema"]
    assert "properties" in schema, (
        f"{tool_name}: inputSchema missing 'properties' key"
    )
    fields = set(schema["properties"].keys())
    expected = EXPECTED_FIELDS[tool_name]
    missing = expected - fields
    extra = fields - expected
    assert not missing, (
        f"{tool_name}: inputSchema.properties missing fields {missing}"
    )
    assert not extra, (
        f"{tool_name}: inputSchema.properties has unexpected fields {extra}"
    )


@pytest.mark.parametrize("tool_name", sorted(STUB_TOOLS))
def test_stub_tool_input_schema_has_properties(tool_name):
    """Stubs get a minimal inputSchema — must have a properties dict."""
    tools = _by_name(_registry())
    schema = tools[tool_name]["inputSchema"]
    assert "properties" in schema, (
        f"{tool_name}: stub inputSchema missing 'properties' key"
    )
    # Stub properties must be a dict (may be empty or contain agent_kind).
    assert isinstance(schema["properties"], dict)


def test_agent_kind_present_on_every_tool():
    """R6: agent_kind is an optional top-level argument on *every* tool."""
    for tool in _registry():
        props = tool["inputSchema"].get("properties", {})
        assert "agent_kind" in props, (
            f"{tool['name']}: agent_kind missing from inputSchema.properties "
            "(R6)"
        )
