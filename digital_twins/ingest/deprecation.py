"""In-package deprecation registry (S4 alignment, task 1.2).

One-run deprecation notices for *API surface* (functions / call sites),
as opposed to config-knob deprecation, which lives in
``digital_twins/config/deprecation.py``. Each notice fires at most once
per process (mirroring the config mechanism's ``_ALREADY_WARNED`` set)
so a hot path that calls a deprecated function does not spam.

Registered notices
-------------------
``ingest.ids.point_id`` -> ``ingest.ids.point_id_s4``
    The legacy content-dependent point-ID scheme is superseded by the
    S4 content-independent scheme (openspec change s4-graph-alignment).
    Scope note that callers of the old ``ingest.ids.content_hash()``
    must keep in mind: the legacy function hashes *chunk* text, while
    the S4 ``SourceItem.content_hash`` / Qdrant payload
    ``content_hash`` hashes *item* text — different scopes, not a
    rename.
"""

from __future__ import annotations

import warnings

# Names that have already fired their one-run warning, for this process.
_ALREADY_WARNED: set[str] = set()

# old dotted name -> new dotted name
DEPRECATED: dict[str, str] = {
    "ingest.ids.point_id": "ingest.ids.point_id_s4",
}


def warn(old_name: str) -> None:
    """Fire the one-run ``DeprecationWarning`` for a registered name.

    Unknown names (not in :data:`DEPRECATED`) are silently ignored so
    call sites can always call this without a membership check. Each
    ``old_name`` fires at most once per process.
    """
    new_name = DEPRECATED.get(old_name)
    if new_name is None or old_name in _ALREADY_WARNED:
        return
    _ALREADY_WARNED.add(old_name)
    warnings.warn(
        f"{old_name} is deprecated; use {new_name} instead.",
        DeprecationWarning,
        stacklevel=2,
    )
