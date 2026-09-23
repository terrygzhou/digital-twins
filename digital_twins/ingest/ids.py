"""Deterministic point-ID scheme (research R5, constitution II).

Two schemes coexist (S4 alignment, openspec change s4-graph-alignment):

* Legacy (deprecated): point ID = deterministic function of
  (source prefix, source item key, chunk index, content hash). The
  content hash enters the ID, so re-ingesting *changed* content mints a
  new point. Kept for one release; ``pipeline.py`` uses the S4 scheme.
* S4: ``point_id_s4(channel, item_id, chunk_index)`` =
  ``uuid5(NAMESPACE_DNS, f"kb:{channel}:{item_id}:{chunk_index}")`` —
  content-independent, matching personal-kb's ``kb/core/ids.py`` so both
  systems share one uuid5 ID space. Re-ingesting changed content
  upserts the same point (BR-6.1 parity); ``content_hash`` becomes the
  staleness signal instead of part of the ID.

UUIDv5 keeps the result a valid Qdrant point ID (UUID or uint64)
without any new dependency.
"""

from __future__ import annotations

import hashlib
import uuid

from .deprecation import warn as _deprecation_warn

# --- S4 scheme (current; personal-kb kb/core/ids.py convention) -------------

def point_id_s4(channel: str, item_id: str, chunk_index: int,
                content: str = "") -> str:
    """S4 point ID (UUID string) for one chunk of one source item.

    Content-independent: the optional ``content`` argument is accepted
    only for call-site compatibility and is NOT part of the ID. The
    (channel, item_id, chunk_index) pair is the identity; content
    changes upsert in place instead of minting a new point.
    """
    return str(uuid.uuid5(
        uuid.NAMESPACE_DNS, f"kb:{channel}:{item_id}:{chunk_index}"))


# --- legacy scheme (deprecated by the S4 alignment) ------------------------

def content_hash(text: str) -> str:
    """SHA-256 hex digest of the text (UTF-8).

    .. deprecated:: S4 alignment
        The legacy scheme feeds this *chunk*-text hash into the point
        ID. The S4 scheme's ``SourceItem.content_hash`` / Qdrant payload
        ``content_hash`` hashes *item*-level text and is a separate
        value with a different scope (see
        ``digital_twins/config/deprecation.py``).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def point_id(prefix: str, item_key: str, chunk_index: int, text: str) -> str:
    """Legacy deterministic point ID (UUID string).

    .. deprecated:: S4 alignment
        Content-dependent scheme — re-ingesting changed content mints a
        new point instead of upserting. Use :func:`point_id_s4`. Kept
        for one release.

        Note: the legacy ``content_hash()`` hashes *chunk* text; the S4
        ``SourceItem.content_hash`` / Qdrant payload ``content_hash``
        hashes *item* text — different scopes, not a rename.
    """
    _deprecation_warn("ingest.ids.point_id")
    digest = content_hash(text)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{prefix}|{item_key}|{chunk_index}|{digest}"))
