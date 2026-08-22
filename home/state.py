"""
home/state.py — 纯函数状态结算引擎
==================================
职责：
- elapsed-time 计算
- clamp 到合法范围
- 状态 delta 计算
- AI 与宠物的不同结算策略
- 休息和睡眠恢复计算

纯函数：不读写数据库，不取系统时间（时间由调用方传入）。
"""

from __future__ import annotations

from typing import Any

# ============================================================
# 常量
# ============================================================

MIN_SETTLE_SECONDS = 60.0       # 小于 60 秒跳过
MAX_SETTLE_HOURS = 48.0         # 单次最大结算跨度
CONNECTION_FLOOR = 20.0         # connection 安全地板

# AI 清醒时每小时衰减率
AI_AWAKE_DECAY = {
    "hunger": 1.5,
    "energy": 1.0,
    "comfort": 0.2,
    "connection": 0.1,
    "cleanliness": 0.2,
}

# AI 休息时恢复率（每小时）
AI_REST_RECOVERY = {
    "energy": 1.0,
    "comfort": 0.3,
}

# AI 睡眠时恢复率（每小时）
AI_SLEEP_RECOVERY = {
    "energy": 2.0,
    "comfort": 0.5,
}

# AI 睡眠时饥饿仍轻微下降（每小时）
AI_SLEEP_HUNGER_DECAY = 0.5

# 陪伴互动每次增长
SPEND_TIME_GAINS = {
    "comfort": 2.0,
    "connection": 1.5,
    "intimacy_per_action": 1.0,
}

# 每日 intimacy 增长上限
DAILY_INTIMACY_CAP = 3.0

# 不自然衰减的字段
NO_DECAY_FIELDS = {"intimacy", "health", "mood"}

# 状态范围
STATE_MIN = 0.0
STATE_MAX = 100.0


# ============================================================
# 基础工具
# ============================================================

def clamp(value: float, min_val: float = STATE_MIN, max_val: float = STATE_MAX) -> float:
    """将数值限制在 [min_val, max_val] 范围内。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return min_val
    return max(min_val, min(max_val, v))


def compute_elapsed_hours(now_ts: float, last_settled_ts: float) -> float:
    """计算经过的小时数。处理负时间（时钟回拨）。

    返回：
    - 负数或零 → 返回 0.0（不结算）
    - 正常值 → 返回实际小时数
    """
    if now_ts is None or last_settled_ts is None:
        return 0.0
    diff = now_ts - last_settled_ts
    if diff < 0:
        return 0.0  # 时钟回拨，不结算
    return diff / 3600.0


def should_settle(elapsed_hours: float) -> tuple[bool, str]:
    """判断是否应该结算。返回 (should, reason)。"""
    if elapsed_hours <= 0:
        return False, "no_elapsed"
    if elapsed_hours * 3600.0 < MIN_SETTLE_SECONDS:
        return False, "interval_too_short"
    return True, ""


def cap_elapsed(elapsed_hours: float) -> float:
    """封顶结算跨度。"""
    return min(elapsed_hours, MAX_SETTLE_HOURS)


# ============================================================
# AI 状态结算（清醒衰减）
# ============================================================

def settle_ai_awake(state: dict, elapsed_hours: float) -> dict:
    """AI 清醒时的自然衰减。返回 changes 列表。

    state: 包含 hunger/energy/comfort/connection/cleanliness 的 dict
    返回: {"changes": [...], "new_state": {...}}
    """
    elapsed_hours = cap_elapsed(elapsed_hours)
    changes = []
    new_state = dict(state)  # 浅拷贝

    for field, rate in AI_AWAKE_DECAY.items():
        old_val = float(state.get(field, 50))
        new_val = old_val - rate * elapsed_hours
        if field == "connection":
            new_val = max(CONNECTION_FLOOR, new_val)
        else:
            new_val = clamp(new_val)
        new_state[field] = new_val
        delta = round(new_val - old_val, 2)
        if abs(delta) > 0.001:
            changes.append({
                "field": field,
                "before": old_val,
                "after": round(new_val, 2),
                "delta": delta,
            })

    return {"changes": changes, "new_state": new_state}


# ============================================================
# 休息/睡眠恢复
# ============================================================

def compute_rest_recovery(
    state: dict, duration_minutes: int, mode: str = "rest"
) -> dict:
    """计算休息或睡眠后的状态恢复。

    返回: {"changes": [...], "new_state": {...}}
    """
    duration_minutes = max(0, min(1440, int(duration_minutes)))
    hours = duration_minutes / 60.0

    recovery = AI_SLEEP_RECOVERY if mode == "sleep" else AI_REST_RECOVERY
    new_state = dict(state)
    changes = []

    # energy 恢复
    old_energy = float(state.get("energy", 50))
    new_energy = clamp(old_energy + recovery["energy"] * hours)
    new_state["energy"] = new_energy
    changes.append({
        "field": "energy", "before": old_energy, "after": round(new_energy, 2),
        "delta": round(new_energy - old_energy, 2),
    })

    # comfort 恢复
    old_comfort = float(state.get("comfort", 50))
    new_comfort = clamp(old_comfort + recovery["comfort"] * hours)
    new_state["comfort"] = new_comfort
    changes.append({
        "field": "comfort", "before": old_comfort, "after": round(new_comfort, 2),
        "delta": round(new_comfort - old_comfort, 2),
    })

    # 睡眠时 hunger 轻微下降
    if mode == "sleep":
        old_hunger = float(state.get("hunger", 50))
        new_hunger = clamp(old_hunger - AI_SLEEP_HUNGER_DECAY * hours)
        new_state["hunger"] = new_hunger
        changes.append({
            "field": "hunger", "before": old_hunger, "after": round(new_hunger, 2),
            "delta": round(new_hunger - old_hunger, 2),
        })

    # 不恢复 intimacy 和 health
    return {"changes": changes, "new_state": new_state}


# ============================================================
# 陪伴互动
# ============================================================

def compute_spend_time_gains(
    actor_state: dict, target_state: dict, today_intimacy_gain: float = 0.0
) -> dict:
    """计算陪伴互动后的双方状态变化。

    today_intimacy_gain: 今日已增长的 intimacy 总量
    返回: {"actor_changes": [...], "target_changes": [...],
           "new_actor_state": {...}, "new_target_state": {...}}
    """
    actor_changes = []
    target_changes = []
    new_actor = dict(actor_state)
    new_target = dict(target_state)

    # comfort
    for member_label, state, new_state, changes in [
        ("actor", actor_state, new_actor, actor_changes),
        ("target", target_state, new_target, target_changes),
    ]:
        old_comfort = float(state.get("comfort", 50))
        new_comfort = clamp(old_comfort + SPEND_TIME_GAINS["comfort"])
        new_state["comfort"] = new_comfort
        changes.append({"member": member_label, "field": "comfort", "before": old_comfort, "after": round(new_comfort, 2)})

        old_conn = float(state.get("connection", 30))
        new_conn = clamp(old_conn + SPEND_TIME_GAINS["connection"])
        new_state["connection"] = new_conn
        changes.append({"member": member_label, "field": "connection", "before": old_conn, "after": round(new_conn, 2)})

    # intimacy 受每日上限控制
    remaining_cap = max(0, DAILY_INTIMACY_CAP - today_intimacy_gain)
    intimacy_gain = min(SPEND_TIME_GAINS["intimacy_per_action"], remaining_cap)

    for member_label, state, new_state, changes in [
        ("actor", actor_state, new_actor, actor_changes),
        ("target", target_state, new_target, target_changes),
    ]:
        old_intimacy = float(state.get("intimacy", 30))
        new_intimacy = clamp(old_intimacy + intimacy_gain)
        new_state["intimacy"] = new_intimacy
        changes.append({"member": member_label, "field": "intimacy", "before": old_intimacy, "after": round(new_intimacy, 2)})

    return {
        "actor_changes": actor_changes,
        "target_changes": target_changes,
        "new_actor_state": new_actor,
        "new_target_state": new_target,
        "intimacy_delta": intimacy_gain,
    }


# ============================================================
# 宠物策略
# ============================================================

def should_settle_pet(member_type: str) -> bool:
    """宠物不由新 Runtime 结算（避免与 cat_tick 双重衰减）。"""
    return member_type != "pet"


# ============================================================
# 默认初始值
# ============================================================

def default_ai_initial_state() -> dict:
    """AI 本体保守中性初始值。"""
    return {
        "hunger": 70,
        "energy": 70,
        "mood": 65,
        "comfort": 60,
        "connection": 60,
        "intimacy": 50,
        "health": 100,
        "cleanliness": 80,
    }


def default_pet_initial_state() -> dict:
    """宠物默认初始值（当旧表无数据时）。"""
    return {
        "hunger": 50,
        "energy": 80,
        "mood": 60,
        "comfort": 60,
        "connection": 30,
        "intimacy": 30,
        "health": 100,
        "cleanliness": 70,
    }
