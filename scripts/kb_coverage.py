#!/usr/bin/env python3
"""kb_coverage.py — per-folder on-disk vs in-Qdrant diff (issue 4-B).

Compares files on disk in the fs source's ``extra.dir`` against item_keys
in the live Qdrant collection, printing a per-top-level-folder table:
on-disk count, in-Qdrant count, and missing count.

Usage:
    python3 scripts/kb_coverage.py [--dir /path/to/fs/dir]
                                    [--qdrant-url http://127.0.0.1:6333]
                                    [--collection personal_kb]

Requires: qdrant_client, a running Qdrant instance, and the fs source
config in kb.local.yml (or explicit --dir).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(description="KB coverage: on-disk vs Qdrant")
    p.add_argument("--dir", default=None,
                   help="fs source directory (default: from kb.local.yml or env)")
    p.add_argument("--qdrant-url", default=None,
                   help="Qdrant URL (default: KB_QDRANT__URL env or http://127.0.0.1:6333)")
    p.add_argument("--collection", default="personal_kb",
                   help="Qdrant collection name (default: personal_kb)")
    p.add_argument("--exclude", nargs="*", default=[],
                   help="folder names to exclude (default: 市场推广)")
    return p.parse_args()


def _load_fs_dir_from_config() -> str | None:
    """Try to read sources.fs.extra.dir from kb.local.yml."""
    import yaml
    for p in [
        Path.home() / ".config" / "digital-twins" / "kb.local.yml",
        Path("kb.local.yml"),
    ]:
        if p.exists():
            with open(p) as f:
                data = yaml.safe_load(f) or {}
            fs = (data.get("sources") or {}).get("fs") or {}
            extra = fs.get("extra") or {}
            if extra.get("dir"):
                return extra["dir"]
    return None


def _qdrant_item_keys(url: str, collection: str, source: str = "fs") -> set[str]:
    """Fetch all item_keys for the given source from Qdrant."""
    from qdrant_client import QdrantClient
    client = QdrantClient(url=url)
    # scroll all points, filter by source
    keys = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection,
            scroll_filter={"must": [{"key": "source", "match": {"value": source}}]},
            limit=1000,
            offset=offset,
            with_payload=True,
        )
        for pt in points:
            ik = (pt.payload or {}).get("item_key")
            if ik:
                keys.add(ik)
        if offset is None:
            break
    client.close()
    return keys


def main() -> None:
    args = parse_args()

    fs_dir = args.dir or _load_fs_dir_from_config()
    if not fs_dir:
        print("ERROR: no fs source directory. Pass --dir or set "
              "sources.fs.extra.dir in kb.local.yml", file=sys.stderr)
        sys.exit(1)

    qurl = args.qdrant_url or os.environ.get("KB_QDRANT__URL", "http://127.0.0.1:6333")

    print(f"fs dir:      {fs_dir}")
    print(f"qdrant url:  {qurl}")
    print(f"collection:  {args.collection}")
    print(f"excludes:    {args.exclude or '(none)'}")
    print()

    # Walk on-disk files, group by top-level folder
    on_disk: Counter[str] = Counter()
    fs_path = Path(fs_dir)
    if not fs_path.exists():
        print(f"ERROR: fs dir {fs_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    for f in fs_path.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(fs_path)
        top = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        if top in args.exclude:
            continue
        on_disk[top] += 1

    # Fetch in-Qdrant keys
    print("Fetching Qdrant item_keys (may take a moment)...")
    qdrant_keys = _qdrant_item_keys(qurl, args.collection)
    print(f"Fetched {len(qdrant_keys)} item_keys from Qdrant.\n")

    # Map on-disk relative paths to item_keys (item_key = relative path)
    in_qdrant: Counter[str] = Counter()
    for ik in qdrant_keys:
        top = ik.split("/", 1)[0] if "/" in ik else "(root)"
        in_qdrant[top] += 1

    all_folders = sorted(set(list(on_disk.keys()) + list(in_qdrant.keys())))
    print(f"{'folder':<30} {'on-disk':>8} {'in-qdrant':>10} {'missing':>8}")
    print("-" * 62)
    total_missing = 0
    for folder in all_folders:
        od = on_disk.get(folder, 0)
        iq = in_qdrant.get(folder, 0)
        missing = max(0, od - iq)
        total_missing += missing
        flag = "  <==" if missing > 0 else ""
        print(f"{folder:<30} {od:>8} {iq:>10} {missing:>8}{flag}")
    print("-" * 62)
    print(f"{'TOTAL':<30} {sum(on_disk.values()):>8} {sum(in_qdrant.values()):>10} "
          f"{total_missing:>8}")
    if total_missing:
        print(f"\n{total_missing} file(s) on disk are NOT in Qdrant.")
    else:
        print("\nAll on-disk files are represented in Qdrant.")


if __name__ == "__main__":
    main()
