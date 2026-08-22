#!/usr/bin/env python3
"""Merge legacy active darkroom entries into one room.

This only touches active entries that do not already have room_id. It is meant
for one-time upgrades from the old per-entry draft shape.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def default_state_dir() -> Path:
    return Path(os.environ.get("OMBRE_STATE_DIR") or "state")


def now_stamp() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S")


def entry_time(entry: dict) -> str:
    return str(entry.get("created_at") or entry.get("entered_at") or entry.get("id") or "")


def is_legacy_active(entry: dict) -> bool:
    visibility = str(entry.get("visibility") or "active").strip().lower()
    return visibility == "active" and not entry.get("room_id")


def load_entries(path: Path) -> tuple[list[dict], int]:
    entries: list[dict] = []
    invalid_count = 0
    if not path.exists():
        return entries, invalid_count
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                invalid_count += 1
                continue
            if isinstance(data, dict):
                entries.append(data)
            else:
                invalid_count += 1
    return entries, invalid_count


def build_plan(entries: list[dict], room_id: str | None = None) -> dict:
    targets = [entry for entry in entries if is_legacy_active(entry)]
    targets.sort(key=entry_time)
    room_id = room_id or f"room_{secrets.token_hex(6)}"
    previous_entry: dict | None = None
    changes = []

    for index, entry in enumerate(targets, start=1):
        changes.append(
            {
                "id": entry.get("id", ""),
                "created_at": entry.get("created_at", ""),
                "revision": index,
                "previous_entry_id": previous_entry.get("id", "") if previous_entry else "",
                "previous_completeness": previous_entry.get("completeness") if previous_entry else None,
            }
        )
        previous_entry = entry

    return {
        "status": "planned",
        "room_id": room_id,
        "eligible_count": len(targets),
        "target_entry_ids": [str(entry.get("id") or "") for entry in targets if entry.get("id")],
        "changes": changes,
    }


def apply_plan(entries: list[dict], plan: dict, entries_path: Path) -> Path:
    changes_by_id = {str(change["id"]): change for change in plan["changes"]}
    backup_path = entries_path.with_name(f"{entries_path.name}.bak.{now_stamp()}")
    entries_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(entries_path, backup_path)

    rewritten = []
    for entry in entries:
        entry_id = str(entry.get("id") or "")
        change = changes_by_id.get(entry_id)
        if change:
            item = dict(entry)
            item["room_id"] = plan["room_id"]
            item["revision"] = change["revision"]
            item["previous_entry_id"] = change["previous_entry_id"]
            item["previous_completeness"] = change["previous_completeness"]
            rewritten.append(item)
        else:
            rewritten.append(entry)

    tmp_path = entries_path.with_suffix(entries_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for entry in rewritten:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    tmp_path.replace(entries_path)
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge legacy active darkroom entries into one room. Suggested one-time use."
    )
    parser.add_argument("--state-dir", default=str(default_state_dir()))
    parser.add_argument("--entries-path", default="")
    parser.add_argument("--room-id", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    entries_path = Path(args.entries_path) if args.entries_path else state_dir / "darkroom" / "entries.jsonl"
    entries, invalid_count = load_entries(entries_path)
    plan = build_plan(entries, room_id=args.room_id or None)
    plan["entries_path"] = str(entries_path)
    plan["invalid_line_count"] = invalid_count

    if invalid_count:
        plan["status"] = "invalid_jsonl"
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 1

    if plan["eligible_count"] == 0:
        plan["status"] = "noop"
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if not args.yes:
        plan["status"] = "confirmation_required"
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 1

    backup_path = apply_plan(entries, plan, entries_path)
    plan["status"] = "applied"
    plan["backup_path"] = str(backup_path)
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
