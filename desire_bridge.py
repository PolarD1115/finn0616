"""
欲望驱动桥接层 (Desire Bridge)
================================
把两个纯函数内核串到网关心跳里。职责边界：

- emotion_engine / desire_engine：纯函数，不碰 IO、不取系统时间。
- 本模块：负责 IO（读写状态到 user_facts 表）、取系统时间、组装管线、读 gating 开关。

管线一拍：
    load_state(情感 + 待处理事件)
      → emotion_engine.tick_evolve()  拿到 16 维 display
      → desire_engine.map_from_emotions()
      → 对「本拍新到的情感冲击」做 pulse（边际递减）
      → desire_engine.pick_intent()
      → 存回状态 → 返回 DesireSnapshot

gating（灰度原则，默认关）：
    DESIRE_DRIVEN=false（默认）：只算 + 只存快照，返回 intent 供观测，
                                但调用方**不应**用它覆盖行为。
    DESIRE_DRIVEN=true         ：调用方按 intent.want_action 覆盖「想做的事」。

状态存储（user_facts key）：
    desire_emotion_state   16 维情感引擎完整状态 JSON
    desire_drive_state     8 维驱动条快照 JSON（含上一拍值，用于算 pulse 增量）
    desire_events_queue    待情感引擎消费的事件队列 JSON
"""

from __future__ import annotations

import os
import json
import time
import copy
from typing import Any, Dict, List, Optional

import emotion_engine as ee
import desire_engine as de


# =============================================================================
# gating / 常数
# =============================================================================

EMOTION_STATE_KEY = "desire_emotion_state"
DRIVE_STATE_KEY = "desire_drive_state"
EVENTS_QUEUE_KEY = "desire_events_queue"

# pulse 增量：主人来的情感冲击 vs 自经历（对齐攻略：自经历更小）
PULSE_FROM_USER = 0.18
PULSE_FROM_SELF = 0.10


def is_desire_driven() -> bool:
    """总闸：欲望是否真覆盖行为。默认关（只读可看不动手）。"""
    return os.environ.get("DESIRE_DRIVEN", "false").strip().lower() in ("1", "true", "yes", "on")


def _tz_offset() -> float:
    try:
        return float(os.environ.get("EMOTION_TZ_OFFSET", ee.TZ_OFFSET_DEFAULT))
    except Exception:
        return ee.TZ_OFFSET_DEFAULT


# =============================================================================
# 状态持久化（IO 隔离在这一层）
# =============================================================================

def _get_supabase():
    """延迟导入 server.supabase，避免循环依赖 / 无库环境报错。"""
    try:
        from server import supabase
        return supabase
    except Exception:
        return None


def _load_fact(key: str) -> Optional[Any]:
    sb = _get_supabase()
    if not sb:
        return None
    try:
        r = sb.table("user_facts").select("value").eq("key", key).execute()
        if r and r.data:
            return json.loads(r.data[0]["value"])
    except Exception as e:
        print(f"[Desire] load {key} failed: {e}")
    return None


def _save_fact(key: str, value: Any) -> None:
    sb = _get_supabase()
    if not sb:
        return
    try:
        sb.table("user_facts").upsert(
            {"key": key, "value": json.dumps(value, ensure_ascii=False), "confidence": 1.0},
            on_conflict="key",
        ).execute()
    except Exception as e:
        print(f"[Desire] save {key} failed: {e}")


def enqueue_event(etype: str, payload: Optional[Dict[str, Any]] = None,
                  classified: Optional[Dict[str, Any]] = None) -> None:
    """
    往情感引擎的事件队列塞一个事件（心跳 tick 时统一消费）。
    classified：msg_user 事件的分类结果 {"label","confidence"}，可选（引擎外分类）。
    """
    q = _load_fact(EVENTS_QUEUE_KEY) or []
    ev = {
        "event_id": "evt_" + str(int(time.time() * 1000)) + "_" + os.urandom(3).hex(),
        "timestamp": ee._ms_to_iso(time.time() * 1000.0),
        "type": etype,
        "payload": payload or {},
    }
    if classified:
        ev["_classified"] = classified
    q.append(ev)
    _save_fact(EVENTS_QUEUE_KEY, q)


# =============================================================================
# 快照数据
# =============================================================================

class DesireSnapshot:
    """一拍的结果，供心跳 / 前端使用。"""

    def __init__(self, driven: bool, drive: Dict[str, float],
                 scores: Dict[str, float], intent: de.Intent,
                 display: Dict[str, float]):
        self.driven = driven          # DESIRE_DRIVEN 是否开
        self.drive = drive            # 8 维驱动条当前值
        self.scores = scores          # 各维召唤力（fatigue 不计）
        self.intent = intent          # pick_intent 结果
        self.display = display        # 16 维情感 display（来源）

    def as_dict(self) -> Dict[str, Any]:
        return {
            "driven": self.driven,
            "drive": self.drive,
            "scores": self.scores,
            "intent": {
                "want_action": self.intent.want_action,
                "drive_key": self.intent.drive_key,
                "reason": self.intent.reason,
                "score": self.intent.score,
                "query_hint": self.intent.query_hint,
            },
            "display": self.display,
        }


# =============================================================================
# 核心：一拍管线
# =============================================================================

def tick(now_ms: Optional[float] = None,
         has_pending_task: bool = False,
         consume_events: bool = True) -> DesireSnapshot:
    """
    推进欲望驱动一拍。IO + 组装都在这里，两个引擎保持纯净。

    now_ms           : 当前时间（epoch 毫秒）。None 则取系统时间（IO 隔离在此层）。
    has_pending_task : 是否有未完成任务（duty 维激活条件），由调用方查库后传入。
    consume_events   : True 则消费并清空事件队列；False 用于纯只读观测（不动状态）。

    返回 DesireSnapshot。无论 DESIRE_DRIVEN 开关如何，都会算并存快照（可观察）；
    是否用 intent 覆盖行为由调用方按 is_desire_driven() 决定。
    """
    if now_ms is None:
        now_ms = time.time() * 1000.0
    tz = _tz_offset()

    # 1) 加载情感状态 + 事件队列
    emo_state = _load_fact(EMOTION_STATE_KEY)
    if not emo_state:
        emo_state = ee.create_initial_state(now_ms, tz)

    events: List[Dict[str, Any]] = []
    if consume_events:
        events = _load_fact(EVENTS_QUEUE_KEY) or []

    # 2) 情感引擎推进（纯函数）→ 拿 16 维 display
    new_emo = ee.tick_evolve(emo_state, events, now_ms, tz_offset=tz)
    display = new_emo["display"]

    # 3) 16 维 → 8 维映射
    mapped = de.map_from_emotions(display, has_pending_task=has_pending_task)

    # 4) pulse：把「本拍情感相对上一拍的正向冲击」喂进驱动条（边际递减）
    #    上一拍驱动值从 DRIVE_STATE_KEY 读；首次则以 mapped 作基线（无 pulse）。
    prev = _load_fact(DRIVE_STATE_KEY)
    if prev and isinstance(prev.get("drive"), dict):
        drive_state = de.DriveState.from_dict(prev["drive"])
        # 对每一维：若映射值高于当前驱动值，视为一次外来冲击 → pulse 上去
        for k in de.RANKED_KEYS:
            gap = mapped.get(k) - drive_state.get(k)
            if gap > 0:
                # 冲击强度按缺口缩放；上限 PULSE_FROM_USER（这拍主要来自主人对话）
                amount = min(PULSE_FROM_USER, gap)
                drive_state = de.pulse(drive_state, k, amount)
        # fatigue 是闸，不 pulse，直接跟随映射值
        drive_state = de.DriveState.from_dict(
            {**drive_state.as_dict(), "fatigue": mapped.fatigue}
        )
    else:
        # 首拍：直接以映射值为基线
        drive_state = mapped

    # 5) pick_intent
    intent = de.pick_intent(drive_state, has_pending_task=has_pending_task)
    scores = de.compute_scores(drive_state)

    # 6) 存回状态
    if consume_events:
        _save_fact(EMOTION_STATE_KEY, new_emo)
        _save_fact(EVENTS_QUEUE_KEY, [])  # 清空已消费队列
    _save_fact(DRIVE_STATE_KEY, {
        "drive": drive_state.as_dict(),
        "snapshot_at": ee._ms_to_iso(now_ms),
    })

    return DesireSnapshot(
        driven=is_desire_driven(),
        drive=drive_state.as_dict(),
        scores=scores,
        intent=intent,
        display=display,
    )


def satisfy_action(action: str) -> None:
    """
    做完某 want_action 后，对驱动条做针对性回落并存回。
    调用方在真正执行了 intent.want_action 对应的行为后调用。
    """
    prev = _load_fact(DRIVE_STATE_KEY)
    if not prev or not isinstance(prev.get("drive"), dict):
        return
    drive_state = de.DriveState.from_dict(prev["drive"])
    drive_state = de.satisfy(drive_state, action)
    _save_fact(DRIVE_STATE_KEY, {
        "drive": drive_state.as_dict(),
        "snapshot_at": ee._ms_to_iso(time.time() * 1000.0),
    })


def read_snapshot(now_ms: Optional[float] = None,
                  has_pending_task: bool = False) -> DesireSnapshot:
    """只读观测：不消费事件、不改情感状态（供 /state 接口 / 前端面板）。"""
    return tick(now_ms=now_ms, has_pending_task=has_pending_task, consume_events=False)


# =============================================================================
# want_action → 自由活动名 映射（把欲望意图落到心跳现有的活动清单上）
# =============================================================================

# desire_engine 的 want_action → heartbeat._FREE_ACTIVITIES 里的活动名
ACTION_TO_FREE_ACTIVITY: Dict[str, List[str]] = {
    "murmur":      ["想对方了", "偷偷关心"],   # attachment：想念主人 → 冒句话
    "explore":     ["分享发现", "查天气"],     # curiosity：好奇外面
    "reflect":     ["翻旧回忆", "写秘密日记"], # reflection：想沉淀
    "duty_murmur": ["写秘密日记"],             # duty：记挂没做完的事
    "socialize":   ["分享发现"],               # social：想看人群（当前无社交源，退化为分享）
    "intimacy":    ["想对方了"],               # libido：凑过去
    "vent":        ["发呆放空", "写秘密日记"], # stress：吐槽/break
    "rest":        ["发呆放空"],               # fatigue 闸：歇着
}


def suggest_free_activity(intent: de.Intent) -> Optional[str]:
    """
    把欲望意图翻成一个「自由活动」候选名（心跳用来偏置模型选择）。
    返回 None 表示没有合适映射（调用方回退到原随机逻辑）。
    """
    cands = ACTION_TO_FREE_ACTIVITY.get(intent.want_action)
    if not cands:
        return None
    return cands[0]
