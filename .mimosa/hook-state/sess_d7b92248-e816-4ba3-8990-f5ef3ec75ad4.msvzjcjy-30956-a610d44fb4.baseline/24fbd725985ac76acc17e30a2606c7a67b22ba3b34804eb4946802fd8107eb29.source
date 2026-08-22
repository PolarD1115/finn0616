#!/usr/bin/env python3
"""Find embeddings whose bucket files no longer exist, optionally delete them."""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine
from utils import load_config


def embedding_rows(db_path: str) -> list[tuple[str, str]]:
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT bucket_id, updated_at FROM embeddings ORDER BY bucket_id"
        ).fetchall()
    finally:
        conn.close()


def find_orphan_embeddings(
    rows: list[tuple[str, str]], live_bucket_ids: set[str]
) -> list[tuple[str, str]]:
    return [(bucket_id, updated_at) for bucket_id, updated_at in rows if bucket_id not in live_bucket_ids]


def delete_embeddings(db_path: str, bucket_ids: list[str]) -> int:
    if not bucket_ids:
        return 0
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany("DELETE FROM embeddings WHERE bucket_id = ?", [(bid,) for bid in bucket_ids])
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true", help="Delete orphan embeddings after confirmation.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--limit", type=int, default=20, help="How many orphan ids to print.")
    args = parser.parse_args()

    config = load_config()
    bucket_mgr = BucketManager(config)
    embedding_engine = EmbeddingEngine(config)

    buckets = await bucket_mgr.list_all(include_archive=True)
    live_ids = {str(bucket["id"]) for bucket in buckets if bucket.get("id")}
    rows = embedding_rows(embedding_engine.db_path)
    orphans = find_orphan_embeddings(rows, live_ids)

    print(f"Buckets: {len(live_ids)}")
    print(f"Embeddings: {len(rows)}")
    print(f"Orphan embeddings: {len(orphans)}")

    for bucket_id, updated_at in orphans[: max(0, args.limit)]:
        print(f"  {bucket_id}  {updated_at}")
    if len(orphans) > args.limit:
        print(f"  ... and {len(orphans) - args.limit} more")

    if not args.delete or not orphans:
        return 0

    if not args.yes:
        answer = input(f"Delete {len(orphans)} orphan embeddings? Type DELETE to continue: ")
        if answer != "DELETE":
            print("Canceled.")
            return 0

    deleted = delete_embeddings(embedding_engine.db_path, [bucket_id for bucket_id, _ in orphans])
    print(f"Deleted orphan embeddings: {deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
