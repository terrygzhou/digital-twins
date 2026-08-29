"""Shared helpers for the agent session-store sources (hermes, pi, dsh)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_MAX_CHARS = 8000


def parse_ts(value) -> datetime | None:
    """ISO-8601 or epoch-millis -> aware UTC datetime (None if unparseable)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def to_iso(dt: datetime | None) -> str:
    if dt is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return dt.isoformat(timespec="microseconds")


def extract_text(content) -> str:
    """Plain text out of an agent message content field (str or part list)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return (part.get("text") or "").strip()
    return ""


def build_content(messages, roles, max_chars: int = _MAX_CHARS) -> str:
    """Flatten (role, text) pairs into a readable transcript."""
    parts = []
    for role, text in messages:
        if role not in roles or not text:
            continue
        label = {"user": "User", "assistant": "Assistant",
                 "system": "System"}.get(role, role)
        parts.append(f"{label}: {text}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[...truncated for KB ingestion...]"
    return text


def project_label(dir_name: str) -> str:
    """'--home-terry-proj-demo--' -> 'home/terry/proj/demo'."""
    raw = dir_name.strip('-').replace('--', '/')
    raw = re.sub(r'[A-Z]', lambda m: '-' + m.group(0).lower(), raw)
    return raw.strip('/') or "(unknown project)"


def last_message_ts(data: dict) -> datetime | None:
    for ts in (t for *_, t in reversed(data["messages"])):
        if ts is not None:
            return ts
    return data["started"]


def flatten_pi(path: Path) -> dict:
    """One pi session .jsonl -> {id, cwd, started, messages: [(role, text, ts)]}."""
    sid = cwd = started = None
    messages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if t == "session":
                sid = o.get("id")
                cwd = o.get("cwd")
                started = parse_ts(o.get("timestamp"))
            elif t == "message":
                msg = o.get("message") or {}
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = extract_text(msg.get("content"))
                if text:
                    messages.append(
                        (role, text, parse_ts(msg.get("timestamp"))))
    return {"id": sid, "cwd": cwd, "started": started, "messages": messages}


def flatten_dsh(path: Path) -> dict:
    """One dsh session .jsonl.zstd -> same shape as flatten_pi (+title, model)."""
    import zstandard

    sid = cwd = model = started = title = None
    messages = []
    dctx = zstandard.ZstdDecompressor()
    with open(path, "rb") as fh:
        raw = dctx.stream_reader(fh).read().decode("utf-8", errors="replace")
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        data = o.get("data") or {}
        if t == "session":
            sid = o.get("id")
            cwd = o.get("cwd")
            started = parse_ts(o.get("createdAt"))
        elif t == "session/title":
            src = data.get("source") or {}
            if src.get("kind") == "provider" or title is None:
                title = data.get("title")
            m = src.get("model") or {}
            if isinstance(m, dict) and m.get("model"):
                model = m["model"]
        elif t in ("user/message", "assistant/message"):
            role = data.get("role")
            if role == "assistant":
                role = (data.get("message") or {}).get("role", "assistant")
                content = (data.get("message") or {}).get("content")
            else:
                content = data.get("content")
            if role not in ("user", "assistant"):
                continue
            text = extract_text(content)
            if text:
                messages.append((role, text, parse_ts(o.get("time"))))
    return {"id": sid, "cwd": cwd, "started": started, "title": title,
            "model": model, "messages": messages}
