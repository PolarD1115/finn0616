"""
home/service.py — 只读观察服务
==============================
本阶段只实现只读观察，不实现写操作和生活副作用。
所有函数返回统一格式 dict（{ok, message, data/error_code}）。
"""

from __future__ import annotations

from typing import Any

from home import repository as repo
from home.schemas import ok_result, err_result, validate_limit


# ============================================================
# 观察接口
# ============================================================

def observe_home() -> dict:
    """观察整个家庭状态：房间、成员、近期事件概览。

    返回 data 包含:
    - rooms: 可见房间列表
    - members: 活跃成员列表（含状态摘要）
    - recent_events: 最近 10 条生活事件
    - pending_jobs: 待执行任务数
    """
    rooms = repo.fetch_rooms(enabled_only=True, include_hidden=False)
    members_raw = repo.fetch_members(active_only=True)

    # 批量获取成员状态
    member_ids = [m.get("id", "") for m in members_raw if m.get("id")]
    states_raw = repo.fetch_member_states(member_ids)
    state_map = {s.get("member_id"): s for s in states_raw if s.get("member_id")}

    # 组装成员摘要
    members = []
    for m in members_raw:
        mid = m.get("id", "")
        state = state_map.get(mid)
        view = _compose_member_view(m, state)
        members.append({
            "stable_key": m.get("stable_key", ""),
            "name": m.get("name", ""),
            "member_type": m.get("member_type", ""),
            "lifecycle_status": m.get("lifecycle_status", ""),
            "current_room_name": view.get("current_room_name"),
            "state": view,
        })

    events = repo.fetch_recent_events(limit=10, exclude_private=True)
    pending_jobs = repo.fetch_pending_jobs(limit=50)

    data = {
        "rooms": [
            {
                "stable_key": r.get("stable_key", ""),
                "name": r.get("name", ""),
                "emoji": r.get("emoji", ""),
                "room_type": r.get("room_type", ""),
                "description": r.get("description", ""),
            }
            for r in rooms
        ],
        "members": members,
        "recent_events": _events_brief(events),
        "pending_jobs_count": len(pending_jobs),
    }
    return ok_result("家庭观察完成", data)


def observe_room(room_key: str) -> dict:
    """观察指定房间：房间详情、物品、近期事件。

    room_key 可以是 stable_key（如 'living_room'）或 UUID。
    """
    if not room_key or not room_key.strip():
        return err_result("房间标识为空", "EMPTY_ROOM_KEY")

    room = _resolve_room(room_key.strip())
    if room is None:
        return err_result(f"未找到房间: {room_key}", "ROOM_NOT_FOUND")

    room_id = room.get("id", "")
    objects = repo.fetch_objects_by_room(room_id, include_hidden=False)
    events = repo.fetch_events_by_room(room_id, limit=10)

    data = {
        "room": {
            "stable_key": room.get("stable_key", ""),
            "name": room.get("name", ""),
            "emoji": room.get("emoji", ""),
            "room_type": room.get("room_type", ""),
            "description": room.get("description", ""),
            "is_hidden": room.get("is_hidden", False),
        },
        "objects": [
            {
                "id": o.get("id", ""),
                "name": o.get("name", ""),
                "object_type": o.get("object_type", ""),
                "description": o.get("description", ""),
                "visual": o.get("visual", {}),
            }
            for o in objects
        ],
        "recent_events": _events_brief(events),
    }
    return ok_result("房间观察完成", data)


def observe_member(member_key: str) -> dict:
    """观察指定家庭成员：成员信息、状态、近期事件。

    member_key 可以是 stable_key（如 'finn'）或 UUID。
    宠物成员的生理状态来自 pets 权威源，关系状态来自 home_member_states。
    """
    if not member_key or not member_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")

    member = _resolve_member(member_key.strip())
    if member is None:
        return err_result(f"未找到成员: {member_key}", "MEMBER_NOT_FOUND")

    member_id = member.get("id", "")
    state = repo.fetch_member_state(member_id)
    events = repo.fetch_events_by_member(member_id, limit=10)

    view = _compose_member_view(member, state)

    data = {
        "member": {
            "stable_key": member.get("stable_key", ""),
            "name": member.get("name", ""),
            "member_type": member.get("member_type", ""),
            "lifecycle_status": member.get("lifecycle_status", ""),
            "is_active": member.get("is_active", True),
        },
        "state": view,
        "current_room_name": view.get("current_room_name"),
        "recent_events": _events_brief(events),
    }
    return ok_result("成员观察完成", data)


def get_recent_events(limit: int = 20, event_type: str = "") -> dict:
    """查看家庭生活事件时间线。"""
    ok, err = validate_limit(limit, max_val=100)
    if not ok:
        return err_result("limit 须为 1..100", err)

    events = repo.fetch_recent_events(limit=limit, event_type=event_type, exclude_private=True)
    data = {"events": _events_brief(events), "count": len(events)}
    return ok_result(f"获取到 {len(events)} 条事件", data)


def get_action_status(action_key: str) -> dict:
    """查询行动执行状态。相同 action_key 重试时返回已有记录，不重复执行。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")

    record = repo.fetch_action_by_key(action_key.strip())
    if record is None:
        return err_result(f"未找到行动记录: {action_key}", "ACTION_NOT_FOUND")

    data = {
        "action_key": record.get("action_key", ""),
        "action_type": record.get("action_type", ""),
        "status": record.get("status", ""),
        "requested_at": record.get("requested_at"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "error_code": record.get("error_code"),
        "error_message": record.get("error_message"),
        "result": record.get("result"),
    }
    return ok_result("行动状态查询完成", data)


# ============================================================
# 内部辅助
# ============================================================

def _compose_member_view(member: dict, state: dict | None) -> dict:
    """组合成员运行时视图。

    【唯一实现】——整个项目中仅此一处读取 pets 并合并权威来源。

    AI 成员：全部状态来自 home_member_states。
    宠物成员：生理状态来自 pets 权威源，关系状态来自 home_member_states。
    pets 不可用时：生理字段返回 null，physiology_source='unavailable'，
    禁止回退到过期 Home 生理快照。关系状态仍正常返回。

    返回结构包含：
    - physiology_source: "pets" | "home_member_states" | "unavailable"
    - physiology_available: bool
    - relationship_source: "home_member_states"
    - 各状态字段
    - current_room_name / room_mapping_status
    """
    if not state:
        state = {}

    member_type = member.get("member_type", "")

    if member_type == "pet":
        pet = repo.fetch_pet_by_member(member)
        if pet is not None:
            # 生理状态来自 pets 权威源
            current_room_key = pet.get("current_room", "")
            room = repo.fetch_room_by_key(current_room_key) if current_room_key else None
            if room:
                room_mapping_status = "mapped"
                current_room_name = room.get("name")
            else:
                room_mapping_status = "unknown"
                current_room_name = None
            return {
                "physiology_source": "pets",
                "physiology_available": True,
                "relationship_source": "home_member_states",
                "hunger": _round(pet.get("hunger")),
                "happiness": _round(pet.get("happiness")),
                "health": _round(pet.get("health")),
                "energy": _round(pet.get("energy")),
                "cleanliness": _round(pet.get("cleanliness")),
                "status": pet.get("status"),
                "mood": pet.get("mood"),
                "current_room": current_room_key,
                "current_room_stable_key": current_room_key if room else None,
                "current_room_name": current_room_name,
                "room_mapping_status": room_mapping_status,
                "comfort": _round(state.get("comfort")),
                "connection": _round(state.get("connection")),
                "intimacy": _round(state.get("intimacy")),
                "last_settled_at": state.get("last_settled_at"),
            }
        else:
            # pets 不可用：生理字段全部 null，禁止回退到过期 Home 快照
            return {
                "physiology_source": "unavailable",
                "physiology_available": False,
                "relationship_source": "home_member_states",
                "hunger": None,
                "happiness": None,
                "health": None,
                "energy": None,
                "cleanliness": None,
                "status": None,
                "mood": None,
                "current_room": None,
                "current_room_stable_key": None,
                "current_room_name": None,
                "room_mapping_status": "unavailable",
                "comfort": _round(state.get("comfort")),
                "connection": _round(state.get("connection")),
                "intimacy": _round(state.get("intimacy")),
                "last_settled_at": state.get("last_settled_at"),
            }
    else:
        # AI / doll / custom：全部来自 home_member_states
        return {
            "physiology_source": "home_member_states",
            "physiology_available": True,
            "relationship_source": "home_member_states",
            "hunger": _round(state.get("hunger")),
            "energy": _round(state.get("energy")),
            "mood": _round(state.get("mood")),
            "comfort": _round(state.get("comfort")),
            "connection": _round(state.get("connection")),
            "intimacy": _round(state.get("intimacy")),
            "health": _round(state.get("health")),
            "cleanliness": _round(state.get("cleanliness")),
            "current_room_id": state.get("current_room_id"),
            "last_settled_at": state.get("last_settled_at"),
        }


def _round(value) -> float | None:
    """安全四舍五入到 1 位小数。None 保留 None。"""
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _resolve_room(key: str) -> dict | None:
    """按 stable_key 或 UUID 解析房间。"""
    # UUID 格式：8-4-4-4-12 hex
    if len(key) == 36 and key.count("-") == 4:
        return repo.fetch_room_by_id(key)
    return repo.fetch_room_by_key(key)


def _resolve_member(key: str) -> dict | None:
    """按 stable_key 或 UUID 解析成员。"""
    if len(key) == 36 and key.count("-") == 4:
        return repo.fetch_member_by_id(key)
    return repo.fetch_member_by_key(key)


def _state_brief(state: dict | None) -> dict | None:
    """从状态记录提取摘要。"""
    if not state:
        return None
    return {
        "hunger": state.get("hunger"),
        "energy": state.get("energy"),
        "mood": state.get("mood"),
        "comfort": state.get("comfort"),
        "connection": state.get("connection"),
        "intimacy": state.get("intimacy"),
        "health": state.get("health"),
        "cleanliness": state.get("cleanliness"),
        "current_room_id": state.get("current_room_id"),
        "last_settled_at": state.get("last_settled_at"),
    }


def _events_brief(events: list[dict]) -> list[dict]:
    """从事件列表提取摘要。"""
    return [
        {
            "event_type": ev.get("event_type", ""),
            "summary": ev.get("summary", ""),
            "source": ev.get("source", ""),
            "visibility": ev.get("visibility", ""),
            "room_id": ev.get("room_id"),
            "actor_member_id": ev.get("actor_member_id"),
            "occurred_at": ev.get("occurred_at"),
        }
        for ev in events
    ]


# ============================================================
# Phase 3: 成员初始化 + 生活动作
# ============================================================

def initialize_members() -> dict:
    """幂等初始化 AI 本体 (ai_primary) + 小满 (pet_xiaoman)。

    AI 使用保守中性初始值；小满从 pets 表只读快照一次。
    多次调用不会覆盖已有成员或状态。
    """
    return repo.rpc_initialize_members()


def settle_member(member_key: str) -> dict:
    """结算成员状态（elapsed-time 衰减）。

    仅 AI 成员结算，宠物不结算（避免与 cat_tick 双重衰减）。
    """
    if not member_key or not member_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    return repo.rpc_settle_member(member_key.strip())


def enter_room(actor_key: str, room_key: str, action_key: str) -> dict:
    """进入房间。

    流程：幂等检查 → 校验成员/房间 → 结算状态 → 更新 current_room_id → 写事件 → 完成 action_run

    宠物成员（member_type='pet'）不允许作为 actor — 宠物移动由旧宠物系统控制。
    """
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not actor_key or not actor_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if not room_key or not room_key.strip():
        return err_result("房间标识为空", "EMPTY_ROOM_KEY")
    # Python 层防御性拦截：宠物不能通过 Home 工具移动
    member = _resolve_member(actor_key.strip())
    if member and member.get("member_type") == "pet":
        return err_result("宠物移动由旧宠物系统控制", "PET_CANNOT_ACT")
    return repo.rpc_enter_room(action_key.strip(), actor_key.strip(), room_key.strip())


def rest(actor_key: str, duration_minutes: int, action_key: str, mode: str = "rest") -> dict:
    """休息（mode='rest'）或睡眠（mode='sleep'）。

    不阻塞线程，是模拟经过一段休息时间后的原子结算。
    """
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not actor_key or not actor_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if mode not in ("rest", "sleep"):
        return err_result("mode 必须为 rest 或 sleep", "INVALID_MODE")
    try:
        dur = int(duration_minutes)
    except (TypeError, ValueError):
        return err_result("duration_minutes 须为整数", "INVALID_DURATION")
    if dur < 1:
        return err_result("持续时间至少 1 分钟", "DURATION_TOO_SHORT")
    if dur > 1440:
        return err_result("持续时间不能超过 1440 分钟", "DURATION_TOO_LONG")
    return repo.rpc_rest(action_key.strip(), actor_key.strip(), dur, mode)


def sleep(actor_key: str, duration_minutes: int, action_key: str) -> dict:
    """睡眠。rest() 的 mode='sleep' 快捷方式。"""
    return rest(actor_key, duration_minutes, action_key, mode="sleep")


def spend_time(
    actor_key: str, target_key: str, activity: str,
    duration_minutes: int, action_key: str
) -> dict:
    """陪伴互动。

    小幅改善双方 comfort/connection，intimacy 受每日上限控制。
    """
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not actor_key or not actor_key.strip():
        return err_result("actor 标识为空", "EMPTY_ACTOR_KEY")
    if not target_key or not target_key.strip():
        return err_result("target 标识为空", "EMPTY_TARGET_KEY")
    if not activity or not activity.strip():
        return err_result("activity 为空", "EMPTY_ACTIVITY")
    try:
        dur = int(duration_minutes)
    except (TypeError, ValueError):
        return err_result("duration_minutes 须为整数", "INVALID_DURATION")
    if dur < 1 or dur > 480:
        return err_result("duration_minutes 须为 1..480", "INVALID_DURATION")
    return repo.rpc_spend_time(
        action_key.strip(), actor_key.strip(), target_key.strip(),
        activity.strip(), dur
    )


# ============================================================
# Phase 4: 植物与烹饪服务
# ============================================================

def garden_observe() -> dict:
    """只读观察花园：植物列表、种子目录、近期种植/收获事件。

    内部先批量结算 active 植物的生长/水分/健康度，确保 observe 看到的
    stage/health/water_level 是最新值（而非上次操作时的 stale 快照）。
    """
    plants = repo.fetch_plants_settled()
    seeds = repo.fetch_seed_catalog()
    events = repo.fetch_recent_events(limit=5, event_type="", exclude_private=True)

    # 过滤植物相关事件
    plant_events = [
        ev for ev in events
        if ev.get("event_type") in ("planted", "watered", "harvested")
    ]

    data = {
        "plants": [
            {
                "id": p.get("id", ""),
                "name": p.get("name", ""),
                "seed_key": p.get("seed_key", ""),
                "stage": p.get("stage", ""),
                "health": p.get("health"),
                "water_level": p.get("water_level"),
                "status": p.get("status", ""),
                "is_mature": p.get("stage") == "mature",
                "planted_at": p.get("planted_at"),
            }
            for p in plants
        ],
        "available_seeds": [
            {"stable_key": s.get("stable_key", ""), "name": s.get("name", ""), "emoji": s.get("emoji", "")}
            for s in seeds
        ],
        "recent_events": _events_brief(plant_events),
    }
    return ok_result("花园观察完成", data)


def plant_seed(actor_key: str, seed_key: str, action_key: str) -> dict:
    """种植种子。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not actor_key or not actor_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if not seed_key or not seed_key.strip():
        return err_result("种子标识为空", "EMPTY_SEED_KEY")
    return repo.rpc_plant_seed(action_key.strip(), actor_key.strip(), seed_key.strip())


def water_plant(actor_key: str, plant_id: str, action_key: str) -> dict:
    """给植物浇水。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not actor_key or not actor_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if not plant_id or not plant_id.strip():
        return err_result("植物标识为空", "EMPTY_PLANT_ID")
    return repo.rpc_water_plant(action_key.strip(), actor_key.strip(), plant_id.strip())


def harvest_plant(actor_key: str, plant_id: str, action_key: str) -> dict:
    """收获植物。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not actor_key or not actor_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if not plant_id or not plant_id.strip():
        return err_result("植物标识为空", "EMPTY_PLANT_ID")
    return repo.rpc_harvest_plant(action_key.strip(), actor_key.strip(), plant_id.strip())


def pantry_observe() -> dict:
    """只读观察库存：食材、菜品、菜谱摘要。"""
    inventory = repo.fetch_inventory()
    recipes = repo.fetch_recipe_catalog()
    dishes = repo.fetch_dishes()

    data = {
        "inventory": [
            {
                "item_key": i.get("item_key", ""),
                "item_kind": i.get("item_kind", ""),
                "storage_location": i.get("storage_location", ""),
                "quantity": i.get("quantity"),
                "unit": i.get("unit", ""),
            }
            for i in inventory
        ],
        "dishes": [
            {
                "id": d.get("id", ""),
                "name": d.get("name", ""),
                "servings": d.get("servings", 0),
                "quality": d.get("quality"),
            }
            for d in dishes
        ],
        "available_recipes": [
            {"stable_key": r.get("stable_key", ""), "name": r.get("name", ""), "emoji": r.get("emoji", "")}
            for r in recipes
        ],
    }
    return ok_result("库存观察完成", data)


def cook_recipe(actor_key: str, recipe_key: str, action_key: str) -> dict:
    """按菜谱烹饪。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not actor_key or not actor_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if not recipe_key or not recipe_key.strip():
        return err_result("菜谱标识为空", "EMPTY_RECIPE_KEY")
    return repo.rpc_cook_recipe(action_key.strip(), actor_key.strip(), recipe_key.strip())


def cook_freestyle(actor_key: str, ingredient_choices: dict, action_key: str) -> dict:
    """自由烹饪。ingredient_choices 是 {item_key: quantity} 字典。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not actor_key or not actor_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if not ingredient_choices or not isinstance(ingredient_choices, dict):
        return err_result("食材选择为空", "EMPTY_INGREDIENTS")
    if len(ingredient_choices) > 5:
        return err_result("食材种类不能超过 5 种", "TOO_MANY_INGREDIENT_TYPES")
    # 验证数量
    for k, v in ingredient_choices.items():
        try:
            qty = int(v)
        except (TypeError, ValueError):
            return err_result(f"食材 {k} 数量无效", "INVALID_QUANTITY")
        if qty <= 0:
            return err_result(f"食材 {k} 数量必须为正整数", "INVALID_QUANTITY")
    return repo.rpc_cook_freestyle(action_key.strip(), actor_key.strip(), ingredient_choices)


def eat_dish(actor_key: str, dish_id: str, action_key: str) -> dict:
    """食用菜品。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not actor_key or not actor_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if not dish_id or not dish_id.strip():
        return err_result("菜品标识为空", "EMPTY_DISH_ID")
    return repo.rpc_eat_dish(action_key.strip(), actor_key.strip(), dish_id.strip())


def feed_member(actor_key: str, target_key: str, dish_id: str, action_key: str) -> dict:
    """喂食家庭成员。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not actor_key or not actor_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if not target_key or not target_key.strip():
        return err_result("目标成员标识为空", "EMPTY_TARGET_KEY")
    if not dish_id or not dish_id.strip():
        return err_result("菜品标识为空", "EMPTY_DISH_ID")
    return repo.rpc_feed_member(action_key.strip(), actor_key.strip(), target_key.strip(), dish_id.strip())


# ============================================================
# Phase 5: 信件/便利贴/私密日记服务
# ============================================================

def write_letter(author_key: str, title: str, content: str, action_key: str,
                 preview: str = "", recipient_key: str = "user", room_key: str = "") -> dict:
    """写信。未拆信件不返回正文。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not author_key or not author_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if not title or not title.strip():
        return err_result("标题为空", "EMPTY_TITLE")
    if not content or not content.strip():
        return err_result("正文为空", "EMPTY_CONTENT")
    if len(content) > 10000:
        return err_result("正文超过 10000 字", "CONTENT_TOO_LONG")
    return repo.rpc_write_letter(action_key.strip(), author_key.strip(), title.strip(),
                                  content, preview, recipient_key, room_key)


def list_letters(status_filter: str = "") -> dict:
    """列出信件。不返回未拆信正文。"""
    letters = repo.fetch_letters(status_filter)
    data = {
        "letters": [
            {
                "letter_key": l.get("letter_key", ""),
                "title": l.get("title", ""),
                "preview": l.get("preview", ""),
                "status": l.get("status", ""),
                "is_unopened": l.get("status") == "unopened",
                "created_at": l.get("created_at"),
            }
            for l in letters
        ],
        "count": len(letters),
    }
    return ok_result("信件列表", data)


def open_letter(letter_key: str, action_key: str) -> dict:
    """拆信。只有调用此函数才返回正文。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not letter_key or not letter_key.strip():
        return err_result("信件标识为空", "EMPTY_LETTER_KEY")
    return repo.rpc_open_letter(action_key.strip(), letter_key.strip())


def archive_letter(letter_key: str, action_key: str) -> dict:
    """归档信件（软归档，不删除）。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not letter_key or not letter_key.strip():
        return err_result("信件标识为空", "EMPTY_LETTER_KEY")
    return repo.rpc_archive_letter(action_key.strip(), letter_key.strip())


def leave_note(author_key: str, room_key: str, content: str, action_key: str) -> dict:
    """在房间留便利贴。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not author_key or not author_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if not room_key or not room_key.strip():
        return err_result("房间标识为空", "EMPTY_ROOM_KEY")
    if not content or not content.strip():
        return err_result("内容为空", "EMPTY_CONTENT")
    if len(content) > 2000:
        return err_result("内容超过 2000 字", "CONTENT_TOO_LONG")
    return repo.rpc_leave_note(action_key.strip(), author_key.strip(), room_key.strip(), content)


def list_room_notes(room_key: str, include_read: bool = False) -> dict:
    """列出房间便利贴。不返回全文，只返回预览。"""
    if not room_key or not room_key.strip():
        return err_result("房间标识为空", "EMPTY_ROOM_KEY")
    # 先解析 room_key 到 room_id
    room = repo.fetch_room_by_key(room_key.strip())
    if room is None:
        return err_result("房间不存在", "ROOM_NOT_FOUND")
    notes = repo.fetch_notes_by_room(room.get("id", ""), include_read)
    data = {
        "notes": [
            {
                "note_key": n.get("note_key", ""),
                "preview": n.get("preview", ""),
                "status": n.get("status", ""),
                "is_read": n.get("status") == "read",
                "created_at": n.get("created_at"),
            }
            for n in notes
        ],
        "count": len(notes),
    }
    return ok_result("便利贴列表", data)


def read_note(note_key: str, action_key: str) -> dict:
    """读取便利贴正文。安全修复：校验便利贴所属房间是否 enabled 且非隐藏。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not note_key or not note_key.strip():
        return err_result("便利贴标识为空", "EMPTY_NOTE_KEY")
    # 安全修复：先查便利贴所属房间，校验房间 enabled 且非隐藏
    note = repo.fetch_note_by_key(note_key.strip())
    if note is None:
        return err_result("便利贴不存在", "NOTE_NOT_FOUND")
    room_id = note.get("room_id")
    if room_id:
        room = repo.fetch_room_by_id(room_id)
        if room is None:
            return err_result("便利贴所属房间不存在", "NOTE_NOT_ACCESSIBLE")
        if not room.get("is_enabled", True):
            return err_result("便利贴所属房间已禁用", "NOTE_NOT_ACCESSIBLE")
        if room.get("is_hidden", False):
            return err_result("便利贴所属房间未解锁", "NOTE_NOT_ACCESSIBLE")
    return repo.rpc_read_note(action_key.strip(), note_key.strip())


def archive_note(note_key: str, action_key: str) -> dict:
    """归档便利贴（软归档）。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not note_key or not note_key.strip():
        return err_result("便利贴标识为空", "EMPTY_NOTE_KEY")
    return repo.rpc_archive_note(action_key.strip(), note_key.strip())


def write_private_diary(author_key: str, title: str, content: str, action_key: str,
                        mood: str = "平静", is_internal: bool = False) -> dict:
    """写私密日记。安全修复：MCP 路径强制 author_key=ai_primary，不信任客户端参数。"""
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    # 安全修复：MCP 路径（is_internal=False）强制 author_key，不信任客户端
    if not is_internal:
        author_key = "ai_primary"
    if not author_key or not author_key.strip():
        return err_result("成员标识为空", "EMPTY_MEMBER_KEY")
    if author_key.strip() != "ai_primary":
        return err_result("仅 AI 本体可写私密日记", "NOT_AUTHORIZED")
    if not title or not title.strip():
        return err_result("标题为空", "EMPTY_TITLE")
    if not content or not content.strip():
        return err_result("正文为空", "EMPTY_CONTENT")
    if len(content) > 10000:
        return err_result("正文超过 10000 字", "CONTENT_TOO_LONG")
    return repo.rpc_write_private_diary(action_key.strip(), author_key.strip(),
                                         title.strip(), content, mood)


def list_private_diary() -> dict:
    """列出私密日记标题（不返回正文）。"""
    diaries = repo.fetch_private_diaries()
    data = {
        "diaries": [
            {
                "diary_key": d.get("diary_key", ""),
                "title": d.get("title", ""),
                "mood": d.get("mood", ""),
                "created_at": d.get("created_at"),
            }
            for d in diaries
        ],
        "count": len(diaries),
    }
    return ok_result("私密日记列表", data)


def read_private_diary(diary_key: str, action_key: str, is_internal: bool = False) -> dict:
    """读取私密日记正文。安全修复：仅内部受控调用（is_internal=True）可读取。"""
    if not is_internal:
        return err_result("私密日记正文不通过通用工具暴露", "PRIVATE_DIARY_ACCESS_DENIED")
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not diary_key or not diary_key.strip():
        return err_result("日记标识为空", "EMPTY_DIARY_KEY")
    return repo.rpc_read_private_diary(action_key.strip(), diary_key.strip())


def archive_private_diary(diary_key: str, action_key: str, is_internal: bool = False) -> dict:
    """归档私密日记（软归档）。安全修复：仅内部受控调用可归档。"""
    if not is_internal:
        return err_result("私密日记归档不通过通用工具暴露", "PRIVATE_DIARY_ACCESS_DENIED")
    if not action_key or not action_key.strip():
        return err_result("action_key 为空", "EMPTY_ACTION_KEY")
    if not diary_key or not diary_key.strip():
        return err_result("日记标识为空", "EMPTY_DIARY_KEY")
    return repo.rpc_archive_private_diary(action_key.strip(), diary_key.strip())


# ============================================================
# Phase 6: 统一私密日记索引（新旧合并元数据，不返回正文）
# ============================================================

def list_private_diary_index(limit: int = 50, offset: int = 0) -> dict:
    """统一私密日记索引：合并旧 memories.Secret_Diary 和新 home_private_diaries 元数据。

    返回结构：
    - items: [{reference, source, title, mood, created_at, status, is_archived}]
    - legacy_count: 旧日记总数
    - home_count: 新日记总数
    - total: 返回条数

    不返回正文、embedding 或内部 UUID。
    reference 格式：legacy:<id> 或 home:<diary_key>
    """
    ok, err = validate_limit(limit, max_val=200)
    if not ok:
        return err_result("limit 须为 1..200", err)
    if offset < 0:
        return err_result("offset 须 >= 0", "INVALID_OFFSET")

    # 查询旧日记元数据（不查 content/embedding）
    legacy_items = repo.fetch_legacy_secret_diaries(limit=limit, offset=offset)
    legacy_count = repo.count_legacy_secret_diaries()

    # 查询新日记元数据（不查 content）
    home_items = repo.fetch_private_diaries()

    # 合并并统一格式
    merged = []
    for item in legacy_items:
        merged.append({
            "reference": f"legacy:{item.get('id', '')}",
            "source": "legacy",
            "title": item.get("title", ""),
            "mood": item.get("mood", ""),
            "created_at": item.get("created_at"),
            "status": "active",
            "is_archived": False,
        })
    for item in home_items:
        merged.append({
            "reference": f"home:{item.get('diary_key', '')}",
            "source": "home",
            "title": item.get("title", ""),
            "mood": item.get("mood", ""),
            "created_at": item.get("created_at"),
            "status": item.get("status", "active"),
            "is_archived": item.get("status") == "archived",
        })

    # 按 created_at 统一倒序
    merged.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    # 分页
    paginated = merged[offset:offset + limit]

    data = {
        "items": paginated,
        "legacy_count": legacy_count,
        "home_count": len(home_items),
        "total": len(paginated),
    }
    return ok_result("统一私密日记索引", data)


def read_private_diary_by_reference(reference: str, is_internal: bool = False) -> dict:
    """按统一引用读取私密日记正文。仅内部受控调用。

    reference 格式：legacy:<id> 或 home:<diary_key>
    """
    if not is_internal:
        return err_result("私密日记正文不通过通用工具暴露", "PRIVATE_DIARY_ACCESS_DENIED")
    if not reference or ":" not in reference:
        return err_result("reference 格式非法", "INVALID_REFERENCE")

    source, key = reference.split(":", 1)
    if source == "legacy":
        # 旧 memories 受控读取
        sb = repo._get_supabase()
        if sb is None:
            return err_result("数据库未连接", "DB_UNAVAILABLE")
        try:
            resp = sb.table("memories").select("id,title,content,mood,created_at").eq("tags", "Secret_Diary").eq("id", int(key)).limit(1).execute()
            if not resp.data:
                return err_result("日记不存在", "NOT_FOUND_OR_FORBIDDEN")
            d = resp.data[0]
            return ok_result("旧私密日记读取", {
                "reference": reference, "source": "legacy",
                "title": d.get("title", ""), "content": d.get("content", ""),
                "mood": d.get("mood", ""), "created_at": d.get("created_at"),
            })
        except (ValueError, TypeError):
            return err_result("reference 格式非法", "INVALID_REFERENCE")
        except Exception:
            return err_result("读取失败", "RPC_ERROR")
    elif source == "home":
        # 新 home_private_diaries 受控读取
        return repo.rpc_read_private_diary("internal_read", key)
    else:
        return err_result("不支持的来源", "INVALID_REFERENCE")
