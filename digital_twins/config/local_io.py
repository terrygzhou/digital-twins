"""Machine-local configuration writes (kb.local.yml) for the admin UI (008).

The loader is read-only and user_config is per-user DB state — the admin UI
needs a process-level persistence path that merges into the machine-local
layer. This module provides it: atomic deep-merge writes into the resolved
kb.local.yml, nothing else.

``channel_write`` (channels-config) adds a validated channel-admin wrapper:
only ``{"sources": {...}}`` updates are accepted, source names and per-source
keys are validated against the resolved config (fail-fast, BR-11.2.2).
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from .loader import load, local_config_path
from .schema import BUILTIN_SOURCES, SchemaError, SOURCE_DEFAULTS

LOCAL_FILENAME = "kb.local.yml"


def merge_write(updates, target=None, env=None) -> Path:
    """Atomically deep-merge ``updates`` into the machine-local layer.

    Returns the resolved path. The previous file is left intact when the
    existing kb.local.yml is unparseable or the rename fails. An explicit
    ``target`` must sit directly inside the resolved config dir.
    """
    if not isinstance(updates, dict) or not updates:
        raise ValueError("updates must be a non-empty dict")
    resolved = Path(local_config_path(env))
    if target is not None:
        target = Path(target).expanduser()
        if target.parent.resolve() != resolved.parent.resolve():
            raise PermissionError(
                f"{target} is outside the config dir ({resolved.parent})")
        resolved = target
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.exists():
        try:
            current = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(
                f"{resolved}: unparseable YAML, refusing to modify ({exc})") from exc
        if not isinstance(current, dict):
            raise ValueError(f"{resolved}: top level must be a mapping")
    else:
        current = {}
    merged = _deep_merge(current, updates)
    tmp = resolved.with_name(LOCAL_FILENAME + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(merged, fh, sort_keys=False, default_flow_style=False)
        os.replace(tmp, resolved)
    finally:
        if tmp.exists():
            tmp.unlink()
    return resolved


def _deep_merge(base: dict, updates: dict) -> dict:
    out = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def channel_write(updates, target=None, env=None) -> Path:
    """Validate + merge channel (source) updates into the machine-local layer.

    ``updates`` must be ``{"sources": {<name>: {enabled, max_items?,
    timeout_s?, ...}}}``. Source names must exist in the resolved config
    (built-in or registered custom); per-source keys are limited to the
    knob set allowed for that source type by the schema. Everything merges
    into kb.local.yml via :func:`merge_write`, preserving unrelated keys.

    Raises:
        SchemaError: unknown source name, unknown source key, or a
            non-sources / empty / malformed payload (fail-fast, BR-11.2.2).
    """
    if not isinstance(updates, dict) or "sources" not in updates:
        raise SchemaError(
            "channel_write: updates must be {'sources': {...}}")
    sources = updates["sources"]
    if not isinstance(sources, dict) or not sources:
        raise SchemaError(
            "channel_write: 'sources' must be a non-empty mapping")
    resolved = load(env=env)
    known = set(resolved.get("sources", {}))
    for name, raw in sources.items():
        if not isinstance(raw, dict):
            raise SchemaError(
                f"sources.{name}: must be a mapping of source settings")
        if name not in BUILTIN_SOURCES and name not in known:
            raise SchemaError(
                f"sources.{name}: unknown source "
                f"(known: {', '.join(sorted(known))})")
        # mirrors schema._validate_source: allowed_keys =
        # SOURCE_DEFAULTS | {"extra", "prefix", "email", "credential"},
        # plus CUSTOM_SOURCE_DEFAULTS keys for registered custom sources
        allowed = set(SOURCE_DEFAULTS) | {"extra", "prefix", "email", "credential"}
        if name not in BUILTIN_SOURCES:
            allowed |= {"entrypoint"}
        for key in raw:
            if key not in allowed:
                raise SchemaError(
                    f"sources.{name}.{key}: unknown source knob "
                    f"(known: {', '.join(sorted(allowed))})")
    payload = {"sources": {n: dict(e) for n, e in sources.items()}}
    return merge_write(payload, target=target, env=env)
