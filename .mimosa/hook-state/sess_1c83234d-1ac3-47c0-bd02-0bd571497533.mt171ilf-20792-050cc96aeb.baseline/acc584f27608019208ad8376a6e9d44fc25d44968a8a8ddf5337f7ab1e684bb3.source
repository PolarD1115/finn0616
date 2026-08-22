#!/usr/bin/env python3
"""Find duplicate memory buckets and optionally delete safe duplicates."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import jieba
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine
from utils import load_config, strip_affect_anchor


NON_DELETABLE_TYPES = {"permanent", "feel", "archived"}


@dataclass
class DuplicatePlan:
    key: str
    keep_id: str
    delete_ids: list[str]
    bucket_ids: list[str]


def normalize_content(text: str) -> str:
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", str(text or ""))
    text = strip_affect_anchor(text)
    text = re.sub(r"[\s\u3000]+", "", text.lower())
    return re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "", text)


def similarity_text(text: str) -> str:
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", str(text or "").lower())
    text = strip_affect_anchor(text)
    text = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", " ", text)
    return " ".join(token for token in jieba.lcut(text) if token.strip())


def is_deletable(bucket: dict) -> bool:
    meta = bucket.get("metadata", {}) if isinstance(bucket, dict) else {}
    if meta.get("type") in NON_DELETABLE_TYPES:
        return False
    if meta.get("pinned") or meta.get("protected") or meta.get("anchor"):
        return False
    if meta.get("comments"):
        return False
    return True


def keep_score(bucket: dict) -> tuple[int, int, int, int, str]:
    meta = bucket.get("metadata", {}) if isinstance(bucket, dict) else {}
    protected_score = int(bool(meta.get("protected") or meta.get("pinned") or meta.get("anchor")))
    permanent_score = int(meta.get("type") == "permanent")
    try:
        importance = int(meta.get("importance", 5))
    except (TypeError, ValueError):
        importance = 5
    try:
        activation_count = int(meta.get("activation_count", 0))
    except (TypeError, ValueError):
        activation_count = 0
    created = str(meta.get("created") or "")
    return (protected_score, permanent_score, importance, activation_count, created)


def title(bucket: dict) -> str:
    meta = bucket.get("metadata", {}) if isinstance(bucket, dict) else {}
    return str(meta.get("name") or bucket.get("id") or "")


def content_preview(bucket: dict, max_chars: int = 120) -> str:
    text = " ".join(str(bucket.get("content") or "").split())
    sentences = re.split(r"(?<=[。！？!?])", text)
    preview = "".join(part for part in sentences[:2]).strip() or text
    if len(preview) > max_chars:
        preview = preview[:max_chars].rstrip() + "..."
    return preview


def print_exact_plan(plan: DuplicatePlan, bucket_map: dict[str, dict]) -> None:
    keep_bucket = bucket_map.get(plan.keep_id, {})
    print(f"[exact 100.0] keep {plan.keep_id} {title(keep_bucket)}")
    print(f"  preview: {content_preview(keep_bucket)}")
    for bucket_id in plan.delete_ids:
        bucket = bucket_map.get(bucket_id, {})
        print(f"  delete {bucket_id} {title(bucket)}")
        print(f"    preview: {content_preview(bucket)}")


def print_near_pair(left_id: str, right_id: str, score: float, bucket_map: dict[str, dict]) -> None:
    left_bucket = bucket_map.get(left_id, {})
    right_bucket = bucket_map.get(right_id, {})
    print(f"{score:.1f}  {left_id} {title(left_bucket)}  <->  {right_id} {title(right_bucket)}")
    print(f"  left:  {content_preview(left_bucket)}")
    print(f"  right: {content_preview(right_bucket)}")


def exact_duplicate_plans(buckets: list[dict], min_chars: int = 20) -> list[DuplicatePlan]:
    grouped: dict[str, list[dict]] = {}
    for bucket in buckets:
        normalized = normalize_content(bucket.get("content", ""))
        if len(normalized) < min_chars:
            continue
        grouped.setdefault(normalized, []).append(bucket)

    plans: list[DuplicatePlan] = []
    for key, group in grouped.items():
        if len(group) < 2:
            continue
        keep = max(group, key=keep_score)
        delete_ids = [
            str(bucket.get("id"))
            for bucket in group
            if bucket.get("id") != keep.get("id") and is_deletable(bucket)
        ]
        if delete_ids:
            plans.append(
                DuplicatePlan(
                    key=key,
                    keep_id=str(keep.get("id")),
                    delete_ids=delete_ids,
                    bucket_ids=[str(bucket.get("id")) for bucket in group],
                )
            )
    return plans


def near_duplicate_pairs(
    buckets: list[dict],
    threshold: float = 88.0,
    min_chars: int = 40,
    limit: int = 20,
    exclude_pairs: set[frozenset[str]] | None = None,
) -> list[tuple[str, str, float]]:
    exclude_pairs = exclude_pairs or set()
    candidates = [
        bucket for bucket in buckets
        if len(normalize_content(bucket.get("content", ""))) >= min_chars
    ]
    prepared = [
        (str(bucket.get("id")), similarity_text(bucket.get("content", "")))
        for bucket in candidates
    ]
    pairs: list[tuple[str, str, float]] = []
    for index, (left_id, left_text) in enumerate(prepared):
        for right_index, (right_id, right_text) in enumerate(prepared[index + 1:], start=index + 1):
            if frozenset((left_id, right_id)) in exclude_pairs:
                continue
            left_bucket = candidates[index]
            right_bucket = candidates[right_index]
            if not is_deletable(left_bucket) and not is_deletable(right_bucket):
                continue
            score = float(fuzz.token_set_ratio(left_text, right_text))
            if score >= threshold:
                pairs.append((left_id, right_id, score))
    pairs.sort(key=lambda item: item[2], reverse=True)
    return pairs[: max(0, limit)]


def suggested_near_action(left_id: str, right_id: str, bucket_map: dict[str, dict]) -> tuple[str | None, str | None]:
    left_bucket = bucket_map.get(left_id, {})
    right_bucket = bucket_map.get(right_id, {})
    left_deletable = is_deletable(left_bucket)
    right_deletable = is_deletable(right_bucket)

    if left_deletable and not right_deletable:
        return right_id, left_id
    if right_deletable and not left_deletable:
        return left_id, right_id
    if not left_deletable and not right_deletable:
        return None, None

    keep_id = max((left_id, right_id), key=lambda bucket_id: keep_score(bucket_map[bucket_id]))
    delete_id = right_id if keep_id == left_id else left_id
    return keep_id, delete_id


async def apply_delete_plan(
    bucket_mgr: BucketManager,
    embedding_engine: EmbeddingEngine,
    plans: list[DuplicatePlan],
) -> list[str]:
    deleted: list[str] = []
    for plan in plans:
        for bucket_id in plan.delete_ids:
            if await bucket_mgr.delete(bucket_id):
                embedding_engine.delete_embedding(bucket_id)
                deleted.append(bucket_id)
    return deleted


async def delete_bucket(
    bucket_mgr: BucketManager,
    embedding_engine: EmbeddingEngine,
    bucket_id: str,
) -> bool:
    if not await bucket_mgr.delete(bucket_id):
        return False
    embedding_engine.delete_embedding(bucket_id)
    return True


async def interactive_cleanup(
    bucket_mgr: BucketManager,
    embedding_engine: EmbeddingEngine,
    plans: list[DuplicatePlan],
    near_pairs: list[tuple[str, str, float]],
    bucket_map: dict[str, dict],
) -> list[str]:
    deleted: list[str] = []
    deleted_set: set[str] = set()

    if plans:
        print("\nInteractive exact duplicate cleanup:")
    for index, plan in enumerate(plans, start=1):
        live_delete_ids = [bucket_id for bucket_id in plan.delete_ids if bucket_id not in deleted_set]
        if not live_delete_ids:
            continue
        print(f"\nExact group {index}/{len(plans)}")
        print_exact_plan(
            DuplicatePlan(
                key=plan.key,
                keep_id=plan.keep_id,
                delete_ids=live_delete_ids,
                bucket_ids=plan.bucket_ids,
            ),
            bucket_map,
        )
        answer = input("Delete this group's listed exact duplicate copies? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            continue
        for bucket_id in live_delete_ids:
            if await delete_bucket(bucket_mgr, embedding_engine, bucket_id):
                deleted.append(bucket_id)
                deleted_set.add(bucket_id)
                print(f"  deleted {bucket_id}")

    if near_pairs:
        print("\nInteractive near duplicate review:")
        print("Choose y to delete the suggested copy, 1/2 to delete left/right, or Enter to skip.")
    for index, (left_id, right_id, score) in enumerate(near_pairs, start=1):
        if left_id in deleted_set or right_id in deleted_set:
            continue
        left_bucket = bucket_map.get(left_id)
        right_bucket = bucket_map.get(right_id)
        if not left_bucket or not right_bucket:
            continue

        print(f"\nNear pair {index}/{len(near_pairs)}")
        print_near_pair(left_id, right_id, score, bucket_map)
        suggested_keep_id, suggested_delete_id = suggested_near_action(left_id, right_id, bucket_map)
        if not suggested_delete_id:
            print("  skipped: neither side is safe to delete.")
            continue
        print(f"  suggested keep: {suggested_keep_id}")
        print(f"  suggested delete if they are the same memory: {suggested_delete_id}")
        print(f"  [1] delete left:  {left_id}")
        print(f"  [2] delete right: {right_id}")

        answer = input("Choice [y/1/2/Enter]: ").strip().lower()
        if not answer:
            continue
        if answer in {"y", "yes"}:
            delete_id = suggested_delete_id
        elif answer == "1":
            delete_id = left_id
        elif answer == "2":
            delete_id = right_id
        else:
            print("  skipped: enter y, 1, 2, or press Enter.")
            continue
        bucket = bucket_map.get(delete_id)
        if not bucket or not is_deletable(bucket):
            print("  skipped: this bucket is protected, pinned, anchored, permanent, or has comments.")
            continue
        if await delete_bucket(bucket_mgr, embedding_engine, delete_id):
            deleted.append(delete_id)
            deleted_set.add(delete_id)
            print(f"  deleted {delete_id}")

    print(f"\nDeleted duplicate buckets: {len(deleted)}")
    for bucket_id in deleted:
        print(f"  {bucket_id}")
    return deleted


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true", help="Delete exact duplicate bucket files.")
    parser.add_argument("--interactive", action="store_true", help="Review exact and near duplicate candidates one by one.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--limit", type=int, default=20, help="How many groups/pairs to print.")
    parser.add_argument("--near-threshold", type=float, default=88.0, help="Similarity threshold for near duplicate review.")
    parser.add_argument("--min-chars", type=int, default=20, help="Minimum normalized content length for exact duplicate cleanup.")
    args = parser.parse_args()

    config = load_config()
    bucket_mgr = BucketManager(config)
    embedding_engine = EmbeddingEngine(config)
    buckets = await bucket_mgr.list_all(include_archive=False)
    bucket_map = {str(bucket.get("id")): bucket for bucket in buckets}

    plans = exact_duplicate_plans(buckets, min_chars=args.min_chars)
    exact_pairs = {
        frozenset((left_id, right_id))
        for plan in plans
        for index, left_id in enumerate(plan.bucket_ids)
        for right_id in plan.bucket_ids[index + 1:]
    }
    near_pairs = near_duplicate_pairs(
        buckets,
        threshold=args.near_threshold,
        min_chars=max(40, args.min_chars),
        limit=args.limit,
        exclude_pairs=exact_pairs,
    )

    print(f"Buckets scanned: {len(buckets)}")
    print(f"Exact duplicate groups: {len(plans)}")
    print(f"Buckets safe to delete: {sum(len(plan.delete_ids) for plan in plans)}")

    if not args.interactive:
        for plan in plans[: max(0, args.limit)]:
            print()
            print_exact_plan(plan, bucket_map)
        if len(plans) > args.limit:
            print(f"\n... and {len(plans) - args.limit} more exact groups")

        if near_pairs:
            print("\nNear duplicates for manual review only:")
            for left_id, right_id, score in near_pairs:
                print("  ", end="")
                print_near_pair(left_id, right_id, score, bucket_map)

    if args.interactive:
        await interactive_cleanup(bucket_mgr, embedding_engine, plans, near_pairs, bucket_map)
        return 0

    if not args.delete or not plans:
        return 0

    if not args.yes:
        answer = input("Delete the exact duplicate buckets listed above? Type DELETE_DUPLICATES to continue: ")
        if answer != "DELETE_DUPLICATES":
            print("Canceled.")
            return 0

    deleted = await apply_delete_plan(bucket_mgr, embedding_engine, plans)
    print(f"Deleted duplicate buckets: {len(deleted)}")
    for bucket_id in deleted:
        print(f"  {bucket_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
