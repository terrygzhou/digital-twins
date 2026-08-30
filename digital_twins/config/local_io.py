"""Machine-local configuration writes (kb.local.yml) for the admin UI (008).

The loader is read-only and user_config is per-user DB state — the admin UI
needs a process-level persistence path that merges into the machine-local
layer. This module provides it: atomic deep-merge writes into the resolved
kb.local.yml, nothing else.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from .loader import local_config_path

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
