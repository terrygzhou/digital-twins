"""020 feature: image-store dispatch — unit level.

Covers FR-4 (the two store modes) and NFR-13 (no host-absolute paths):

1. ``image_store: filesystem`` — ``_run_image_extraction`` writes the
   bytes under a *relative* ``storage/images/{channel}/`` path (no ``~``
   or absolute prefix, NFR-13) and the ``images[]`` payload entries
   carry ``image_store: "filesystem"`` + a relative ``image_path`` with
   ``url: None``.
2. ``image_store: payload`` (default) — ``images[]`` entries carry a
   base64 data-URI in ``url`` and no ``image_path`` key.

The tests monkeypatch ``_run_image_extraction``'s filesystem view by
chdir-ing into a tmp dir (the helper writes CWD-relative), so no host
paths are touched.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from digital_twins.ingest.pipeline import _run_image_extraction


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _png_1x1_red() -> bytes:
    """A 1x1 red PNG, built by hand (no PIL dependency)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = b"\x00\xff\x00\x00"
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


class _Item:
    """Minimal stand-in for IngestItem (only .metadata + .content are read)."""

    def __init__(self, key: str, content: bytes, path: Path) -> None:
        self.key = key
        self.content = content
        self.metadata = {"path": str(path), "is_image": True}


def _cfg(store: str, tmp_dir: Path) -> dict:
    return {
        "state_dir": str(tmp_dir / "state"),
        "config_dir": str(tmp_dir / "config"),
        "extraction": {
            "extract_images": True,
            "image_caption": False,
            "image_store": store,
            "image_max_bytes": 10_000_000,
        },
    }


# ---------------------------------------------------------------------------
# FR-4: filesystem store
# ---------------------------------------------------------------------------

def test_filesystem_store_writes_relative_path_and_payload(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "chart.png").write_bytes(_png_1x1_red())
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)  # CWD-relative writes land under work/

    item = _Item("chart.png", _png_1x1_red(), src / "chart.png")
    _run_image_extraction(
        _cfg("filesystem", tmp_path), [item], run_id="r1", channel="fs")

    payload = item.metadata["images"]
    assert len(payload) == 1
    entry = payload[0]
    # NFR-13: the path is relative (no ~, no absolute prefix).
    assert entry["image_store"] == "filesystem"
    assert entry["url"] is None
    ip = entry["image_path"]
    assert not str(ip).startswith("/") and not str(ip).startswith("~")
    assert not Path(ip).is_absolute()
    # The bytes landed on disk under storage/images/{channel}/.
    # (The pipeline appends the MIME suffix to path.name, so
    # ``chart.png`` → ``chart.png.png`` — the same contract the
    # integration test test_image_store_filesystem_writes_relative_path
    # asserts at L206.)
    on_disk = work / "storage" / "images" / "fs" / "chart.png.png"
    assert on_disk.is_file()
    assert on_disk.read_bytes() == _png_1x1_red()


# ---------------------------------------------------------------------------
# FR-4: payload store (default)
# ---------------------------------------------------------------------------

def test_payload_store_carrys_base64_data_uri(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch):
    src = tmp_path / "src"
    src.mkdir()
    png = _png_1x1_red()
    (src / "a.png").write_bytes(png)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    item = _Item("a.png", png, src / "a.png")
    _run_image_extraction(
        _cfg("payload", tmp_path), [item], run_id="r1", channel="fs")

    payload = item.metadata["images"]
    assert len(payload) == 1
    entry = payload[0]
    assert entry["image_store"] == "payload"
    assert entry["url"].startswith("data:image/png;base64,")
    # No image_path key in payload mode.
    assert "image_path" not in entry
    # No filesystem write (nothing under storage/).
    assert not (work / "storage").exists()
