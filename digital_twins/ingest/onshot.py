"""One-shot folder source (feature 016, FR-3).

``OneShotFolderSource`` conforms to the ``Source`` contract
(``prerequisites()`` / ``read(since)`` / ``close()``) but takes a
caller-supplied ``Path`` instead of reading a config entry — the folder
arrives at call time via the ``kb_ingest_folder`` tool args, never from
config (FR-6).  Read semantics mirror ``FsSource.read`` exactly
(recursive walk, relative keys, mtime ts, ``errors="replace"`` reads,
``metadata["path"]``) so dedup behaviour matches the config-fixed
``fs`` source (FR-7).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from digital_twins.sources.base import Capability, IngestItem, Source

CAPABILITY = Capability(runtime=None, credential=None, prefix="folder:")


class OneShotFolderSource(Source):
    """A folder chosen at call time, not in config.

    ``source_name`` defaults to ``"folder"`` and is stamped onto
    ``metadata["source_name"]`` so the MCP body can pass it through as
    the pipeline's source tag without a config entry.
    """

    name = "folder"
    capability = CAPABILITY

    def __init__(self, path: Path | str, source_name: str = "folder",
                 recursive: bool = True):
        self.path = Path(path)
        self.source_name = source_name
        self.recursive = recursive

    def prerequisites(self) -> list:
        if not self.path.exists():
            return [
                f"path '{self.path}' does not exist — supply a readable "
                f"directory"
            ]
        if not self.path.is_dir():
            return [
                f"path '{self.path}' is not a directory — supply a "
                f"readable directory"
            ]
        return []

    def read(self, since: str | None) -> Iterator[IngestItem]:
        if self.recursive:
            paths = sorted(self.path.rglob("*"))
        else:
            paths = sorted(self.path.glob("*"))
        for path in paths:
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            ts = datetime.fromtimestamp(mtime, timezone.utc).isoformat(
                timespec="microseconds")
            if since and ts <= since:
                continue
            yield IngestItem(
                key=str(path.relative_to(self.path)),
                content=path.read_text(encoding="utf-8", errors="replace"),
                ts=ts,
                metadata={
                    "path": str(path),
                    "source_name": self.source_name,
                },
            )

    def close(self) -> None:
        pass
