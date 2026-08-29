"""`paperclip` source: paperclip issue-chat comments via a Postgres DSN."""

from __future__ import annotations

from .base import Capability, IngestItem, Source

CAPABILITY = Capability(runtime="paperclip", credential=None, prefix="paperclip:")

_QUERY = (
    "SELECT ic.id, ic.body, ic.created_at::text AS created_at, "
    "i.identifier AS issue_id, i.title AS issue_title, "
    "ic.author_type, a.name AS agent_name "
    "FROM issue_comments ic "
    "JOIN issues i ON i.id = ic.issue_id "
    "LEFT JOIN agents a ON a.id = ic.author_agent_id "
    "WHERE ic.created_at > %(since)s "
    "ORDER BY ic.created_at"
)

_QUERY_NO_SINCE = (
    "SELECT ic.id, ic.body, ic.created_at::text AS created_at, "
    "i.identifier AS issue_id, i.title AS issue_title, "
    "ic.author_type, a.name AS agent_name "
    "FROM issue_comments ic "
    "JOIN issues i ON i.id = ic.issue_id "
    "LEFT JOIN agents a ON a.id = ic.author_agent_id "
    "ORDER BY ic.created_at"
)


class PaperclipSource(Source):
    name = "paperclip"
    capability = CAPABILITY

    def __init__(self, entry: dict):
        extra = entry.get("extra") or {}
        self.dsn: str = str(extra.get("pg_dsn") or "")

    def prerequisites(self) -> list:
        if not self.dsn:
            return [
                "sources.paperclip.extra.pg_dsn is not set (Postgres DSN for the "
                "paperclip database) — set it or disable sources.paperclip "
                "(env: KB_SOURCES__PAPERCLIP__EXTRA__PG_DSN)"
            ]
        return []

    def read(self, since: str | None):
        if not self.dsn:
            return
        conn = _pg_connect(self.dsn)
        try:
            cur = conn.cursor()
            if since:
                cur.execute(_QUERY, {"since": since})
            else:
                cur.execute(_QUERY_NO_SINCE)
            for row in cur.fetchall():
                body = (row.get("body") or "").strip()
                if not body:
                    continue
                created = row.get("created_at") or ""
                agent = row.get("agent_name") or "System"
                yield IngestItem(
                    key=str(row.get("id", "")),
                    content=(
                        f"[{row.get('issue_id', '')}] {row.get('issue_title', '')}\n"
                        f"Agent: {agent}\n{body}"
                    ),
                    ts=created,
                    metadata={
                        "issue_id": row.get("issue_id", ""),
                        "issue_title": row.get("issue_title", ""),
                        "agent": agent,
                        "author_type": row.get("author_type", ""),
                    },
                )
        finally:
            conn.close()


def _pg_connect(dsn: str):
    """Connect to Postgres; the returned object is used by read().

    Overridable in tests: monkeypatch ``digital_twins.sources.paperclip._pg_connect``.
    """
    import psycopg2

    return psycopg2.connect(dsn)


def factory(entry: dict) -> PaperclipSource:
    return PaperclipSource(entry)
