"""`fs` source: a directory of files (demo/test double, baseline parity)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .base import Capability, IngestItem, Source

CAPABILITY = Capability(runtime=None, credential=None, prefix="fs:")

#: Default "supported" document/text extensions. Files with one of these
#: suffixes are always ingested regardless of size; every other file is
#: ingested only when it sits below ``extra.max_bytes`` (default 10 MB).
#: This keeps binary blobs (videos, archives, executables, CAD) out of the
#: text pipeline while retaining the documents that carry real content.
_DEFAULT_SUPPORTED_EXTS = frozenset(
    {
        # office / documents
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".odt", ".ods", ".odg", ".epub",
        # plain text / notes
        ".txt", ".text", ".md", ".markdown", ".rst", ".rtf", ".tex",
        # logs / config / data
        ".log", ".ini", ".cfg", ".conf", ".csv", ".tsv",
        ".json", ".jsonl", ".xml",
        # web
        ".html", ".htm",
    }
)


def _as_bool(value, default: bool) -> bool:
    """Coerce a config/env value to bool, tolerating native bools and strings."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def _as_int(value, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class FsSource(Source):
    name = "fs"
    capability = CAPABILITY

    def __init__(self, entry: dict):
        extra = entry.get("extra") or {}
        self.dir = Path(extra.get("dir") or "")
        # Ingestion-scope filter: only supported types OR files below a size cap.
        self.filter_enabled = _as_bool(extra.get("filter_enabled"), True)
        self.max_bytes = _as_int(extra.get("max_bytes"), 10 * 1000 * 1000)
        self.supported_exts = {
            str(e).lower() for e in (extra.get("supported_exts") or _DEFAULT_SUPPORTED_EXTS)
        }

    def prerequisites(self) -> list:
        if not self.dir.is_dir():
            return [
                f"path '{self.dir}' does not exist — set sources.fs.extra.dir "
                f"(env: KB_SOURCES__FS__EXTRA__DIR)"
            ]
        return []

    def _keep(self, ext: str, size: int) -> bool:
        """Return True when a file passes the ingestion-scope filter.

        Keep the file if its extension is a supported document/text type, OR if
        it is below the size cap. Drop only files that are both unsupported and
        at/above the cap (large binaries such as videos, archives, executables).
        """
        if not self.filter_enabled:
            return True
        if ext in self.supported_exts:
            return True
        return size < self.max_bytes

    def read(self, since: str | None):
        for path in sorted(self.dir.rglob("*")):
            if not path.is_file():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            mtime = st.st_mtime
            ts = datetime.fromtimestamp(mtime, timezone.utc).isoformat(
                timespec="microseconds")
            if since and ts <= since:
                continue
            if not self._keep(path.suffix.lower(), st.st_size):
                continue
            yield IngestItem(
                key=str(path.relative_to(self.dir)),
                content=path.read_text(encoding="utf-8", errors="replace"),
                ts=ts,
                metadata={"path": str(path)},
            )


def factory(entry: dict) -> FsSource:
    return FsSource(entry)
