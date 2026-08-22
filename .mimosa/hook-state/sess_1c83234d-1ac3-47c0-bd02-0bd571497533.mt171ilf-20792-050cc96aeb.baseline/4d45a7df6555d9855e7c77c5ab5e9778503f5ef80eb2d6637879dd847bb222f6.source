"""
home/models.py — Home Runtime 数据模型
=====================================
定义房间、家庭成员、成员状态、物品、事件、行动、任务的 dataclass。
优先使用 dataclass，不引入 pydantic 等重量级框架。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class HomeRoom:
    """房间定义。stable_key 是业务主键（如 'living_room'），id 是数据库主键。"""
    stable_key: str
    name: str
    id: str = ""
    description: str = ""
    emoji: str = "🏠"
    room_type: str = "common"
    sort_order: int = 0
    is_enabled: bool = True
    is_hidden: bool = False
    unlock_condition: dict = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class HomeMember:
    """家庭成员（AI / 宠物 / 玩偶 / 自定义）。"""
    stable_key: str
    name: str
    member_type: str = "custom"
    id: str = ""
    is_active: bool = True
    lifecycle_status: str = "alive"
    profile: dict = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class HomeMemberState:
    """成员状态（1:1 对应 HomeMember）。核心状态结构化，扩展状态放 extra。"""
    member_id: str
    hunger: float = 50.0
    energy: float = 80.0
    mood: float = 60.0
    comfort: float = 60.0
    connection: float = 30.0
    intimacy: float = 30.0
    health: float = 90.0
    cleanliness: float = 70.0
    current_room_id: Optional[str] = None
    extra: dict = field(default_factory=dict)
    last_settled_at: Optional[str] = None

    def as_display(self) -> dict:
        """返回用于观察接口的状态摘要。"""
        return {
            "hunger": round(float(self.hunger), 1),
            "energy": round(float(self.energy), 1),
            "mood": round(float(self.mood), 1),
            "comfort": round(float(self.comfort), 1),
            "connection": round(float(self.connection), 1),
            "intimacy": round(float(self.intimacy), 1),
            "health": round(float(self.health), 1),
            "cleanliness": round(float(self.cleanliness), 1),
            "current_room_id": self.current_room_id,
            "last_settled_at": self.last_settled_at,
        }


@dataclass
class HomeObject:
    """房间物品。object_type 区分家具/容器/装饰/交互物/植物/电器。"""
    room_id: str
    name: str
    object_type: str = "furniture"
    id: str = ""
    stable_key: Optional[str] = None
    description: str = ""
    visual: dict = field(default_factory=dict)
    is_hidden: bool = False
    state: dict = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class HomeEvent:
    """统一生活事件（追加型记录，不保存当前状态）。"""
    event_type: str
    summary: str
    id: str = ""
    event_key: Optional[str] = None
    actor_member_id: Optional[str] = None
    target_member_id: Optional[str] = None
    room_id: Optional[str] = None
    source: str = "system"
    visibility: str = "home"
    details: dict = field(default_factory=dict)
    occurred_at: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class HomeActionRun:
    """行动执行记录。action_key 用于幂等。"""
    action_key: str
    action_type: str
    id: str = ""
    actor_member_id: Optional[str] = None
    status: str = "requested"
    requested_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    input: dict = field(default_factory=dict)
    result: Optional[dict] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class HomeJob:
    """Home Runtime 后台任务。dedupe_key 用于幂等。"""
    job_type: str
    id: str = ""
    dedupe_key: Optional[str] = None
    status: str = "pending"
    priority: int = 50
    not_before: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 3
    payload: dict = field(default_factory=dict)
    last_error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
