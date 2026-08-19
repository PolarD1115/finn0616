"""
home/context.py — 上下文构建
============================
将 Home Runtime 的观察结果转换为 AI 可理解的上下文文本。
约束：
- 只注入真实存在的数据
- 新表为空时返回明确的空状态
- 不伪造家庭成员、房间物品或生活事件
- 不把秘密日记全文注入普通聊天
- 不绕过五层记忆 MCP
"""

from __future__ import annotations

from typing import Any

from home import repository as repo
from home import service as _svc
from home.schemas import validate_state_value


def build_home_context() -> str:
    """构建家庭环境上下文文本，供 AI 理解当前居家状态。

    宠物成员的生理状态来自 pets 权威源（通过 service._compose_member_view），
    不使用过期 Home 快照。pets 不可用时显示"状态未知"。
    不显示内部 UUID。
    """
    rooms = repo.fetch_rooms(enabled_only=True, include_hidden=False)
    members_raw = repo.fetch_members(active_only=True)

    member_ids = [m.get("id", "") for m in members_raw if m.get("id")]
    states_raw = repo.fetch_member_states(member_ids)
    state_map = {s.get("member_id"): s for s in states_raw if s.get("member_id")}

    events = repo.fetch_recent_events(limit=5, exclude_private=True)

    lines: list[str] = []

    # 房间
    if rooms:
        room_strs = [f"{r.get('emoji', '🏠')}{r.get('name', '')}" for r in rooms]
        lines.append(f"🏠 家里有的房间：{'、'.join(room_strs)}")
    else:
        lines.append("🏠 家里还没有设置房间。")

    # 成员
    if members_raw:
        member_strs = []
        for m in members_raw:
            mid = m.get("id", "")
            name = m.get("name", "")
            mtype = m.get("member_type", "")
            state = state_map.get(mid)
            view = _svc._compose_member_view(m, state)
            type_label = {"ai": "AI", "pet": "宠物", "doll": "玩偶", "custom": "成员"}.get(mtype, mtype)
            if view.get("physiology_available"):
                if mtype == "pet":
                    hunger = view.get("hunger")
                    happiness = view.get("happiness")
                    energy = view.get("energy")
                    member_strs.append(f"{name}({type_label}，饱腹{hunger}，快乐{happiness}，精力{energy})")
                else:
                    mood = view.get("mood")
                    energy = view.get("energy")
                    member_strs.append(f"{name}({type_label}，心情{mood}，精力{energy})")
            else:
                member_strs.append(f"{name}({type_label}，状态未知)")
        lines.append(f"👥 家庭成员：{'、'.join(member_strs)}")
    else:
        lines.append("👥 目前还没有家庭成员。")

    # 近期事件
    if events:
        event_strs = [f"{ev.get('summary', '')}({ev.get('occurred_at', '')[:10]})" for ev in events]
        lines.append(f"📋 最近发生的事：{'；'.join(event_strs)}")
    else:
        lines.append("📋 最近没有生活事件记录。")

    # 花园状态（Phase 4）
    plants = repo.fetch_plants()
    if plants:
        plant_strs = []
        for p in plants:
            name = p.get("name", "")
            stage = p.get("stage", "")
            stage_label = {"planted": "刚种下", "growing": "生长中", "mature": "已成熟", "harvested": "已收获"}.get(stage, stage)
            water = p.get("water_level", 0)
            plant_strs.append(f"{name}({stage_label}，水分{water:.0f})")
        lines.append(f"🌱 花园植物：{'、'.join(plant_strs)}")

    # 库存摘要（Phase 4）
    inventory = repo.fetch_inventory()
    if inventory:
        inv_strs = [f"{i.get('item_key','')}×{i.get('quantity',0)}" for i in inventory if i.get("quantity", 0) > 0]
        if inv_strs:
            lines.append(f"🥘 库存食材：{'、'.join(inv_strs)}")

    # 菜品摘要
    dishes = repo.fetch_dishes()
    if dishes:
        dish_strs = [f"{d.get('name','')}×{d.get('servings',0)}份" for d in dishes if d.get("servings", 0) > 0]
        if dish_strs:
            lines.append(f"🍽️ 现有菜品：{'、'.join(dish_strs)}")

    # 未拆信件数量（Phase 5）
    unopened = repo.fetch_unopened_letter_count()
    if unopened > 0:
        lines.append(f"✉️ 有 {unopened} 封未拆开的信")

    return "\n".join(lines)


def format_room_brief(room: dict) -> str:
    """格式化单个房间的简要描述。"""
    emoji = room.get("emoji", "🏠")
    name = room.get("name", "")
    desc = room.get("description", "")
    rtype = room.get("room_type", "common")
    type_label = {"common": "公共", "private": "私密", "outdoor": "户外", "special": "特殊"}.get(rtype, rtype)
    parts = [f"{emoji} {name}（{type_label}）"]
    if desc:
        parts.append(desc)
    return "——".join(parts)


def format_member_brief(member: dict, state: dict | None = None) -> str:
    """格式化单个成员的简要描述。使用组合视图确保宠物显示 pets 权威值。"""
    name = member.get("name", "")
    mtype = member.get("member_type", "custom")
    type_label = {"ai": "AI", "pet": "宠物", "doll": "玩偶", "custom": "成员"}.get(mtype, mtype)
    status = member.get("lifecycle_status", "alive")

    view = _svc._compose_member_view(member, state)
    parts = [f"{name}（{type_label}，{status}）"]

    if view.get("physiology_available"):
        if mtype == "pet":
            hunger = view.get("hunger")
            happiness = view.get("happiness")
            energy = view.get("energy")
            parts.append(f"饱腹{hunger}·快乐{happiness}·精力{energy}")
        else:
            mood = view.get("mood")
            energy = view.get("energy")
            hunger = view.get("hunger")
            parts.append(f"心情{mood}·精力{energy}·饱腹{hunger}")
    elif view.get("physiology_source") == "unavailable":
        parts.append("状态未知")
    return "——".join(parts)


def format_event_brief(event: dict) -> str:
    """格式化单个事件的简要描述。"""
    etype = event.get("event_type", "")
    summary = event.get("summary", "")
    occurred = event.get("occurred_at", "")
    time_str = occurred[:16].replace("T", " ") if occurred else ""
    return f"[{time_str}] {etype}: {summary}"
