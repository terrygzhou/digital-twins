"""Typed configuration schema (BR-11.6.4, contracts/config-schema.md).

Single source of truth for the knob surface: names, types, defaults, and
env-var mappings. The four-layer loader (config/loader.py) and the knob-sync
guard both read from this registry. A knob that is not defined here does
not exist.
"""

from __future__ import annotations

import re

# --- knob registry ---------------------------------------------------------
#
# value semantics: None = "optional / unset" (no default), empty string
# "" = string knob, 0 = integer knob.  Nested keys are `section.field`.

DEFAULTS: dict = {
    "state_dir": "~/.digital-twins",
    "config_dir": "~/.config/digital-twins",
    # endpoints (user-supplied; init prompts for these) — unset by default
    "qdrant.url": None,
    "qdrant.api_key": None,
    "neo4j.url": None,
    "neo4j.user": None,
    "neo4j.password": None,
    "llm.endpoint": None,
    "llm.model": None,
    "llm.api_key": None,
    # extraction (s4-entity-extraction; LLM entity extraction, config-gated)
    "extraction.enabled": False,
    "extraction.max_text_chars": 12000,
    "extraction.prompt_version": "",
    # embedding — model version pinned by the package
    "embedding.model": "BAAI/bge-small-en-v1.5",
    "embedding.device": "auto",
    "embedding.endpoint": None,
    "embedding.api_key": None,
    # chunking
    "chunking.max_chars": 800,
    "chunking.overlap": 100,
    # scheduler
    "scheduler.status_port": 8765,
}

# string knobs that accept empty values (endpoints are placeholders until init)
_ALLOW_EMPTY: frozenset = frozenset({
    "qdrant.url", "qdrant.api_key",
    "neo4j.url", "neo4j.user", "neo4j.password",
    "llm.endpoint", "llm.model", "llm.api_key",
    "extraction.prompt_version",
    "embedding.endpoint", "embedding.api_key",
})

# Knobs with an int type that aren't in the legacy DEFAULTS registry
# (008/US2 — the KNOBS registry declares these as "int"; _declared_type
# consults this set to coerce them correctly).
_INT_KNOBS: frozenset = frozenset({
    "mcp.port", "web.port",
    "scheduler.run_hour", "scheduler.run_minute", "scheduler.run_minute_jitter_s",
})

EMBEDDING_DEVICES: tuple = ("auto", "cpu", "cuda")

# built-in source names (contract: Sources section)
BUILTIN_SOURCES: tuple = (
    "hermes", "pi", "dsh", "paperclip", "yahoo", "gmail", "fs",
)

# per-source defaults applied to every built-in source on a fresh install
SOURCE_DEFAULTS: dict = {
    "enabled": False,       # BR-11.2.7 — all disabled on a fresh install
    "max_items": 200,       # per-run cap (baseline 200 cap preserved)
    "timeout_s": 1500,      # per-source timeout
}

# custom (user-defined) source defaults
CUSTOM_SOURCE_DEFAULTS: dict = {
    "entrypoint": "",       # module:factory
    "credential": "",       # env-var name the source requires
}

SCHEMA: dict = {
    "defaults": DEFAULTS,
    "allow_empty": _ALLOW_EMPTY,
    "embedding_devices": EMBEDDING_DEVICES,
    "builtin_sources": BUILTIN_SOURCES,
    "source_defaults": SOURCE_DEFAULTS,
    "custom_source_defaults": CUSTOM_SOURCE_DEFAULTS,
}


# --- env-var mapping --------------------------------------------------------

_ENV_PREFIX = "KB_"
_NEST = "__"


def env_var_for(path: str) -> str:
    """`qdrant.url` -> `KB_QDRANT__URL`; `state_dir` -> `KB_STATE_DIR`."""
    return _ENV_PREFIX + path.upper().replace(".", _NEST)


def env_path_for(name: str) -> str:
    """Inverse of env_var_for; None when the name is not a KB_ var."""
    if not name.startswith(_ENV_PREFIX) or len(name) <= len(_ENV_PREFIX):
        return None
    body = name[len(_ENV_PREFIX):].lower()
    return body.replace(_NEST, ".")


# --- value helpers ----------------------------------------------------------

def _type_of(default):
    if default is None:
        return str
    if isinstance(default, bool):
        return bool
    if isinstance(default, int):
        return int
    return str


# per-source knob types (the contract's `sources.<name>.*` rows)
_SOURCE_KNOB_TYPES = {
    "enabled": bool,
    "max_items": int,
    "timeout_s": int,
    "prefix": str,
    "entrypoint": str,
    "credential": str,
    "email": str,
}


def _declared_type(path: str):
    default = DEFAULTS.get(path)
    if default is not None:
        if isinstance(default, bool):
            return bool
        if isinstance(default, int):
            return int
        return str
    if path in _INT_KNOBS:
        return int
    if path.startswith("sources."):
        return _SOURCE_KNOB_TYPES.get(path.rsplit(".", 1)[-1], str)
    return str


# Knobs with a minimum value (fail-fast on out-of-range, principle IV).
# 0 is a sentinel meaning "disabled" — it is NOT a minimum here; the minimum
# is the value itself (0 is allowed).
_MIN_VALUE: dict = {
    "scheduler.status_port": 0,
}


def coerce(path: str, value):
    """Coerce one value to the type declared for the knob.

    `None`/missing always coerces to `None` (unset). Raises SchemaError on
    values that cannot be interpreted in the declared type. Empty strings
    are rejected for string knobs unless the knob allows empty values.
    """
    if value is None:
        return None
    target = _declared_type(path)
    if target is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low == "true":
                return True
            if low == "false":
                return False
        raise SchemaError(f"{path}: expected true/false, got {value!r}")
    if target is int:
        if isinstance(value, int) and not isinstance(value, bool):
            result = value
        elif isinstance(value, str):
            try:
                result = int(value.strip())
            except ValueError:
                raise SchemaError(f"{path}: must be an integer, got {value!r}")
        else:
            raise SchemaError(f"{path}: must be an integer, got {value!r}")
        if path in _MIN_VALUE and result < _MIN_VALUE[path]:
            raise SchemaError(
                f"{path}: must be >= {_MIN_VALUE[path]}, got {value!r}"
            )
        return result
    if isinstance(value, str):
        if value == "" and path not in _ALLOW_EMPTY:
            raise SchemaError(f"{path}: must be a non-empty string")
        return value
    raise SchemaError(f"{path}: must be a string, got {value!r}")


def get(cfg: dict, path: str):
    """`get(cfg, "qdrant.url")` -> value (None when unset)."""
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# --- validation -------------------------------------------------------------

class SchemaError(Exception):
    """Configuration value or structure is invalid (fail-fast, SC-001)."""


def _validate_source(path: str, raw: dict, custom: bool):
    allowed_keys = set(SOURCE_DEFAULTS) | {"extra", "prefix", "email", "credential"}
    if custom:
        allowed_keys |= set(CUSTOM_SOURCE_DEFAULTS)
    if not isinstance(raw, dict):
        raise SchemaError(f"{path}: must be a mapping of source settings")
    out = {}
    for key, value in raw.items():
        if key not in allowed_keys:
            raise SchemaError(
                f"{path}.{key}: unknown source knob "
                f"(known: {', '.join(sorted(allowed_keys))})"
            )
        if key == "enabled":
            out[key] = coerce(f"{path}.enabled", value)
        elif key in ("max_items", "timeout_s"):
            out[key] = coerce(f"{path}.{key}", value)
        elif key in ("prefix", "entrypoint", "credential", "email"):
            if not isinstance(value, str):
                raise SchemaError(f"{path}.{key}: must be a string")
            out[key] = value
        elif key == "extra":
            if not isinstance(value, dict):
                raise SchemaError(f"{path}.extra: must be a mapping")
            out[key] = value
    defaults = CUSTOM_SOURCE_DEFAULTS if custom else {}
    defaults = {**SOURCE_DEFAULTS, **defaults}
    if not custom:
        defaults = dict(SOURCE_DEFAULTS)
        defaults["prefix"] = path.rsplit(".", 1)[-1] + ":"
    for key, default in defaults.items():
        out.setdefault(key, default)
    out.setdefault("prefix", path.rsplit(".", 1)[-1] + ":")
    return out


def validate(cfg: dict) -> dict:
    """Validate + normalize a fully-merged config dict; return a clean copy.

    Unknown keys raise SchemaError (fail-fast). Every section with a default
    (qdrant, neo4j, llm, embedding, chunking, scheduler) is always present in
    the returned dict — even when omitted from the input — with each sub-key
    default-filled; unset optional values in those sections stay None. Every
    built-in source is always present (disabled by default).
    """
    if not isinstance(cfg, dict):
        raise SchemaError("config root must be a mapping")
    known_sections = {
        "state_dir", "config_dir", "qdrant", "neo4j", "llm",
        "extraction",
        "embedding", "chunking", "scheduler", "sources",
        "mcp", "web",
    }
    out = {}
    for key, value in cfg.items():
        if key not in known_sections:
            raise SchemaError(
                f"{key}: unknown config key "
                f"(known: {', '.join(sorted(known_sections))})"
            )
        if key == "sources":
            if not isinstance(value, dict):
                raise SchemaError("sources: must be a mapping of source names")
            out["sources"] = {}
            for name, raw in value.items():
                out["sources"][name] = _validate_source(
                    f"sources.{name}", raw, custom=name not in BUILTIN_SOURCES
                )
        elif key in ("state_dir", "config_dir"):
            out[key] = coerce(key, value)
        elif key in ("qdrant", "neo4j", "llm", "extraction", "embedding",
                     "chunking", "scheduler", "mcp", "web"):
            if not isinstance(value, dict):
                raise SchemaError(f"{key}: must be a mapping")
            # For sections not in the legacy DEFAULTS registry (mcp, web),
            # build the known sub-keys from the KNOBS registry instead.
            known_subs = {p.split(".", 1)[1] for p in DEFAULTS
                          if p.startswith(key + ".")}
            if not known_subs:
                from .knobs import KNOBS as _knoabs_registry
                known_subs = {p.split(".", 1)[1] for p in _knoabs_registry
                              if p.startswith(key + ".")}
            section = {}
            for sub, sub_value in value.items():
                if sub not in known_subs:
                    raise SchemaError(
                        f"{key}.{sub}: unknown config key "
                        f"(known: {', '.join(sorted(known_subs))})"
                    )
                section[sub] = coerce(f"{key}.{sub}", sub_value)
            for sub, default in DEFAULTS.items():
                if sub.startswith(key + ".") and sub.split(".", 1)[1] not in section:
                    sub_key = sub.split(".", 1)[1]
                    section[sub_key] = coerce(f"{key}.{sub_key}", default)
            # For sections not in DEFAULTS (mcp, web): fill from KNOBS
            # registry defaults.
            if not {p for p in DEFAULTS if p.startswith(key + ".")}:
                from .knobs import KNOBS as _knobs_registry
                for dotted, entry in _knobs_registry.items():
                    if dotted.startswith(key + "."):
                        sub_key = dotted.split(".", 1)[1]
                        if sub_key not in section:
                            section[sub_key] = entry["default"]
            out[key] = section
    # every section with a default is always present, even when omitted
    for section_key in ("qdrant", "neo4j", "llm", "extraction", "embedding",
                        "chunking", "scheduler", "mcp", "web"):
        out.setdefault(section_key, {})
        for sub, default in DEFAULTS.items():
            if sub.startswith(section_key + "."):
                sub_key = sub.split(".", 1)[1]
                out[section_key].setdefault(sub_key, coerce(f"{section_key}.{sub_key}", default))
        # Sections not in DEFAULTS (mcp, web): fill from KNOBS registry.
        if not {p for p in DEFAULTS if p.startswith(section_key + ".")}:
            from .knobs import KNOBS as _knobs_registry
            for dotted, entry in _knobs_registry.items():
                if dotted.startswith(section_key + "."):
                    sub_key = dotted.split(".", 1)[1]
                    out[section_key].setdefault(sub_key, entry["default"])
    # embedding.device must be one of the documented values
    device = get(out, "embedding.device")
    if device is not None and device not in EMBEDDING_DEVICES:
        raise SchemaError(
            f"embedding.device {device!r} must be one of "
            f"{'|'.join(EMBEDDING_DEVICES)}"
        )
    # every built-in source is present (disabled by default, BR-11.2.7)
    out.setdefault("sources", {})
    for builtin in BUILTIN_SOURCES:
        out["sources"].setdefault(
            builtin, _validate_source(f"sources.{builtin}", {}, custom=False)
        )
    # cross-field check
    max_chars = get(out, "chunking.max_chars")
    overlap = get(out, "chunking.overlap")
    if max_chars is not None and overlap is not None and overlap >= max_chars:
        raise SchemaError(
            f"chunking.overlap ({overlap}) must be less than "
            f"chunking.max_chars ({max_chars})"
        )
    return out
