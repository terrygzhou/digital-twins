"""007 Phase 2 (T005–T009): real ``inputSchema`` builders + registry entries.

Feature 007 replaces the four 004 BR-10 stub declarations in
``digital_twins/mcp/registry.py`` with full ``inputSchema``s and
real-behavior descriptions, and deletes the now-unused ``_stub_schema()``
helper.

Constitution III (Test-First): each task's RED test is committed and
failing before its GREEN implementation.

- T005 RED/GREEN: ``_kb_search_schema()`` builder.
- T006 RED/GREEN: ``_kb_chat_schema()`` builder.
- T007 RED/GREEN: ``_kb_ingest_schema()`` builder.
- T008 RED/GREEN: ``_kb_health_schema()`` builder.
- T009 RED/GREEN: the four registry entries carry the real schemas +
  no-stub descriptions; the six 004 scheduler declarations are
  byte-identical (canary); ``_stub_schema`` is deleted.
"""
from __future__ import annotations

import digital_twins.mcp.registry as registry


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _agent_kind_expected() -> dict:
    """The shared ``agent_kind`` property every schema reuses."""
    return {
        "type": "string",
        "description":
            "The client's declared kind (e.g. 'hermes', 'claude-desktop'). "
            "Recorded on the audit row.",
    }


def _registry_by_name() -> dict:
    tools = registry.build_tool_registry()
    return {t["name"]: t for t in tools}


# ---------------------------------------------------------------------------
# T005 RED: _kb_search_schema() builder (007-R1a)
# ---------------------------------------------------------------------------

def test_kb_search_schema_builder_exists():
    """RED: the builder function does not exist yet."""
    assert hasattr(registry, "_kb_search_schema"), (
        "registry._kb_search_schema() missing (007 T005 RED)"
    )


def test_kb_search_schema_shape():
    """007-R1a: {query (string, required), limit (int, default 5,
    description notes max 100), agent_kind}."""
    schema = registry._kb_search_schema()
    assert schema["type"] == "object"
    props = schema["properties"]
    assert set(props.keys()) == {"query", "limit", "agent_kind"}

    # query: string, required
    assert props["query"]["type"] == "string"
    assert "query" in schema["required"]

    # limit: integer, default 5, description notes max 100
    assert props["limit"]["type"] == "integer"
    assert props["limit"]["default"] == 5
    assert "max 100" in props["limit"]["description"]

    # agent_kind: the shared property
    assert props["agent_kind"] == _agent_kind_expected()

    # required list is exactly [query]
    assert schema["required"] == ["query"]


# ---------------------------------------------------------------------------
# T006 RED: _kb_chat_schema() builder (007-R2a)
# ---------------------------------------------------------------------------

def test_kb_chat_schema_builder_exists():
    """RED: the builder function does not exist yet."""
    assert hasattr(registry, "_kb_chat_schema"), (
        "registry._kb_chat_schema() missing (007 T006 RED)"
    )


def test_kb_chat_schema_shape():
    """007-R2a: {query (string, required), agent_kind}."""
    schema = registry._kb_chat_schema()
    assert schema["type"] == "object"
    props = schema["properties"]
    assert set(props.keys()) == {"query", "agent_kind"}
    assert props["query"]["type"] == "string"
    assert "query" in schema["required"]
    assert props["agent_kind"] == _agent_kind_expected()
    assert schema["required"] == ["query"]
