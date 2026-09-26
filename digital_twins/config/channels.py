"""Channel (source) admin helpers: effective per-source channel view.

Used by the CLI ``channels`` group and the web admin surface (channels-config
plan, task 1). Resolves the four-layer config and reports, per channel, the
effective knob values plus credential-set booleans and prerequisites.

Credentials are secrets (BR-12.2.2): only ``credential_set`` booleans are
exposed — never the credential value.
"""
from __future__ import annotations

import os

from . import loader as _loader
from .schema import BUILTIN_SOURCES, SOURCE_DEFAULTS

__all__ = ["channel_view"]


def channel_view(config_dir=None, env=None) -> dict:
    """Return the effective channel view for every registered source.

    Args:
        config_dir: config directory (default: resolved from env/defaults).
        env: explicit environment mapping (``.env`` files ignored). When
            given, this mapping is authoritative for the credential-set
            check; when ``env`` is None, the real process environment
            (``os.environ``) is consulted instead.

    Returns:
        ``{source_name: {"enabled": bool, "max_items": int, "timeout_s": int,
        "credential_set": bool, "prerequisites": [str, ...]}}`` for every
        built-in source (``BUILTIN_SOURCES``) plus any custom source
        registered in the resolved config (``sources.<name>`` carrying an
        ``entrypoint``). ``prerequisites`` is ``[]`` when the channel is
        ready; a broken entrypoint (unimportable module, bad factory) shows
        up as a single summary string naming the failure (fail-fast,
        BR-11.2.2).
    """
    cfg = _loader.load(config_dir=config_dir, env=env)
    view = {}
    for name, entry in cfg["sources"].items():
        if name not in BUILTIN_SOURCES and not entry.get("entrypoint"):
            continue
        view[name] = _channel_row(name, entry, env=env)
    return view


def _channel_row(name: str, entry: dict, env=None) -> dict:
    from ..sources import build as _build  # lazy: keep config layer light

    row = {
        "enabled": bool(entry.get("enabled", SOURCE_DEFAULTS["enabled"])),
        "max_items": int(entry.get("max_items", SOURCE_DEFAULTS["max_items"])),
        "timeout_s": int(entry.get("timeout_s", SOURCE_DEFAULTS["timeout_s"])),
    }
    source = None
    try:
        source = _build(name, dict(entry))
    except Exception as exc:
        row["prerequisites"] = [f"{type(exc).__name__}: {exc}"]
    else:
        try:
            row["prerequisites"] = list(source.prerequisites())
        except Exception as exc:
            row["prerequisites"] = [f"{type(exc).__name__}: {exc}"]

    credential = ""
    if source is not None:
        credential = getattr(source.capability, "credential", None) or ""
    if not credential:
        # custom sources declare their credential env var in config
        credential = str(entry.get("credential") or "")
    if credential:
        if env is not None:
            row["credential_set"] = bool(env.get(credential, ""))
        else:
            row["credential_set"] = bool(os.environ.get(credential, ""))
    else:
        row["credential_set"] = True  # no credential declared -> ready
    # Non-secret per-source knobs the UI may surface (email address,
    # IMAP host).  These are NOT credentials — they are user-visible
    # account identifiers that the imap_mail source requires.
    row["email"] = str(entry.get("email") or "")
    extra = entry.get("extra") or {}
    row["imap_host"] = str(extra.get("imap_host") or "")
    return row
