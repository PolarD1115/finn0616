import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine
from utils import load_config


RELATIONSHIP_WEATHER_TAGS = {"relationship_weather", "daily_impression", "weekly_impression"}


def collect_migrated_feel_ids(buckets: list[dict]) -> set[str]:
    feel_ids = set()
    for bucket in buckets:
        comments = bucket.get("metadata", {}).get("comments", [])
        if not isinstance(comments, list):
            continue
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            feel_id = str(comment.get("original_feel_id") or "").strip()
            if feel_id:
                feel_ids.add(feel_id)
    return feel_ids


async def build_cleanup_plan(mgr: BucketManager, extra_ids: set[str]) -> list[dict]:
    buckets = await mgr.list_all(include_archive=True)
    migrated_feel_ids = collect_migrated_feel_ids(buckets)
    target_ids = sorted(migrated_feel_ids | extra_ids)
    plan = []

    for bucket_id in target_ids:
        bucket = await mgr.get(bucket_id)
        if not bucket:
            plan.append({"id": bucket_id, "status": "missing", "explicit": bucket_id in extra_ids})
            continue

        meta = bucket.get("metadata", {})
        tags = {str(tag) for tag in meta.get("tags", []) or []}
        explicit = bucket_id in extra_ids
        if not explicit:
            if meta.get("type") not in {"feel", "archived"}:
                plan.append({"id": bucket_id, "status": "skipped", "reason": "not_migrated_feel_type", "type": meta.get("type")})
                continue
            if tags & RELATIONSHIP_WEATHER_TAGS:
                plan.append({"id": bucket_id, "status": "skipped", "reason": "relationship_weather"})
                continue

        plan.append(
            {
                "id": bucket_id,
                "status": "ready",
                "explicit": explicit,
                "type": meta.get("type", "dynamic"),
                "name": meta.get("name", bucket_id),
                "path": bucket.get("path", ""),
            }
        )
    return plan


async def apply_cleanup(plan: list[dict], mgr: BucketManager, embedding_engine: EmbeddingEngine) -> list[dict]:
    results = []
    for item in plan:
        record = dict(item)
        if item.get("status") != "ready":
            results.append(record)
            continue
        deleted = await mgr.delete(item["id"])
        record["status"] = "deleted" if deleted else "delete_failed"
        if deleted:
            try:
                embedding_engine.delete_embedding(item["id"])
                record["embedding_deleted"] = True
            except Exception as exc:
                record["embedding_deleted"] = False
                record["embedding_error"] = str(exc)
        results.append(record)
    return results


def summarize(items: list[dict]) -> dict:
    counts = {}
    for item in items:
        status = item.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete standalone feel buckets that have already been migrated into source bucket comments."
    )
    parser.add_argument("--extra-id", action="append", default=[], help="Additional bucket id to delete explicitly.")
    parser.add_argument("--apply", action="store_true", help="Delete buckets. Default is dry-run.")
    args = parser.parse_args()

    config = load_config()
    mgr = BucketManager(config)
    extra_ids = {str(item).strip() for item in args.extra_id if str(item).strip()}
    plan = await build_cleanup_plan(mgr, extra_ids)

    if args.apply:
        results = await apply_cleanup(plan, mgr, EmbeddingEngine(config))
    else:
        results = [{**item, "status": "dry_run" if item.get("status") == "ready" else item.get("status")} for item in plan]

    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "buckets_dir": config["buckets_dir"],
                "summary": summarize(results),
                "items": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
