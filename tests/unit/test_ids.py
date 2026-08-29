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
