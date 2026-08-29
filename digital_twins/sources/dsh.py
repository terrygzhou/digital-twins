"""`dsh` source: dsh session store (<dir>/<project>/<sid>/session.jsonl.zstd)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .base import Capability, IngestItem, Source
from .session import build_content, flatten_dsh, last_message_ts, project_label, to_iso

CAPABILITY = Capability(
    runtime="dsh-session-store", credential=None, prefix="dsh:")


class DshSource(Source):
    name = "dsh"
    capability = CAPABILITY

    def __init__(self, entry: dict):
        extra = entry.get("extra") or {}
        self._sessions_dir = str(extra.get("sessions_dir") or "")
        self.dir = Path(self._sessions_dir).expanduser() if self._sessions_dir else None
        self.window_hours = float(extra.get("window_hours", 24))
        self.min_messages = int(extra.get("min_messages", 4))

    def prerequisites(self) -> list:
        if not self._sessions_dir:
            return [
                "sources.dsh.extra.sessions_dir is not set (dsh session store " \
                "path) — set it or disable sources.dsh "
                "(env: KB_SOURCES__DSH__EXTRA__SESSIONS_DIR)"
            ]
        if not self.dir.is_dir():
            return [
                f"session store '{self.dir}' does not exist — set "
                f"sources.dsh.extra.sessions_dir "
                f"(env: KB_SOURCES__DSH__EXTRA__SESSIONS_DIR)"
            ]
        return []

    def read(self, since: str | None):
        # ponytail: same window ceiling as the pi source.
        cutoff = datetime.now(timezone.utc).timestamp() - self.window_hours * 3600
        if self.dir is None:
            return
        for f in sorted(self.dir.glob("*/*/session.jsonl.zstd")):
            data = flatten_dsh(f)
            last = last_message_ts(data)
            if last is None or last.timestamp() < cutoff:
                continue
            if len(data["messages"]) < self.min_messages:
                continue
            content = build_content(
                [(r, t) for r, t, _ in data["messages"]], ("user", "assistant"))
            if not content:
                continue
            sid = data["id"] or f.parent.name
            yield IngestItem(
                key=sid,
                content=content,
                ts=to_iso(last),
                metadata={"project": project_label(f.parent.parent.name),
                          "title": data["title"], "model": data["model"]},
            )


def factory(entry: dict) -> DshSource:
    return DshSource(entry)
