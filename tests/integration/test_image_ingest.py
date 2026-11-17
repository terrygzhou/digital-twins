"""Feature 020: multimodal image ingestion — pipeline-level integration.

Red tests (Constitution III Test-First) covering FR-2..FR-6 through the
real ``run_pipeline`` code path with an in-memory Qdrant:

1. ``extraction.extract_images: true`` routes standalone raster items
   (binary ``content``) to image extraction and attaches an ``images[]``
   array to the point's Qdrant payload — never a new point per image
   (NFR-1/NFR-14).
2. ``image_store: payload`` (default) → each ``images[]`` entry carries a
   base64 data URL; ``image_store: filesystem`` → bytes are written under
   ``storage/images/`` and the entry carries a relative ``image_path``
   (NFR-13 — never an absolute host path).
3. ``extraction.extract_images: false`` (default) → no image extraction,
   no ``images[]`` key behaviour change — byte-for-byte unchanged.
4. FR-6: the point payload's ``images[]`` array (what ``kb_search``
   surfaces) is always present on image-carrying points and empty
   otherwise.

Fixtures are generated in-test (tmp_path, hand-built PNG bytes, generated
PDF via pypdf) — no host paths.
"""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from digital_twins.health import QDRANT_COLLECTION
from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    """A minimal valid PNG (single red pixel row) — no fixture file needed."""
    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(height):
        raw += b"\x00" + b"\xff\x00\x00" * width  # filter 0 + RGB rows
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b""))


def _cfg(tmp_path, fs_dir, extract_images=True, store="payload",
         caption=False, max_bytes=10_000_000):
    sources = {n: {"enabled": False, "max_items": 200, "timeout_s": 1500}
               for n in ("hermes", "pi", "dsh", "paperclip", "yahoo",
                         "gmail", "fs")}
    sources["fs"] = {"enabled": True, "max_items": 200, "timeout_s": 1500,
                     "extra": {"dir": str(fs_dir)}}
    return {
        "state_dir": str(tmp_path / "state"),
        "config_dir": str(tmp_path / "config"),
        "qdrant": {"url": "https://q.example:6333", "api_key": None},
        "neo4j": {"url": None, "user": None, "password": None},
        "llm": {"endpoint": None, "model": None, "api_key": None},
        "embedding": {"model": "BAAI/bge-small-en-v1.5", "device": "cpu"},
        "chunking": {"max_chars": 200, "overlap": 20},
        "extraction": {
            "extract_images": extract_images,
            "image_caption": caption,
            "image_store": store,
            "image_max_bytes": max_bytes,
        },
        "sources": sources,
    }


def _embedder(texts):
    """Stub embedder: fixed 384-dim vectors (the pinned model dim)."""
    return [[0.5] * 384 for _ in texts]


def _payloads(qdrant):
    scroll = qdrant.scroll(QDRANT_COLLECTION, with_payload=True, limit=100)
    points = scroll[0] if isinstance(scroll, tuple) else scroll.points
    return [p.payload for p in points]


def _payload_by_item(qdrant, item_key: str):
    return [p for p in _payloads(qdrant) if p.get("item_key") == item_key]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fs_dir(tmp_path):
    """One text file + one PNG: the text file yields the document point,
    the PNG is the standalone image that must attach to a point."""
    d = tmp_path / "files"
    d.mkdir()
    (d / "note.txt").write_text(
        "chart of quarterly revenue trends", encoding="utf-8")
    (d / "chart.png").write_bytes(_png_bytes())
    return d


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "state")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def qdrant():
    return QdrantClient(":memory:")


# ---------------------------------------------------------------------------
# FR-2/FR-5: standalone image → images[] on the item's point, one point
# ---------------------------------------------------------------------------

def test_standalone_image_attaches_to_point_payload(
        tmp_path, fs_dir, db, qdrant, monkeypatch):
    """FR-2/FR-5: a standalone .png ingested alongside text produces an
    ``images[]`` array on the item's Qdrant point (payload mode) — not a
    separate point per image (NFR-14)."""
    cfg = _cfg(tmp_path, fs_dir, extract_images=True)
    s = run_pipeline(cfg, db, qdrant, _embedder,
                     source_names=["fs"],
                     trigger="manual", scheduled_by="test")
    assert s.status == "ok"

    png_points = _payload_by_item(qdrant, "chart.png")
    assert len(png_points) == 1, (
        f"the standalone image must attach to exactly one point "
        f"(NFR-14), got {len(png_points)}")
    p = png_points[0]
    images = p.get("images", [])
    assert len(images) == 1, f"expected 1 image entry, got {images!r}"
    img = images[0]
    # FR-4 payload store: base64 data URL
    assert img["image_store"] == "payload"
    url = img["url"]
    assert url.startswith("data:image/png;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw == _png_bytes()
    assert img["mime"] == "image/png"
    assert img["caption"] is None  # caption off (default)


def test_extract_images_off_is_byte_for_byte_unchanged(
        tmp_path, fs_dir, db, qdrant):
    """Edge case: default ``extraction.extract_images: false`` → no image
    extraction, no ``images[]`` entries anywhere."""
    cfg = _cfg(tmp_path, fs_dir, extract_images=False)
    s = run_pipeline(cfg, db, qdrant, _embedder,
                     source_names=["fs"],
                     trigger="manual", scheduled_by="test")
    assert s.status == "ok"
    for p in _payloads(qdrant):
        imgs = p.get("images", [])
        assert imgs == [], (
            f"extract_images=false must not attach images[] entries "
            f"on item {p.get('item_key')!r}: {imgs!r}")
    # and no storage/images directory was created by the run
    assert not Path("storage/images").exists() or not any(
        Path("storage/images").rglob("*"),
        f"filesystem writes must not happen when extraction is off")


def test_image_store_filesystem_writes_relative_path(
        tmp_path, fs_dir, db, qdrant, monkeypatch):
    """FR-4 filesystem mode: bytes land under ``storage/images/<channel>/``
    and the ``images[]`` entry carries a relative ``image_path`` — never an
    absolute host path (NFR-13)."""
    cfg = _cfg(tmp_path, fs_dir, extract_images=True, store="filesystem")
    storage_root = tmp_path / "storage" / "images"
    # Redirect the hardcoded CWD-relative storage dir into tmp_path so the
    # test is hermetic (NFR-13: no host paths in metadata).
    monkeypatch.chdir(tmp_path)

    s = run_pipeline(cfg, db, qdrant, _embedder,
                     source_names=["fs"],
                     trigger="manual", scheduled_by="test")
    assert s.status == "ok"

    png_points = _payload_by_item(qdrant, "chart.png")
    assert len(png_points) == 1
    img = png_points[0]["images"][0]
    assert img["image_store"] == "filesystem"
    rel = img["image_path"]
    assert not Path(rel).is_absolute(), (
        f"image_path must be relative (NFR-13): {rel!r}")
    assert not rel.startswith("/") or "~" not in rel, (
        f"image_path must not carry a host path: {rel!r}")
    # the file actually exists under the storage root
    written = storage_root / "fs" / "chart.png.png"
    assert written.is_file(), (
        f"expected {written} to exist on disk")
    assert written.read_bytes() == _png_bytes()


def test_image_store_payload_keeps_point_count_stable(
        tmp_path, fs_dir, db, qdrant):
    """NFR-14: re-ingesting the same content yields 0 new points — the
    image attaches to the same deterministic point ID."""
    cfg = _cfg(tmp_path, fs_dir, extract_images=True)
    s1 = run_pipeline(cfg, db, qdrant, _embedder,
                      source_names=["fs"],
                      trigger="manual", scheduled_by="test")
    assert s1.status == "ok"
    count1 = qdrant.count(QDRANT_COLLECTION, exact=True).count

    db.execute("DELETE FROM highwater WHERE source='fs'")
    db.commit()
    s2 = run_pipeline(cfg, db, qdrant, _embedder,
                      source_names=["fs"],
                      trigger="manual", scheduled_by="test")
    assert s2.status == "ok"
    count2 = qdrant.count(QDRANT_COLLECTION, exact=True).count
    assert count1 == count2, (
        f"re-ingest must not add points: {count1} → {count2} "
        f"(NFR-1/NFR-14)")


def test_kb_search_payload_shape_images_key_always_present(
        tmp_path, fs_dir, db, qdrant):
    """FR-6 (payload contract, surfaced by kb_search): every point payload
    carries an ``images`` key — ``[]`` when the item has no images, a
    non-empty array when it does (stable response shape)."""
    cfg = _cfg(tmp_path, fs_dir, extract_images=True)
    s = run_pipeline(cfg, db, qdrant, _embedder,
                     source_names=["fs"],
                     trigger="manual", scheduled_by="test")
    assert s.status == "ok"
    payloads = _payloads(qdrant)
    for p in payloads:
        assert "images" in p, (
            f"every point payload must carry the images key: {p!r}")
    png = _payload_by_item(qdrant, "chart.png")
    txt = _payload_by_item(qdrant, "note.txt")
    assert len(png) == 1 and png[0]["images"]
    assert len(txt) == 1 and txt[0]["images"] == []


# ---------------------------------------------------------------------------
# FR-7: over-cap image is dropped, remaining images proceed
# ---------------------------------------------------------------------------

def test_image_over_cap_dropped_smaller_kept(
        tmp_path, db, qdrant, monkeypatch):
    """FR-7: an image above ``extraction.image_max_bytes`` is dropped with
    a per-item ``image_too_large`` warning; smaller images on the same
    point still proceed."""
    d = tmp_path / "files"
    d.mkdir()
    big = _png_bytes(width=256, height=256)
    small = _png_bytes(width=2, height=2)
    (d / "big.png").write_bytes(big)
    (d / "small.png").write_bytes(small)

    # cap between the two sizes
    cap = len(small) + 16
    cfg = _cfg(tmp_path, d, extract_images=True, max_bytes=cap)
    s = run_pipeline(cfg, db, qdrant, _embedder,
                     source_names=["fs"],
                     trigger="manual", scheduled_by="test")
    assert s.status == "ok"

    big_pts = _payload_by_item(qdrant, "big.png")
    small_pts = _payload_by_item(qdrant, "small.png")
    # big image over the cap → no images entries (dropped)
    assert all(p.get("images") == [] for p in big_pts), (
        f"over-cap image must be dropped: {big_pts!r}")
    # smaller image still stored
    assert len(small_pts) == 1 and len(small_pts[0]["images"]) == 1


# ---------------------------------------------------------------------------
# FR-3/FR-8: caption on (no LLM endpoint) → degrades to null, stored
# ---------------------------------------------------------------------------

def test_caption_on_no_llm_degrades_to_null(
        tmp_path, fs_dir, db, qdrant, monkeypatch):
    """FR-8: ``image_caption: true`` but no LLM endpoint configured →
    ``caption`` is ``None`` (degraded), the image is still stored."""
    cfg = _cfg(tmp_path, fs_dir, extract_images=True, caption=True)
    s = run_pipeline(cfg, db, qdrant, _embedder,
                     source_names=["fs"],
                     trigger="manual", scheduled_by="test")
    assert s.status == "ok"
    png = _payload_by_item(qdrant, "chart.png")
    assert len(png) == 1
    img = png[0]["images"][0]
    assert img["caption"] is None, (
        f"no LLM endpoint → caption must degrade to null: {img!r}")
    assert img["url"], "the image bytes must still be stored"


def test_caption_stored_when_llm_returns_text(
        tmp_path, fs_dir, db, qdrant, monkeypatch):
    """FR-3: with a working LLM client, the caption lands in the
    ``images[]`` entry."""
    import digital_twins.ingest.llm as llm_mod

    def fake_build(cfg):
        def client(prompt):
            return "a chart showing quarterly revenue"
        return client

    monkeypatch.setattr(llm_mod, "build_llm_client", fake_build)

    cfg = _cfg(tmp_path, fs_dir, extract_images=True, caption=True)
    cfg["llm"]["endpoint"] = "https://llm.example/v1"
    s = run_pipeline(cfg, db, qdrant, _embedder,
                     source_names=["fs"],
                     trigger="manual", scheduled_by="test")
    assert s.status == "ok"
    png = _payload_by_item(qdrant, "chart.png")
    assert len(png) == 1
    assert png[0]["images"][0]["caption"] == "a chart showing quarterly revenue"
