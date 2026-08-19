"""
home/repository.py — Supabase 查询封装层
========================================
职责：封装所有 home_* 表的只读查询，不拼接 SQL。
约束：业务层不直接散落 .table() 调用，统一走这里。
异常处理：数据库异常保留错误上下文，返回空列表而非崩溃。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 状态字段列表（与 home_member_states 表列对齐）
STATE_FIELDS = [
    "hunger", "energy", "mood", "comfort",
    "connection", "intimacy", "health", "cleanliness",
]


def _get_supabase():
    """延迟获取 server.py 中初始化的 supabase 客户端。"""
    try:
        import server
        return server.supabase
    except Exception:
        return None


# ============================================================
# 房间查询
# ============================================================

def fetch_rooms(enabled_only: bool = True, include_hidden: bool = False) -> list[dict]:
    """查询房间列表，按 sort_order 排序。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("home_rooms").select("*")
        if enabled_only:
            q = q.eq("is_enabled", True)
        if not include_hidden:
            q = q.eq("is_hidden", False)
        q = q.order("sort_order").order("created_at")
        resp = q.execute()
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_rooms 失败: %s", e)
        return []


def fetch_room_by_key(stable_key: str) -> Optional[dict]:
    """按 stable_key 查询单个房间。"""
    sb = _get_supabase()
    if sb is None:
        return None
    try:
        resp = sb.table("home_rooms").select("*").eq("stable_key", stable_key).limit(1).execute()
        return (resp.data or [None])[0]
    except Exception as e:
        logger.warning("home.repository.fetch_room_by_key 失败: %s", e)
        return None


def fetch_room_by_id(room_id: str) -> Optional[dict]:
    """按 UUID 查询单个房间。"""
    sb = _get_supabase()
    if sb is None:
        return None
    try:
        resp = sb.table("home_rooms").select("*").eq("id", room_id).limit(1).execute()
        return (resp.data or [None])[0]
    except Exception as e:
        logger.warning("home.repository.fetch_room_by_id 失败: %s", e)
        return None


# ============================================================
# 成员查询
# ============================================================

def fetch_members(active_only: bool = True) -> list[dict]:
    """查询家庭成员列表。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("home_members").select("*")
        if active_only:
            q = q.eq("is_active", True)
        q = q.order("created_at")
        resp = q.execute()
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_members 失败: %s", e)
        return []


def fetch_member_by_key(stable_key: str) -> Optional[dict]:
    """按 stable_key 查询单个成员。"""
    sb = _get_supabase()
    if sb is None:
        return None
    try:
        resp = sb.table("home_members").select("*").eq("stable_key", stable_key).limit(1).execute()
        return (resp.data or [None])[0]
    except Exception as e:
        logger.warning("home.repository.fetch_member_by_key 失败: %s", e)
        return None


def fetch_member_by_id(member_id: str) -> Optional[dict]:
    """按 UUID 查询单个成员。"""
    sb = _get_supabase()
    if sb is None:
        return None
    try:
        resp = sb.table("home_members").select("*").eq("id", member_id).limit(1).execute()
        return (resp.data or [None])[0]
    except Exception as e:
        logger.warning("home.repository.fetch_member_by_id 失败: %s", e)
        return None


# ============================================================
# 成员状态查询
# ============================================================

def fetch_member_state(member_id: str) -> Optional[dict]:
    """查询指定成员的状态。"""
    sb = _get_supabase()
    if sb is None:
        return None
    try:
        resp = (sb.table("home_member_states")
                .select("*")
                .eq("member_id", member_id)
                .limit(1)
                .execute())
        return (resp.data or [None])[0]
    except Exception as e:
        logger.warning("home.repository.fetch_member_state 失败: %s", e)
        return None


def fetch_member_states(member_ids: list[str]) -> list[dict]:
    """批量查询多个成员的状态。"""
    sb = _get_supabase()
    if sb is None or not member_ids:
        return []
    try:
        resp = (sb.table("home_member_states")
                .select("*")
                .in_("member_id", member_ids)
                .execute())
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_member_states 失败: %s", e)
        return []


# ============================================================
# 宠物权威源查询（Phase 7）
# ============================================================

def fetch_pet_by_member(member: dict) -> Optional[dict]:
    """通过 home_members.profile.legacy_id 只读查询 pets 表。

    校验：legacy_source='pets'，legacy_id 存在。
    返回 pets 行 dict 或 None。不写入任何表。
    失败原因包括：profile 缺失、legacy_source 不匹配、
    legacy_id 为空、UUID 格式非法、数据库不可用、pets 行不存在。
    所有失败均返回 None，不抛出异常。
    """
    sb = _get_supabase()
    if sb is None:
        return None
    if not member or not isinstance(member, dict):
        return None
    profile = member.get("profile")
    if not profile or not isinstance(profile, dict):
        return None
    if profile.get("legacy_source") != "pets":
        return None
    legacy_id = profile.get("legacy_id")
    if not legacy_id or not isinstance(legacy_id, str):
        return None
    try:
        resp = sb.table("pets").select("*").eq("id", legacy_id).limit(1).execute()
        return (resp.data or [None])[0]
    except Exception as e:
        logger.warning("home.repository.fetch_pet_by_member 失败: %s", e)
        return None


# ============================================================
# 物品查询
# ============================================================

def fetch_objects_by_room(room_id: str, include_hidden: bool = False) -> list[dict]:
    """查询指定房间的物品。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("home_objects").select("*").eq("room_id", room_id)
        if not include_hidden:
            q = q.eq("is_hidden", False)
        q = q.order("created_at")
        resp = q.execute()
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_objects_by_room 失败: %s", e)
        return []


# ============================================================
# 事件查询
# ============================================================

def fetch_recent_events(
    limit: int = 20,
    event_type: str = "",
    room_id: str = "",
    actor_member_id: str = "",
    exclude_private: bool = True,
) -> list[dict]:
    """查询最近生活事件，按 occurred_at DESC 排序。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("home_events").select("*")
        if event_type:
            q = q.eq("event_type", event_type)
        if room_id:
            q = q.eq("room_id", room_id)
        if actor_member_id:
            q = q.eq("actor_member_id", actor_member_id)
        if exclude_private:
            # 排除 private 事件（秘密日记等不通过统一事件暴露）
            q = q.neq("visibility", "private")
        q = q.order("occurred_at", desc=True).limit(limit)
        resp = q.execute()
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_recent_events 失败: %s", e)
        return []


def fetch_events_by_room(room_id: str, limit: int = 10) -> list[dict]:
    """查询指定房间的最近事件。"""
    return fetch_recent_events(limit=limit, room_id=room_id, exclude_private=True)


def fetch_events_by_member(member_id: str, limit: int = 10) -> list[dict]:
    """查询指定成员参与的最近事件（作为 actor 或 target）。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        # 查询 actor 或 target 是该成员的事件
        resp_actor = (sb.table("home_events")
                      .select("*")
                      .eq("actor_member_id", member_id)
                      .neq("visibility", "private")
                      .order("occurred_at", desc=True)
                      .limit(limit)
                      .execute())
        resp_target = (sb.table("home_events")
                       .select("*")
                       .eq("target_member_id", member_id)
                       .neq("visibility", "private")
                       .order("occurred_at", desc=True)
                       .limit(limit)
                       .execute())
        # 合并去重并排序
        seen = set()
        merged = []
        for ev in (resp_actor.data or []) + (resp_target.data or []):
            eid = ev.get("id")
            if eid and eid not in seen:
                seen.add(eid)
                merged.append(ev)
        merged.sort(key=lambda x: x.get("occurred_at", ""), reverse=True)
        return merged[:limit]
    except Exception as e:
        logger.warning("home.repository.fetch_events_by_member 失败: %s", e)
        return []


# ============================================================
# 行动查询
# ============================================================

def fetch_action_by_key(action_key: str) -> Optional[dict]:
    """按 action_key 查询行动执行记录。"""
    sb = _get_supabase()
    if sb is None:
        return None
    try:
        resp = (sb.table("home_action_runs")
                .select("*")
                .eq("action_key", action_key)
                .limit(1)
                .execute())
        return (resp.data or [None])[0]
    except Exception as e:
        logger.warning("home.repository.fetch_action_by_key 失败: %s", e)
        return None


# ============================================================
# 任务查询
# ============================================================

def fetch_pending_jobs(limit: int = 10) -> list[dict]:
    """查询待执行的任务。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        resp = (sb.table("home_jobs")
                .select("*")
                .eq("status", "pending")
                .order("priority")
                .order("not_before")
                .limit(limit)
                .execute())
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_pending_jobs 失败: %s", e)
        return []


# ============================================================
# 写操作 RPC 封装（Phase 3）
# ============================================================

def _call_rpc(name: str, params: dict | None = None) -> dict:
    """调用 Home Runtime RPC 函数。返回 RPC 返回的 JSON 或错误 dict。"""
    sb = _get_supabase()
    if sb is None:
        return {"ok": False, "error_code": "DB_UNAVAILABLE", "message": "数据库未连接"}
    try:
        resp = sb.rpc(name, params or {}).execute()
        if resp.data is not None:
            return resp.data
        return {"ok": False, "error_code": "RPC_EMPTY", "message": "RPC 返回空数据"}
    except Exception as e:
        logger.warning("home.repository._call_rpc(%s) 失败: %s", name, e)
        return {"ok": False, "error_code": "RPC_ERROR", "message": f"RPC 调用失败"}


def rpc_initialize_members() -> dict:
    """幂等初始化 AI 本体 + 小满。"""
    return _call_rpc("rpc_home_initialize_members")


def rpc_settle_member(member_key: str) -> dict:
    """结算成员状态（elapsed-time）。"""
    return _call_rpc("rpc_home_settle_member", {"p_member_key": member_key})


def rpc_enter_room(action_key: str, member_key: str, room_key: str) -> dict:
    """进入房间行动。"""
    return _call_rpc("rpc_home_enter_room", {
        "p_action_key": action_key,
        "p_member_key": member_key,
        "p_room_key": room_key,
    })


def rpc_rest(action_key: str, member_key: str, duration_minutes: int, mode: str = "rest") -> dict:
    """休息/睡眠行动。"""
    return _call_rpc("rpc_home_rest", {
        "p_action_key": action_key,
        "p_member_key": member_key,
        "p_duration_minutes": int(duration_minutes),
        "p_mode": mode,
    })


def rpc_spend_time(action_key: str, actor_key: str, target_key: str, activity: str, duration_minutes: int) -> dict:
    """陪伴互动行动。"""
    return _call_rpc("rpc_home_spend_time", {
        "p_action_key": action_key,
        "p_actor_key": actor_key,
        "p_target_key": target_key,
        "p_activity": activity,
        "p_duration_minutes": int(duration_minutes),
    })


# ============================================================
# Phase 4: 植物与烹饪 RPC 封装
# ============================================================

def rpc_plant_seed(action_key: str, actor_key: str, seed_key: str) -> dict:
    """种植。"""
    return _call_rpc("rpc_home_plant_seed", {
        "p_action_key": action_key,
        "p_actor_key": actor_key,
        "p_seed_key": seed_key,
    })


def rpc_water_plant(action_key: str, actor_key: str, plant_id: str) -> dict:
    """浇水。"""
    return _call_rpc("rpc_home_water_plant", {
        "p_action_key": action_key,
        "p_actor_key": actor_key,
        "p_plant_id": plant_id,
    })


def rpc_harvest_plant(action_key: str, actor_key: str, plant_id: str) -> dict:
    """收获。"""
    return _call_rpc("rpc_home_harvest_plant", {
        "p_action_key": action_key,
        "p_actor_key": actor_key,
        "p_plant_id": plant_id,
    })


def rpc_cook_recipe(action_key: str, actor_key: str, recipe_key: str) -> dict:
    """按菜谱烹饪。"""
    return _call_rpc("rpc_home_cook_recipe", {
        "p_action_key": action_key,
        "p_actor_key": actor_key,
        "p_recipe_key": recipe_key,
    })


def rpc_cook_freestyle(action_key: str, actor_key: str, ingredients: dict) -> dict:
    """自由烹饪。"""
    return _call_rpc("rpc_home_cook_freestyle", {
        "p_action_key": action_key,
        "p_actor_key": actor_key,
        "p_ingredients": ingredients,
    })


def rpc_eat_dish(action_key: str, actor_key: str, dish_id: str) -> dict:
    """食用菜品。"""
    return _call_rpc("rpc_home_eat_dish", {
        "p_action_key": action_key,
        "p_actor_key": actor_key,
        "p_dish_id": dish_id,
    })


def rpc_feed_member(action_key: str, actor_key: str, target_key: str, dish_id: str) -> dict:
    """喂食家庭成员。"""
    return _call_rpc("rpc_home_feed_member", {
        "p_action_key": action_key,
        "p_actor_key": actor_key,
        "p_target_key": target_key,
        "p_dish_id": dish_id,
    })


# ============================================================
# Phase 4: 只读查询（种子目录/植物/库存/菜谱/菜品）
# ============================================================

def fetch_seed_catalog() -> list[dict]:
    """查询种子目录。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        resp = sb.table("home_seed_catalog").select("*").eq("is_enabled", True).order("name").execute()
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_seed_catalog 失败: %s", e)
        return []


def fetch_plants(owner_member_id: str = "", room_id: str = "") -> list[dict]:
    """查询植物列表。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("home_plants").select("*")
        if owner_member_id:
            q = q.eq("owner_member_id", owner_member_id)
        if room_id:
            q = q.eq("room_id", room_id)
        q = q.order("created_at", desc=True)
        resp = q.execute()
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_plants 失败: %s", e)
        return []


def fetch_inventory(owner_member_id: str = "", item_kind: str = "") -> list[dict]:
    """查询库存。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("home_inventory").select("*")
        if owner_member_id:
            q = q.eq("owner_member_id", owner_member_id)
        if item_kind:
            q = q.eq("item_kind", item_kind)
        q = q.order("storage_location").order("item_key")
        resp = q.execute()
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_inventory 失败: %s", e)
        return []


def fetch_recipe_catalog() -> list[dict]:
    """查询菜谱目录。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        resp = sb.table("home_recipe_catalog").select("*").eq("is_enabled", True).order("name").execute()
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_recipe_catalog 失败: %s", e)
        return []


def fetch_dishes(owner_member_id: str = "") -> list[dict]:
    """查询菜品列表。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("home_dishes").select("*").gt("servings", 0)
        if owner_member_id:
            q = q.eq("owner_member_id", owner_member_id)
        q = q.order("created_at", desc=True)
        resp = q.execute()
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_dishes 失败: %s", e)
        return []


# ============================================================
# Phase 5: 信件/便利贴/私密日记 RPC 封装 + 只读查询
# ============================================================

def rpc_write_letter(action_key, author_key, title, content, preview="", recipient_key="user", room_key=""):
    return _call_rpc("rpc_home_write_letter", {
        "p_action_key": action_key, "p_author_key": author_key,
        "p_title": title, "p_content": content, "p_preview": preview,
        "p_recipient_key": recipient_key, "p_room_key": room_key,
    })

def rpc_open_letter(action_key, letter_key):
    return _call_rpc("rpc_home_open_letter", {"p_action_key": action_key, "p_letter_key": letter_key})

def rpc_archive_letter(action_key, letter_key):
    return _call_rpc("rpc_home_archive_letter", {"p_action_key": action_key, "p_letter_key": letter_key})

def rpc_leave_note(action_key, author_key, room_key, content):
    return _call_rpc("rpc_home_leave_note", {
        "p_action_key": action_key, "p_author_key": author_key,
        "p_room_key": room_key, "p_content": content,
    })

def rpc_read_note(action_key, note_key):
    return _call_rpc("rpc_home_read_note", {"p_action_key": action_key, "p_note_key": note_key})

def rpc_archive_note(action_key, note_key):
    return _call_rpc("rpc_home_archive_note", {"p_action_key": action_key, "p_note_key": note_key})

def rpc_write_private_diary(action_key, author_key, title, content, mood="平静"):
    return _call_rpc("rpc_home_write_private_diary", {
        "p_action_key": action_key, "p_author_key": author_key,
        "p_title": title, "p_content": content, "p_mood": mood,
    })

def rpc_read_private_diary(action_key, diary_key):
    return _call_rpc("rpc_home_read_private_diary", {"p_action_key": action_key, "p_diary_key": diary_key})

def rpc_archive_private_diary(action_key, diary_key):
    return _call_rpc("rpc_home_archive_private_diary", {"p_action_key": action_key, "p_diary_key": diary_key})


# --- 只读查询 ---

def fetch_letters(status_filter: str = "") -> list[dict]:
    """查询信件列表（不返回 content，只返回 preview）。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("home_letters").select("id,letter_key,author_member_id,title,preview,status,created_at,opened_at,archived_at").neq("status", "archived") if not status_filter else sb.table("home_letters").select("id,letter_key,author_member_id,title,preview,status,created_at,opened_at,archived_at").eq("status", status_filter)
        q = q.order("created_at", desc=True)
        resp = q.execute()
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_letters 失败: %s", e)
        return []

def fetch_unopened_letter_count() -> int:
    """查询未拆信件数量。"""
    sb = _get_supabase()
    if sb is None:
        return 0
    try:
        resp = sb.table("home_letters").select("id", count="exact").eq("status", "unopened").execute()
        return resp.count or 0
    except Exception:
        return 0

def fetch_notes_by_room(room_id: str, include_read: bool = False) -> list[dict]:
    """查询指定房间的便利贴（SQL 层不查询 content 列，用 left() 生成预览）。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        # 安全修复：不 SELECT content 列，在 SQL 层用 left() 截取预览
        q = sb.table("home_notes").select("id,note_key,room_id,author_member_id,status,created_at,read_at").neq("status", "archived") if not include_read else sb.table("home_notes").select("id,note_key,room_id,author_member_id,status,created_at,read_at")
        q = q.eq("room_id", room_id)
        if not include_read:
            q = q.neq("status", "archived")
        q = q.order("created_at", desc=True)
        resp = q.execute()
        # content 不在 SELECT 列中，无法截取预览；返回空 preview
        result = resp.data or []
        for n in result:
            n["preview"] = ""  # content 未查询，预览为空
        return result
    except Exception as e:
        logger.warning("home.repository.fetch_notes_by_room 失败: %s", e)
        return []

def fetch_note_by_key(note_key: str) -> Optional[dict]:
    """按 note_key 查询便利贴（含 content，仅内部受控调用使用）。"""
    sb = _get_supabase()
    if sb is None:
        return None
    try:
        resp = sb.table("home_notes").select("*").eq("note_key", note_key).limit(1).execute()
        return (resp.data or [None])[0]
    except Exception as e:
        logger.warning("home.repository.fetch_note_by_key 失败: %s", e)
        return None


def fetch_private_diaries(author_member_id: str = "") -> list[dict]:
    """查询私密日记列表（不返回 content，只返回 title/mood/时间）。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        q = sb.table("home_private_diaries").select("id,diary_key,author_member_id,title,mood,status,created_at").neq("status", "archived")
        if author_member_id:
            q = q.eq("author_member_id", author_member_id)
        q = q.order("created_at", desc=True)
        resp = q.execute()
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_private_diaries 失败: %s", e)
        return []


def fetch_legacy_secret_diaries(limit: int = 50, offset: int = 0) -> list[dict]:
    """查询旧 memories.Secret_Diary 元数据（不返回 content/embedding）。"""
    sb = _get_supabase()
    if sb is None:
        return []
    try:
        # 安全：不 SELECT content 和 embedding 列
        resp = (sb.table("memories")
                .select("id,title,mood,tags,created_at")
                .eq("tags", "Secret_Diary")
                .order("created_at", desc=True)
                .range(offset, offset + limit - 1)
                .execute())
        return resp.data or []
    except Exception as e:
        logger.warning("home.repository.fetch_legacy_secret_diaries 失败: %s", e)
        return []


def count_legacy_secret_diaries() -> int:
    """统计旧 Secret_Diary 数量。"""
    sb = _get_supabase()
    if sb is None:
        return 0
    try:
        resp = sb.table("memories").select("id", count="exact").eq("tags", "Secret_Diary").execute()
        return resp.count or 0
    except Exception:
        return 0
