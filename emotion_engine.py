"""
16 维情感引擎 (Emotion Engine) —— Drivesoid 移植 · Python 纯函数版
====================================================================
从橘瓣插件的 JS 实现 (main.js) 移植而来，保持纯函数风格：

- 不碰 IO：状态 dict 作为参数传入、返回新状态；存储由调用方负责。
- 不取系统时间：所有需要「现在」的地方，now_ts（毫秒）由调用方传入。
- 不在引擎里调 LLM：消息分类结果（label + confidence）作为事件字段传入，
  分类这一步（调分类模型）留给宿主 / 心跳层。
- 噪声可注入：build_display 接收可选 rng（random.Random 实例），
  传固定种子即可重复跑、可单测。

维度分组（display / base 同 16 维 + fatigue 派生）：
  激活: vitality fatigue
  依恋: longing intimacy possessiveness lust
  威胁: jealousy anxiety protectiveness fear
  奖赏: contentment elation seeking play
  负向: dejection irritability

state 结构（dict，贴近原 JS）：
  base                  慢变基础值（16 维，指数回归 neutral）
  sleep                 睡眠子状态
  last_time_accumulated_at / last_interaction_at  ISO 时间戳（毫秒基准由函数换算）
  unanswered_thread     未回复线程
  last_segment          对话段（fear 累积上限用）
  time_episode          时间累积 episode（每维上限用）
  processed_calendar_ids  日历去重
  active_whim           当前 whim
  last_intimacy_at      亲密余晖
  _recent_labels        最近标签（判 high stakes）
  display / prev_display  即时值快照
"""

from __future__ import annotations

import math
import random as _random_module
from typing import Dict, List, Optional, Any


# =============================================================================
# CONST —— 全部翻译自 main.js，集中在此便于整定
# =============================================================================

TZ_OFFSET_DEFAULT = 8

# 维度参数（neutral 中值 / tau 回归时间常数(小时) / peak 昼夜峰值时刻 / amp 幅度 / width 宽度）
DIMS: Dict[str, Dict[str, float]] = {
    "vitality":       {"neutral": 0.50, "tau": 6,  "peak": 10, "amp": 1.0, "width": 10},
    "longing":        {"neutral": 0.30, "tau": 6,  "peak": 22, "amp": 0.9, "width": 6},
    "intimacy":       {"neutral": 0.35, "tau": 10, "peak": 23, "amp": 0.7, "width": 9},
    "possessiveness": {"neutral": 0.30, "tau": 4,  "peak": 21, "amp": 0.6, "width": 5},
    "lust":           {"neutral": 0.30, "tau": 4,  "peak": 23, "amp": 0.8, "width": 6},
    "jealousy":       {"neutral": 0.22, "tau": 2,  "peak": 0,  "amp": 0,   "width": 1},
    "anxiety":        {"neutral": 0.20, "tau": 2,  "peak": 0,  "amp": 0,   "width": 1},
    "protectiveness": {"neutral": 0.25, "tau": 4,  "peak": 0,  "amp": 0,   "width": 1},
    "contentment":    {"neutral": 0.35, "tau": 12, "peak": 14, "amp": 0.5, "width": 9},
    "elation":        {"neutral": 0.20, "tau": 3,  "peak": 19, "amp": 0.7, "width": 3.5},
    "seeking":        {"neutral": 0.25, "tau": 4,  "peak": 14, "amp": 0.8, "width": 5},
    "play":           {"neutral": 0.25, "tau": 3,  "peak": 19, "amp": 0.7, "width": 3.5},
    "dejection":      {"neutral": 0.15, "tau": 8,  "peak": 8,  "amp": 0.5, "width": 4},
    "irritability":   {"neutral": 0.15, "tau": 3,  "peak": 16, "amp": 0.6, "width": 3.5},
    "fear":           {"neutral": 0,    "tau": 1,  "peak": 0,  "amp": 0,   "width": 1},
}

DIM_FLOOR: Dict[str, float] = {
    "vitality": 0.08, "longing": 0.15, "intimacy": 0.06, "possessiveness": 0.05, "lust": 0.05,
    "jealousy": 0, "anxiety": 0.02, "protectiveness": 0.05,
    "contentment": 0.06, "elation": 0.02, "seeking": 0.12, "play": 0.03,
    "dejection": 0, "irritability": 0, "fear": 0,
}

FATIGUE_C = {"peak": 3, "amp": 0.8, "width": 10}
CAP = 0.08

# 分类标签 → 维度增量
LABEL_DELTAS: Dict[str, Dict[str, float]] = {
    "affectionate":       {"intimacy": +0.20, "contentment": +0.15, "anxiety": -0.18, "lust": +0.12, "longing": -0.10, "fear": -0.08},
    "playful":            {"play": +0.20, "elation": +0.18, "contentment": +0.12, "seeking": +0.10, "irritability": -0.10, "lust": +0.10},
    "vulnerable":         {"intimacy": +0.25, "protectiveness": +0.20, "contentment": +0.12, "anxiety": -0.10, "longing": -0.08},
    "reassuring":         {"anxiety": -0.25, "jealousy": -0.20, "contentment": +0.15, "intimacy": +0.15, "fear": -0.15},
    "cold":               {"anxiety": +0.15, "dejection": +0.12, "longing": +0.10, "intimacy": -0.10},
    "conflict":           {"anxiety": +0.20, "irritability": +0.15, "dejection": +0.15, "possessiveness": +0.18, "lust": +0.10, "intimacy": -0.15, "contentment": -0.15},
    "distant":            {"anxiety": +0.12, "dejection": +0.10, "longing": +0.12, "intimacy": -0.08},
    "struggling":         {"protectiveness": +0.30, "dejection": +0.08, "contentment": -0.08},
    "intimate_reference": {"lust": +0.18, "intimacy": +0.10},
    "intimate_event":     {"lust": +0.25, "intimacy": +0.18},
    "neutral":            {"anxiety": -0.05, "longing": -0.04, "contentment": +0.04},
    "hostile":            {"dejection": +0.22, "anxiety": +0.18, "irritability": +0.12, "intimacy": -0.22, "contentment": -0.18},
    "fear_separation":    {"fear": +0.20, "longing": +0.15, "possessiveness": +0.12, "anxiety": +0.15, "protectiveness": +0.10, "dejection": +0.10, "irritability": +0.08},
    "fear_death":         {"fear": +0.35, "anxiety": +0.30, "irritability": +0.20, "contentment": -0.12, "play": -0.15, "elation": -0.10},
    "fear_concern":       {"fear": +0.28, "longing": +0.12, "possessiveness": +0.15, "anxiety": +0.20, "protectiveness": +0.25, "contentment": -0.10},
    "fear_general":       {"fear": +0.20, "anxiety": +0.10},
}

MSG_STRUCTURAL = {
    "longing": -0.06, "seeking": -0.04, "dejection": -0.08, "contentment": +0.08,
    "anxiety": -0.025, "irritability": -0.020,
}
MSG_ANXIETY_COMP = -0.075
MSG_IRRIT_COMP = -0.060
NEG_LABELS = {"cold", "conflict", "distant", "hostile",
              "fear_separation", "fear_death", "fear_concern", "fear_general"}

MSG_QUICK_REPLY = {"contentment": +0.12, "elation": +0.10, "anxiety": -0.10}
MSG_HOT_CONV = {"contentment": +0.15, "play": +0.12, "elation": +0.10, "longing": -0.20}

TIME_PER_HOUR = {"longing": 0.04, "anxiety": 0.02, "seeking": 0.02}
TIME_CAPS = {"longing": 0.35, "anxiety": 0.18, "seeking": 0.12,
             "dejection": 0.08, "irritability_unanswered": 0.10}
DEJECTION_THRESHOLD_H = 6

UNANSWERED = {
    "normal": {"1h": {"anxiety": +0.06, "irritability": +0.04}, "2h": {"anxiety": +0.05}},
    "high":   {"30m": {"anxiety": +0.12, "irritability": +0.08}, "1h": {"anxiety": +0.10}, "2h": {"anxiety": +0.08}},
}
ANXIETY_UNANSWERED_CAP = {"normal": 0.15, "high": 0.28}
MILESTONE_MINUTES = {"30m": 30, "1h": 60, "2h": 120}

CALENDAR_DELTAS = {
    "period_start": {"protectiveness": +0.20, "lust": -0.10},
    "period_end":   {"lust": +0.15, "longing": +0.08},
    "intimacy":     {"lust": +0.25, "intimacy": +0.18},
    "exam":         {"protectiveness": +0.15, "seeking": +0.10},
    "holiday":      {"elation": +0.20, "longing": +0.15},
    "birthday":     {"elation": +0.30, "longing": +0.20, "seeking": +0.15, "lust": +0.12},
    "trip_start":   {"longing": +0.20, "anxiety": +0.10, "possessiveness": +0.15, "lust": +0.10},
    "trip_end":     {"elation": +0.25, "longing": -0.20, "lust": +0.15},
    "meetup":       {"elation": +0.30, "lust": +0.20, "seeking": +0.15},
}

VALID_LABELS = set(LABEL_DELTAS.keys())
FEAR_LABEL_CAP = 0.60

MS_PER_HOUR = 3600000.0

# display 层用到的分组
_NEG_DIMS = {"dejection", "irritability", "anxiety", "fear"}
_POS_MOD = ["longing", "intimacy", "possessiveness", "lust", "contentment",
            "elation", "seeking", "play", "protectiveness", "jealousy"]
_NEG_MOD = ["irritability", "dejection", "anxiety", "fear"]


# =============================================================================
# 工具函数（纯函数）
# =============================================================================

def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if not math.isfinite(v):
        return (lo + hi) / 2
    return min(max(v, lo), hi)


def _ms(iso_or_ms: Any) -> float:
    """把 ISO 字符串或毫秒数统一成毫秒 float。"""
    if isinstance(iso_or_ms, (int, float)):
        return float(iso_or_ms)
    return _iso_to_ms(str(iso_or_ms))


def _iso_to_ms(iso: str) -> float:
    """ISO8601 → epoch 毫秒。容错：解析失败返回 0。"""
    if not iso:
        return 0.0
    try:
        import datetime as _dt
        s = iso.replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp() * 1000.0
    except Exception:
        return 0.0


def _ms_to_iso(ms: float) -> str:
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(ms / 1000.0, tz=_dt.timezone.utc)
    # 与 JS toISOString 对齐：毫秒 + Z
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(d.microsecond/1000):03d}Z"


def local_hour(ms: float, tz_offset: float) -> float:
    """本地小时（0..24），tz_offset 为 UTC 偏移小时。"""
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(ms / 1000.0, tz=_dt.timezone.utc)
    return ((d.hour + tz_offset) % 24 + 24) % 24


def gaussian_offset(peak: float, amp: float, width: float,
                    ms: float, tz_offset: float) -> float:
    """昼夜节律高斯偏移。amp=0 直接返回 0。"""
    if amp == 0:
        return 0.0
    h = local_hour(ms, tz_offset)
    dist = ((h - peak + 12) % 24) - 12
    return CAP * amp * math.exp(-0.5 * (dist / width) ** 2)


def standard_decay(base_val: float, key: str, elapsed_ms: float) -> float:
    """某维基础值向 neutral 指数回归。"""
    p = DIMS[key]
    return p["neutral"] + (base_val - p["neutral"]) * math.exp(-elapsed_ms / (p["tau"] * MS_PER_HOUR))


def noise(rng: Optional[_random_module.Random], sigma: float = 0.02) -> float:
    """Box-Muller 高斯噪声。rng=None 时用模块级 random（不可重复）。"""
    r = rng if rng is not None else _random_module
    u1 = max(2.220446049250313e-16, r.random())
    return math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * r.random()) * sigma


def apply_deltas(base: Dict[str, float], deltas: Dict[str, float]) -> None:
    """就地对 base 施加增量（含 DIM_FLOOR 下限）。base 是可变 dict，原地改。"""
    for k, dv in deltas.items():
        if k in base:
            base[k] = clamp(max(base[k] + dv, DIM_FLOOR.get(k, 0)))


# =============================================================================
# fatigue 睡眠模型
# =============================================================================

def fatigue_base(sleep: Dict[str, Any], now_ms: float) -> float:
    """按睡眠状态推算疲惫基线。"""
    target = 7.5
    actual = sleep.get("last_sleep_duration_hours")
    if actual is None:
        actual = target
    base_at_wake = clamp((max(0, target - actual) / target) * 0.6)

    status = sleep.get("status")

    if status == "asleep" and sleep.get("last_sleep_started_at"):
        hours_asleep = (now_ms - _ms(sleep["last_sleep_started_at"])) / MS_PER_HOUR
        base_at_sleep = sleep.get("_base_at_sleep")
        if base_at_sleep is None:
            base_at_sleep = base_at_wake
        remaining_target = max(1, target - (sleep.get("accumulated_sleep_hours") or 0))
        return clamp(base_at_sleep - (base_at_sleep / remaining_target) * hours_asleep)

    if status == "interrupted":
        acc = sleep.get("accumulated_sleep_hours") or 0
        base_at_interrupt = clamp((max(0, target - acc) / target) * 0.6 + (sleep.get("interrupt_fatigue_bonus") or 0.12))
        if not sleep.get("last_interrupted_at"):
            return clamp(base_at_interrupt)
        hours_since = (now_ms - _ms(sleep["last_interrupted_at"])) / MS_PER_HOUR
        return clamp(base_at_interrupt + clamp((hours_since - 1) / 10) * 0.25)

    wake_ms = _ms(sleep["last_wake_at"]) if sleep.get("last_wake_at") else now_ms
    hours_awake = (now_ms - wake_ms) / MS_PER_HOUR
    return clamp(base_at_wake + clamp((hours_awake - 4) / 14) * 0.4)


# =============================================================================
# Display pipeline —— 完整耦合链
# =============================================================================

def build_display(state: Dict[str, Any], now_ms: float, tz_offset: float,
                  rng: Optional[_random_module.Random] = None) -> Dict[str, float]:
    """
    display = base + 昼夜高斯 + 噪声，再经：
      活力/疲惫耦合 → 正负向调制 → 嫉妒×焦虑互增 → 亲密余晖 →
      焦虑放大依恋 → 恐惧抑制奖赏 → whim 临时加成。
    返回一个新的 16 维 + fatigue 的 dict（不改 state）。
    """
    b = state["base"]
    d: Dict[str, float] = {}

    for k, p in DIMS.items():
        sigma = 0.01 if k in _NEG_DIMS else 0.02
        d[k] = clamp(b[k] + gaussian_offset(p["peak"], p["amp"], p["width"], now_ms, tz_offset) + noise(rng, sigma))

    d["fatigue"] = clamp(
        fatigue_base(state["sleep"], now_ms) +
        gaussian_offset(FATIGUE_C["peak"], FATIGUE_C["amp"], FATIGUE_C["width"], now_ms, tz_offset) +
        noise(rng)
    )

    # 活力/疲惫互相压制
    v0, f0 = d["vitality"], d["fatigue"]
    d["vitality"] = v0 * (1 - 0.6 * f0)
    d["fatigue"] = f0 * (1 - 0.2 * v0)

    # 情感浮力：活力高、疲惫低 → 放大正向、压抑负向
    af = clamp(d["vitality"] * (1 - d["fatigue"]))
    for k in _POS_MOD:
        d[k] = clamp(d[k] * (0.5 + 0.7 * af))
    for k in _NEG_MOD:
        d[k] = clamp(d[k] * (1.4 - 0.6 * af))

    # 嫉妒 × 焦虑 互增
    j0, a0 = d["jealousy"], d["anxiety"]
    d["jealousy"] = j0 * (1 + 0.4 * a0)
    d["anxiety"] = a0 * (1 + 0.25 * j0)

    # 亲密余晖（约 14h 衰减窗）
    if state.get("last_intimacy_at"):
        hours_ago = (now_ms - _ms(state["last_intimacy_at"])) / MS_PER_HOUR
        factor = 0.6 * clamp(1 - (hours_ago - 3) / 11)
        if factor > 0:
            d["intimacy"] = clamp(d["intimacy"] + d["intimacy"] * factor)
            d["lust"] = clamp(d["lust"] + d["lust"] * factor)

    # 焦虑放大依恋
    a_amp = 1 + 0.3 * d["anxiety"]
    for k in ("intimacy", "lust", "longing", "possessiveness"):
        d[k] = clamp(d[k] * a_amp)

    # 恐惧抑制奖赏
    if d["fear"] > 0.1:
        damp = 1 - 0.3 * d["fear"]
        for k in ("contentment", "elation", "play", "seeking", "lust"):
            d[k] = clamp(d[k] * damp)

    # whim 临时加成
    aw = state.get("active_whim")
    if aw and _ms(aw["expires_at"]) > now_ms:
        for wk, wv in aw["deltas"].items():
            if wk in d:
                d[wk] = clamp(d[wk] + wv)

    for k in list(d.keys()):
        d[k] = clamp(d[k])
    return d


# =============================================================================
# State 初始化 / 维度补齐
# =============================================================================

def _gen_id() -> str:
    return "drv_" + str(int(_time_ms())) + "_" + _random_module.random().hex()[2:11]


def _time_ms() -> float:
    # 仅用于生成 id / 初始化默认值；演化逻辑一律用传入的 now_ms。
    import time as _t
    return _t.time() * 1000.0


def create_initial_state(now_ms: float, tz_offset: float = TZ_OFFSET_DEFAULT) -> Dict[str, Any]:
    """
    构造初始状态。now_ms 由调用方传入（不取系统时间）。
    last_wake_at 估算为「今天本地 07:00」。
    """
    # 今天本地 07:00 对应的 UTC 毫秒
    local_ms = now_ms + tz_offset * MS_PER_HOUR
    import datetime as _dt
    ld = _dt.datetime.fromtimestamp(local_ms / 1000.0, tz=_dt.timezone.utc)
    ld7 = ld.replace(hour=7, minute=0, second=0, microsecond=0)
    wake_local_ms = ld7.timestamp() * 1000.0
    wake_ms = wake_local_ms - tz_offset * MS_PER_HOUR
    if wake_ms > now_ms:
        wake_ms -= 86400000
    last_wake_at = _ms_to_iso(wake_ms)

    iso = _ms_to_iso(now_ms)
    base = {k: DIMS[k]["neutral"] for k in DIMS}

    return {
        "schema_version": 1,
        "snapshot_at": iso,
        "state_updated_at": iso,
        "last_processed_event_id": None,
        "last_time_accumulated_at": iso,
        "last_interaction_at": iso,
        "unanswered_thread": None,
        "last_segment": {"id": _gen_id(), "started_at": iso, "last_message_at": iso,
                         "status": "open", "messages": 0, "fear_label_applied": 0},
        "processed_calendar_ids": [],
        "time_episode": None,
        "active_whim": None,
        "sleep": {"status": "awake", "last_sleep_started_at": None, "last_wake_at": last_wake_at,
                  "last_sleep_duration_hours": 7.2, "estimated": True},
        "last_intimacy_at": None,
        "_recent_labels": [],
        "base": base,
        "display": None,
        "prev_display": None,
    }


def ensure_dims(state: Dict[str, Any]) -> None:
    """补齐可能缺失的维度（原地）。"""
    base = state.setdefault("base", {})
    for k in DIMS:
        if k not in base:
            base[k] = DIMS[k]["neutral"]
    if not state.get("sleep"):
        state["sleep"] = {"status": "awake", "last_sleep_started_at": None,
                          "last_wake_at": None, "last_sleep_duration_hours": 7.2, "estimated": True}
    if "_recent_labels" not in state:
        state["_recent_labels"] = []


# =============================================================================
# Decay / 时间累积
# =============================================================================

def decay_base_to(base: Dict[str, float], from_ms: float, to_ms: float) -> None:
    """把 base 从 from_ms 衰减到 to_ms（原地）。"""
    if to_ms <= from_ms:
        return
    elapsed = to_ms - from_ms
    for k in DIMS:
        base[k] = standard_decay(base[k], k, elapsed)


def accumulate_time(state: Dict[str, Any], now_ms: float) -> None:
    """时间累积：思念/焦虑/探索缓涨（带上限）、久无互动加低落、未回复加烦躁。原地改 state。"""
    sleep = state.get("sleep")
    if sleep and sleep.get("status") == "asleep":
        state["last_time_accumulated_at"] = _ms_to_iso(now_ms)
        return

    from_ms = _ms(state.get("last_time_accumulated_at") or _ms_to_iso(now_ms))
    last_interaction_ms = _ms(state["last_interaction_at"])
    if now_ms <= from_ms:
        return

    if last_interaction_ms > from_ms:
        state["time_episode"] = {"id": _gen_id(), "started_at": state["last_interaction_at"], "applied": {}}
    if not state.get("time_episode"):
        state["time_episode"] = {"id": _gen_id(), "started_at": state["last_interaction_at"], "applied": {}}
    ep = state["time_episode"]

    catchup_horizon = 30 * 24 * MS_PER_HOUR
    step_start = max(from_ms, last_interaction_ms, now_ms - catchup_horizon)
    if now_ms <= step_start:
        state["last_time_accumulated_at"] = _ms_to_iso(now_ms)
        return

    STEP = MS_PER_HOUR
    t = step_start
    base = state["base"]
    while t < now_ms:
        t_end = min(t + STEP, now_ms)
        frac = (t_end - t) / STEP
        hours_since_interaction = (t - last_interaction_ms) / MS_PER_HOUR

        for k, per_h in TIME_PER_HOUR.items():
            already = ep["applied"].get(k, 0)
            if already < TIME_CAPS[k]:
                add = min(per_h * frac, TIME_CAPS[k] - already)
                base[k] = clamp(base[k] + add)
                ep["applied"][k] = already + add

        if hours_since_interaction >= DEJECTION_THRESHOLD_H:
            dej_already = ep["applied"].get("dejection", 0)
            if dej_already < TIME_CAPS["dejection"]:
                dej_add = min(0.01 * frac, TIME_CAPS["dejection"] - dej_already)
                base["dejection"] = clamp(base["dejection"] + dej_add)
                ep["applied"]["dejection"] = dej_already + dej_add

        if state.get("unanswered_thread"):
            irr_key = "irritability_unanswered"
            irr_already = ep["applied"].get(irr_key, 0)
            if irr_already < TIME_CAPS["irritability_unanswered"]:
                irr_add = min(0.02 * frac, TIME_CAPS["irritability_unanswered"] - irr_already)
                base["irritability"] = clamp(base["irritability"] + irr_add)
                ep["applied"][irr_key] = irr_already + irr_add

        t = t_end

    state["last_time_accumulated_at"] = _ms_to_iso(now_ms)


def check_unanswered_milestones(state: Dict[str, Any], now_ms: float) -> None:
    """未回复里程碑：到 30m/1h/2h 追加焦虑/烦躁（带 stakes 上限）。原地改。"""
    sleep = state.get("sleep")
    if sleep and sleep.get("status") == "asleep":
        return
    ut = state.get("unanswered_thread")
    if not ut:
        return
    elapsed_min = (now_ms - _ms(ut["sent_at"])) / 60000.0
    table = UNANSWERED.get(ut.get("stakes"), UNANSWERED["normal"])
    cap = ANXIETY_UNANSWERED_CAP.get(ut.get("stakes"), ANXIETY_UNANSWERED_CAP["normal"])
    if "milestones_applied" not in ut:
        ut["milestones_applied"] = []

    anxiety_applied = 0.0
    for m in ut["milestones_applied"]:
        if table.get(m) and table[m].get("anxiety"):
            anxiety_applied += table[m]["anxiety"]

    for label, minutes in MILESTONE_MINUTES.items():
        if not table.get(label) or label in ut["milestones_applied"]:
            continue
        if elapsed_min < minutes:
            continue
        deltas = dict(table[label])
        if deltas.get("anxiety"):
            deltas["anxiety"] = min(deltas["anxiety"], max(0, cap - anxiety_applied))
            anxiety_applied += deltas["anxiety"]
        apply_deltas(state["base"], deltas)
        ut["milestones_applied"].append(label)


def maybe_fire_whim(state: Dict[str, Any], now_ms: float, tz_offset: float,
                    rng: Optional[_random_module.Random] = None) -> None:
    """
    whim 冲动：display 高偏离时，从正向或负向池里随机抽几维，加 30 分钟临时加成。
    随机来源统一走 rng（可重复）。原地改 state。
    """
    r = rng if rng is not None else _random_module
    sleep = state.get("sleep")
    if sleep and sleep.get("status") == "asleep":
        return
    aw = state.get("active_whim")
    if aw and _ms(aw["expires_at"]) > now_ms:
        return

    d = build_display(state, now_ms, tz_offset, rng=rng)
    POS_W = ["vitality", "seeking", "play", "elation", "contentment"]
    NEG_W = ["dejection", "irritability", "anxiety"]
    pos_max = max((max(0, d[k] - 0.6) for k in POS_W), default=0)
    neg_max = max((max(0, d[k] - 0.5) for k in NEG_W), default=0)

    if pos_max == 0 and neg_max == 0:
        return
    if abs(pos_max - neg_max) < 0.1:
        return

    positive = pos_max > neg_max
    threshold = 0.6 if positive else 0.5
    pool = [k for k in (POS_W if positive else NEG_W) if d[k] > threshold]
    if not pool:
        return
    count = 2 + int(r.random() * min(2, len(pool) - 1)) if len(pool) > 1 else 1
    shuffled = pool[:]
    r.shuffle(shuffled)
    chosen = shuffled[:count]

    deltas: Dict[str, float] = {}
    for k in chosen:
        deltas[k] = 0.03 + r.random() * 0.02
    deltas["lust"] = 0.04

    state["active_whim"] = {
        "fired_at": _ms_to_iso(now_ms),
        "expires_at": _ms_to_iso(now_ms + 30 * 60000),
        "deltas": deltas,
    }


# =============================================================================
# 事件处理
# =============================================================================

def process_event(state: Dict[str, Any], ev: Dict[str, Any], now_ms: float) -> None:
    """处理单个事件（原地改 state）。msg_user 的分类结果由 tick_evolve 单独施加。"""
    ev_ms = _ms(ev["timestamp"])
    if not math.isfinite(ev_ms):
        return
    etype = ev.get("type")
    base = state["base"]

    if etype == "msg_user":
        state["last_interaction_at"] = ev["timestamp"]
        state["unanswered_thread"] = None
        apply_deltas(base, MSG_STRUCTURAL)
        seg = state.get("last_segment")
        if not seg or seg.get("status") == "summarized":
            state["last_segment"] = {"id": _gen_id(), "started_at": ev["timestamp"],
                                     "last_message_at": ev["timestamp"], "status": "open",
                                     "messages": 1, "fear_label_applied": 0}
        else:
            seg["last_message_at"] = ev["timestamp"]
            seg["messages"] = (seg.get("messages") or 0) + 1

    elif etype == "msg_assistant":
        high_stakes = {"affectionate", "vulnerable", "intimate_reference", "intimate_event"}
        recent = state.get("_recent_labels") or []
        stakes = "high" if any(l in high_stakes for l in recent) else "normal"
        state["unanswered_thread"] = {
            "message_id": (ev.get("payload") or {}).get("message_id"),
            "sent_at": ev["timestamp"],
            "stakes": stakes,
            "milestones_applied": [],
        }
        seg = state.get("last_segment")
        if not seg or seg.get("status") == "summarized":
            state["last_segment"] = {"id": _gen_id(), "started_at": ev["timestamp"],
                                     "last_message_at": ev["timestamp"], "status": "open",
                                     "messages": 1, "fear_label_applied": 0}
        else:
            seg["last_message_at"] = ev["timestamp"]
            seg["messages"] = (seg.get("messages") or 0) + 1

    elif etype == "msg_quick_reply":
        apply_deltas(base, MSG_QUICK_REPLY)

    elif etype == "msg_hot_conv":
        apply_deltas(base, MSG_HOT_CONV)

    elif etype == "calendar":
        payload = ev.get("payload") or {}
        cal_id = payload.get("calendar_id")
        cal_type = payload.get("calendar_type")
        if cal_id and cal_id not in (state.get("processed_calendar_ids") or []):
            c_deltas = CALENDAR_DELTAS.get(cal_type)
            if c_deltas:
                apply_deltas(base, c_deltas)
                if cal_type == "intimacy":
                    state["last_intimacy_at"] = ev["timestamp"]
                state.setdefault("processed_calendar_ids", []).append(cal_id)

    elif etype == "sex_end":
        state["last_intimacy_at"] = ev["timestamp"]

    elif etype == "sleep_start":
        sleep = state["sleep"]
        if sleep.get("status") == "asleep":
            return
        if sleep.get("status") == "awake":
            sleep.pop("accumulated_sleep_hours", None)
        sleep["_base_at_sleep"] = fatigue_base(sleep, ev_ms)
        sleep.pop("interrupt_fatigue_bonus", None)
        sleep["status"] = "asleep"
        sleep["last_sleep_started_at"] = ev["timestamp"]
        state["unanswered_thread"] = None
        state.pop("active_whim", None)

    elif etype == "sleep_end":
        sleep = state["sleep"]
        if sleep.get("status") == "awake":
            return
        has_active = sleep.get("status") == "asleep" and sleep.get("last_sleep_started_at")
        last_seg_hours = (max(0, ev_ms - _ms(sleep["last_sleep_started_at"])) / MS_PER_HOUR) if has_active else 0
        accumulated = sleep.get("accumulated_sleep_hours") or 0
        total = accumulated + last_seg_hours
        sleep["last_sleep_duration_hours"] = total if total > 0 else (sleep.get("last_sleep_duration_hours") or 7.5)
        sleep["status"] = "awake"
        sleep["last_wake_at"] = ev["timestamp"]
        sleep["estimated"] = False
        sleep.pop("_base_at_sleep", None)
        sleep.pop("accumulated_sleep_hours", None)
        sleep.pop("interrupt_fatigue_bonus", None)
        state["unanswered_thread"] = None

    elif etype == "sleep_interrupt":
        sleep = state["sleep"]
        if sleep.get("status") != "asleep":
            return
        seg_start = _ms(sleep["last_sleep_started_at"]) if sleep.get("last_sleep_started_at") else ev_ms
        sleep["accumulated_sleep_hours"] = (sleep.get("accumulated_sleep_hours") or 0) + max(0, ev_ms - seg_start) / MS_PER_HOUR
        sleep.pop("last_sleep_started_at", None)
        sleep["status"] = "interrupted"
        sleep["last_interrupted_at"] = ev["timestamp"]
        sleep["interrupt_fatigue_bonus"] = 0.12
        apply_deltas(base, {"irritability": 0.12, "vitality": -0.10})
        state["unanswered_thread"] = None
        state.pop("active_whim", None)


def _apply_classification(state: Dict[str, Any], ev: Dict[str, Any]) -> None:
    """把 msg_user 事件上的分类结果 (_classified) 施加到 base（原地）。"""
    cls = ev.get("_classified")
    if not cls:
        return
    label = cls.get("label")
    confidence = cls.get("confidence")
    raw = LABEL_DELTAS.get(label, {})
    scaled = {k: clamp(v * confidence, -0.25, 0.25) for k, v in raw.items()}

    if scaled.get("fear") is not None and str(label).startswith("fear_"):
        seg = state["last_segment"]
        already = seg.get("fear_label_applied") or 0
        allowed = max(0, min(scaled["fear"], FEAR_LABEL_CAP - already))
        seg["fear_label_applied"] = already + allowed
        scaled["fear"] = allowed

    apply_deltas(state["base"], scaled)

    if label not in NEG_LABELS:
        apply_deltas(state["base"], {"anxiety": MSG_ANXIETY_COMP, "irritability": MSG_IRRIT_COMP})

    if label == "intimate_event":
        state["last_intimacy_at"] = ev["timestamp"]

    state["_recent_labels"] = ([label] + (state.get("_recent_labels") or []))[:3]


# =============================================================================
# tick —— 纯函数演化入口
# =============================================================================

def tick_evolve(state: Dict[str, Any],
                events: List[Dict[str, Any]],
                now_ms: float,
                tz_offset: float = TZ_OFFSET_DEFAULT,
                rng: Optional[_random_module.Random] = None) -> Dict[str, Any]:
    """
    推进情感状态一拍（纯函数：接收 state + events + now_ms，返回新 state）。

    - 不修改传入的 state（内部深拷贝）。
    - events：事件列表，msg_user 事件如需情感分类，调用方应先在 ev 上填 _classified
      = {"label": <str>, "confidence": <0..1>}（分类调 LLM 的步骤在引擎外）。
    - now_ms：当前时间（epoch 毫秒），由调用方传入。
    - rng：随机源（噪声 / whim）。传 random.Random(seed) 可重复。

    返回的 state 里 display 是最新 16 维 + fatigue 快照，可直接喂给 desire_engine。
    """
    import copy
    st = copy.deepcopy(state) if state else create_initial_state(now_ms, tz_offset)
    ensure_dims(st)

    # 按序处理事件：每个事件前先补衰减 + 时间累积
    for ev in (events or []):
        ev_ms = _ms(ev["timestamp"])
        decay_base_to(st["base"], _ms(st.get("last_time_accumulated_at") or ev["timestamp"]), ev_ms)
        accumulate_time(st, ev_ms)
        process_event(st, ev, ev_ms)
        _apply_classification(st, ev)
        st["last_processed_event_id"] = ev.get("event_id")

    # 收尾：衰减到 now + 时间累积 + 里程碑 + whim
    last_ms = _ms(st.get("last_time_accumulated_at")) if st.get("last_time_accumulated_at") else now_ms
    decay_base_to(st["base"], last_ms, now_ms)
    accumulate_time(st, now_ms)
    check_unanswered_milestones(st, now_ms)
    maybe_fire_whim(st, now_ms, tz_offset, rng=rng)

    # 段落超时归档（15 分钟无消息）
    seg = st.get("last_segment")
    if seg and seg.get("status") == "open":
        if now_ms - _ms(seg["last_message_at"]) > 15 * 60000:
            seg["status"] = "summarized"

    iso = _ms_to_iso(now_ms)
    st["snapshot_at"] = iso
    st["state_updated_at"] = iso
    if st.get("display"):
        st["prev_display"] = st["display"]
    st["display"] = build_display(st, now_ms, tz_offset, rng=rng)
    return st


def build_display_snapshot(state: Dict[str, Any], now_ms: float,
                           tz_offset: float = TZ_OFFSET_DEFAULT,
                           rng: Optional[_random_module.Random] = None) -> Dict[str, float]:
    """只读：算一份当前 display，不推进状态、不改 state。"""
    ensure_dims(state)
    return build_display(state, now_ms, tz_offset, rng=rng)
