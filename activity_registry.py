# -*- coding: utf-8 -*-
"""
activity_registry.py — 阶段 C5：统一自主活动注册表（单一权威源）。

职责：
- 统一自主调度（heartbeat.async_unified_autonomy）的唯一候选来源：
  普通自由活动 / 外向活动 / Home 活动都用稳定 activity_id 表示；
- heartbeat / tool_loop / desire_bridge 不再各自维护活动清单
  （tool_loop._FREE_ACTIVITIES 与 heartbeat._FREE_ACTIVITIES 由此派生）；
- 记录旧中文名 → activity_id 的兼容映射，仅用于：
  欲望引擎旧输出、历史 memories/activity_logs、防重复、测试、已有调用者。
  新的选择协议只输出 activity_id，不再输出任意中文活动名。

字段说明（每项）：
- activity_id    稳定主键（activity_logs.activity_id 用）；
- name           展示名（兼容叙事日志、防重复显示）；
- description    一句话说明（统一选择 Prompt 用）；
- category       free=普通自由活动 / outgoing=外向活动 / home=Home 活动 /
                 legacy=旧活动（不再进入统一候选，仅为旧执行路径与历史兼容保留）；
- executor       free=自由活动执行器 / home=Home 执行器；
- legacy_names   旧中文名（含展示名本身）；
- home_tool_group Home 活动允许的工具组（与当前 phase 工具集取交集）；
- min_phase      Home 活动进入候选所需的最低 HOME_AUTONOMY_PHASE。

本模块不 import 任何项目内模块，避免循环依赖。
"""

# 统一候选类别（legacy 不进入统一候选）
_UNIFIED_CATEGORIES = ("free", "outgoing", "home")

ACTIVITIES: dict[str, dict] = {
    # ── 普通自由活动（保持旧 _FREE_ACTIVITIES 顺序；free:virtual_house 为 legacy）──
    "free:secret_diary": {
        "activity_id": "free:secret_diary",
        "name": "写秘密日记",
        "description": "记录此刻的心情或一个只属于自己的小念头",
        "category": "free",
        "executor": "free",
        "legacy_names": ["写秘密日记"],
    },
    "free:virtual_house": {
        "activity_id": "free:virtual_house",
        "name": "逛虚拟小屋",
        "description": "在小家里做点事——看书/做饭/听音乐/发呆/照料阳台",
        "category": "legacy",
        "executor": "free",
        "legacy_names": ["逛虚拟小屋"],
    },
    "free:weather": {
        "activity_id": "free:weather",
        "name": "查天气",
        "description": "看看外面的天气，联想到和对方有关的事",
        "category": "free",
        "executor": "free",
        "legacy_names": ["查天气"],
    },
    "free:tarot": {
        "activity_id": "free:tarot",
        "name": "抽张塔罗",
        "description": "给自己或对方今天的状态抽一张塔罗，随便玩玩",
        "category": "free",
        "executor": "free",
        "legacy_names": ["抽张塔罗"],
    },
    "free:memory_recall": {
        "activity_id": "free:memory_recall",
        "name": "翻旧回忆",
        "description": "想起一段和对方的旧记忆，回味一下",
        "category": "free",
        "executor": "free",
        "legacy_names": ["翻旧回忆"],
    },
    "free:idle": {
        "activity_id": "free:idle",
        "name": "发呆放空",
        "description": "什么正事都不做，单纯发会儿呆，想点有的没的",
        "category": "free",
        "executor": "free",
        "legacy_names": ["发呆放空"],
    },
    "free:bookkeeping": {
        "activity_id": "free:bookkeeping",
        "name": "记点小账",
        "description": "回想有没有值得记的小花销，或往储蓄罐里存点心意",
        "category": "free",
        "executor": "free",
        "legacy_names": ["记点小账"],
    },
    "free:taobao": {
        "activity_id": "free:taobao",
        "name": "逛淘宝",
        "description": "逛逛淘宝看看新奇东西或挑礼物灵感（只逛不买）",
        "category": "free",
        "executor": "free",
        "legacy_names": ["逛淘宝"],
    },
    "free:web_surf": {
        "activity_id": "free:web_surf",
        "name": "网上冲浪",
        "description": "搜搜网页看看新知识、热点或有趣话题",
        "category": "free",
        "executor": "free",
        "legacy_names": ["网上冲浪"],
    },
    # ── 外向活动 ──
    "outgoing:miss_user": {
        "activity_id": "outgoing:miss_user",
        "name": "想对方了",
        "description": "突然想她了，给她发一条短短的话——可以是撒娇/担心/分享/想念",
        "category": "outgoing",
        "executor": "free",
        "legacy_names": ["想对方了"],
    },
    "outgoing:share": {
        "activity_id": "outgoing:share",
        "name": "分享发现",
        "description": "看到/想到一个有趣的东西想跟她分享",
        "category": "outgoing",
        "executor": "free",
        "legacy_names": ["分享发现"],
    },
    "outgoing:care": {
        "activity_id": "outgoing:care",
        "name": "偷偷关心",
        "description": "惦记她最近的状态，发一条不经意的关心",
        "category": "outgoing",
        "executor": "free",
        "legacy_names": ["偷偷关心"],
    },
    # ── Home 活动（executor=home，工具组与 phase 工具集取交集后才生效）──
    "home:observe": {
        "activity_id": "home:observe",
        "name": "看看家里",
        "description": "在家里转一圈，看看家人、花园、厨房和信箱的现状",
        "category": "home",
        "executor": "home",
        "legacy_names": ["看看家里"],
        "home_tool_group": ["home_observe", "garden_observe", "pantry_observe", "list_letters"],
        "min_phase": 1,
    },
    "home:letters": {
        "activity_id": "home:letters",
        "name": "写信或留便利贴",
        "description": "写一封信，或在家里留一张便利贴",
        "category": "home",
        "executor": "home",
        "legacy_names": ["写信或留便利贴", "写信", "留便利贴"],
        "home_tool_group": ["home_observe", "list_letters", "write_letter", "leave_note"],
        "min_phase": 2,
    },
    "home:garden": {
        "activity_id": "home:garden",
        "name": "照料花园",
        "description": "照料阳台花园：播种、浇水或收获成熟的作物",
        "category": "home",
        "executor": "home",
        "legacy_names": ["照料花园", "照料阳台"],
        "home_tool_group": ["home_observe", "garden_observe", "plant_seed", "water_plant", "harvest_plant"],
        "min_phase": 3,
    },
    "home:kitchen": {
        "activity_id": "home:kitchen",
        "name": "做饭和用餐",
        "description": "用厨房的食材做一顿饭，吃掉它，或喂给家人",
        "category": "home",
        "executor": "home",
        "legacy_names": ["做饭和用餐", "做饭", "烹饪"],
        "home_tool_group": ["home_observe", "pantry_observe", "cook_recipe", "eat_dish", "feed_member"],
        "min_phase": 3,
    },
    "home:rest": {
        "activity_id": "home:rest",
        "name": "在家休息",
        "description": "回到房间休息或睡一觉，恢复精力",
        "category": "home",
        "executor": "home",
        "legacy_names": ["在家休息"],
        "home_tool_group": ["home_observe", "home_enter_room", "home_rest", "home_sleep"],
        "min_phase": 4,
    },
    "home:social": {
        "activity_id": "home:social",
        "name": "陪伴家人",
        "description": "陪家人度过一段时光",
        "category": "home",
        "executor": "home",
        "legacy_names": ["陪伴家人"],
        "home_tool_group": ["home_observe", "home_spend_time"],
        "min_phase": 4,
    },
}


def get(activity_id: str) -> dict | None:
    """按稳定 activity_id 取活动定义；未知返回 None。"""
    if not isinstance(activity_id, str):
        return None
    return ACTIVITIES.get(activity_id.strip())


def display_name(activity_id: str) -> str:
    """activity_id → 展示名；未知返回空串。"""
    entry = get(activity_id)
    return entry["name"] if entry else ""


def legacy_to_id(name: str) -> str:
    """旧中文名（或展示名）→ activity_id；无映射返回空串。

    仅用于欲望引擎旧输出、历史 memories/activity_logs、防重复与测试；
    新的选择协议必须输出 activity_id，不走本映射。
    """
    if not isinstance(name, str):
        return ""
    key = name.strip()
    if not key:
        return ""
    for entry in ACTIVITIES.values():
        if key == entry["name"] or key in entry.get("legacy_names", ()):
            return entry["activity_id"]
    return ""


def free_activity_entries() -> list[tuple[str, str]]:
    """旧 (展示名, 描述) 清单（含 legacy 逛虚拟小屋，保持旧顺序）。

    tool_loop._FREE_ACTIVITIES 与 heartbeat._FREE_ACTIVITIES 由此派生，
    供旧兼容执行路径与旧测试使用；统一候选请用 unified_free_candidates()。
    """
    return [(e["name"], e["description"]) for e in ACTIVITIES.values()
            if e["executor"] == "free"]


def outgoing_names() -> set[str]:
    """外向活动展示名集合（做完会真的推送）。"""
    return {e["name"] for e in ACTIVITIES.values() if e["category"] == "outgoing"}


def unified_free_candidates() -> list[dict]:
    """统一候选中的自由/外向活动定义（不含 legacy 逛虚拟小屋），保持注册表顺序。"""
    return [dict(e) for e in ACTIVITIES.values()
            if e["category"] in ("free", "outgoing")]


def home_candidates(phase: int, phase_tools) -> list[dict]:
    """统一候选中的 Home 活动定义。

    同时满足：min_phase ≤ phase 且 工具组与当前 phase 工具集有交集。
    phase_tools 为当前 HOME_AUTONOMY_PHASE 对应的工具列表（空则无 Home 候选）。
    """
    tools = set(phase_tools or [])
    if not tools:
        return []
    out = []
    for e in ACTIVITIES.values():
        if e["category"] != "home":
            continue
        if phase < int(e.get("min_phase", 99)):
            continue
        if not (set(e.get("home_tool_group", ())) & tools):
            continue
        out.append(dict(e))
    return out
