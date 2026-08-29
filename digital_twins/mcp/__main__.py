"""Entry point for ``python -m digital_twins.mcp``.

Mirrors ``digital_twins.mcp.cli``: runs the stdio transport by default
(the process-owner credential identifies the caller; see
``digital_twins.mcp.stdio`` for the auth order). Use
``digital-twins serve-mcp --transport http`` for the HTTP transport
instead of this entry point (the stdio path is a local, single-user
channel).

The stdio path is the documented ``python -m`` surface because it needs
no network listener — it just reads / writes the process's stdio.
"""
from __future__ import annotations

import sys

from ..config import load
from ..config.schema import get
from ..state.db import connect
from ..state.migrations import migrate
from . import stdio


def main() -> None:
    cfg = load()
    state_dir = _path(cfg["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    db = connect(state_dir)
    try:
        migrate(db)
        service_account_email = get(cfg, "mcp.service_account_email")
        stdio.main(db, service_account_email=service_account_email)
    finally:
        db.close()


def _path(p):
    from pathlib import Path
    return Path(p)


if __name__ == "__main__":
    main()
