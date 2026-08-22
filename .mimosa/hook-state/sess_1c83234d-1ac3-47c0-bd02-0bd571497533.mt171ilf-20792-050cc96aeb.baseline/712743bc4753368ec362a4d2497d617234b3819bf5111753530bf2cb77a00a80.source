#!/usr/bin/env python3
"""
Bidirectional sync between Ombre Brain bucket Markdown files and Supabase.

Default mode is dry-run. Use --apply to write local files or upsert Supabase.
Remote records authored by C-side clients must use source="chatgpt".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


DEFAULT_SUPABASE_URL = "https://nuhbpesfpoywzcxlqfhs.supabase.co"
DEFAULT_BUCKETS_DIR = "/srv/ombre-brain/buckets"
TABLE_NAME = "memories"
TOMBSTONE_DIRNAME = ".tombstones"
SYNC_FIELDS = (
    "id",
    "title",
    "type",
    "domain",
    "tags",
    "content",
    "valence",
    "arousal",
    "confidence",
    "importance",
    "period",
    "date",
    "pinned",
    "anchor",
    "resolved",
    "digested",
    "comments",
    "comment_count",
    "source",
)
SUPABASE_FIELDS = SYNC_FIELDS + ("created", "last_active", "updated_at", "activation_count")


@dataclass
class Plan:
    to_push: list[dict[str, Any]]
    to_pull: list[dict[str, Any]]
    to_delete_local: list[dict[str, Any]]
    conflicts: list[str]
    duplicate_local_ids: dict[str, list[str]]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: Any) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sanitize_filename(value: str) -> str:
    value = (value or "").strip() or "未命名"
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:80] or "未命名"


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def ensure_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in SUPABASE_FIELDS if key in record}


def is_tombstone(record: dict[str, Any] | None) -> bool:
    return bool(record) and str(record.get("source") or "") == "deleted"


def _stable_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _stable_value(value[key]) for key in sorted(value)}
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 6)
    return value


def sync_fingerprint(record: dict[str, Any]) -> str:
    payload = {field: _stable_value(record.get(field)) for field in SYNC_FIELDS}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sync_payload_changed(local: dict[str, Any], remote: dict[str, Any]) -> bool:
    return sync_fingerprint(local) != sync_fingerprint(remote)


def content_time(record: dict[str, Any]) -> datetime:
    return parse_time(
        record.get("updated_at")
        or record.get("_file_updated_at")
        or record.get("created")
        or record.get("last_active")
    )


def parse_md(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"WARN read failed: {path}: {exc}", file=sys.stderr)
        return None
    match = re.match(r"^---\n(.*?)\n---\n?(.*)", raw, re.DOTALL)
    if not match:
        print(f"WARN missing frontmatter: {path}", file=sys.stderr)
        return None
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        print(f"WARN yaml failed: {path}: {exc}", file=sys.stderr)
        return None

    bucket_id = str(meta.get("id") or path.stem)
    title = str(meta.get("name") or meta.get("title") or path.stem)
    created = meta.get("created") or format_time(now_utc())
    last_active = meta.get("last_active") or created
    updated_at = meta.get("updated_at")
    file_updated_at = format_time(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    return {
        "id": bucket_id,
        "title": title,
        "type": meta.get("type", "dynamic"),
        "domain": ensure_list(meta.get("domain")) or ["未分类"],
        "tags": ensure_list(meta.get("tags")),
        "content": match.group(2).strip(),
        "valence": float(meta.get("valence", 0.5)),
        "arousal": float(meta.get("arousal", 0.5)),
        "confidence": float(meta.get("confidence", 0.5)),
        "importance": float(meta.get("importance", 1.0)),
        "period": str(meta.get("period")) if meta.get("period") else None,
        "date": str(meta.get("date")) if meta.get("date") else None,
        "pinned": ensure_bool(meta.get("pinned", False)),
        "anchor": ensure_bool(meta.get("anchor", False)),
        "resolved": ensure_bool(meta.get("resolved", False)),
        "digested": ensure_bool(meta.get("digested", False)),
        "comments": ensure_list(meta.get("comments")),
        "comment_count": int(float(meta.get("comment_count", len(ensure_list(meta.get("comments")))))),
        "activation_count": int(float(meta.get("activation_count", 0))),
        "created": str(created),
        "last_active": str(last_active),
        "updated_at": str(updated_at) if updated_at else file_updated_at,
        "source": str(meta.get("source") or "ombre"),
        "_path": str(path),
        "_file_updated_at": file_updated_at,
    }


def record_to_md(record: dict[str, Any], path: Path) -> None:
    existing = record.get("_existing_local") or {}
    meta = {
        "id": record["id"],
        "name": record.get("title") or record["id"],
        "type": record.get("type", "dynamic"),
        "domain": ensure_list(record.get("domain")) or ["未分类"],
        "tags": ensure_list(record.get("tags")),
        "valence": float(record.get("valence", 0.5)),
        "arousal": float(record.get("arousal", 0.5)),
        "confidence": float(record.get("confidence", 0.5)),
        "importance": float(record.get("importance", 1.0)),
        "period": record.get("period") or None,
        "date": record.get("date") or None,
        "pinned": ensure_bool(record.get("pinned", False)),
        "anchor": ensure_bool(record.get("anchor", False)),
        "resolved": ensure_bool(record.get("resolved", False)),
        "digested": ensure_bool(record.get("digested", False)),
        "comments": ensure_list(record.get("comments")),
        "comment_count": int(float(record.get("comment_count", len(ensure_list(record.get("comments")))))),
        "activation_count": int(float(existing.get("activation_count", record.get("activation_count", 0)))),
        "created": str(record.get("created") or format_time(now_utc())),
        "last_active": str(
            existing.get("last_active")
            or record.get("last_active")
            or record.get("created")
            or format_time(now_utc())
        ),
        "updated_at": str(
            record.get("updated_at")
            or record.get("created")
            or record.get("last_active")
            or format_time(now_utc())
        ),
        "source": str(record.get("source") or "chatgpt"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter_text = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    content = str(record.get("content") or "").strip()
    path.write_text(f"---\n{frontmatter_text}\n---\n{content}\n", encoding="utf-8")


def tombstone_path(buckets_dir: Path, bucket_id: str) -> Path:
    return buckets_dir / TOMBSTONE_DIRNAME / f"{bucket_id}.json"


def tombstone_record(bucket_id: str, *, title: str = "", deleted_at: str | None = None) -> dict[str, Any]:
    timestamp = deleted_at or format_time(now_utc())
    return {
        "id": bucket_id,
        "title": title or bucket_id,
        "type": "archived",
        "domain": ["deleted"],
        "tags": ["deleted"],
        "content": "",
        "valence": 0.5,
        "arousal": 0.5,
        "confidence": 0.5,
        "importance": 1,
        "period": None,
        "date": None,
        "pinned": False,
        "anchor": False,
        "resolved": True,
        "digested": True,
        "comments": [],
        "comment_count": 0,
        "activation_count": 0,
        "created": timestamp,
        "last_active": timestamp,
        "updated_at": timestamp,
        "source": "deleted",
        "_deleted_at": timestamp,
        "_tombstone": True,
    }


def write_tombstone(record: dict[str, Any], buckets_dir: Path) -> None:
    path = tombstone_path(buckets_dir, str(record["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload["source"] = "deleted"
    payload.setdefault("_deleted_at", payload.get("updated_at") or format_time(now_utc()))
    payload["_tombstone"] = True
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_tombstone(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARN tombstone failed: {path}: {exc}", file=sys.stderr)
        return None
    bucket_id = str(raw.get("id") or path.stem)
    record = tombstone_record(
        bucket_id,
        title=str(raw.get("title") or raw.get("name") or bucket_id),
        deleted_at=str(raw.get("_deleted_at") or raw.get("deleted_at") or raw.get("updated_at") or format_time(now_utc())),
    )
    for key in SUPABASE_FIELDS:
        if key in raw:
            record[key] = raw[key]
    record["source"] = "deleted"
    record["_path"] = str(path)
    record["_tombstone"] = True
    record["_deleted_at"] = str(raw.get("_deleted_at") or raw.get("deleted_at") or record["updated_at"])
    return record


def collect_local_md(buckets_dir: Path) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    records: list[dict[str, Any]] = []
    seen: dict[str, list[str]] = defaultdict(list)
    for path in sorted(buckets_dir.rglob("*.md")):
        record = parse_md(path)
        if record is None:
            continue
        records.append(record)
        seen[record["id"]].append(str(path))
    duplicates = {bucket_id: paths for bucket_id, paths in seen.items() if len(paths) > 1}
    return records, duplicates


def collect_local_tombstones(buckets_dir: Path) -> list[dict[str, Any]]:
    tombstones: list[dict[str, Any]] = []
    root = buckets_dir / TOMBSTONE_DIRNAME
    if not root.exists():
        return tombstones
    for path in sorted(root.glob("*.json")):
        record = parse_tombstone(path)
        if record is not None:
            tombstones.append(record)
    return tombstones


def local_path_for_record(record: dict[str, Any], buckets_dir: Path) -> Path:
    bucket_type = str(record.get("type") or "dynamic")
    if bucket_type == "archived":
        folder_type = "archive"
    else:
        folder_type = bucket_type

    domains = ensure_list(record.get("domain"))
    if bucket_type == "feel":
        primary_domain = "沉淀物"
    else:
        primary_domain = sanitize_filename(str(domains[0])) if domains else "未分类"

    bucket_id = str(record["id"])
    title = sanitize_filename(str(record.get("title") or bucket_id))
    if title and title != bucket_id:
        filename = f"{title}_{bucket_id}.md"
    else:
        filename = f"{bucket_id}.md"
    return buckets_dir / folder_type / primary_domain / filename


def build_plan(
    local_records: list[dict[str, Any]],
    remote_records: list[dict[str, Any]],
    duplicate_local_ids: dict[str, list[str]] | None = None,
) -> Plan:
    duplicate_local_ids = duplicate_local_ids or {}
    local_map = {record["id"]: record for record in local_records if not is_tombstone(record)}
    local_tombstones = {record["id"]: record for record in local_records if is_tombstone(record)}
    remote_map = {str(record["id"]): record for record in remote_records if record.get("id")}
    remote_tombstones = {bucket_id: record for bucket_id, record in remote_map.items() if is_tombstone(record)}
    to_push: list[dict[str, Any]] = []
    to_pull: list[dict[str, Any]] = []
    to_delete_local: list[dict[str, Any]] = []
    conflicts: list[str] = []

    for bucket_id, tombstone in local_tombstones.items():
        local_file = local_map.get(bucket_id)
        if local_file is not None:
            to_delete_local.append(local_file)
        remote = remote_map.get(bucket_id)
        if remote is None or not is_tombstone(remote) or content_time(tombstone) > content_time(remote):
            to_push.append(tombstone)

    for bucket_id, local in local_map.items():
        if bucket_id in remote_tombstones:
            to_delete_local.append(local)
            continue

        remote = remote_map.get(bucket_id)
        if remote is None:
            to_push.append(local)
            continue

        if not sync_payload_changed(local, remote):
            continue

        local_time = content_time(local)
        remote_time = content_time(remote)
        local_source = str(local.get("source") or "ombre")
        remote_source = str(remote.get("source") or "")

        if remote_source == "chatgpt" and local_source != "chatgpt" and remote_time > local_time:
            conflicts.append(f"{bucket_id}: remote chatgpt is newer than local ombre")
            continue
        if local_time > remote_time:
            to_push.append(local)

    for bucket_id, remote in remote_map.items():
        if is_tombstone(remote):
            local = local_map.get(bucket_id)
            if local is not None:
                to_delete_local.append(local)
            continue
        if bucket_id in local_tombstones:
            continue
        if str(remote.get("source") or "") != "chatgpt":
            continue
        local = local_map.get(bucket_id)
        if local is None:
            to_pull.append(remote)
            continue
        if sync_payload_changed(local, remote) and content_time(remote) > content_time(local):
            to_pull.append(remote)

    seen_delete_paths = set()
    unique_deletes = []
    for record in to_delete_local:
        key = record.get("_path") or record.get("id")
        if key in seen_delete_paths:
            continue
        seen_delete_paths.add(key)
        unique_deletes.append(record)

    return Plan(
        to_push=to_push,
        to_pull=to_pull,
        to_delete_local=unique_deletes,
        conflicts=conflicts,
        duplicate_local_ids=duplicate_local_ids,
    )


class SupabaseClient:
    def __init__(self, url: str, service_key: str):
        self.url = url.rstrip("/")
        self.service_key = service_key

    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def get_all(self) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"select": "*", "order": "created.desc"})
        request = urllib.request.Request(
            f"{self.url}/rest/v1/{TABLE_NAME}?{query}",
            headers=self._headers(),
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))

    def upsert(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        now = format_time(now_utc())
        payload = []
        for record in records:
            item = public_record(record)
            item["synced_at"] = now
            payload.append(item)

        for index in range(0, len(payload), 20):
            batch = payload[index:index + 20]
            body = json.dumps(batch, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                f"{self.url}/rest/v1/{TABLE_NAME}",
                data=body,
                headers=self._headers("resolution=merge-duplicates"),
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                response.read()
            print(f"  ↑ pushed batch {index // 20 + 1}: {len(batch)}")


def apply_pull(records: list[dict[str, Any]], local_map: dict[str, dict[str, Any]], buckets_dir: Path) -> None:
    for record in records:
        existing = local_map.get(str(record["id"]), {})
        existing_path = existing.get("_path")
        path = Path(existing_path) if existing_path else local_path_for_record(record, buckets_dir)
        record["_existing_local"] = existing
        record_to_md(record, path)
        print(f"  ↓ wrote {path}")


def apply_delete_local(records: list[dict[str, Any]], buckets_dir: Path) -> None:
    for record in records:
        path_value = record.get("_path")
        if path_value:
            path = Path(path_value)
            if path.exists():
                path.unlink()
                print(f"  × deleted {path}")
        tombstone = tombstone_record(
            str(record["id"]),
            title=str(record.get("title") or record.get("name") or record["id"]),
            deleted_at=record.get("updated_at") or format_time(now_utc()),
        )
        write_tombstone(tombstone, buckets_dir)


def print_plan(plan: Plan) -> None:
    print(f"Push to Supabase: {len(plan.to_push)}")
    print(f"Pull to local:     {len(plan.to_pull)}")
    print(f"Delete local:      {len(plan.to_delete_local)}")
    print(f"Conflicts:         {len(plan.conflicts)}")
    print(f"Duplicate local:   {len(plan.duplicate_local_ids)}")
    for label, records in (("push", plan.to_push), ("pull", plan.to_pull)):
        for record in records[:10]:
            print(f"  {label}: {record.get('id')} {record.get('title')}")
        if len(records) > 10:
            print(f"  {label}: ... {len(records) - 10} more")
    for record in plan.to_delete_local[:10]:
        print(f"  delete-local: {record.get('id')} {record.get('title')}")
    if len(plan.to_delete_local) > 10:
        print(f"  delete-local: ... {len(plan.to_delete_local) - 10} more")
    for conflict in plan.conflicts[:10]:
        print(f"  conflict: {conflict}")
    for bucket_id, paths in list(plan.duplicate_local_ids.items())[:10]:
        print(f"  duplicate: {bucket_id} -> {paths}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Ombre Brain buckets with Supabase.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this, only prints a dry-run plan.")
    parser.add_argument("--direction", choices=["both", "push", "pull"], default="both")
    parser.add_argument("--buckets-dir", default=os.environ.get("OMBRE_BUCKETS_DIR", DEFAULT_BUCKETS_DIR))
    parser.add_argument("--supabase-url", default=os.environ.get("SUPABASE_URL", DEFAULT_SUPABASE_URL))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not service_key:
        print("ERROR: SUPABASE_SERVICE_KEY is not set", file=sys.stderr)
        return 2

    buckets_dir = Path(args.buckets_dir)
    local_records, duplicates = collect_local_md(buckets_dir)
    local_records.extend(collect_local_tombstones(buckets_dir))
    remote_records = SupabaseClient(args.supabase_url, service_key).get_all()
    local_map = {record["id"]: record for record in local_records if not is_tombstone(record)}
    plan = build_plan(local_records, remote_records, duplicates)

    if args.direction == "push":
        plan.to_pull = []
        plan.to_delete_local = []
    elif args.direction == "pull":
        plan.to_push = []

    print(f"Local records:  {len(local_records)}")
    print(f"Remote records: {len(remote_records)}")
    print_plan(plan)

    if duplicates:
        print("ERROR: duplicate local ids found; clean them before syncing.", file=sys.stderr)
        return 3

    if not args.apply:
        print("Dry run only. Re-run with --apply to write changes.")
        return 0

    client = SupabaseClient(args.supabase_url, service_key)
    if plan.to_push:
        client.upsert(plan.to_push)
    if plan.to_delete_local:
        apply_delete_local(plan.to_delete_local, buckets_dir)
    if plan.to_pull:
        apply_pull(plan.to_pull, local_map, buckets_dir)
    print("Sync complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
