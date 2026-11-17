"""Red tests for the 025 FR-7 fail-fast guard (task-9, C2).

Contract (spec §5.3, FR-7):

- A driver built from a resolved ``KB_NEO4J__URL`` that carries
  ``?db=<slug>`` passes the guard.
- A URL lacking ``?db=`` on a tenant-partitioned **write** path fails
  fast — construction raises, it does not silently fall through to the
  server-default ``neo4j`` database.
- A read path without ``?db=`` does NOT raise: reads silently fall
  through to the default. The asymmetry is the contract.

The guard MUST fire on every driver-construction path: scheduler
bootstrap, ``run --once``, migration CLI, MCP server startup — all of
which funnel through ``build_neo4j_driver`` /
``_neo4j_driver_factory`` in ``scheduler.loop``.
"""

import pytest

from digital_twins.scheduler.loop import build_neo4j_driver


def _config(url, user="neo4j", password="pw"):
    """Nested-dict config, the shape ``digital_twins.config.schema.get``
    walks (``get(cfg, "neo4j.url")`` -> ``cfg["neo4j"]["url"]``).  The
    flat ``{"neo4j.url": ...}`` shape used in the first draft does not
    resolve through ``get`` — it is the loaded-config shape only after
    the four-layer loader has folded env-var overrides, not what
    ``build_neo4j_driver`` receives directly."""
    return {
        "neo4j": {"url": url, "user": user, "password": password},
    }


class TestFr7Guard:
    def test_url_with_db_pin_passes(self):
        """A URL carrying ?db=<slug> passes the guard on a tenant
        write path — the expected A1-hard posture. The write path
        constructs with tenant_install=True and the URL pin is present,
        so no exception."""
        driver = build_neo4j_driver(
            _config("bolt://host:7687?db=kb-pk"), tenant_install=True
        )
        assert driver is not None

    def test_missing_db_pin_fails_fast_on_write(self):
        """A URL lacking ?db= on a tenant-partitioned write path MUST
        raise at construction — never silently target the server
        default.

        Red today: build_neo4j_driver returns the driver unconditionally
        and no construction-time exception is raised. Green after C2's
        one-line validation lands in the driver path.
        """
        with pytest.raises(Exception, match="\\?db=|db="):
            build_neo4j_driver(_config("bolt://host:7687"), tenant_install=True)

    def test_read_path_without_pin_does_not_raise(self):
        """The asymmetry: reads without ?db= are allowed (silent
        fall-through to the default DB). Only the write path is guarded.
        A read path constructs the driver with no tenant-install flag,
        so the guard does not fire — today and after the fix."""
        driver = build_neo4j_driver(_config("bolt://host:7687"))
        assert driver is not None
