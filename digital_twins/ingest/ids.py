"""Deterministic point-ID scheme (research R5, constitution II).

Point ID = deterministic function of (source prefix, source item key,
chunk index, content hash). Same item + same content + same chunk => same
ID across every trigger (schedule / run / mcp / ui), so Qdrant upserts are
idempotent. UUIDv5 keeps the result a valid Qdrant point ID (UUID or uint64)
without any new dependency.
"""

from __future__ import annotations

import hashlib
import uuid


def content_hash(text: str) -> str:
    """SHA-256 hex digest of the chunk text (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def point_id(prefix: str, item_key: str, chunk_index: int, text: str) -> str:
    """Deterministic point ID (UUID string) for one chunk of one source item."""
    digest = content_hash(text)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{prefix}|{item_key}|{chunk_index}|{digest}"))
