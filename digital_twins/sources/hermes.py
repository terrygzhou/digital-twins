"""`hermes` source: hermes sessions via the `hermes sessions export` CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import Capability, IngestItem, Source
from .session import build_content, parse_ts, to_iso

CAPABILITY = Capability(runtime="hermes", credential=None, prefix="hermes:")


class HermesSource(Source):
    name = "hermes"
    capability = CAPABILITY

    def __init__(self, entry: dict):
        extra = entry.get("extra") or {}
        self.window_hours = int(extra.get("window_hours", 24))
        self.min_messages = int(extra.get("min_messages", 2))

    def prerequisites(self) -> list:
        if shutil.which("hermes") is None:
            return [
                "'hermes' CLI not found on PATH — install hermes or disable "
                "sources.hermes"
            ]
        return []

    def read(self, since: str | None):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            proc = subprocess.run(
                ["hermes", "sessions", "export", "--format", "jsonl",
                 "--newer-than", f"{self.window_hours}h",
                 "--min-messages", str(self.min_messages),
                 "--yes", str(tmp)],
                capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"hermes sessions export failed "
                    f"(rc={proc.returncode}): {proc.stderr.strip()[:300]}")
            if not tmp.exists():
                return
            for line in tmp.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                sid = r.get("id") or r.get("session_id")
                if not sid:
                    continue
                content = build_content(
                    [(m.get("role"), (m.get("content") or "").strip())
                     for m in (r.get("messages") or [])],
                    ("user", "assistant", "system"))
                if not content:
                    continue
                yield IngestItem(
                    key=sid,
                    content=content,
                    ts=to_iso(parse_ts(r.get("started_at"))),
                    metadata={"title": r.get("title"),
                              "model": r.get("model")},
                )
        finally:
            tmp.unlink(missing_ok=True)


def factory(entry: dict) -> HermesSource:
    return HermesSource(entry)
