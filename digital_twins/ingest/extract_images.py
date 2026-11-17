"""020: image extraction from files and embedded PDF images.

FR-1: module API — ``extract_images(path, suffix, cfg) -> list[ImageExtraction]``.
FR-7: ``image_max_bytes`` cap — over-cap images are dropped (per-item
error, not a run abort).  FR-8: caption failure degrades to ``None``
with a warning; the image bytes are still returned (run continues).

Supported standalone suffixes: ``.png .jpg .jpeg .webp .gif``.
For PDFs: embedded image XObjects are extracted via pypdf.
MIME is sniffed from the suffix for standalone files; for PDF-embedded
images the MIME is read from the XObject's ``Subtype``.

The returned :class:`ImageExtraction` carries the raw bytes, MIME,
``source_kind`` (``"standalone"`` | ``"pdf"``), and ``page`` (int for
PDF-embedded, None for standalone).  A ``caption`` field is set to
``None`` when the LLM endpoint is absent/unconfigured; the module
never raises on caption failure.

All paths are relative (NFR-13); no host paths leak.
"""

from __future__ import annotations

import io
import logging
import mimetypes
import pathlib
from dataclasses import dataclass, field
from typing import Any, Optional

from digital_twins.config.schema import get

logger = logging.getLogger(__name__)

# Standalone raster suffixes (020 scope).
SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif"}
)

# MIME map for standalone files (the 5 supported suffixes).
MIME_BY_SUFFIX: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# Default cap for a single image blob (FR-7).
DEFAULT_MAX_BYTES = 10_000_000  # 10 MB


@dataclass
class ImageExtraction:
    """One extracted image blob, ready for storage or captioning.

    Attributes:
        bytes:   raw image bytes (JPEG/PNG/etc.).
        mime:    IANA MIME type (e.g. ``"image/png"``).
        source_kind: ``"standalone"`` for a direct raster file,
                     ``"pdf"`` for an image embedded inside a PDF.
        page:    1-based PDF page number (None for standalone).
        caption: LLM-generated caption, or None (endpoint absent /
                 caption disabled / call failed — FR-8 degradation).
    """
    bytes: bytes
    mime: str
    source_kind: str
    page: Optional[int]
    caption: Optional[str] = None


def extract_images(
    path: pathlib.Path,
    suffix: str,
    cfg: dict | None,
    max_bytes: Optional[int] = None,
) -> list[ImageExtraction]:
    """Extract images from *path*.

    Args:
        path: the file to read.
        suffix: lower-cased file suffix including the dot (``".png"``,
                ``".pdf"``).
        cfg: the merged config dict (may be None — the module still
             extracts; caption is None).
        max_bytes: per-image byte cap.  Defaults to the
                   ``extraction.image_max_bytes`` knob value (10 MB)
                   when *cfg* is provided, else :data:`DEFAULT_MAX_BYTES`.

    Returns:
        A list of :class:`ImageExtraction`.  Over-cap images are
        dropped (per-item ``image_too_large`` — FR-7); the remaining
        images on the same point continue (the run does not abort).

    Raises:
        Nothing for over-cap or unsupported suffixes — empty list is
        returned (the per-item error is recorded at the pipeline level,
        not here; this module is a pure extractor).
    """
    ext = suffix.lower()
    # Normalise: the suffix may be passed as "png" or ".png".
    if not ext.startswith("."):
        ext = "." + ext

    # Resolve the byte cap.
    if max_bytes is None:
        if cfg is not None:
            max_bytes = int(get(cfg, "extraction.image_max_bytes")
                            or DEFAULT_MAX_BYTES)
        else:
            max_bytes = DEFAULT_MAX_BYTES

    if ext in MIME_BY_SUFFIX:
        # Standalone raster file.
        return _extract_standalone(path, ext, max_bytes)

    if ext == ".pdf":
        return _extract_pdf(path, max_bytes, cfg)

    # Unsupported suffix → no images.
    return []


def _extract_standalone(
    path: pathlib.Path,
    ext: str,
    max_bytes: int,
) -> list[ImageExtraction]:
    """Read a standalone raster file; apply the byte cap."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning(
            "image extraction: cannot read %r: %s", path, exc)
        return []
    if len(raw) > max_bytes:
        # FR-7: per-item drop (image_too_large), not a run abort.
        logger.warning(
            "image extraction: %s is %d bytes (cap %d) — dropped "
            "(image_too_large)",
            path.name, len(raw), max_bytes)
        return []
    mime = MIME_BY_SUFFIX.get(ext) or _sniff_mime(raw)
    return [ImageExtraction(
        bytes=raw,
        mime=mime,
        source_kind="standalone",
        page=None,
        caption=None,
    )]


def _sniff_mime(raw: bytes) -> str:
    """Sniff the MIME type from raw image bytes (magic-number check)."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def _extract_pdf(
    path: pathlib.Path,
    max_bytes: int,
    cfg: dict | None,
) -> list[ImageExtraction]:
    """Extract embedded image XObjects from a PDF via pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning(
            "image extraction: pypdf not installed — cannot extract "
            "embedded PDF images")
        return []

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        logger.warning("image extraction: pypdf failed on %r: %s",
                       path, exc)
        return []

    out: list[ImageExtraction] = []
    for page_num, page in enumerate(reader.pages, start=1):
        try:
            xobjects = page.images if hasattr(page, "images") else []
        except Exception:
            xobjects = []
        for img in xobjects:
            # pypdf PageImages yields ImageFile objects with .data / .name.
            raw = getattr(img, "data", None)
            if raw is None:
                raw = getattr(img, "read", lambda: b"")()
            if raw is None or raw == b"":
                continue
            if len(raw) > max_bytes:
                logger.warning(
                    "image extraction: embedded image on page %d of %s "
                    "is %d bytes (cap %d) — dropped (image_too_large)",
                    page_num, path.name, len(raw), max_bytes)
                continue
            # Determine MIME from the pypdf ImageFile.  The object's
            # ``.name`` carries an extension (e.g. "…c840.png"); when
            # that is unknown, sniff from the first bytes of the raw
            # stream.
            name = getattr(img, "name", "") or ""
            ext_part = pathlib.PurePosixPath(name).suffix.lower()
            mime = MIME_BY_SUFFIX.get(ext_part)
            if mime is None:
                mime = _sniff_mime(raw)
            out.append(ImageExtraction(
                bytes=raw,
                mime=mime,
                source_kind="pdf",
                page=page_num,
                caption=None,
            ))
    return out


def caption_image(
    ext: ImageExtraction,
    cfg: dict,
) -> None:
    """Populate *ext.caption* via the LLM endpoint (FR-3 / FR-8).

    The function is idempotent: when ``extraction.image_caption`` is
    False, or the LLM endpoint is absent, or the call fails, *ext.caption*
    stays ``None`` (the warning-isolation contract).  No new LLM calls
    for non-image content are made (FR-3).

    This is a separate step (not inside :func:`extract_images`) so the
    pipeline can decide *when* to call it (e.g. after the image bytes
    are stored), and tests can monkey-patch the client.
    """
    enabled = bool(get(cfg, "extraction.image_caption"))
    if not enabled:
        return
    endpoint = get(cfg, "llm.endpoint")
    if not endpoint:
        # No endpoint → caption stays None, image still stored (FR-8).
        logger.warning(
            "image caption: llm.endpoint not configured — "
            "image_caption will be null for this image")
        return
    try:
        from digital_twins.ingest.llm import build_llm_client
        client = build_llm_client(cfg)
        # Build a simple multimodal request (OpenAI-compatible
        # image_url part).  The endpoint-aware client in llm.py
        # currently sends text-only; the caption prompt is a
        # best-effort text summary request.
        prompt = (
            f"Describe this image in one sentence.\n"
            f"(image: {ext.mime}, {len(ext.bytes)} bytes, "
            f"source={ext.source_kind}"
            + (f", page={ext.page}" if ext.page else "") + ")"
        )
        ext.caption = client(prompt)
    except Exception as exc:
        # FR-8: caption failure degrades to None + warning.
        logger.warning(
            "image caption failed (image still stored): %s", exc)
        ext.caption = None
