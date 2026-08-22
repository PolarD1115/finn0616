import json
import os
import re
import secrets
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from identity import identity_names


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
LOCK_FOR_RE = re.compile(r"^\s*(\d+)\s*(h|hr|hour|hours|小时|d|day|days|天)\s*$", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(LOCAL_TZ)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_lock_for(value: str | None) -> timedelta | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    match = LOCK_FOR_RE.match(raw)
    if not match:
        raise ValueError("invalid lock_for")
    amount = int(match.group(1))
    if amount <= 0:
        return None
    unit = match.group(2).lower()
    if unit in {"h", "hr", "hour", "hours", "小时"}:
        return timedelta(hours=amount)
    return timedelta(days=amount)


def _split_tags(tags: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        raw = tags.split(",")
    else:
        raw = [str(item) for item in tags]
    clean: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = item.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        clean.append(tag[:40])
    return clean[:12]


def _normalize_mode(value: str | None) -> str:
    mode = str(value or "continue").strip().lower()
    if mode not in {"continue", "single"}:
        raise ValueError("invalid mode")
    return mode


def _normalize_visibility(value: str | None) -> str:
    visibility = str(value or "active").strip().lower()
    if visibility not in {"active", "archived", "retracted"}:
        raise ValueError("invalid visibility")
    return visibility


def _bool_value(value: bool | str | int | None) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "新开", "new"}


class DarkroomStore:
    """Private reflection storage: public status, private notes."""

    def __init__(self, config: dict):
        self.config = config
        state_dir = config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(config.get("buckets_dir", "buckets"))),
            "state",
        )
        self.base_dir = Path(state_dir) / "darkroom"
        self.entries_path = self.base_dir / "entries.jsonl"
        self.release_log_path = self.base_dir / "releases.jsonl"
        self.state_path = self.base_dir / "state.json"
        self._lock = Lock()

    def enter(
        self,
        note: str,
        *,
        mood: str = "",
        tags: str | list[str] | tuple[str, ...] | None = None,
        source: str = "mcp",
        mode: str = "continue",
        visibility: str = "active",
        lock_for: str = "",
        new_room: bool | str | int = True,
    ) -> dict:
        text = str(note or "").strip()
        if not text:
            raise ValueError("note is empty")
        if len(text) > 12000:
            raise ValueError("note is too long")
        mode_key = _normalize_mode(mode)
        visibility_key = _normalize_visibility(visibility)
        lock_delta = _parse_lock_for(lock_for)
        now = _now()

        with self._lock:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            open_new_room = _bool_value(new_room)
            previous = None if open_new_room else self._last_room_unlocked(visibility="active")
            state = self._status_unlocked()
            continuation_anchor = {} if open_new_room else self._continuation_anchor_unlocked(mode_key)
            room_id = self._entry_room_id(previous) if previous else self._new_room_id()
            entry = {
                "id": self._new_entry_id(),
                "room_id": room_id,
                "revision": int(previous.get("revision") or 1) + 1 if previous else 1,
                "created_at": now.isoformat(timespec="seconds"),
                "note": text,
                "mode": mode_key,
                "previous_entry_id": previous.get("id") if previous else "",
                "continuation_anchor": continuation_anchor,
                "mood": str(mood or "").strip()[:80],
                "tags": _split_tags(tags),
                "source": str(source or "mcp").strip()[:80],
                "visibility": visibility_key,
                "lock_for": str(lock_for or "").strip()[:40],
                "locked_until": (now + lock_delta).isoformat(timespec="seconds") if lock_delta else "",
            }
            self._append_jsonl_unlocked(self.entries_path, entry)
            state = self._active_state_unlocked(base_state=state)
            if not state.get("created_at"):
                state["created_at"] = entry["created_at"]
            self._write_json_unlocked(self.state_path, state)
            return self._public_enter_payload(entry, state)

    def status(self) -> dict:
        with self._lock:
            return self._status_unlocked()

    def rooms(self, *, limit: int = 20, visibility: str = "active") -> dict:
        visibility_key = str(visibility or "active").strip().lower()
        visibility_filter = None if visibility_key in {"all", "*"} else _normalize_visibility(visibility_key)
        limit_count = max(1, min(100, int(limit)))
        with self._lock:
            entries = self._current_room_entries_unlocked(visibility=visibility_filter)
            selected = list(reversed(entries[-limit_count:]))
            return {
                "status": "ok",
                "visibility": visibility_key if visibility_filter is None else visibility_filter,
                "count": len(selected),
                "total": len(entries),
                "rooms": [self._room_door_payload_unlocked(entry) for entry in selected],
            }

    def delete_room(self, room_id: str, *, confirm: str = "") -> dict:
        """Delete one exact room and every revision from the live store, keeping a private local backup."""
        target = str(room_id or "").strip()
        if str(confirm or "") != "DELETE":
            raise ValueError("confirmation required")
        if not target or target.lower() in {"latest", "all", "*"}:
            raise ValueError("exact room_id required")
        if len(target) > 200:
            raise ValueError("invalid room_id")

        with self._lock:
            room_entries = [
                entry
                for entry in self._iter_entries_unlocked(visibility=None)
                if self._entry_room_id(entry) == target
            ]
            if not room_entries:
                raise KeyError("room not found")

            deleted_entry_ids = {
                str(entry.get("id") or "")
                for entry in room_entries
                if str(entry.get("id") or "")
            }
            entry_lines, deleted_revisions = self._filter_jsonl_lines_unlocked(
                self.entries_path,
                lambda row: self._entry_room_id(row) == target,
            )
            release_lines, deleted_releases = self._filter_jsonl_lines_unlocked(
                self.release_log_path,
                lambda row: str(row.get("entry_id") or "") in deleted_entry_ids,
            )

            backup_id = f"delete-{_now().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
            backup_dir = self.base_dir / "backups" / backup_id
            tracked_paths = (self.entries_path, self.release_log_path, self.state_path)
            existed_before = {path: path.exists() for path in tracked_paths}
            temp_paths: dict[Path, Path] = {}
            try:
                backup_dir.mkdir(parents=True, exist_ok=False)
                for path in tracked_paths:
                    if path.exists():
                        shutil.copy2(path, backup_dir / path.name)

                temp_paths[self.entries_path] = self._write_jsonl_temp_unlocked(
                    self.entries_path,
                    entry_lines,
                    backup_id,
                )
                if self.release_log_path.exists():
                    temp_paths[self.release_log_path] = self._write_jsonl_temp_unlocked(
                        self.release_log_path,
                        release_lines,
                        backup_id,
                    )
                for path, temp_path in temp_paths.items():
                    temp_path.replace(path)

                remaining_entries = list(self._iter_entries_unlocked(visibility=None))
                remaining_releases = list(self._iter_jsonl_unlocked(self.release_log_path))
                release_dates = [
                    str(row.get("created_at") or "")
                    for row in remaining_releases
                    if str(row.get("created_at") or "")
                ]
                entry_dates = [
                    str(row.get("created_at") or "")
                    for row in remaining_entries
                    if str(row.get("created_at") or "")
                ]
                state = self._active_state_unlocked(
                    base_state={
                        "version": 1,
                        "created_at": min(entry_dates) if entry_dates else "",
                        "last_release_at": max(release_dates) if release_dates else "",
                        "released_count": len(remaining_releases),
                    }
                )
                self._write_json_unlocked(self.state_path, state)
            except Exception:
                for path in tracked_paths:
                    backup_path = backup_dir / path.name
                    if existed_before[path] and backup_path.exists():
                        shutil.copy2(backup_path, path)
                    elif not existed_before[path] and path.exists():
                        path.unlink()
                raise
            finally:
                for temp_path in temp_paths.values():
                    if temp_path.exists():
                        temp_path.unlink()

            current_rooms = self._current_room_entries_unlocked(visibility=None)
            return {
                "status": "deleted",
                "room_id": target,
                "deleted_revisions": deleted_revisions,
                "deleted_release_records": deleted_releases,
                "backup_created": True,
                "backup_id": backup_id,
                "remaining_rooms": len(current_rooms),
            }

    def release(self, entry_id: str = "latest", *, reason: str = "") -> dict:
        with self._lock:
            entry = self._find_entry_unlocked(entry_id)
            if not entry:
                raise KeyError("entry not found")
            locked = self._locked_payload_unlocked(entry)
            if locked:
                return locked
            release = {
                "id": f"rel_{secrets.token_hex(6)}",
                "entry_id": entry["id"],
                "created_at": _now_iso(),
                "reason": str(reason or "").strip()[:200],
            }
            self._append_jsonl_unlocked(self.release_log_path, release)
            state = self._status_unlocked()
            state["updated_at"] = release["created_at"]
            state["last_release_at"] = release["created_at"]
            state["released_count"] = int(state.get("released_count") or 0) + 1
            self._write_json_unlocked(self.state_path, state)
            return {
                "status": "released",
                "entry_id": entry["id"],
                "room_id": self._entry_room_id(entry),
                "revision": entry.get("revision", 1),
                "created_at": entry.get("created_at", ""),
                "mood": entry.get("mood", ""),
                "tags": entry.get("tags", []),
                "content": entry.get("note", ""),
            }

    def view(self, entry_id: str = "latest") -> dict:
        with self._lock:
            entry = self._find_entry_unlocked(entry_id)
            if not entry:
                raise KeyError("entry not found")
            locked = self._locked_payload_unlocked(entry)
            if locked:
                return locked
            room_entries = self._room_entries_unlocked(self._entry_room_id(entry), visibility="active")
            return {
                "status": "visible",
                "entry_id": entry["id"],
                "room_id": self._entry_room_id(entry),
                "revision": entry.get("revision", 1),
                "created_at": entry.get("created_at", ""),
                "written_at": entry.get("created_at", ""),
                "mood": entry.get("mood", ""),
                "tags": entry.get("tags", []),
                "visibility": entry.get("visibility", "active"),
                "locked_until": str(entry.get("locked_until") or ""),
                "content": entry.get("note", ""),
                "entries": [
                    {
                        "entry_id": item.get("id", ""),
                        "room_id": self._entry_room_id(item),
                        "revision": item.get("revision", 1),
                        "created_at": item.get("created_at", ""),
                        "written_at": item.get("created_at", ""),
                        "mood": item.get("mood", ""),
                        "tags": item.get("tags", []),
                        "visibility": item.get("visibility", "active"),
                        "locked_until": str(item.get("locked_until") or ""),
                        "content": item.get("note", ""),
                    }
                    for item in room_entries
                ],
            }

    def continue_context(self, limit: int = 3) -> dict:
        with self._lock:
            entry = self._last_room_unlocked(visibility="active")
            entries = [entry] if entry else []
            return {
                "status": "ok",
                "count": len(entries),
                "entries": [
                    {
                        "entry_id": entry.get("id", ""),
                        "room_id": self._entry_room_id(entry),
                        "revision": entry.get("revision", 1),
                        "created_at": entry.get("created_at", ""),
                        "mode": entry.get("mode", "continue"),
                        "mood": entry.get("mood", ""),
                        "tags": entry.get("tags", []),
                        "visibility": entry.get("visibility", "active"),
                        "locked_until": str(entry.get("locked_until") or ""),
                        "content": entry.get("note", ""),
                    }
                    for entry in entries
                ],
            }

    def _new_entry_id(self) -> str:
        return f"dr_{_now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}"

    def _new_room_id(self) -> str:
        return f"room_{secrets.token_hex(6)}"

    def _ai_name(self) -> str:
        return identity_names(self.config).get("ai_name") or "AI"

    def _door_text(self) -> str:
        return f"暗房存在。钥匙只给 {self._ai_name()}；门口只显示状态，不显示未显影正文。"

    def _public_enter_payload(self, entry: dict, state: dict) -> dict:
        ai_name = self._ai_name()
        return {
            "status": "entered",
            "entry_id": entry["id"],
            "room_id": self._entry_room_id(entry),
            "revision": entry.get("revision", 1),
            "entered_at": entry["created_at"],
            "mode": entry.get("mode", "continue"),
            "visibility": entry.get("visibility", "active"),
            "count": state.get("count", 0),
            "previous_entry_id": entry.get("previous_entry_id", ""),
            "continuation_anchor_entries": len(entry.get("continuation_anchor", {}).get("entry_ids", [])),
            "mood": entry.get("mood", ""),
            "tags": entry.get("tags", []),
            "locked_until": str(entry.get("locked_until") or ""),
            "visible_note": f"{ai_name} 进入了暗房。",
        }

    def _locked_payload_unlocked(self, entry: dict) -> dict | None:
        raw = str(entry.get("locked_until") or "").strip()
        if not raw:
            return None
        try:
            unlock_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if unlock_at.tzinfo is None:
            unlock_at = unlock_at.replace(tzinfo=LOCAL_TZ)
        unlock_at = unlock_at.astimezone(LOCAL_TZ)
        if _now() >= unlock_at:
            return None
        return {
            "status": "locked",
            "entry_id": entry.get("id", ""),
            "room_id": self._entry_room_id(entry),
            "revision": entry.get("revision", 1),
            "created_at": entry.get("created_at", ""),
            "unlock_at": unlock_at.isoformat(timespec="seconds"),
        }

    def _room_door_payload_unlocked(self, entry: dict) -> dict:
        room_id = self._entry_room_id(entry)
        room_entries = self._room_entries_unlocked(room_id)
        first = room_entries[0] if room_entries else entry
        locked = self._locked_payload_unlocked(entry)
        return {
            "room_id": room_id,
            "latest_entry_id": str(entry.get("id") or ""),
            "revision": entry.get("revision", 1),
            "revision_count": len(room_entries) if room_entries else 1,
            "created_at": str(first.get("created_at") or ""),
            "latest_written_at": str(entry.get("created_at") or ""),
            "visibility": str(entry.get("visibility") or "active"),
            "locked_until": str(entry.get("locked_until") or ""),
            "locked": bool(locked),
            "unlock_at": str(locked.get("unlock_at") or "") if locked else "",
        }

    def _status_unlocked(self) -> dict:
        stored_state: dict = {}
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    stored_state = data
            except (OSError, json.JSONDecodeError):
                pass
        return self._public_status(self._active_state_unlocked(base_state=stored_state))

    def _active_state_unlocked(self, *, base_state: dict | None = None) -> dict:
        base_state = base_state or {}
        rooms = self._current_room_entries_unlocked(visibility="active")
        count = len(rooms)
        last = rooms[-1] if rooms else None
        last_active_at = last.get("created_at", "") if last else ""
        last_release_at = str(base_state.get("last_release_at") or "")
        state = {
            "version": 1,
            "created_at": str(base_state.get("created_at") or ""),
            "updated_at": max([item for item in [last_active_at, last_release_at] if item], default=""),
            "count": count,
            "last_room_id": self._entry_room_id(last) if last else "",
            "last_entry_id": last.get("id", "") if last else "",
            "last_entered_at": last_active_at,
            "last_mood": last.get("mood", "") if last else "",
            "last_tags": last.get("tags", []) if last else [],
            "last_release_at": last_release_at,
            "released_count": int(base_state.get("released_count") or 0),
        }
        return state

    def _public_status(self, state: dict) -> dict:
        return {
            "status": "ok",
            "door": self._door_text(),
            "version": int(state.get("version") or 1),
            "created_at": str(state.get("created_at") or ""),
            "updated_at": str(state.get("updated_at") or ""),
            "count": int(state.get("count") or 0),
            "last_room_id": str(state.get("last_room_id") or ""),
            "last_entry_id": str(state.get("last_entry_id") or ""),
            "last_entered_at": str(state.get("last_entered_at") or ""),
            "last_mood": str(state.get("last_mood") or ""),
            "last_tags": state.get("last_tags") if isinstance(state.get("last_tags"), list) else [],
            "last_release_at": str(state.get("last_release_at") or ""),
            "released_count": int(state.get("released_count") or 0),
        }

    def _last_entry_unlocked(self, *, visibility: str = "active") -> dict | None:
        last = None
        for entry in self._iter_entries_unlocked(visibility=visibility):
            last = entry
        return last

    def _entry_room_id(self, entry: dict | None) -> str:
        if not entry:
            return ""
        return str(entry.get("room_id") or entry.get("id") or "")

    def _current_room_entries_unlocked(self, *, visibility: str | None = None) -> list[dict]:
        rooms: dict[str, dict] = {}
        for entry in self._iter_entries_unlocked(visibility=None):
            room_id = self._entry_room_id(entry)
            if not room_id:
                continue
            if room_id in rooms:
                del rooms[room_id]
            rooms[room_id] = entry
        entries = list(rooms.values())
        if visibility is not None:
            entries = [
                entry
                for entry in entries
                if str(entry.get("visibility") or "active") == visibility
            ]
        return entries

    def _last_room_unlocked(self, *, visibility: str = "active") -> dict | None:
        rooms = self._current_room_entries_unlocked(visibility=visibility)
        return rooms[-1] if rooms else None

    def _room_entries_unlocked(self, room_id: str, *, visibility: str | None = None) -> list[dict]:
        target = str(room_id or "").strip()
        if not target:
            return []
        entries = [
            entry
            for entry in self._iter_entries_unlocked(visibility=None)
            if self._entry_room_id(entry) == target
        ]
        if visibility is not None:
            entries = [
                entry
                for entry in entries
                if str(entry.get("visibility") or "active") == visibility
            ]
        return entries

    def _recent_entries_unlocked(self, limit: int = 3, *, visibility: str = "active") -> list[dict]:
        recent: list[dict] = []
        for entry in self._iter_entries_unlocked(visibility=visibility):
            recent.append(entry)
            if len(recent) > limit:
                recent.pop(0)
        return recent

    def _continuation_anchor_unlocked(self, mode: str) -> dict:
        if mode != "continue":
            return {}
        recent = self._recent_entries_unlocked(limit=3)
        if not recent:
            return {}
        return {
            "kind": "local_continuation",
            "generated_at": _now_iso(),
            "entry_ids": [str(entry.get("id") or "") for entry in recent if entry.get("id")],
            "notes": [
                {
                    "created_at": str(entry.get("created_at") or ""),
                    "note": str(entry.get("note") or "")[:600],
                }
                for entry in recent
            ],
        }

    def _find_entry_unlocked(self, entry_id: str) -> dict | None:
        target = str(entry_id or "latest").strip()
        if target in {"", "latest"}:
            return self._last_room_unlocked()
        for entry in self._current_room_entries_unlocked(visibility="active"):
            if entry.get("id") == target or self._entry_room_id(entry) == target:
                return entry
        for entry in self._iter_entries_unlocked(visibility="active"):
            if entry.get("id") == target and not entry.get("room_id"):
                return entry
        return None

    def _iter_entries_unlocked(self, *, visibility: str | None = None):
        if not self.entries_path.exists():
            return
        with self.entries_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    if visibility is not None and str(data.get("visibility") or "active") != visibility:
                        continue
                    yield data

    def _iter_jsonl_unlocked(self, path: Path):
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    yield data

    def _filter_jsonl_lines_unlocked(self, path: Path, should_remove) -> tuple[list[str], int]:
        if not path.exists():
            return [], 0
        kept: list[str] = []
        removed = 0
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                try:
                    row = json.loads(raw_line.strip())
                except json.JSONDecodeError:
                    kept.append(raw_line)
                    continue
                if isinstance(row, dict) and should_remove(row):
                    removed += 1
                    continue
                kept.append(raw_line)
        return kept, removed

    def _write_jsonl_temp_unlocked(self, path: Path, lines: list[str], token: str) -> Path:
        temp_path = path.with_name(f"{path.name}.{token}.tmp")
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.writelines(lines)
        return temp_path

    def _append_jsonl_unlocked(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _write_json_unlocked(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
