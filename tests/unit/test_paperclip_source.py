"""Paperclip PG chat source: fail-fast prerequisites, stubbed read."""

from digital_twins.sources import build
from digital_twins.sources import paperclip as paperclip_mod


class StubPG:
    """Minimal stand-in for a psycopg2 connection (unit tests only)."""

    def __init__(self, rows):
        self._rows = rows
        self.executed_sql = None

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.executed_sql = sql
        self._params = params
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        pass


def test_paperclip_capability_declared():
    source = build("paperclip", {"extra": {"pg_dsn": "postgres://test"}})
    cap = source.capability
    assert cap.credential is None  # DSN comes from extra, not env var
    assert cap.prefix == "paperclip:"


def test_paperclip_missing_dsn_is_prerequisite():
    source = build("paperclip", {})
    missing = source.prerequisites()
    assert len(missing) == 1
    assert "sources.paperclip.extra.pg_dsn" in missing[0]
    # read() without connection must yield nothing
    assert list(source.read(None)) == []


def test_paperclip_read_with_stubbed_pg(monkeypatch):
    """read() yields IngestItems from a stubbed PG connection."""
    source = build("paperclip", {"extra": {"pg_dsn": "postgres://test"}})
    assert source.prerequisites() == []

    rows = [
        {"id": 42, "body": "hello world",
         "created_at": "2025-01-15T10:00:00+00:00",
         "issue_id": "PC-100", "issue_title": "Demo Issue",
         "author_type": "agent", "agent_name": "terry-bot"},
        {"id": 43, "body": "second comment",
         "created_at": "2025-01-15T11:00:00+00:00",
         "issue_id": "PC-101", "issue_title": "Another",
         "author_type": "human", "agent_name": None},
    ]
    pg = StubPG(rows)
    monkeypatch.setattr(paperclip_mod, "_pg_connect", lambda dsn: pg)

    items = list(source.read(None))
    assert len(items) == 2
    assert items[0].key == "42"
    assert "hello world" in items[0].content
    assert items[0].ts == "2025-01-15T10:00:00+00:00"
    assert items[0].metadata["issue_id"] == "PC-100"
    assert items[0].metadata["agent"] == "terry-bot"
    assert items[1].key == "43"
    assert items[1].metadata["agent"] == "System"


def test_paperclip_read_passes_since_to_sql(monkeypatch):
    """When since is set, the parameterized WHERE query is used."""
    source = build("paperclip", {"extra": {"pg_dsn": "postgres://test"}})
    pg = StubPG(rows=[])
    monkeypatch.setattr(paperclip_mod, "_pg_connect", lambda dsn: pg)

    list(source.read("2025-01-15T00:00:00+00:00"))
    assert "WHERE" in pg.executed_sql
