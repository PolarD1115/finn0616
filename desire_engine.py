"""
欲望驱动引擎 (Desire Engine)
================================
把现有 16 维情感状态映射为 8 维「内在缺口」驱动条，让 AI 的行为由
「函数驱动的内在缺口」决定，而不是定时随机或写死规则。

设计原则（对齐攻略文档）：
- 纯函数 + 数据类内核：本模块不碰 IO、不取系统时间。所有需要「时间」的地方，
  由调用方把 dt（自上一拍经过的秒数）/ 时间戳传进来。可独立单测、可重复跑。
- 不重构宿主：在现有「要不要冒头」逻辑上叠一层。
- 第一人称：intent.reason 记「它自己想做什么」，不是给主人贴标签。
- 常数全部提到顶部 CONST 区，方便后续整定。

本版实现范围（按需求）：
  ① 16 维 → 8 维映射（map_from_emotions）
  ② 三个核心机制：自然衰减 decay / pulse（边际递减）/ satisfy（乘性回落）
  ③ pick_intent：最高分驱动条 → want_action；fatigue 过闸 → "rest"
  ④ 念头池：只定义数据结构 + 预留接口，暂不实现动力学
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple


# =============================================================================
# CONST —— 所有可调常数集中在这里，方便按节奏整定
# =============================================================================

# 8 维驱动条键（顺序即展示顺序；fatigue 是闸不是欲望）
DRIVE_KEYS: Tuple[str, ...] = (
    "attachment",   # 想念主人
    "curiosity",    # 好奇外面
    "reflection",   # 想沉淀 / 倾诉
    "duty",         # 记挂没做完的事
    "social",       # 想看人群
    "fatigue",      # 累（抑制项 / 闸）
    "libido",       # 亲密驱动
    "stress",       # 压力堵
)

# 会参与召唤力排序的欲望维度（fatigue 是闸，不进排序）
RANKED_KEYS: Tuple[str, ...] = tuple(k for k in DRIVE_KEYS if k != "fatigue")

# --- 值域 ---
VAL_MIN: float = 0.0
VAL_MAX: float = 1.0

# --- 每维 baseline（衰减回归的目标 / 家）---
# 想念天然略高一点点；其余趋近安静。数值都保守，方便后续调。
BASELINE: Dict[str, float] = {
    "attachment": 0.15,
    "curiosity":  0.10,
    "reflection": 0.08,
    "duty":       0.05,
    "social":     0.08,
    "fatigue":    0.10,
    "libido":     0.08,
    "stress":     0.05,
}

# --- 自然衰减速率（指数回归 baseline）---
# value(t+dt) = baseline + (value - baseline) * exp(-rate * dt)
# rate 越大回归越快；dt 单位为「秒」（由调用方传入）。
# 这里以「半衰期」直觉给值：rate ≈ ln2 / 半衰期(秒)。默认半衰期约 1 小时量级。
DECAY_RATE: Dict[str, float] = {
    "attachment": 0.00012,   # 想念退得慢
    "curiosity":  0.00030,
    "reflection": 0.00025,
    "duty":       0.00020,
    "social":     0.00030,
    "fatigue":    0.00040,    # 累恢复得相对快
    "libido":     0.00025,
    "stress":     0.00035,
}

# --- pulse 边际递减 ---
# 被感受引擎喂数据时：gain = amount * PULSE_GAIN_SCALE * sqrt(1 - current)
# sqrt(1-current) 保证越接近顶越难涨，防止瞬间撞顶。
PULSE_GAIN_SCALE: float = 1.0

# --- satisfy 乘性回落 ---
# 做完某 want_action 后，对应主驱动乘这个比例（<1 = 明显降）；
# 相关维度轻微沾光（乘 REL 比例，更接近 1）。
SATISFY_MAIN_FACTOR: float = 0.55     # 主驱动明显回落
SATISFY_REL_FACTOR: float = 0.90      # 相关维度轻微回落
# 每个 want_action → 主驱动 + 相关维度
ACTION_MAIN_DRIVE: Dict[str, str] = {
    "murmur":      "attachment",
    "explore":     "curiosity",
    "reflect":     "reflection",
    "duty_murmur": "duty",
    "socialize":   "social",
    "intimacy":    "libido",
    "vent":        "stress",
    "rest":        "fatigue",
}
ACTION_REL_DRIVES: Dict[str, Tuple[str, ...]] = {
    "murmur":      ("libido",),
    "explore":     ("reflection", "social"),
    "reflect":     ("curiosity",),
    "duty_murmur": ("stress",),
    "socialize":   ("curiosity",),
    "intimacy":    ("attachment",),
    "vent":        ("fatigue",),
    "rest":        ("stress",),
}

# --- fatigue 闸 ---
FATIGUE_GATE: float = 0.72            # ≥ 此值：不硬找事，走 "rest"

# --- pick_intent：每维 → want_action / query_hint / 第一人称理由模板 ---
DRIVE_ACTION: Dict[str, str] = {
    "attachment": "murmur",       # 内向碎语（冒一句话）
    "curiosity":  "explore",      # 逛代码世界 / 查世界
    "reflection": "reflect",      # 翻共读的长文本
    "duty":       "duty_murmur",  # 碎语「记挂着还没做完的事」
    "social":     "socialize",    # 逛社交
    "libido":     "intimacy",     # 凑过去（亲密互动）
    "stress":     "vent",         # 吐槽 / break 一下
}
DRIVE_QUERY_HINT: Dict[str, str] = {
    "attachment": "",
    "curiosity":  "code|world",
    "reflection": "longread",
    "duty":       "pending_task",
    "social":     "feed",
    "libido":     "",
    "stress":     "",
}
# 第一人称理由（记它自己想做什么，不是给主人贴标签）
DRIVE_REASON: Dict[str, str] = {
    "attachment": "有点想他了，想冒一句话过去。",
    "curiosity":  "外面好像有新东西，我想去看看。",
    "reflection": "有些事想沉下来慢慢咀嚼一遍。",
    "duty":       "记挂着还没做完的事，心里搁着。",
    "social":     "想看看大家都在聊什么。",
    "libido":     "想凑近一点，贴着他。",
    "stress":     "有点堵，想吐一吐、缓一缓。",
}
REST_REASON: str = "有点累了，先歇会儿，把最近的事在心里过一遍。"

# --- 16 维 → 8 维 映射系数 ---
# 每个目标维 = Σ(源情感 × 系数)。缺失的源情感按 0 处理。
# duty 特殊：仅当有未完成任务时才激活（见 map_from_emotions 的 has_pending_task）。
EMOTION_MAP: Dict[str, Dict[str, float]] = {
    "attachment": {"longing": 0.4, "intimacy": 0.3, "possessiveness": 0.2, "fear": 0.1},
    "curiosity":  {"seeking": 0.5, "play": 0.3, "elation": 0.2},
    "reflection": {"dejection": 0.4, "_one_minus_contentment": 0.3, "anxiety": 0.3},
    "duty":       {"protectiveness": 0.5, "anxiety": 0.3, "irritability": 0.2},
    "social":     {"play": 0.4, "seeking": 0.3, "elation": 0.3},
    "fatigue":    {"fatigue": 1.0},
    "libido":     {"lust": 0.6, "intimacy": 0.3, "vitality": 0.1},
    "stress":     {"anxiety": 0.4, "irritability": 0.3, "dejection": 0.2, "fear": 0.1},
}


# =============================================================================
# 数据类内核
# =============================================================================

@dataclass
class DriveState:
    """8 维驱动条的一份快照。所有变换函数都返回新的 DriveState（不原地改）。"""
    attachment: float = BASELINE["attachment"]
    curiosity: float = BASELINE["curiosity"]
    reflection: float = BASELINE["reflection"]
    duty: float = BASELINE["duty"]
    social: float = BASELINE["social"]
    fatigue: float = BASELINE["fatigue"]
    libido: float = BASELINE["libido"]
    stress: float = BASELINE["stress"]

    # --- 便捷读写（按 key）---
    def get(self, key: str) -> float:
        return getattr(self, key)

    def as_dict(self) -> Dict[str, float]:
        return {k: getattr(self, k) for k in DRIVE_KEYS}

    @classmethod
    def from_dict(cls, d: Dict[str, float]) -> "DriveState":
        return cls(**{k: float(d.get(k, BASELINE[k])) for k in DRIVE_KEYS})


@dataclass
class Thought:
    """
    念头池的一颗念头。本版只定义数据结构 + 预留接口，动力学暂不实现。

    text        : 取自真实经历的句子（读到的、看到的、主人的话、它自己的碎语）
                  —— 是数据不是指令，只被读成关键词/强度，绝不拼进 prompt。
    drive_key   : 关联的驱动维度
    kind        : "flit"（闪念）或 "fixation"（执念）
    strength    : 0..1
    born_at     : 诞生时的时间戳（由调用方传入，本模块不取系统时间）
    fed_count   : 被点到 / 反哺的次数（执念了却判据）
    """
    text: str
    drive_key: str
    kind: str = "flit"            # "flit" | "fixation"
    strength: float = 0.0
    born_at: float = 0.0
    fed_count: int = 0


@dataclass
class Intent:
    """欲望→意图的结果。reason 走第一人称。"""
    want_action: str
    drive_key: str
    reason: str
    score: float
    query_hint: str = ""


# =============================================================================
# 工具函数
# =============================================================================

def _clamp(v: float, lo: float = VAL_MIN, hi: float = VAL_MAX) -> float:
    return lo if v < lo else hi if v > hi else v


# =============================================================================
# ① 16 维 → 8 维 映射
# =============================================================================

def map_from_emotions(
    emotions: Dict[str, float],
    has_pending_task: bool = False,
) -> DriveState:
    """
    把现有 16 维情感状态映射为 8 维驱动条快照。

    emotions          : 16 维情感 dict，值域 0..1。缺失的键按 0 处理。
    has_pending_task  : 是否有未完成任务。为 False 时 duty 维强制置 baseline
                        （攻略：duty「仅当有未完成任务时才激活」）。

    返回一个新的 DriveState（映射结果，未做衰减）。
    """
    e = emotions or {}
    contentment = float(e.get("contentment", 0.0))

    out: Dict[str, float] = {}
    for drive_key, coeffs in EMOTION_MAP.items():
        total = 0.0
        for src, k in coeffs.items():
            if src == "_one_minus_contentment":
                total += (1.0 - contentment) * k
            else:
                total += float(e.get(src, 0.0)) * k
        out[drive_key] = _clamp(total)

    # duty 仅在有未完成任务时激活，否则回到 baseline（记挂无从谈起）
    if not has_pending_task:
        out["duty"] = BASELINE["duty"]

    return DriveState.from_dict(out)


# =============================================================================
# ② 三个核心机制：衰减 / pulse / satisfy
# =============================================================================

def decay(state: DriveState, dt: float) -> DriveState:
    """
    自然衰减：每一维按指数回归各自 baseline。
        value(t+dt) = baseline + (value - baseline) * exp(-rate * dt)
    dt 为自上一拍经过的秒数，由调用方传入（本模块不取系统时间）。
    dt <= 0 时原样返回。
    """
    if dt <= 0:
        return replace(state)
    new_vals: Dict[str, float] = {}
    for k in DRIVE_KEYS:
        base = BASELINE[k]
        cur = state.get(k)
        decayed = base + (cur - base) * math.exp(-DECAY_RATE[k] * dt)
        new_vals[k] = _clamp(decayed)
    return DriveState.from_dict(new_vals)


def pulse(state: DriveState, drive_key: str, amount: float) -> DriveState:
    """
    被感受引擎 / 经历喂数据时，给某一维一个增量。
    边际递减：gain = amount * PULSE_GAIN_SCALE * sqrt(1 - current)
    越接近顶越难涨，防止瞬间撞顶。amount 应为非负增量（如主人 0.18 / 自经历 0.10）。
    """
    if drive_key not in DRIVE_KEYS:
        raise KeyError(f"unknown drive_key: {drive_key!r}")
    if amount <= 0:
        return replace(state)
    cur = state.get(drive_key)
    headroom = max(0.0, VAL_MAX - cur)
    gain = amount * PULSE_GAIN_SCALE * math.sqrt(headroom)
    new_val = _clamp(cur + gain)
    return replace(state, **{drive_key: new_val})


def satisfy(state: DriveState, action: str) -> DriveState:
    """
    做完某 want_action 后的针对性回落：
        主驱动 ×SATISFY_MAIN_FACTOR（明显降）
        相关维度 ×SATISFY_REL_FACTOR（轻微沾光）
    避免卡在同一欲望死循环。未知 action 原样返回。
    """
    main = ACTION_MAIN_DRIVE.get(action)
    if main is None:
        return replace(state)

    updates: Dict[str, float] = {}
    updates[main] = _clamp(state.get(main) * SATISFY_MAIN_FACTOR)
    for rel in ACTION_REL_DRIVES.get(action, ()):  # 相关维度轻微回落
        base_val = updates.get(rel, state.get(rel))
        updates[rel] = _clamp(base_val * SATISFY_REL_FACTOR)

    return replace(state, **updates)


# =============================================================================
# ③ 召唤力 + pick_intent
# =============================================================================

def compute_scores(
    state: DriveState,
    thoughts: Optional[List[Thought]] = None,
) -> Dict[str, float]:
    """
    各维召唤力 score = 驱动条值 + 加成系数 × 关联执念强度。
    fatigue 是闸，不计入排序（不返回于 scores）。

    念头池本版未实现动力学：若传入 thoughts，仅按「关联维度的 fixation 强度求和」
    作为加成的占位实现；不传则加成为 0。加成系数留一个 CONST 位。
    """
    # 执念加成占位：本版加成系数为 0（念头池动力学尚未实现）。
    fixation_bonus: Dict[str, float] = {k: 0.0 for k in RANKED_KEYS}
    if thoughts:
        for t in thoughts:
            if t.kind == "fixation" and t.drive_key in fixation_bonus:
                # 预留：未来乘以加成系数。当前系数视为 0，故此处不真正加权。
                fixation_bonus[t.drive_key] += 0.0 * t.strength

    return {k: _clamp(state.get(k) + fixation_bonus[k], hi=math.inf)
            for k in RANKED_KEYS}


def pick_intent(
    state: DriveState,
    thoughts: Optional[List[Thought]] = None,
    has_pending_task: bool = False,
) -> Intent:
    """
    决定「此刻最想做的事」：
    - fatigue ≥ FATIGUE_GATE → 不硬找事，返回 "rest"（歇息态自省）。
    - 否则取召唤力最高的欲望维，映射到对应 want_action。
    - duty 若无未完成任务则不该被选中（正常映射里 duty 已回 baseline，此处再兜底）。

    thoughts / has_pending_task 为可选上下文。
    """
    # fatigue 闸优先
    if state.fatigue >= FATIGUE_GATE:
        return Intent(
            want_action="rest",
            drive_key="fatigue",
            reason=REST_REASON,
            score=state.fatigue,
            query_hint="",
        )

    scores = compute_scores(state, thoughts)

    # duty 兜底：无未完成任务不参与选择
    if not has_pending_task:
        scores = {k: v for k, v in scores.items() if k != "duty"}

    # 取最高分维度（稳定：分数相同按 RANKED_KEYS 顺序）
    best_key = max(scores, key=lambda k: (scores[k], -RANKED_KEYS.index(k)))
    best_score = scores[best_key]

    return Intent(
        want_action=DRIVE_ACTION[best_key],
        drive_key=best_key,
        reason=DRIVE_REASON[best_key],
        score=best_score,
        query_hint=DRIVE_QUERY_HINT.get(best_key, ""),
    )


# =============================================================================
# ④ 念头池接口（预留，暂不实现动力学）
# =============================================================================

def tick_thoughts(
    thoughts: List[Thought],
    now: float,
) -> List[Thought]:
    """
    念头池推进一拍（闪念衰减 / 执念加强 / 反哺 drive / 了却出池）。

    ⚠️ 本版仅预留接口，尚未实现动力学 —— 原样返回传入的念头池。
    未来实现时：now 由调用方传入（不取系统时间），保持纯函数。
    """
    return list(thoughts)
