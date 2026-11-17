"""Shared fixtures for 025 unit tests.

Patches ``neo4j.GraphDatabase.driver`` so FR-7 guard tests never open a
real network connection.  The guard under test is the pre-driver
construction-time validation; the driver object returned is a stub.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stub_neo4j_driver(monkeypatch):
    """Replace ``neo4j.GraphDatabase.driver`` with a no-op factory.

    The FR-7 guard fires *before* the driver is constructed, so a
    successful path returns a trivial object.  The missing-pin path
    raises before reaching this stub.
    """
    import neo4j

    class _StubDriver:
        """Minimal stand-in for a live ``neo4j.Driver``."""

        def session(self, *a, **kw):
            raise RuntimeError("stub driver: no session support")

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        neo4j.GraphDatabase, "driver", lambda *a, **kw: _StubDriver()
    )
    yield
