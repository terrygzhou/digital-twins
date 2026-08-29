"""`fs` source: a directory of files (demo/test double, baseline parity)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .base import Capability, IngestItem, Source

CAPABILITY = Capability(runtime=None, credential=None, prefix="fs:")


class FsSource(Source):
    name = "fs"
    capability = CAPABILITY

    def __init__(self, entry: dict):
        self.dir = Path(((entry.get("extra") or {}).get("dir")) or "")

    def prerequisites(self) -> list:
        if not self.dir.is_dir():
            return [
                f"path '{self.dir}' does not exist — set sources.fs.extra.dir "
                f"(env: KB_SOURCES__FS__EXTRA__DIR)"
            ]
        return []

    def read(self, since: str | None):
        for path in sorted(self.dir.rglob("*")):
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            ts = datetime.fromtimestamp(mtime, timezone.utc).isoformat(
                timespec="microseconds")
            if since and ts <= since:
                continue
            yield IngestItem(
                key=str(path.relative_to(self.dir)),
                content=path.read_text(encoding="utf-8", errors="replace"),
                ts=ts,
                metadata={"path": str(path)},
            )


def factory(entry: dict) -> FsSource:
    return FsSource(entry)
