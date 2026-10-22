#!/usr/bin/env python3
"""kb_status.py — one-line KB status (issue 4-B).

Prints a compact status line:
    qdrant=<N> points | entity=<N> | last_audit=<status> |
    highwater.fs=<N> rows | llm_err_rate=<pct> | indexed_vectors=<N>

Requires: qdrant_client, neo4j (optional), and access to state.db.

Usage:
    python3 scripts/kb_status.py
    python3 scripts/kb_status.py --state-dir ~/.digital-twins
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(description="One-line KB status")
    p.add_argument("--state-dir", default=None,
                   help="state dir (default: ~/.digital-twins)")
    p.add_argument("--qdrant-url", default=None,
                   help="Qdrant URL (default: KB_QDRANT__URL or http://127.0.0.1:6333)")
    p.add_argument("--neo4j-url", default=None,
                   help="Neo4j URL (default: KB_NEO4J__URL; skip if absent)")
    p.add_argument("--neo4j-user", default="neo4j")
    p.add_argument("--neo4j-pass", default=None)
    p.add_argument("--collection", default="personal_kb")
    return p.parse_args()


def _qdrant_status(url: str, collection: str):
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=url)
        info = client.get_collection(collection)
        count = info.points_count
        # Try to get indexed_vectors_count from collection config
        indexed = getattr(info, "indexed_vectors_count", None)
        if indexed is None:
            # Fallback: check the optimizers status
            indexed = 0
        client.close()
        return count, indexed
    except Exception as e:
        print(f"  qdrant error: {e}", file=sys.stderr)
        return None, None


def _neo4j_entity_count(url, user, password):
    if not url:
        return None
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(url, auth=(user, password))
        with driver.session() as s:
            r = s.run("MATCH (e:Entity) RETURN count(e) AS n")
            n = r.single()["n"]
        driver.close()
        return n
    except Exception as e:
        print(f"  neo4j error: {e}", file=sys.stderr)
        return None


def _state_status(state_dir: str):
    """Read state.db for audit_runs + highwater counts."""
    db_path = Path(state_dir) / "state.db"
    if not db_path.exists():
        print(f"  state.db not found at {db_path}", file=sys.stderr)
        return None, None, None

    conn = sqlite3.connect(str(db_path))
    try:
        # Last audit run
        row = conn.execute(
            "SELECT status, completed_at FROM audit_runs "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        last_audit_status = row[0] if row else "none"
        last_audit_at = row[1] if row else None

        # highwater.fs row count
        hw = conn.execute(
            "SELECT COUNT(*) FROM highwater WHERE source='fs'"
        ).fetchone()
        highwater_fs = hw[0] if hw else 0

        # LLM error rate: audit_items failed / total in last 100 items
        ai_total = conn.execute(
            "SELECT COUNT(*) FROM audit_items"
        ).fetchone()[0]
        ai_failed = conn.execute(
            "SELECT COUNT(*) FROM audit_items WHERE status='failed'"
        ).fetchone()[0]
        llm_err_rate = (ai_failed / ai_total * 100) if ai_total else 0.0

        return last_audit_status, last_audit_at, (highwater_fs, llm_err_rate)
    finally:
        conn.close()


def main() -> None:
    args = parse_args()

    state_dir = args.state_dir or str(
        Path.home() / ".digital-twins")
    qurl = args.qdrant_url or os.environ.get(
        "KB_QDRANT__URL", "http://127.0.0.1:6333")
    nurl = args.neo4j_url or os.environ.get("KB_NEO4J__URL")
    npass = args.neo4j_pass or os.environ.get("KB_NEO4J__PASSWORD")

    # Qdrant
    qcount, qindexed = _qdrant_status(qurl, args.collection)

    # Neo4j
    nentities = _neo4j_entity_count(nurl, args.neo4j_user, npass)

    # State DB
    last_status, last_at, hw_data = _state_status(state_dir)
    highwater_fs = hw_data[0] if hw_data else 0
    llm_err_rate = hw_data[1] if hw_data else 0.0

    # Build one-line summary
    parts = []
    parts.append(f"qdrant={qcount if qcount is not None else '?'} points")
    if qindexed is not None:
        parts.append(f"indexed_vectors={qindexed}")
    parts.append(f"entity={nentities if nentities is not None else '?'}")
    parts.append(f"last_audit={last_status or 'none'}")
    if last_at:
        parts.append(f"at={last_at}")
    parts.append(f"highwater.fs={highwater_fs}")
    parts.append(f"llm_err_rate={llm_err_rate:.1f}%")

    print(" ".join(parts))


if __name__ == "__main__":
    main()
