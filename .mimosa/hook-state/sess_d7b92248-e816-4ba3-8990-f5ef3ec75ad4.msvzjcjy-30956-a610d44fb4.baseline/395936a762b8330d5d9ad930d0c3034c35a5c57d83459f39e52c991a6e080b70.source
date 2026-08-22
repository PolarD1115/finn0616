#!/usr/bin/env python3
"""One-shot bucket file migration between two Ombre deployments.

This copies Markdown bucket files as files, preserving frontmatter, content,
comments, timestamps, and custom metadata from a heavily modified source.
Default mode is dry-run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bucket_manager import BucketManager
from memory_moments import MemoryMomentStore
from utils import load_config


BUCKET_SUBDIRS = {"dynamic", "permanent", "archive", "feel"}
TOMBSTONE_DIRNAME = ".tombstones"


@dataclass
class BucketFile:
    path: str
    rel_path: str
    bucket_id: str
    title: str
    bucket_type: str
    comment_count: int
    sha256: str
    parse_warning: str = ""


@dataclass
class MigrationItem:
    kind: str
    bucket_id: str
    action: str
    reason: str
    source: str
    target: str
    title: str = ""
    bucket_type: str = ""
    comment_count: int = 0
    source_sha256: str = ""
    target_sha256: str = ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_relative(path: Path, root: Path) -> str:
    rel = path.resolve().relative_to(root.resolve())
    if any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"unsafe relative path: {path}")
    return rel.as_posix()


def infer_bucket_id(path: Path) -> str:
    stem = path.stem.strip()
    match = re.search(r"_([0-9a-fA-F]{8,}|[A-Za-z0-9][A-Za-z0-9_-]{7,})$", stem)
    return (match.group(1) if match else stem).strip()


def coerce_comment_count(value: Any, comments: Any) -> int:
    if isinstance(comments, list):
        fallback = len(comments)
    else:
        fallback = 0
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return fallback


def parse_bucket_file(path: Path, root: Path) -> BucketFile:
    warning = ""
    try:
        post = frontmatter.load(path)
        meta = dict(post.metadata)
    except Exception as exc:
        meta = {}
        warning = f"frontmatter_parse_failed: {exc}"
    bucket_id = str(meta.get("id") or infer_bucket_id(path)).strip()
    title = str(meta.get("name") or meta.get("title") or path.stem).strip()
    rel_parts = path.relative_to(root).parts
    inferred_type = rel_parts[0] if rel_parts and rel_parts[0] in BUCKET_SUBDIRS else "dynamic"
    bucket_type = str(meta.get("type") or inferred_type).strip() or "dynamic"
    comments = meta.get("comments")
    return BucketFile(
        path=str(path),
        rel_path=safe_relative(path, root),
        bucket_id=bucket_id,
        title=title,
        bucket_type=bucket_type,
        comment_count=coerce_comment_count(meta.get("comment_count"), comments),
        sha256=sha256_file(path),
        parse_warning=warning,
    )


def looks_like_bucket_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    if any((path / name).is_dir() for name in BUCKET_SUBDIRS | {TOMBSTONE_DIRNAME}):
        return True
    return any(path.glob("*.md"))


def resolve_source_buckets_dir(source: str) -> Path:
    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"source does not exist: {path}")
    if (path / "buckets").is_dir() and looks_like_bucket_dir(path / "buckets"):
        return (path / "buckets").resolve()
    if looks_like_bucket_dir(path):
        return path
    raise FileNotFoundError(f"cannot find a buckets directory under: {path}")


def resolve_target_buckets_dir(target: str | None) -> Path:
    if target:
        return Path(target).expanduser().resolve()
    config = load_config()
    return Path(str(config["buckets_dir"])).expanduser().resolve()


def iter_bucket_markdown(root: Path) -> list[Path]:
    paths = []
    for path in root.rglob("*.md"):
        if TOMBSTONE_DIRNAME in path.relative_to(root).parts:
            continue
        paths.append(path)
    return sorted(paths)


def collect_bucket_files(root: Path) -> tuple[list[BucketFile], dict[str, list[BucketFile]]]:
    files = [parse_bucket_file(path, root) for path in iter_bucket_markdown(root)]
    by_id: dict[str, list[BucketFile]] = {}
    for item in files:
        by_id.setdefault(item.bucket_id, []).append(item)
    duplicates = {bucket_id: items for bucket_id, items in by_id.items() if len(items) > 1}
    return files, duplicates


def tombstone_id(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = str(payload.get("id") or path.stem).strip()
    except Exception:
        value = path.stem
    return value or path.stem


def collect_tombstones(root: Path) -> list[BucketFile]:
    base = root / TOMBSTONE_DIRNAME
    if not base.is_dir():
        return []
    items: list[BucketFile] = []
    for path in sorted(base.glob("*.json")):
        items.append(
            BucketFile(
                path=str(path),
                rel_path=safe_relative(path, root),
                bucket_id=tombstone_id(path),
                title=path.stem,
                bucket_type="tombstone",
                comment_count=0,
                sha256=sha256_file(path),
            )
        )
    return items


def target_path_for(source_item: BucketFile, source_root: Path, target_root: Path) -> Path:
    rel = Path(source_item.rel_path)
    target = target_root / rel
    target.resolve().relative_to(target_root.resolve())
    return target


def build_bucket_plan(
    source_root: Path,
    target_root: Path,
    *,
    overwrite: bool = False,
) -> tuple[list[MigrationItem], dict[str, Any]]:
    source_files, source_dupes = collect_bucket_files(source_root)
    target_files, target_dupes = collect_bucket_files(target_root) if target_root.exists() else ([], {})
    target_by_id = {item.bucket_id: item for item in target_files if item.bucket_id not in target_dupes}

    items: list[MigrationItem] = []
    for source in source_files:
        base = {
            "kind": "bucket",
            "bucket_id": source.bucket_id,
            "source": source.path,
            "title": source.title,
            "bucket_type": source.bucket_type,
            "comment_count": source.comment_count,
            "source_sha256": source.sha256,
        }
        if source.bucket_id in source_dupes:
            items.append(
                MigrationItem(
                    **base,
                    action="conflict",
                    reason="duplicate_source_id",
                    target="",
                )
            )
            continue
        if source.parse_warning:
            items.append(
                MigrationItem(
                    **base,
                    action="conflict",
                    reason=source.parse_warning,
                    target="",
                )
            )
            continue

        existing = target_by_id.get(source.bucket_id)
        if source.bucket_id in target_dupes:
            items.append(
                MigrationItem(
                    **base,
                    action="conflict",
                    reason="duplicate_target_id",
                    target="",
                )
            )
            continue
        if existing:
            action = "skip"
            reason = "identical"
            if existing.sha256 != source.sha256:
                action = "overwrite" if overwrite else "conflict"
                reason = "target_id_exists"
            items.append(
                MigrationItem(
                    **base,
                    action=action,
                    reason=reason,
                    target=existing.path,
                    target_sha256=existing.sha256,
                )
            )
            continue

        target = target_path_for(source, source_root, target_root)
        if target.exists():
            target_hash = sha256_file(target)
            action = "skip" if target_hash == source.sha256 else "conflict"
            reason = "identical_path" if target_hash == source.sha256 else "target_path_exists"
            items.append(
                MigrationItem(
                    **base,
                    action=action,
                    reason=reason,
                    target=str(target),
                    target_sha256=target_hash,
                )
            )
            continue

        items.append(
            MigrationItem(
                **base,
                action="copy",
                reason="missing_in_target",
                target=str(target),
            )
        )

    summary = {
        "source_count": len(source_files),
        "target_count": len(target_files),
        "source_duplicate_ids": sorted(source_dupes),
        "target_duplicate_ids": sorted(target_dupes),
    }
    return items, summary


def build_tombstone_plan(
    source_root: Path,
    target_root: Path,
    *,
    overwrite: bool = False,
) -> list[MigrationItem]:
    source_items = collect_tombstones(source_root)
    if not source_items:
        return []
    target_files, target_dupes = collect_bucket_files(target_root) if target_root.exists() else ([], {})
    live_target_ids = {item.bucket_id for item in target_files}
    target_tombstones = {item.bucket_id: item for item in collect_tombstones(target_root)} if target_root.exists() else {}

    items: list[MigrationItem] = []
    for source in source_items:
        base = {
            "kind": "tombstone",
            "bucket_id": source.bucket_id,
            "source": source.path,
            "title": source.title,
            "bucket_type": "tombstone",
            "comment_count": 0,
            "source_sha256": source.sha256,
        }
        if source.bucket_id in live_target_ids or source.bucket_id in target_dupes:
            items.append(
                MigrationItem(
                    **base,
                    action="conflict",
                    reason="target_live_bucket_exists",
                    target="",
                )
            )
            continue
        existing = target_tombstones.get(source.bucket_id)
        if existing:
            action = "skip"
            reason = "identical"
            if existing.sha256 != source.sha256:
                action = "overwrite" if overwrite else "conflict"
                reason = "target_tombstone_exists"
            items.append(
                MigrationItem(
                    **base,
                    action=action,
                    reason=reason,
                    target=existing.path,
                    target_sha256=existing.sha256,
                )
            )
            continue
        items.append(
            MigrationItem(
                **base,
                action="copy",
                reason="missing_in_target",
                target=str(target_path_for(source, source_root, target_root)),
            )
        )
    return items


def summarize_items(items: list[MigrationItem]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in items:
        key = f"{item.kind}_{item.action}"
        summary[key] = summary.get(key, 0) + 1
    summary["total"] = len(items)
    return summary


def default_backup_dir(target_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return target_root.parent / "state" / "backups" / f"bucket-file-migration-{stamp}"


def copy_with_backup(item: MigrationItem, backup_root: Path | None) -> bool:
    source = Path(item.source)
    target = Path(item.target)
    if item.action not in {"copy", "overwrite"}:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and item.action == "overwrite":
        if backup_root is None:
            raise ValueError("overwrite requires backup_root")
        backup = backup_root / target.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
    shutil.copy2(source, target)
    return True


def apply_plan(items: list[MigrationItem], backup_root: Path | None) -> dict[str, Any]:
    written = []
    failed = []
    if backup_root is not None:
        backup_root.mkdir(parents=True, exist_ok=True)
    for item in items:
        if item.action not in {"copy", "overwrite"}:
            continue
        try:
            copy_with_backup(item, backup_root)
            written.append(item.bucket_id)
        except Exception as exc:
            failed.append({**asdict(item), "error": str(exc)})
    return {
        "written_count": len(written),
        "written_ids": written,
        "failed": failed,
        "backup_dir": str(backup_root) if backup_root else "",
    }


async def refresh_moment_index(target_root: Path, state_dir: str | None, include_archive: bool) -> dict[str, Any]:
    config = load_config()
    config["buckets_dir"] = str(target_root)
    if state_dir:
        config["state_dir"] = str(Path(state_dir).expanduser().resolve())
    elif not config.get("state_dir"):
        config["state_dir"] = str(target_root.parent / "state")
    mgr = BucketManager(config)
    store = MemoryMomentStore(config)
    buckets = await mgr.list_all(include_archive=include_archive)
    stats = store.bulk_upsert(buckets)
    return {
        "bucket_count": len(buckets),
        "include_archive": include_archive,
        "indexed": stats,
    }


def write_json(path: str, payload: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy bucket Markdown files from an old Ombre deployment into a target deployment."
    )
    parser.add_argument("--source", required=True, help="Old deployment root or buckets directory.")
    parser.add_argument("--target-buckets-dir", default="", help="Target buckets directory. Defaults to config buckets_dir.")
    parser.add_argument("--target-state-dir", default="", help="Target state directory for --refresh-moments.")
    parser.add_argument("--include-tombstones", action="store_true", help="Also copy .tombstones JSON files.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite target files when the same bucket id differs.")
    parser.add_argument("--apply", action="store_true", help="Copy files. Default is dry-run only.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before applying.")
    parser.add_argument("--output", default="", help="Write JSON report to this path.")
    parser.add_argument("--backup-dir", default="", help="Backup directory for overwritten target files.")
    parser.add_argument("--refresh-moments", action="store_true", help="Refresh v2 memory_moments index after apply.")
    parser.add_argument("--refresh-archive-moments", action="store_true", help="Include archive buckets when refreshing moments.")
    return parser.parse_args(argv)


def prompt_confirm() -> bool:
    answer = input("Apply bucket file migration? Type MIGRATE to continue: ").strip()
    return answer == "MIGRATE"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_root = resolve_source_buckets_dir(args.source)
    target_root = resolve_target_buckets_dir(args.target_buckets_dir or None)
    if source_root == target_root:
        print(f"ERROR: source and target buckets dir are the same: {source_root}", file=sys.stderr)
        return 2

    items, base_summary = build_bucket_plan(source_root, target_root, overwrite=bool(args.overwrite))
    if args.include_tombstones:
        items.extend(build_tombstone_plan(source_root, target_root, overwrite=bool(args.overwrite)))

    payload: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry_run",
        "source_buckets_dir": str(source_root),
        "target_buckets_dir": str(target_root),
        "include_tombstones": bool(args.include_tombstones),
        "overwrite": bool(args.overwrite),
        "summary": {**base_summary, **summarize_items(items)},
        "items": [asdict(item) for item in items],
    }

    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    conflicts = [item for item in items if item.action == "conflict"]
    if conflicts:
        print(f"Conflicts: {len(conflicts)}", file=sys.stderr)

    if not args.apply:
        write_json(args.output, payload)
        print("Dry run only. Re-run with --apply after reviewing the report.")
        return 0

    if conflicts:
        print("ERROR: resolve conflicts before --apply, or use --overwrite for same-id target conflicts.", file=sys.stderr)
        write_json(args.output, payload)
        return 3
    if not args.yes and not prompt_confirm():
        print("Cancelled.")
        write_json(args.output, payload)
        return 0

    backup_root = Path(args.backup_dir).expanduser().resolve() if args.backup_dir else default_backup_dir(target_root)
    apply_result = apply_plan(items, backup_root)
    payload["apply_result"] = apply_result

    if args.refresh_moments and apply_result["written_count"]:
        payload["moment_refresh"] = asyncio.run(
            refresh_moment_index(
                target_root,
                args.target_state_dir or None,
                include_archive=bool(args.refresh_archive_moments),
            )
        )

    write_json(args.output, payload)
    print(json.dumps(apply_result, ensure_ascii=False, indent=2))
    if args.refresh_moments and "moment_refresh" in payload:
        print(json.dumps({"moment_refresh": payload["moment_refresh"]}, ensure_ascii=False, indent=2))
    return 4 if apply_result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
