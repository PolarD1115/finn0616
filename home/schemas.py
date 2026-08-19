"""
home/schemas.py — 输入校验与常量定义
====================================
定义合法的枚举值、校验函数、统一返回格式。
不引入 pydantic，使用纯函数校验。
"""

from __future__ import annotations

from typing import Any

# ============================================================
# 合法枚举值（与数据库 CHECK 约束对齐）
# ============================================================

VALID_ROOM_TYPES = frozenset({"common", "private", "outdoor", "special"})

VALID_MEMBER_TYPES = frozenset({"ai", "pet", "doll", "custom"})

VALID_LIFECYCLE_STATUS = frozenset({"alive", "sleeping", "inactive", "departed"})

VALID_OBJECT_TYPES = frozenset({
    "furniture", "container", "decoration", "interactive", "plant", "appliance"
})

VALID_EVENT_TYPES = frozenset({
    "entered_room", "rested", "ate", "cooked", "planted", "watered",
    "harvested", "fed_member", "played", "created_art", "wrote_letter",
    "left_note", "wrote_diary", "state_changed", "system_tick"
})

VALID_VISIBILITY = frozenset({"private", "home", "user_visible", "system"})

VALID_ACTION_STATUS = frozenset({"requested", "running", "succeeded", "failed", "skipped"})

VALID_JOB_STATUS = frozenset({"pending", "claimed", "running", "succeeded", "failed", "cancelled"})


# ============================================================
# 校验函数
# ============================================================

def validate_stable_key(key: Any) -> tuple[bool, str]:
    """校验 stable_key：非空字符串，不含前后空格。"""
    if key is None:
        return False, "EMPTY_KEY"
    if not isinstance(key, str):
        return False, "INVALID_KEY"
    if key.strip() == "":
        return False, "EMPTY_KEY"
    if key != key.strip():
        return False, "KEY_HAS_WHITESPACE"
    return True, ""


def validate_member_type(mtype: Any) -> tuple[bool, str]:
    """校验成员类型。"""
    if mtype not in VALID_MEMBER_TYPES:
        return False, "INVALID_MEMBER_TYPE"
    return True, ""


def validate_state_value(value: Any, field_name: str = "value") -> tuple[bool, str]:
    """校验状态值在 [0, 100] 范围内。"""
    if value is None:
        return False, f"EMPTY_{field_name.upper()}"
    if isinstance(value, bool):
        return False, f"INVALID_{field_name.upper()}"
    try:
        val = float(value)
    except (TypeError, ValueError):
        return False, f"INVALID_{field_name.upper()}"
    if val < 0 or val > 100:
        return False, f"{field_name.upper()}_OUT_OF_RANGE"
    return True, ""


def validate_event_type(etype: Any) -> tuple[bool, str]:
    """校验事件类型。"""
    if etype not in VALID_EVENT_TYPES:
        return False, "INVALID_EVENT_TYPE"
    return True, ""


def validate_visibility(vis: Any) -> tuple[bool, str]:
    """校验可见性。"""
    if vis not in VALID_VISIBILITY:
        return False, "INVALID_VISIBILITY"
    return True, ""


def validate_action_status(status: Any) -> tuple[bool, str]:
    """校验行动状态。"""
    if status not in VALID_ACTION_STATUS:
        return False, "INVALID_ACTION_STATUS"
    return True, ""


def validate_limit(limit: Any, max_val: int = 100) -> tuple[bool, str]:
    """校验 limit 范围 1..max_val。"""
    try:
        val = int(limit)
    except (TypeError, ValueError):
        return False, "INVALID_LIMIT"
    if val < 1 or val > max_val:
        return False, "INVALID_LIMIT"
    return True, ""


# ============================================================
# 统一返回格式
# ============================================================

def ok_result(message: str, data: dict | None = None) -> dict:
    """成功返回格式。"""
    result = {"ok": True, "message": message}
    if data is not None:
        result["data"] = data
    return result


def err_result(message: str, error_code: str = "") -> dict:
    """失败返回格式。"""
    result = {"ok": False, "message": message}
    if error_code:
        result["error_code"] = error_code
    return result
