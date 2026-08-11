"""
home_system.py — 小屋/小满/小钱包 业务逻辑层
===========================================
职责划分：
- 纯函数：金额校验、周界/生日周计算、结果格式化
- DB IO 封装：调用 Supabase PostgreSQL RPC，异步走 asyncio.to_thread
- 无 Supabase 时返回结构化错误

约束：
- 所有外部输入必须经过参数绑定（不拼接 SQL）
- 时间统一按 UTC 存储、Asia/Shanghai 计算业务周
- 钱包写操作全部走数据库原子 RPC，Python 层不做 select-then-update
"""

from __future__ import annotations

import os
import datetime
from typing import Any

# ============================================================
# 常量与配置（环境变量驱动，带默认值）
# ============================================================
WALLET_WEEK_CAP = float(os.environ.get("WALLET_WEEK_CAP", "80"))
WALLET_OVERTIME_RATE = float(os.environ.get("WALLET_OVERTIME_RATE", "0.5"))
WALLET_BIRTHDAY_WEEK = os.environ.get("WALLET_BIRTHDAY_WEEK", "true").strip().lower() in ("1", "true", "yes")
WALLET_OVERTIME_WITHDRAW_MAX = float(os.environ.get("WALLET_OVERTIME_WITHDRAW_MAX", "20"))

# 默认钱包 ID（阶段 1 seed 的 singleton）
DEFAULT_WALLET_ID = "finn_wallet"


# ============================================================
# 1. 纯函数：金额校验
# ============================================================
def _validate_amount(amount: Any) -> tuple[bool, str]:
    """校验金额合法性。返回 (ok, error_code)。"""
    if amount is None:
        return False, "INVALID_AMOUNT"
    # 拒绝 bool（bool 是 int 子类）
    if isinstance(amount, bool):
        return False, "INVALID_AMOUNT"
    try:
        val = float(amount)
    except (TypeError, ValueError):
        return False, "INVALID_AMOUNT"
    if val <= 0:
        return False, "INVALID_AMOUNT"
    if val > 1e9:
        return False, "OVERSIZED_AMOUNT"
    return True, ""


def _validate_reason(reason: Any) -> tuple[bool, str]:
    """校验原因非空。"""
    if reason is None:
        return False, "EMPTY_REASON"
    if not isinstance(reason, str):
        return False, "EMPTY_REASON"
    if reason.strip() == "":
        return False, "EMPTY_REASON"
    return True, ""


def _validate_limit(limit: Any) -> tuple[bool, str]:
    """校验 limit 范围 1..100。"""
    try:
        val = int(limit)
    except (TypeError, ValueError):
        return False, "INVALID_LIMIT"
    if val < 1 or val > 100:
        return False, "INVALID_LIMIT"
    return True, ""


def _validate_target(target: Any) -> tuple[bool, str]:
    """校验兑换目标。"""
    if target is None:
        return False, "INVALID_TARGET"
    if not isinstance(target, str):
        return False, "INVALID_TARGET"
    if target.strip().lower() not in {"tea", "gift"}:
        return False, "INVALID_TARGET"
    return True, ""


# ============================================================
# 2. 纯函数：周界/生日周计算
# ============================================================
def _bj_week_start(dt_utc: datetime.datetime) -> datetime.datetime:
    """给定 UTC 时间，返回北京时间周一 00:00 的 UTC 时间戳。
    使用固定 UTC+8 偏移，避免对 tzdata 的依赖。"""
    # 先转成北京时间 (UTC+8)
    bj = dt_utc + datetime.timedelta(hours=8)
    # 本周一 00:00（北京时间）
    monday_bj = bj - datetime.timedelta(days=bj.weekday())
    monday_bj = monday_bj.replace(hour=0, minute=0, second=0, microsecond=0)
    # 再转回 UTC，返回 naive datetime（与旧行为一致）
    return (monday_bj - datetime.timedelta(hours=8)).replace(tzinfo=None)


def _is_birthday_week(dt_utc: datetime.datetime) -> bool:
    """给定 UTC 时间，判断其北京时间所在周是否包含 4月5日或11月15日。
    使用固定 UTC+8 偏移，避免对 tzdata 的依赖。"""
    bj = dt_utc + datetime.timedelta(hours=8)
    year = bj.year
    # 本周一（含）到下周日（含）
    monday = bj - datetime.timedelta(days=bj.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = monday.date()
    week_end = week_start + datetime.timedelta(days=6)

    apr5 = datetime.date(year, 4, 5)
    nov15 = datetime.date(year, 11, 15)

    return (week_start <= apr5 <= week_end) or (week_start <= nov15 <= week_end)


def _format_result(ok: bool, message: str, data: dict | None = None, error_code: str = "") -> dict:
    """统一返回格式 {ok, message, data/error_code}。"""
    result = {"ok": ok, "message": message}
    if ok:
        result["data"] = data or {}
    else:
        result["error_code"] = error_code
    return result


# ============================================================
# 3. DB IO 封装（调用 Supabase RPC）
# ============================================================
def _get_supabase():
    """延迟获取 server.py 中初始化的 supabase 客户端。"""
    try:
        import server
        return server.supabase
    except Exception:
        return None


def _rpc(name: str, params: dict) -> dict:
    """调用 PostgreSQL RPC 函数。"""
    sb = _get_supabase()
    if sb is None:
        return _format_result(False, "数据库未连接", error_code="DB_UNAVAILABLE")
    try:
        # Supabase Python 客户端通过 .rpc(name, params) 调用函数
        resp = sb.rpc(name, params).execute()
        # rpc 返回的数据在 resp.data 中
        if resp.data is not None:
            return resp.data
        return _format_result(False, "RPC 返回空数据", error_code="RPC_EMPTY")
    except Exception as e:
        return _format_result(False, f"RPC 调用失败: {e}", error_code="RPC_ERROR")


# ============================================================
# 4. 钱包业务接口
# ============================================================
def wallet_check(wallet_id: str = DEFAULT_WALLET_ID) -> dict:
    """查询钱包当前状态（含周统计）。"""
    if not wallet_id or not isinstance(wallet_id, str):
        return _format_result(False, "钱包ID无效", error_code="INVALID_WALLET")

    return _rpc("rpc_wallet_check", {
        "p_wallet_id": wallet_id,
        "p_week_cap": WALLET_WEEK_CAP,
        "p_birthday_enabled": WALLET_BIRTHDAY_WEEK,
    })


def wallet_earn(wallet_id: str, amount: float, source_key: str, reason: str, meta: dict | None = None) -> dict:
    """入账（原子 RPC）。"""
    ok, err = _validate_amount(amount)
    if not ok:
        return _format_result(False, "金额非法", error_code=err)
    ok, err = _validate_reason(reason)
    if not ok:
        return _format_result(False, "原因不能为空", error_code=err)

    return _rpc("rpc_wallet_earn", {
        "p_wallet_id": wallet_id,
        "p_amount": float(amount),
        "p_source_key": source_key or "",
        "p_reason": reason,
        "p_meta": meta or {},
        "p_week_cap": WALLET_WEEK_CAP,
        "p_overtime_rate": WALLET_OVERTIME_RATE,
        "p_birthday_enabled": WALLET_BIRTHDAY_WEEK,
    })


def wallet_spend(wallet_id: str, amount: float, reason: str, meta: dict | None = None) -> dict:
    """支出（原子 RPC）。"""
    ok, err = _validate_amount(amount)
    if not ok:
        return _format_result(False, "金额非法", error_code=err)
    ok, err = _validate_reason(reason)
    if not ok:
        return _format_result(False, "原因不能为空", error_code=err)

    return _rpc("rpc_wallet_spend", {
        "p_wallet_id": wallet_id,
        "p_amount": float(amount),
        "p_reason": reason,
        "p_meta": meta or {},
    })


def wallet_exchange(wallet_id: str, target: str, reason: str, meta: dict | None = None) -> dict:
    """兑换（tea/gift，原子 RPC）。"""
    ok, err = _validate_target(target)
    if not ok:
        return _format_result(False, "非法兑换目标", error_code=err)
    ok, err = _validate_reason(reason)
    if not ok:
        return _format_result(False, "原因不能为空", error_code=err)

    return _rpc("rpc_wallet_exchange", {
        "p_wallet_id": wallet_id,
        "p_target": target.strip().lower(),
        "p_reason": reason,
        "p_meta": meta or {},
        "p_week_cap": WALLET_WEEK_CAP,
        "p_overtime_rate": WALLET_OVERTIME_RATE,
        "p_birthday_enabled": WALLET_BIRTHDAY_WEEK,
    })


def wallet_overtime_withdraw(wallet_id: str, amount: float, reason: str, meta: dict | None = None) -> dict:
    """从加班银行取出（原子 RPC）。"""
    ok, err = _validate_amount(amount)
    if not ok:
        return _format_result(False, "金额非法", error_code=err)
    ok, err = _validate_reason(reason)
    if not ok:
        return _format_result(False, "原因不能为空", error_code=err)

    return _rpc("rpc_wallet_overtime_withdraw", {
        "p_wallet_id": wallet_id,
        "p_amount": float(amount),
        "p_reason": reason,
        "p_meta": meta or {},
        "p_single_max": WALLET_OVERTIME_WITHDRAW_MAX,
    })


def wallet_log(wallet_id: str, limit: int = 20, offset: int = 0) -> dict:
    """查询钱包流水（分页）。"""
    ok, err = _validate_limit(limit)
    if not ok:
        return _format_result(False, "limit 非法", error_code=err)

    if not wallet_id or not isinstance(wallet_id, str):
        return _format_result(False, "钱包ID无效", error_code="INVALID_WALLET")

    return _rpc("rpc_wallet_log", {
        "p_wallet_id": wallet_id,
        "p_limit": int(limit),
        "p_offset": int(offset),
    })


# ============================================================
# 5. 小屋业务接口（有状态 Memory House）
# ============================================================
VALID_ROOMS = {"living_room", "bedroom", "kitchen", "study", "balcony"}


def _validate_room(room_id: Any) -> tuple[bool, str]:
    """校验房间 ID 合法性。"""
    if room_id is None:
        return False, "EMPTY_ROOM"
    if not isinstance(room_id, str):
        return False, "INVALID_ROOM"
    if room_id.strip() == "":
        return False, "EMPTY_ROOM"
    return True, ""


def _validate_entry_type(entry_type: Any) -> tuple[bool, str]:
    """校验日记条目类型。"""
    if entry_type is None:
        return False, "EMPTY_ENTRY_TYPE"
    if not isinstance(entry_type, str):
        return False, "INVALID_ENTRY_TYPE"
    if entry_type.strip() == "":
        return False, "EMPTY_ENTRY_TYPE"
    return True, ""


def _validate_object_name(name: Any) -> tuple[bool, str]:
    """校验物品名称。"""
    if name is None:
        return False, "EMPTY_OBJECT_NAME"
    if not isinstance(name, str):
        return False, "INVALID_OBJECT_NAME"
    if name.strip() == "":
        return False, "EMPTY_OBJECT_NAME"
    if len(name) > 100:
        return False, "OBJECT_NAME_TOO_LONG"
    return True, ""


def house_look(room_id: str) -> dict:
    """查看房间详情（含物品 + 近期日记）。"""
    ok, err = _validate_room(room_id)
    if not ok:
        return _format_result(False, "房间ID无效", error_code=err)
    return _rpc("rpc_house_look", {"p_room_id": room_id.strip()})


def house_do(room_id: str, entry_type: str, content: str, mood: str | None = None,
             weather: str | None = None, tags: list | None = None) -> dict:
    """在房间做某事（写入日记）。"""
    ok, err = _validate_room(room_id)
    if not ok:
        return _format_result(False, "房间ID无效", error_code=err)
    ok, err = _validate_entry_type(entry_type)
    if not ok:
        return _format_result(False, "条目类型无效", error_code=err)
    if content is None or not isinstance(content, str) or content.strip() == "":
        return _format_result(False, "内容不能为空", error_code="EMPTY_CONTENT")

    return _rpc("rpc_house_do", {
        "p_room_id": room_id.strip(),
        "p_entry_type": entry_type.strip(),
        "p_content": content.strip(),
        "p_mood": mood,
        "p_weather": weather,
        "p_tags": tags or [],
    })


def house_put(room_id: str, name: str, emoji: str = "📦", description: str | None = None) -> dict:
    """放置物品到房间。"""
    ok, err = _validate_room(room_id)
    if not ok:
        return _format_result(False, "房间ID无效", error_code=err)
    ok, err = _validate_object_name(name)
    if not ok:
        return _format_result(False, "物品名称无效", error_code=err)

    return _rpc("rpc_house_put", {
        "p_room_id": room_id.strip(),
        "p_name": name.strip(),
        "p_emoji": emoji or "📦",
        "p_description": description,
    })


def house_take(object_id: str) -> dict:
    """从房间拿走物品。"""
    if object_id is None or not isinstance(object_id, str) or object_id.strip() == "":
        return _format_result(False, "物品ID无效", error_code="INVALID_OBJECT_ID")

    return _rpc("rpc_house_take", {"p_object_id": object_id.strip()})


def house_update_desc(room_id: str, description: str) -> dict:
    """更新房间描述。"""
    ok, err = _validate_room(room_id)
    if not ok:
        return _format_result(False, "房间ID无效", error_code=err)
    if description is None or not isinstance(description, str):
        return _format_result(False, "描述不能为空", error_code="EMPTY_DESCRIPTION")

    return _rpc("rpc_house_update_desc", {
        "p_room_id": room_id.strip(),
        "p_description": description.strip(),
    })


# ============================================================
# 6. 小满猫系统业务接口
# ============================================================

# 猫商店白名单（10个物品）
CAT_SHOP_WHITELIST = {
    # food（消耗品）
    "fish", "cat_milk", "tuna_can", "wet_food", "apple",
    # toy（耐用品）
    "ball", "catnip", "feather",
    # clean（消耗品）
    "brush", "soap",
}

# 物品类型映射（用于前端校验）
CAT_ITEM_TYPES = {
    "fish": "food", "cat_milk": "food", "tuna_can": "food",
    "wet_food": "food", "apple": "food",
    "ball": "toy", "catnip": "toy", "feather": "toy",
    "brush": "clean", "soap": "clean",
}


def _validate_cat_item_id(item_id: Any) -> tuple[bool, str]:
    """校验物品ID在白名单中。"""
    if item_id is None:
        return False, "EMPTY_ITEM_ID"
    if not isinstance(item_id, str):
        return False, "INVALID_ITEM_ID"
    item = item_id.strip()
    if item == "":
        return False, "EMPTY_ITEM_ID"
    if item not in CAT_SHOP_WHITELIST:
        return False, "ITEM_NOT_IN_WHITELIST"
    return True, ""


def _validate_cat_qty(qty: Any) -> tuple[bool, str]:
    """校验购买数量 1-99。"""
    if qty is None:
        return False, "INVALID_QTY"
    try:
        val = int(qty)
    except (TypeError, ValueError):
        return False, "INVALID_QTY"
    if val < 1 or val > 99:
        return False, "INVALID_QTY"
    return True, ""


def _clamp(value: Any, min_val: float = 0, max_val: float = 100) -> float:
    """将数值限制在 [min, max] 范围内。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return min_val
    return max(min_val, min(max_val, v))


# 猫 DB IO 封装

def cat_status(user_id: str = "user_finn") -> dict:
    """查询宠物状态（含属性、冷却、库存摘要）。"""
    if not user_id or not isinstance(user_id, str):
        return _format_result(False, "用户ID无效", error_code="INVALID_USER")
    return _rpc("rpc_cat_status", {"p_user_id": user_id})


def cat_feed(user_id: str, item_id: str) -> dict:
    """喂食（food类型，扣消耗品库存）。"""
    ok, err = _validate_cat_item_id(item_id)
    if not ok:
        return _format_result(False, "物品ID无效", error_code=err)
    if not user_id or not isinstance(user_id, str):
        return _format_result(False, "用户ID无效", error_code="INVALID_USER")
    return _rpc("rpc_cat_feed", {
        "p_user_id": user_id,
        "p_item_id": item_id.strip(),
    })


def cat_play(user_id: str, item_id: str | None = None) -> dict:
    """玩耍（toy耐用品，不扣数量）。"""
    if item_id is not None:
        ok, err = _validate_cat_item_id(item_id)
        if not ok:
            return _format_result(False, "物品ID无效", error_code=err)
    if not user_id or not isinstance(user_id, str):
        return _format_result(False, "用户ID无效", error_code="INVALID_USER")
    return _rpc("rpc_cat_play", {
        "p_user_id": user_id,
        "p_item_id": item_id.strip() if item_id else None,
    })


def cat_clean(user_id: str, item_id: str | None = None) -> dict:
    """清洁（clean消耗品，扣库存）。"""
    if item_id is not None:
        ok, err = _validate_cat_item_id(item_id)
        if not ok:
            return _format_result(False, "物品ID无效", error_code=err)
    if not user_id or not isinstance(user_id, str):
        return _format_result(False, "用户ID无效", error_code="INVALID_USER")
    return _rpc("rpc_cat_clean", {
        "p_user_id": user_id,
        "p_item_id": item_id.strip() if item_id else None,
    })


def cat_pet(user_id: str = "user_finn") -> dict:
    """抚摸（快乐+5，10分钟冷却）。"""
    if not user_id or not isinstance(user_id, str):
        return _format_result(False, "用户ID无效", error_code="INVALID_USER")
    return _rpc("rpc_cat_pet", {"p_user_id": user_id})


def cat_restore_energy(user_id: str = "user_finn") -> dict:
    """恢复精力（明确、受限的恢复路径）。"""
    if not user_id or not isinstance(user_id, str):
        return _format_result(False, "用户ID无效", error_code="INVALID_USER")
    return _rpc("rpc_cat_restore_energy", {"p_user_id": user_id})


def cat_shop_list() -> dict:
    """商店列表（10个白名单物品）。"""
    return _rpc("rpc_cat_shop_list", {})


def cat_shop_buy(user_id: str, item_id: str, qty: int = 1) -> dict:
    """商店购买（钱包扣款 + wallet_log + inventory upsert，原子事务）。"""
    ok, err = _validate_cat_item_id(item_id)
    if not ok:
        return _format_result(False, "物品ID无效", error_code=err)
    ok, err = _validate_cat_qty(qty)
    if not ok:
        return _format_result(False, "数量非法", error_code=err)
    if not user_id or not isinstance(user_id, str):
        return _format_result(False, "用户ID无效", error_code="INVALID_USER")
    return _rpc("rpc_cat_shop_buy", {
        "p_user_id": user_id,
        "p_item_id": item_id.strip(),
        "p_qty": int(qty),
    })


# ============================================================
# 7. 小满后台 tick 接口（状态衰减 + 事件 + 自动收入）
# ============================================================

# 衰减率常量（每小时）
TICK_DECAY_RATES = {
    "hunger": -2.0,      # 饥饿度每小时 -2
    "happiness": -1.5,   # 快乐度每小时 -1.5
    "cleanliness": -1.0, # 清洁度每小时 -1
    "energy_sleep": 2.0, # 睡觉时精力每小时 +2
}

# 睡眠滞回阈值
SLEEP_THRESHOLD = 20   # 精力 < 20 时入睡
WAKE_THRESHOLD = 40    # 精力 >= 40 时醒来

# 自动工资常量
WAGE_DIARY_RATE = 2.0   # 日记每篇 2 CNY
WAGE_CHAT_RATE = 1.0    # 陪聊每小时 1 CNY


def cat_tick(user_id: str = "user_finn") -> dict:
    """触发宠物状态 tick（elapsed-time 衰减 + 睡眠滞回 + 阈值事件）。"""
    if not user_id or not isinstance(user_id, str):
        return _format_result(False, "用户ID无效", error_code="INVALID_USER")
    return _rpc("rpc_cat_tick", {"p_user_id": user_id})


def cat_room_mischief(user_id: str = "user_finn") -> dict:
    """受控换房 + 物品轻微破坏（素材生成）。"""
    if not user_id or not isinstance(user_id, str):
        return _format_result(False, "用户ID无效", error_code="INVALID_USER")
    return _rpc("rpc_cat_room_mischief", {"p_user_id": user_id})


def cat_auto_wage(wallet_id: str = DEFAULT_WALLET_ID, diary_count: int = 0, chat_hours: int = 0) -> dict:
    """自动结算工资（日记 + 陪聊）。"""
    if not wallet_id or not isinstance(wallet_id, str):
        return _format_result(False, "钱包ID无效", error_code="INVALID_WALLET")
    return _rpc("rpc_cat_auto_wage", {
        "p_wallet_id": wallet_id,
        "p_diary_count": int(diary_count),
        "p_chat_hours": int(chat_hours),
    })


def agent_outbound_poll(agent_id: str = "pet_house", limit: int = 10) -> dict:
    """查询待处理事件（consumer 用）。"""
    if not agent_id or not isinstance(agent_id, str):
        return _format_result(False, "agent_id 无效", error_code="INVALID_AGENT")
    return _rpc("rpc_agent_outbound_poll", {
        "p_agent_id": agent_id,
        "p_limit": int(limit),
    })


def agent_outbound_ack(event_id: str) -> dict:
    """标记事件为已处理。"""
    if not event_id or not isinstance(event_id, str):
        return _format_result(False, "event_id 无效", error_code="INVALID_EVENT_ID")
    return _rpc("rpc_agent_outbound_ack", {"p_event_id": event_id})
