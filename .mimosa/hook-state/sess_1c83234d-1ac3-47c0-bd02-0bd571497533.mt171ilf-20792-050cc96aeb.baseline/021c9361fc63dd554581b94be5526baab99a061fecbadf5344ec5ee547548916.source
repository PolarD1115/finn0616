from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from utils import LOCAL_TZ, now_iso


REMINDER_STATUSES = {"active", "done", "archived"}
REMINDER_REPEAT_RULES = {"once", "none", "every_n_rounds", "daily", "morning_evening"}
MORNING_EVENING_SLOTS = ((6, 0), (20, 0))


class ReminderStore:
    """Standalone active reminders, separate from memory buckets and embeddings."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        state_dir = config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(config.get("buckets_dir", "buckets"))),
            "state",
        )
        self.db_path = str(config.get("reminder_db_path") or os.path.join(state_dir, "reminders.sqlite"))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                source TEXT NOT NULL DEFAULT 'manual',
                channel TEXT NOT NULL DEFAULT 'global',
                session_id TEXT NOT NULL DEFAULT '',
                start_at TEXT,
                end_at TEXT,
                next_due_at TEXT,
                repeat_rule TEXT NOT NULL DEFAULT 'every_n_rounds',
                interval_rounds INTEGER NOT NULL DEFAULT 6,
                cooldown_minutes INTEGER NOT NULL DEFAULT 0,
                daily_limit INTEGER NOT NULL DEFAULT 1,
                daily_reminder_date TEXT NOT NULL DEFAULT '',
                daily_reminder_count INTEGER NOT NULL DEFAULT 0,
                max_injections INTEGER NOT NULL DEFAULT 0,
                last_reminded_at TEXT,
                last_reminded_round INTEGER NOT NULL DEFAULT 0,
                reminder_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT
            )
            """
        )
        self._ensure_columns(
            conn,
            "reminders",
            {
                "daily_limit": "INTEGER NOT NULL DEFAULT 1",
                "daily_reminder_date": "TEXT NOT NULL DEFAULT ''",
                "daily_reminder_count": "INTEGER NOT NULL DEFAULT 0",
                "max_injections": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_status_due ON reminders(status, next_due_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_scope ON reminders(channel, session_id)")
        conn.commit()
        conn.close()

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def create(
        self,
        *,
        title: str,
        content: str,
        source: str = "manual",
        channel: str = "global",
        session_id: str = "",
        start_at: str = "",
        end_at: str = "",
        next_due_at: str = "",
        repeat_rule: str = "every_n_rounds",
        interval_rounds: int = 6,
        cooldown_minutes: int = 0,
        daily_limit: int | None = None,
        max_injections: int = 0,
        reminder_id: str = "",
    ) -> dict:
        safe_title = str(title or "").strip()
        safe_content = str(content or "").strip()
        if not safe_title:
            raise ValueError("title is required")
        if not safe_content:
            raise ValueError("content is required")

        repeat = self._normalize_repeat_rule(repeat_rule)
        interval = self._safe_int(interval_rounds, 6)
        if repeat == "every_n_rounds":
            interval = max(1, interval)
        else:
            interval = max(0, interval)

        item_id = str(reminder_id or "").strip() or uuid.uuid4().hex[:16]
        now = now_iso()
        values = {
            "id": item_id,
            "title": safe_title,
            "content": safe_content,
            "status": "active",
            "source": str(source or "manual").strip() or "manual",
            "channel": str(channel or "global").strip() or "global",
            "session_id": str(session_id or "").strip(),
            "start_at": self._validate_optional_time(start_at),
            "end_at": self._validate_optional_time(end_at),
            "next_due_at": self._validate_optional_time(next_due_at),
            "repeat_rule": repeat,
            "interval_rounds": interval,
            "cooldown_minutes": max(0, self._safe_int(cooldown_minutes, 0)),
            "daily_limit": self._normalize_daily_limit(daily_limit, repeat),
            "max_injections": max(0, self._safe_int(max_injections, 0)),
            "created_at": now,
            "updated_at": now,
        }
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO reminders (
                        id, title, content, status, source, channel, session_id,
                        start_at, end_at, next_due_at, repeat_rule, interval_rounds,
                        cooldown_minutes, daily_limit, max_injections, created_at, updated_at
                    )
                    VALUES (
                        :id, :title, :content, :status, :source, :channel, :session_id,
                        :start_at, :end_at, :next_due_at, :repeat_rule, :interval_rounds,
                        :cooldown_minutes, :daily_limit, :max_injections, :created_at, :updated_at
                    )
                    """,
                    values,
                )
            return self.get(item_id) or values
        finally:
            conn.close()

    def list(self, *, status: str = "active", limit: int = 50, archive: bool = True) -> list[dict]:
        if archive:
            self.archive_expired()
        status = str(status or "active").strip().lower()
        params: list[Any] = []
        where = []
        if status and status != "all":
            if status not in REMINDER_STATUSES:
                raise ValueError("invalid reminder status")
            where.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM reminders"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(next_due_at, updated_at, created_at) ASC, created_at ASC LIMIT ?"
        params.append(max(1, min(200, int(limit or 50))))
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def get(self, reminder_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM reminders WHERE id = ?", (str(reminder_id or ""),)).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def due(
        self,
        *,
        session_id: str = "",
        channel: str = "gateway",
        channels: list[str] | tuple[str, ...] | set[str] | None = None,
        round_id: int = 0,
        now: datetime | str | None = None,
        limit: int = 2,
    ) -> list[dict]:
        safe_now = self._coerce_now(now)
        self.archive_expired(now=safe_now)
        safe_session = str(session_id or "").strip()
        safe_channel = str(channel or "gateway").strip()
        safe_round = max(0, self._safe_int(round_id, 0))
        safe_channels = [
            str(item or "").strip()
            for item in (channels or [safe_channel])
            if str(item or "").strip()
        ] or [safe_channel]
        rows = self.list(status="active", limit=200, archive=False)
        due_rows = [
            row
            for row in rows
            if any(
                self._row_is_due(row, session_id=safe_session, channel=item, round_id=safe_round, now=safe_now)
                for item in safe_channels
            )
        ]
        return due_rows[: max(0, min(10, int(limit or 2)))]

    def set_status(self, reminder_id: str, status: str, *, resolved_at: str | None = None) -> dict | None:
        safe_status = str(status or "").strip().lower()
        if safe_status not in REMINDER_STATUSES:
            raise ValueError("invalid reminder status")
        now = now_iso()
        resolved = (resolved_at or now) if safe_status in {"done", "archived"} else None
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE reminders
                    SET status = ?, updated_at = ?, resolved_at = ?
                    WHERE id = ?
                    """,
                    (safe_status, now, resolved, str(reminder_id or "")),
                )
            return self.get(reminder_id)
        finally:
            conn.close()

    def snooze(self, reminder_id: str, *, minutes: int = 60) -> dict | None:
        now = datetime.now(LOCAL_TZ)
        next_due = now + timedelta(minutes=max(1, int(minutes or 60)))
        return self.update(reminder_id, next_due_at=next_due.isoformat(timespec="seconds"), status="active")

    def update(
        self,
        reminder_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        status: str | None = None,
        channel: str | None = None,
        session_id: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        next_due_at: str | None = None,
        repeat_rule: str | None = None,
        interval_rounds: int | None = None,
        cooldown_minutes: int | None = None,
        daily_limit: int | None = None,
        max_injections: int | None = None,
    ) -> dict | None:
        current = self.get(reminder_id)
        if not current:
            return None
        updates: dict[str, Any] = {"updated_at": now_iso()}
        if title is not None:
            cleaned = str(title or "").strip()
            if not cleaned:
                raise ValueError("title is required")
            updates["title"] = cleaned
        if content is not None:
            cleaned = str(content or "").strip()
            if not cleaned:
                raise ValueError("content is required")
            updates["content"] = cleaned
        if status is not None:
            cleaned = str(status or "").strip().lower()
            if cleaned not in REMINDER_STATUSES:
                raise ValueError("invalid reminder status")
            updates["status"] = cleaned
            updates["resolved_at"] = now_iso() if cleaned in {"done", "archived"} else None
        if channel is not None:
            updates["channel"] = str(channel or "global").strip() or "global"
        if session_id is not None:
            updates["session_id"] = str(session_id or "").strip()
        if start_at is not None:
            updates["start_at"] = self._validate_optional_time(start_at)
        if end_at is not None:
            updates["end_at"] = self._validate_optional_time(end_at)
        if next_due_at is not None:
            updates["next_due_at"] = self._validate_optional_time(next_due_at)
        if repeat_rule is not None:
            updates["repeat_rule"] = self._normalize_repeat_rule(repeat_rule)
        if interval_rounds is not None:
            updates["interval_rounds"] = max(0, self._safe_int(interval_rounds, 0))
        if cooldown_minutes is not None:
            updates["cooldown_minutes"] = max(0, self._safe_int(cooldown_minutes, 0))
        if daily_limit is not None:
            updates["daily_limit"] = max(0, self._safe_int(daily_limit, 1))
            updates["daily_reminder_date"] = ""
            updates["daily_reminder_count"] = 0
        if max_injections is not None:
            updates["max_injections"] = max(0, self._safe_int(max_injections, 0))

        assignments = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values()) + [str(reminder_id or "")]
        conn = self._connect()
        try:
            with conn:
                conn.execute(f"UPDATE reminders SET {assignments} WHERE id = ?", params)
            return self.get(reminder_id)
        finally:
            conn.close()

    def mark_reminded(
        self,
        reminder_id: str,
        *,
        round_id: int = 0,
        reminded_at: str | None = None,
    ) -> dict | None:
        item = self.get(reminder_id)
        if not item:
            return None
        now = reminded_at or now_iso()
        now_dt = self._coerce_now(now)
        next_due = self._next_due_after_reminder(item, now)
        next_count = int(item.get("reminder_count") or 0) + 1
        day_key = now_dt.date().isoformat()
        if str(item.get("daily_reminder_date") or "") == day_key:
            daily_count = int(item.get("daily_reminder_count") or 0) + 1
        else:
            daily_count = 1
        next_status = "active"
        resolved_at = None
        if self._should_archive_after_reminder(item, next_count=next_count, next_due=next_due, now=now_dt):
            next_status = "archived"
            resolved_at = now
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE reminders
                    SET last_reminded_at = ?,
                        last_reminded_round = ?,
                        reminder_count = reminder_count + 1,
                        daily_reminder_date = ?,
                        daily_reminder_count = ?,
                        next_due_at = ?,
                        status = ?,
                        resolved_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        max(0, int(round_id or 0)),
                        day_key,
                        daily_count,
                        next_due,
                        next_status,
                        resolved_at,
                        now,
                        str(reminder_id or ""),
                    ),
                )
            return self.get(reminder_id)
        finally:
            conn.close()

    def archive_expired(self, *, now: datetime | str | None = None) -> list[str]:
        safe_now = self._coerce_now(now)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status = 'active' AND end_at IS NOT NULL AND end_at != ''"
            ).fetchall()
            expired_ids = []
            for row in rows:
                item = self._row_to_dict(row)
                end_at = self._parse_time(item.get("end_at"), now=safe_now, end_of_day=True)
                if end_at and safe_now > end_at:
                    expired_ids.append(str(item.get("id") or ""))
            if expired_ids:
                timestamp = safe_now.isoformat(timespec="seconds")
                with conn:
                    conn.executemany(
                        """
                        UPDATE reminders
                        SET status = 'archived',
                            resolved_at = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        [(timestamp, timestamp, item_id) for item_id in expired_ids],
                    )
            return expired_ids
        finally:
            conn.close()

    def _row_is_due(
        self,
        row: dict,
        *,
        session_id: str,
        channel: str,
        round_id: int,
        now: datetime,
    ) -> bool:
        row_session = str(row.get("session_id") or "").strip()
        if row_session and row_session != session_id:
            return False
        row_channel = str(row.get("channel") or "global").strip()
        if row_channel not in {"", "global", "*"} and row_channel != channel:
            return False
        start_at = self._parse_time(row.get("start_at"), now=now, end_of_day=False)
        if start_at and now < start_at:
            return False
        end_at = self._parse_time(row.get("end_at"), now=now, end_of_day=True)
        if end_at and now > end_at:
            return False
        next_due_at = self._parse_time(row.get("next_due_at"), now=now, end_of_day=False)
        if next_due_at and now < next_due_at:
            return False

        repeat_rule = self._normalize_repeat_rule(row.get("repeat_rule"))
        if repeat_rule in {"once", "none"} and int(row.get("reminder_count") or 0) > 0:
            return False
        max_injections = max(0, self._safe_int(row.get("max_injections"), 0))
        if max_injections > 0 and int(row.get("reminder_count") or 0) >= max_injections:
            return False
        daily_limit = max(0, self._safe_int(row.get("daily_limit"), 1))
        if daily_limit > 0 and str(row.get("daily_reminder_date") or "") == now.date().isoformat():
            if int(row.get("daily_reminder_count") or 0) >= daily_limit:
                return False

        last_at = self._parse_time(row.get("last_reminded_at"), now=now, end_of_day=False)
        cooldown_minutes = max(0, self._safe_int(row.get("cooldown_minutes"), 0))
        if last_at and cooldown_minutes > 0 and now - last_at < timedelta(minutes=cooldown_minutes):
            return False

        interval_rounds = max(0, self._safe_int(row.get("interval_rounds"), 0))
        last_round = max(0, self._safe_int(row.get("last_reminded_round"), 0))
        if interval_rounds > 0 and last_round > 0 and round_id > 0:
            return round_id - last_round >= interval_rounds
        return True

    def _should_archive_after_reminder(self, item: dict, *, next_count: int, next_due: str, now: datetime) -> bool:
        repeat_rule = self._normalize_repeat_rule(item.get("repeat_rule"))
        if repeat_rule in {"once", "none"}:
            return True
        max_injections = max(0, self._safe_int(item.get("max_injections"), 0))
        if max_injections > 0 and next_count >= max_injections:
            return True
        end_at = self._parse_time(item.get("end_at"), now=now, end_of_day=True)
        if end_at and now > end_at:
            return True
        next_due_dt = self._parse_time(next_due, now=now, end_of_day=False)
        return bool(end_at and next_due_dt and next_due_dt > end_at)

    def _next_due_after_reminder(self, item: dict, reminded_at: str) -> str:
        repeat_rule = self._normalize_repeat_rule(item.get("repeat_rule"))
        if repeat_rule == "daily":
            base = self._coerce_now(reminded_at)
            return self._next_daily_due(item, base).isoformat(timespec="seconds")
        if repeat_rule == "morning_evening":
            base = self._coerce_now(reminded_at)
            return self._next_morning_evening_due(base).isoformat(timespec="seconds")
        if repeat_rule in {"once", "none"}:
            return ""
        return str(item.get("next_due_at") or "")

    def _next_daily_due(self, item: dict, base: datetime) -> datetime:
        anchor = self._time_anchor(item, base)
        candidate = base.replace(
            hour=anchor.hour,
            minute=anchor.minute,
            second=anchor.second,
            microsecond=0,
        )
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _next_morning_evening_due(base: datetime) -> datetime:
        for hour, minute in MORNING_EVENING_SLOTS:
            candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > base:
                return candidate
        hour, minute = MORNING_EVENING_SLOTS[0]
        return (base + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _time_anchor(self, item: dict, base: datetime) -> datetime:
        for key in ("next_due_at", "start_at"):
            raw = str(item.get(key) or "").strip()
            if not raw or self._is_date_only(raw):
                continue
            parsed = self._parse_time(raw, now=base, end_of_day=False)
            if parsed:
                return parsed
        return base

    @staticmethod
    def _normalize_repeat_rule(value: Any) -> str:
        rule = str(value or "every_n_rounds").strip().lower()
        return rule if rule in REMINDER_REPEAT_RULES else "every_n_rounds"

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _validate_optional_time(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        parsed = ReminderStore._parse_time(text, now=datetime.now(LOCAL_TZ), end_of_day=False)
        if not parsed:
            raise ValueError("invalid time; use YYYY-MM-DD or ISO datetime")
        return text

    @staticmethod
    def _normalize_daily_limit(value: Any, repeat_rule: str) -> int:
        raw = ReminderStore._safe_int(value, -1)
        if raw < 0:
            return 2 if repeat_rule == "morning_evening" else 1
        return max(0, raw)

    @staticmethod
    def _is_date_only(value: Any) -> bool:
        text = str(value or "").strip()
        return len(text) == 10 and text[4] == "-" and text[7] == "-"

    @staticmethod
    def _coerce_now(value: datetime | str | None) -> datetime:
        if isinstance(value, datetime):
            return value.astimezone(LOCAL_TZ) if value.tzinfo else value.replace(tzinfo=LOCAL_TZ)
        parsed = ReminderStore._parse_time(value, now=datetime.now(LOCAL_TZ), end_of_day=False)
        return parsed or datetime.now(LOCAL_TZ)

    @staticmethod
    def _parse_time(value: Any, *, now: datetime, end_of_day: bool) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            try:
                base = datetime.fromisoformat(text).replace(tzinfo=LOCAL_TZ)
            except ValueError:
                return None
            if end_of_day:
                return base.replace(hour=23, minute=59, second=59)
            return base
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=now.tzinfo or LOCAL_TZ)
        return parsed.astimezone(now.tzinfo or LOCAL_TZ)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "status": row["status"],
            "source": row["source"],
            "channel": row["channel"],
            "session_id": row["session_id"],
            "start_at": row["start_at"] or "",
            "end_at": row["end_at"] or "",
            "next_due_at": row["next_due_at"] or "",
            "repeat_rule": row["repeat_rule"],
            "interval_rounds": row["interval_rounds"],
            "cooldown_minutes": row["cooldown_minutes"],
            "daily_limit": row["daily_limit"],
            "daily_reminder_date": row["daily_reminder_date"] or "",
            "daily_reminder_count": row["daily_reminder_count"],
            "max_injections": row["max_injections"],
            "last_reminded_at": row["last_reminded_at"] or "",
            "last_reminded_round": row["last_reminded_round"],
            "reminder_count": row["reminder_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row["resolved_at"] or "",
        }
