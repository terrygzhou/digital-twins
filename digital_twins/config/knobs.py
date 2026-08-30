"""Machine-readable knob registry (single source of truth for the config surface).

Every knob the package understands is listed here with its type, built-in
default, env-var mapping (if any), and documentation group. The knob-doc
sync guard (tests/unit/test_knob_docs.py) verifies this registry stays in
lock-step with the shipped example files (config.example.yml, .env.example).
"""

from __future__ import annotations

from .schema import BUILTIN_SOURCES

# --- groups ------------------------------------------------------------------

GROUP_GLOBAL = "Global"
GROUP_ENDPOINTS = "Endpoints"
GROUP_EMBEDDING = "Embedding"
GROUP_CHUNKING = "Chunking"
GROUP_SCHEDULER = "Scheduler"
GROUP_SOURCES = "Sources"
GROUP_MCP = "MCP"
GROUP_WEB = "Web"

# --- registry ------------------------------------------------------------------
#
# Keys are dotted paths (section.field for nested knobs, bare for top-level).
# Fields per entry:
#   type:    "str" | "int" | "bool"
#   default: built-in default (None = optional/unset)
#   env:     environment variable name, or None when no env mapping exists
#   group:   documentation group (config.example.yml section)

KNOBS: dict[str, dict] = {
    # --- Global ---
    "state_dir": {
        "type": "str",
        "default": "~/.digital-twins",
        "env": "KB_STATE_DIR",
        "group": GROUP_GLOBAL,
    },
    "config_dir": {
        "type": "str",
        "default": "~/.config/digital-twins",
        "env": "KB_CONFIG_DIR",
        "group": GROUP_GLOBAL,
    },

    # --- Endpoints ---
    "qdrant.url": {
        "type": "str",
        "default": None,
        "env": "KB_QDRANT__URL",
        "group": GROUP_ENDPOINTS,
    },
    "qdrant.api_key": {
        "type": "str",
        "default": None,
        "env": "KB_QDRANT__API_KEY",
        "group": GROUP_ENDPOINTS,
    },
    "neo4j.url": {
        "type": "str",
        "default": None,
        "env": "KB_NEO4J__URL",
        "group": GROUP_ENDPOINTS,
    },
    "neo4j.user": {
        "type": "str",
        "default": None,
        "env": "KB_NEO4J__USER",
        "group": GROUP_ENDPOINTS,
    },
    "neo4j.password": {
        "type": "str",
        "default": None,
        "env": "KB_NEO4J__PASSWORD",
        "group": GROUP_ENDPOINTS,
    },
    "llm.endpoint": {
        "type": "str",
        "default": None,
        "env": "KB_LLM__ENDPOINT",
        "group": GROUP_ENDPOINTS,
    },
    "llm.model": {
        "type": "str",
        "default": None,
        "env": "KB_LLM__MODEL",
        "group": GROUP_ENDPOINTS,
    },
    "llm.api_key": {
        "type": "str",
        "default": None,
        "env": "KB_LLM__API_KEY",
        "group": GROUP_ENDPOINTS,
    },

    # --- Embedding ---
    "embedding.model": {
        "type": "str",
        "default": "BAAI/bge-small-en-v1.5",
        "env": "KB_EMBEDDING__MODEL",
        "group": GROUP_EMBEDDING,
    },
    "embedding.device": {
        "type": "str",
        "default": "auto",
        "env": "KB_EMBEDDING__DEVICE",
        "group": GROUP_EMBEDDING,
    },
    "embedding.endpoint": {
        "type": "str",
        "default": None,
        "env": "KB_EMBEDDING__ENDPOINT",
        "group": GROUP_EMBEDDING,
    },
    "embedding.api_key": {
        "type": "str",
        "default": None,
        "env": "KB_EMBEDDING__API_KEY",
        "group": GROUP_EMBEDDING,
    },

    # --- Chunking ---
    "chunking.max_chars": {
        "type": "int",
        "default": 800,
        "env": "KB_CHUNKING__MAX_CHARS",
        "group": GROUP_CHUNKING,
    },
    "chunking.overlap": {
        "type": "int",
        "default": 100,
        "env": "KB_CHUNKING__OVERLAP",
        "group": GROUP_CHUNKING,
    },

    # --- Scheduler ---
    "scheduler.status_port": {
        "type": "int",
        "default": 8765,
        "env": "KB_SCHEDULER__STATUS_PORT",
        "group": GROUP_SCHEDULER,
    },

    # --- MCP ---
    "mcp.port": {
        "type": "int",
        "default": 8770,
        "env": "KB_MCP__PORT",
        "group": GROUP_MCP,
    },
    "mcp.service_account_email": {
        "type": "str",
        "default": "system",
        "env": "KB_MCP__SERVICE_ACCOUNT_EMAIL",
        "group": GROUP_MCP,
    },

    # --- Web ---
    "web.bind": {
        "type": "str",
        "default": "127.0.0.1",
        "env": "KB_WEB__BIND",
        "group": GROUP_WEB,
    },
    "web.port": {
        "type": "int",
        "default": 8767,
        "env": "KB_WEB__PORT",
        "group": GROUP_WEB,
    },
    "web.base_url": {
        "type": "str",
        "default": "http://localhost:8767",
        "env": "KB_WEB__BASE_URL",
        "group": GROUP_WEB,
    },

    # --- Sources (per built-in source: enabled / max_items / timeout_s) ---
}

for _name in BUILTIN_SOURCES:
    KNOBS[f"sources.{_name}.enabled"] = {
        "type": "bool",
        "default": False,
        "env": f"KB_SOURCES__{_name.upper()}__ENABLED",
        "group": GROUP_SOURCES,
    }
    KNOBS[f"sources.{_name}.max_items"] = {
        "type": "int",
        "default": 200,
        "env": f"KB_SOURCES__{_name.upper()}__MAX_ITEMS",
        "group": GROUP_SOURCES,
    }
    KNOBS[f"sources.{_name}.timeout_s"] = {
        "type": "int",
        "default": 1500,
        "env": f"KB_SOURCES__{_name.upper()}__TIMEOUT_S",
        "group": GROUP_SOURCES,
    }

# --- Source credentials + account env vars (non-KB_ env vars, documented in
#     .env.example) ---
# `credential` is the env var holding the required secret. The IMAP mail
# sources also read the account address from a per-provider env var
# (`sources.<name>.email`) — documented here so the knob-doc sync guard
# (SC-002) keeps it in .env.example.
KNOBS["sources.yahoo.credential"] = {
    "type": "str",
    "default": None,
    "env": "YMAIL_APP_PASSWORD",
    "group": GROUP_SOURCES,
}
KNOBS["sources.yahoo.email"] = {
    "type": "str",
    "default": None,
    "env": "YMAIL_EMAIL",
    "group": GROUP_SOURCES,
}
KNOBS["sources.gmail.credential"] = {
    "type": "str",
    "default": None,
    "env": "GMAIL_APP_PASSWORD",
    "group": GROUP_SOURCES,
}
KNOBS["sources.gmail.email"] = {
    "type": "str",
    "default": None,
    "env": "GMAIL_EMAIL",
    "group": GROUP_SOURCES,
}

# --- Custom-source example knobs (documented via the mytool block) ---
# mytool is the documented custom source in config.example.yml; these entries
# keep the sync guard aligned with that example block.
KNOBS["sources.mytool.enabled"] = {
    "type": "bool",
    "default": False,
    "env": None,
    "group": GROUP_SOURCES,
}
KNOBS["sources.mytool.entrypoint"] = {
    "type": "str",
    "default": None,
    "env": None,
    "group": GROUP_SOURCES,
}
KNOBS["sources.mytool.credential"] = {
    "type": "str",
    "default": None,
    "env": "MYTOOL_TOKEN",
    "group": GROUP_SOURCES,
}
KNOBS["sources.mytool.prefix"] = {
    "type": "str",
    "default": "mytool:",
    "env": None,
    "group": GROUP_SOURCES,
}
