"""020 feature: image extraction from files and PDFs.

Covers FR-1 (the module), FR-7 (image_max_bytes cap), and FR-8's
warning-isolation contract (caption failure never loses the image).

Tests use in-memory PDFs (reportlab) and in-memory PNGs — no host
paths, no fixtures on disk beyond tmp_path.
"""

from __future__ import annotations

import io
import struct
import zlib

import pytest

from digital_twins.ingest.extract_images import (
    ImageExtraction,
    MIME_BY_SUFFIX,
    SUFFIXES,
    extract_images,
)


# --- tiny PNG builder -------------------------------------------------------

def _png_1x1_red() -> bytes:
    """A 1x1 red PNG, built by hand (no PIL dependency)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR",
                 struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    # filter byte 0 + one red pixel (RGB)
    raw = b"\x00\xff\x00\x00"
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def _make_pdf_with_image(tmp_path) -> "pathlib.Path":
    """Build a one-page PDF with one embedded PNG figure in-memory.

    Uses reportlab (017's dep).  The PNG is written to a temp file so
    reportlab's drawImage (which needs a file path) can embed it.  The
    PDF is returned as a Path under tmp_path so extract_images can
    open it.
    """
    import pathlib

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    out = str(tmp_path / "sample.pdf")
    png = _png_1x1_red()
    # reportlab's drawImage requires a file path (PathLike), not a
    # PIL object or a bytes object.
    png_path = str(tmp_path / "fig.png")
    (tmp_path / "fig.png").write_bytes(png)

    c = canvas.Canvas(out, pagesize=A4)
    c.drawString(100, 700, "Figure 1: a test image")
    c.drawImage(str(png_path), 100, 500, width=200, height=200)
    c.showPage()
    c.save()
    return out


# --- FR-1: module API -------------------------------------------------------

def test_extract_images_returns_dataclass_list():
    import pathlib
    p = pathlib.Path("x.png")
    result = extract_images(p, "png", None)
    assert isinstance(result, list)
    # No file exists → empty list (file read happens on the first item
    # only when the file is actually present; absent file → no images).
    assert result == []


def test_image_extraction_dataclass_fields():
    ext = ImageExtraction(
        bytes=b"pngdata", mime="image/png", source_kind="standalone",
        page=None)
    assert ext.bytes == b"pngdata"
    assert ext.mime == "image/png"
    assert ext.source_kind == "standalone"
    assert ext.page is None


def test_supported_suffixes():
    assert ".png" in SUFFIXES
    assert ".jpg" in SUFFIXES
    assert ".jpeg" in SUFFIXES
    assert ".webp" in SUFFIXES
    assert ".gif" in SUFFIXES
    assert "image/png" in MIME_BY_SUFFIX[".png"]
    assert "image/jpeg" in MIME_BY_SUFFIX[".jpg"]


def test_standalone_png_extraction(tmp_path):
    import pathlib

    p = tmp_path / "t.png"
    p.write_bytes(_png_1x1_red())
    result = extract_images(p, "png", None)
    assert len(result) == 1
    ext = result[0]
    assert isinstance(ext, ImageExtraction)
    assert ext.bytes == _png_1x1_red()
    assert ext.mime == "image/png"
    assert ext.source_kind == "standalone"
    assert ext.page is None


def test_standalone_jpeg_mime(tmp_path):
    p = tmp_path / "t.jpg"
    p.write_bytes(b"\xff\xd8\xff\xdbfakejpeg")
    result = extract_images(p, "jpg", None)
    assert len(result) == 1
    assert result[0].mime == "image/jpeg"


# --- FR-7: image_max_bytes cap ----------------------------------------------

def test_image_too_large_dropped(tmp_path):
    p = tmp_path / "big.png"
    p.write_bytes(b"z" * 100)  # 100 bytes
    result = extract_images(p, "png", None,
                           max_bytes=50)
    # Over the cap → dropped (empty list), not an exception.
    assert result == []


def test_image_within_cap_kept(tmp_path):
    p = tmp_path / "ok.png"
    p.write_bytes(_png_1x1_red())
    result = extract_images(p, "png", None,
                           max_bytes=100_000)
    assert len(result) == 1


# --- PDF extraction ---------------------------------------------------------

@pytest.fixture(scope="module")
def _reportlab():
    try:
        import reportlab  # noqa: F401
        return True
    except ImportError:
        return False


def test_pdf_embedded_image_extraction(tmp_path, _reportlab):
    if not _reportlab:
        pytest.skip("reportlab not installed")
    import pathlib

    pdf = _make_pdf_with_image(tmp_path)
    result = extract_images(pdf, "pdf", None)
    # pypdf finds the embedded image XObject.
    assert len(result) >= 1
    ext = result[0]
    assert ext.source_kind == "pdf"
    assert ext.page is not None
    assert isinstance(ext.mime, str) and ext.mime.startswith("image/")


# --- FR-8: warning-isolation contract --------------------------------------

def test_caption_failure_does_not_lose_image(tmp_path, monkeypatch):
    """When the LLM endpoint is absent/unconfigured, caption is None
    but the image bytes are still returned (the run continues)."""
    p = tmp_path / "c.png"
    p.write_bytes(_png_1x1_red())
    # cfg with image_caption=True but llm.endpoint=None → the module
    # degrades to caption=None without raising.
    cfg = {
        "extraction": {
            "extract_images": True,
            "image_caption": True,
            "image_store": "payload",
            "image_max_bytes": 10_000_000,
        },
        "llm": {"endpoint": None, "model": None, "api_key": None},
    }
    result = extract_images(p, "png", cfg)
    assert len(result) == 1
    # The image was captured (bytes present).
    assert result[0].bytes == _png_1x1_red()
    # Caption is None (no LLM endpoint configured).
    assert result[0].caption is None
