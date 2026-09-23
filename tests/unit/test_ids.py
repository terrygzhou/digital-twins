"""Deterministic point IDs + config-driven chunking (T011)."""

import uuid

import pytest

from digital_twins.config import SchemaError, load
from digital_twins.ingest.chunking import chunk_text
from digital_twins.ingest.ids import content_hash, point_id


# --- point IDs -------------------------------------------------------------

def test_point_id_is_valid_uuid_and_deterministic():
    a = point_id("fs:", "notes/a.txt", 0, "hello world")
    b = point_id("fs:", "notes/a.txt", 0, "hello world")
    assert a == b
    uuid.UUID(a)  # must parse as a UUID


def test_point_id_varies_with_every_input():
    base = point_id("fs:", "k", 0, "text")
    assert point_id("fs:", "k", 0, "text-changed") != base   # content
    assert point_id("fs:", "k2", 0, "text") != base          # item key
    assert point_id("fs:", "k", 1, "text") != base           # chunk index
    assert point_id("dsh:", "k", 0, "text") != base           # prefix


def test_content_hash_is_sha256_hex():
    assert len(content_hash("x")) == 64
    assert content_hash("x") != content_hash("y")


# --- chunking ---------------------------------------------------------------

def test_short_text_is_single_chunk():
    assert chunk_text("hello", 800, 100) == ["hello"]


def test_empty_text_yields_no_chunks():
    assert chunk_text("", 800, 100) == [""]


def test_chunks_respect_max_chars_and_overlap_exact():
    text = " ".join(f"word{i}" for i in range(400))
    chunks = chunk_text(text, 800, 100)
    assert len(chunks) > 1
    assert all(len(c) <= 800 for c in chunks)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt[:100] == prev[-100:]


def test_split_prefers_word_boundary():
    # window end (50) lands inside the y-run; the cut must move back to
    # the single space at position 48 instead of splitting mid-word
    text = "x" * 48 + " " + "y" * 48  # 97 chars
    chunks = chunk_text(text, 50, 0)
    assert chunks[0] == "x" * 48
    assert len(chunks[1]) == 49
    assert all(len(c) <= 50 for c in chunks)


def test_overlap_ge_max_chars_rejected():
    with pytest.raises(ValueError):
        chunk_text("any text at all here", 10, 10)
    with pytest.raises(ValueError):
        chunk_text("any text at all here", 10, 11)
    with pytest.raises(ValueError):
        chunk_text("text", 0, 0)


def test_overlap_equal_to_max_chars_rejected_by_schema(tmp_path):
    (tmp_path / "kb.yml").write_text(
        "chunking:\n  max_chars: 100\n  overlap: 100\n", encoding="utf-8")
    with pytest.raises(SchemaError):
        load(cwd=tmp_path, config_dir=tmp_path)

# --- S4 point-ID scheme (s4-graph-alignment task 1.1) -----------------------
# The content-independent uuid5(NAMESPACE_DNS, "kb:{channel}:{item_id}:{idx}")
# scheme is the personal-kb kb/core/ids.py convention: it shares one uuid5
# ID space across the two systems so re-ingest upserts in place instead of
# minting a new point when content changes.

from digital_twins.ingest.ids import point_id_s4


def test_point_id_s4_is_valid_uuid_and_deterministic():
    a = point_id_s4("fs", "notes/a.txt", 0)
    b = point_id_s4("fs", "notes/a.txt", 0)
    assert a == b
    uuid.UUID(a)


def test_point_id_s4_is_content_independent():
    # The whole point of S4: content change no longer mints a new point ID.
    assert point_id_s4("fs", "k", 0) == point_id_s4("fs", "k", 0)
    assert point_id_s4("fs", "k", 0, content="one") == \
        point_id_s4("fs", "k", 0, content="two")
    # but channel / item_id / chunk_index still vary the ID
    assert point_id_s4("fs", "k", 0) != point_id_s4("fs", "k", 1)
    assert point_id_s4("fs", "k", 0) != point_id_s4("hermes", "k", 0)
    assert point_id_s4("fs", "k", 0) != point_id_s4("fs", "k2", 0)


def test_point_id_s4_namespace_is_dns():
    """The ID space must match personal-kb: uuid5 over NAMESPACE_DNS."""
    expected = str(uuid.uuid5(
        uuid.NAMESPACE_DNS, "kb:fs:a:0"))
    assert point_id_s4("fs", "a", 0) == expected


def test_point_id_s4_no_cross_system_collision_with_pk_string_form():
    """personal-kb mints `uuid5(NAMESPACE_DNS, f"kb:{channel}:{item_id}:"
    f"{chunk_index}")`; a different (channel, item_id) pair must never
    collide. digital-twins `fs` vs personal-kb `files` stay distinct by
    construction (the recorded cross-system collision decision)."""
    pk_files = str(uuid.uuid5(
        uuid.NAMESPACE_DNS, "kb:files:a:0"))
    assert point_id_s4("fs", "a", 0) != pk_files
    pk_sessions = str(uuid.uuid5(
        uuid.NAMESPACE_DNS, "kb:sessions:msg1:0"))
    assert point_id_s4("session", "msg1", 0) != pk_sessions


def test_legacy_point_id_fires_one_run_deprecation_warning(monkeypatch):
    """Task 1.2: the legacy content-dependent point_id is deprecated in
    favor of point_id_s4 — one-run DeprecationWarning naming the
    replacement (mirror of the config-knob mechanism)."""
    import warnings
    from digital_twins.ingest import deprecation as dep
    monkeypatch.delattr(dep, "_ALREADY_WARNED", raising=False)
    dep._ALREADY_WARNED = set()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        point_id("fs:", "a", 0, "x")
        assert len(caught) == 1
        assert issubclass(caught[0].category, DeprecationWarning)
        assert "point_id_s4" in str(caught[0].message)
    # second call in the same process: no re-warn
    with warnings.catch_warnings(record=True) as caught2:
        warnings.simplefilter("always")
        point_id("fs:", "a", 0, "x")
        assert caught2 == []
