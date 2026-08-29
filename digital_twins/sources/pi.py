"""`pi` source: pi agent session store (<dir>/<project>/*.jsonl)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .base import Capability, IngestItem, Source
from .session import build_content, flatten_pi, last_message_ts, project_label, to_iso

CAPABILITY = Capability(
    runtime="pi-session-store", credential=None, prefix="pi:")


class PiSource(Source):
    name = "pi"
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
                "sources.pi.extra.sessions_dir is not set (pi session store "
                "path) — set it or disable sources.pi "
                "(env: KB_SOURCES__PI__EXTRA__SESSIONS_DIR)"
            ]
        if not self.dir.is_dir():
            return [
                f"session store '{self.dir}' does not exist — set "
                f"sources.pi.extra.sessions_dir (env: KB_SOURCES__PI__EXTRA__SESSIONS_DIR)"
            ]
        return []

    def read(self, since: str | None):
        # ponytail: fixed look-back window; a session that goes quiet mid-window
        # may be re-read (idempotent upsert) or missed past the window. A per-session
        # ingested-id set replaces this if that ever bites.
        if self.dir is None:
            return
        cutoff = datetime.now(timezone.utc).timestamp() - self.window_hours * 3600
        for f in sorted(self.dir.glob("*/*.jsonl")):
            data = flatten_pi(f)
            last = last_message_ts(data)
            if last is None or last.timestamp() < cutoff:
                continue
            if len(data["messages"]) < self.min_messages:
                continue
            content = build_content(
                [(r, t) for r, t, _ in data["messages"]], ("user", "assistant"))
            if not content:
                continue
            sid = data["id"] or f.name
            yield IngestItem(
                key=sid,
                content=content,
                ts=to_iso(last),
                metadata={"project": project_label(f.parent.name),
                          "cwd": data["cwd"]},
            )


def factory(entry: dict) -> PiSource:
    return PiSource(entry)
