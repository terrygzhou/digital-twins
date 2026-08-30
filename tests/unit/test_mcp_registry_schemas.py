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


# ---------------------------------------------------------------------------
# T007 RED: _kb_ingest_schema() builder (007-R3a)
# ---------------------------------------------------------------------------

def test_kb_ingest_schema_builder_exists():
    """RED: the builder function does not exist yet."""
    assert hasattr(registry, "_kb_ingest_schema"), (
        "registry._kb_ingest_schema() missing (007 T007 RED)"
    )


def test_kb_ingest_schema_shape():
    """007-R3a: {source (string, optional), agent_kind}; required: []."""
    schema = registry._kb_ingest_schema()
    assert schema["type"] == "object"
    props = schema["properties"]
    assert set(props.keys()) == {"source", "agent_kind"}
    assert props["source"]["type"] == "string"
    # source is optional — not in required
    assert schema["required"] == []
    assert props["agent_kind"] == _agent_kind_expected()


# ---------------------------------------------------------------------------
# T008 RED: _kb_health_schema() builder (007-R4a)
# ---------------------------------------------------------------------------

def test_kb_health_schema_builder_exists():
    """RED: the builder function does not exist yet."""
    assert hasattr(registry, "_kb_health_schema"), (
        "registry._kb_health_schema() missing (007 T008 RED)"
    )


def test_kb_health_schema_shape():
    """007-R4a: {agent_kind} only; required: []."""
    schema = registry._kb_health_schema()
    assert schema["type"] == "object"
    props = schema["properties"]
    assert set(props.keys()) == {"agent_kind"}
    assert schema["required"] == []
    assert props["agent_kind"] == _agent_kind_expected()


# ---------------------------------------------------------------------------
# T009 RED: build_tool_registry entries — real schemas + no-stub wording
# (007-R5a/R5b, SC-006) + the six 004 scheduler declarations unchanged
# ---------------------------------------------------------------------------

SCHEDULER_TOOLS = {
    "kb_schedule_list",
    "kb_schedule_create",
    "kb_schedule_update",
    "kb_schedule_delete",
    "kb_schedule_run",
    "kb_run_history",
}

# The four KB entries, their builders, and their required lists (007-R5a).
KB_ENTRY_EXPECT: dict[str, dict] = {
    "kb_search": {"builder": "_kb_search_schema", "required": ["query"]},
    "kb_chat": {"builder": "_kb_chat_schema", "required": ["query"]},
    "kb_ingest": {"builder": "_kb_ingest_schema", "required": []},
    "kb_health": {"builder": "_kb_health_schema", "required": []},
}

# The canary (004) description for one scheduler tool — asserted byte-
# identical to prove the six 004 declarations are untouched.
_CANARY_TOOL = "kb_schedule_list"
_CANARY_DESCRIPTION = (
    "List schedules. Own scope by default; all_users=true is "
    "admin-only and returns every owner's schedules."
)


def test_registry_entries_use_real_schemas():
    """007-R5a: each KB entry's inputSchema equals its builder's output."""
    tools = _registry_by_name()
    for name, expected in KB_ENTRY_EXPECT.items():
        tool = tools[name]
        expected_schema = getattr(registry, expected["builder"])()
        assert tool["inputSchema"] == expected_schema, (
            f"{name}: inputSchema does not equal {expected['builder']}() "
            "output"
        )
        assert tool["inputSchema"]["required"] == expected["required"]


def test_registry_entries_no_stub_wording():
    """007-R5b / SC-006: no KB description contains 'stub' or
    'not implemented in 004'."""
    tools = _registry_by_name()
    for name in KB_ENTRY_EXPECT:
        desc = tools[name]["description"]
        lowered = desc.lower()
        assert "stub" not in lowered, (
            f"{name}: description still contains 'stub': {desc!r}"
        )
        assert "not implemented in 004" not in lowered, (
            f"{name}: description still contains 'not implemented in 004': "
            f"{desc!r}"
        )


def test_registry_kb_descriptions_name_error_codes():
    """The four descriptions describe the real behavior + error codes."""
    tools = _registry_by_name()
    assert "bad_request" in tools["kb_search"]["description"]
    assert "qdrant_unavailable" in tools["kb_search"]["description"]
    assert "embedding_unavailable" in tools["kb_search"]["description"]
    assert "not_implemented" in tools["kb_chat"]["description"]
    assert "permission_denied" in tools["kb_ingest"]["description"]
    assert "run_failed" in tools["kb_ingest"]["description"]
    assert "config_not_loaded" in tools["kb_ingest"]["description"]
    assert "checks" in tools["kb_health"]["description"]


def test_scheduler_declarations_canary_unchanged():
    """Canary: one 004 scheduler declaration is byte-identical to 004 —
    proves the six 004 scheduler declarations were not touched."""
    tools = _registry_by_name()
    assert tools[_CANARY_TOOL]["description"] == _CANARY_DESCRIPTION, (
        "004 scheduler declaration changed — the six 004 scheduler "
        "declarations must stay byte-identical"
    )


def test_stub_schema_helper_deleted():
    """After T009 GREEN, _stub_schema() no longer exists."""
    assert not hasattr(registry, "_stub_schema"), (
        "registry._stub_schema still exists — T009 GREEN deletes it "
        "(no remaining callers after the four stub entries are swapped)"
    )
