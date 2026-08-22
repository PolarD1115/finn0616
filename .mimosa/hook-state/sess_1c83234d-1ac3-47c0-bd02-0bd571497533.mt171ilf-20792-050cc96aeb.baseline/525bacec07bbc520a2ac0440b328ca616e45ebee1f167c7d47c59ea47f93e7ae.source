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
import random as _random
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

# 鸭子类型：任何有 .random() 和 .choice(seq) 的对象（如 random.Random 实例）
_RandomLike = Any



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

# --- v2② 不应期 refractory ---
# 刚满足过的欲望，短时间内压着别立刻复燃（"刚做完别马上又馋"）。
# 纯 tick 计数实现（不用 wall-clock，契合纯函数原则）。
REFRACTORY_TICKS: int = 3            # satisfy 后该维度的冷却拍数

# --- v2③ wildcard 心血来潮 ---
# 不是独立 random 柱子，而是长在张力上的泄洪口：
# 总张力高、但前几名召唤力胶着 / 最高那个卡死时，从小候选集抽一件做，事后不可归因。
WILDCARD_TENSION_THRESHOLD: float = 0.55   # 总张力（参与排序维度的平均值）≥ 此值才有资格
WILDCARD_TIE_MARGIN: float = 0.08          # 前两名召唤力分差 < 此值 = 胶着
WILDCARD_STUCK_TICKS: int = 2              # 最高分 want_action 连续 ≥ 此拍数没变 = 卡死
WILDCARD_CANDIDATES: List[str] = ["reflect", "explore", "socialize", "rest"]  # 小候选集
WILDCARD_REASON: str = "说不上来……就突然想换个事做。"  # 第一人称、不可归因

# --- v2⑤ 自主心跳（张力 ↔ 间隔）---
# 心跳间隔不再固定：张力高→醒得勤、疲劳高→拉长；安静时段有 floor 不打扰。
HEARTBEAT_BASE_INTERVAL: int = 900      # 基准间隔（秒）= 15 分钟
HEARTBEAT_MIN_INTERVAL: int = 300       # 下限 = 5 分钟
HEARTBEAT_MAX_INTERVAL: int = 3600      # 上限 = 1 小时
HEARTBEAT_TENSION_GAIN: float = 0.4     # 张力越高→间隔越短
HEARTBEAT_FATIGUE_GAIN: float = 0.3     # 疲劳越高→间隔越长
HEARTBEAT_LOW_TENSION_REST: float = 0.3 # 低张力时额外拉长（歇息增益）
HEARTBEAT_QUIET_HOURS: Tuple[int, int] = (1, 7)  # 勿扰时段（本地时，左闭右开）
HEARTBEAT_QUIET_FLOOR: int = 2400       # 勿扰时段间隔下限（秒）= 40 分钟
HEARTBEAT_IDLE_MULT: float = 1.5        # 全员在不应期（没事可做）→ 间隔 ×此值

# --- v2④ 基线漂移（只作用于 attachment 想念地板）---
# 🛑 设计红线：想念可以涨，但永远不许变成压人的东西。
# baseline 只影响 attachment 地板，不影响其他维度。
# 安全阀不许省略、不许绕过、不许在后续迭代中移除。
BASELINE_HOME: float = 0.30           # attachment 的正常地板（家）
BASELINE_CAP: float = 0.45            # 地板涨到这里封顶（硬上限，任何路径不许突破）
BASELINE_RISE_PER_HOUR: float = 0.01  # 久没见，每小时地板涨 0.01
BASELINE_PULLBACK: float = 0.65       # 主人一次互动，把抬高的 floor 拉回 65% 朝 HOME

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

# --- v2① 耦合网（维度间联动）---
# 每条边：(source_key, target_key, coefficient, mode)
#   mode="level" : 每拍按源当前值持续施压   target += source × coeff
#   mode="delta" : 仅当源本拍有正向变化时激发 target += (source - prev_source) × coeff（仅正向）
# ⚠️ 耦合是反馈系统，会自激/震荡。两条防失控：
#   - 系数初值压小（|k| ≤ 0.06）；
#   - apply_coupling 里加全局阻尼：每拍所有维度向各自 baseline 回归一小步。
COUPLING_EDGES: List[Tuple[str, str, float, str]] = [
    ("stress",      "attachment",  +0.05, "level"),   # 压力大→更想念
    ("stress",      "curiosity",   -0.04, "level"),   # 压力大→好奇降
    ("attachment",  "libido",      +0.06, "delta"),   # 想念涨→亲密欲涨
    ("libido",      "attachment",  +0.03, "delta"),   # 亲密欲涨→想念涨
    ("fatigue",     "curiosity",   -0.05, "level"),   # 累→不想探索
    ("fatigue",     "social",      -0.04, "level"),   # 累→不想社交
    ("curiosity",   "reflection",  +0.04, "delta"),   # 好奇涨→想沉淀
    ("social",      "curiosity",   +0.03, "delta"),   # 社交涨→好奇涨
]

# 全局阻尼系数：每拍每维向 baseline 回归的比例（防自激发散）
COUPLING_DAMPING: float = 0.02


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
    is_wildcard: bool = False        # v2③ 本拍是否走了 wildcard（心血来潮，不可归因）


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
# v2② 不应期 refractory（纯 tick 计数，不用 wall-clock）
# =============================================================================
# 约定：refractory 是一个 Dict[str, int]（key=drive_key, value=剩余冷却拍数），
#       由调用方持久化。这里提供纯函数维护它，DriveState 保持只装 8 维数值。

def tick_refractory(refractory: Optional[Dict[str, int]]) -> Dict[str, int]:
    """
    每拍开头调用：所有冷却计数 -1，归零则删。返回新 dict（不改入参）。
    传 None 视为空。
    """
    if not refractory:
        return {}
    out: Dict[str, int] = {}
    for k, v in refractory.items():
        nv = int(v) - 1
        if nv > 0:
            out[k] = nv
    return out


def enter_refractory(refractory: Optional[Dict[str, int]],
                     drive_key: str,
                     ticks: int = REFRACTORY_TICKS) -> Dict[str, int]:
    """
    satisfy 某维度后调用：把该维度置入不应期（剩余 ticks 拍）。返回新 dict。
    fatigue 是闸不参与排序，置它无意义但也无害。
    """
    out: Dict[str, int] = dict(refractory or {})
    if ticks > 0:
        out[drive_key] = int(ticks)
    return out


def satisfy_with_refractory(state: DriveState,
                            action: str,
                            refractory: Optional[Dict[str, int]] = None,
                            ticks: int = REFRACTORY_TICKS,
                            ) -> Tuple[DriveState, Dict[str, int]]:
    """
    satisfy 的便捷组合：既做乘性回落，又把该 action 的主驱动置入不应期。
    返回 (新 DriveState, 新 refractory)。未知 action 时驱动条原样、refractory 不变。
    """
    new_state = satisfy(state, action)
    main = ACTION_MAIN_DRIVE.get(action)
    if main is None:
        return new_state, dict(refractory or {})
    return new_state, enter_refractory(refractory, main, ticks)


# =============================================================================
# v2① 耦合网（维度间联动）
# =============================================================================

def apply_coupling(
    state: DriveState,
    prev_state: DriveState,
    edges: List[Tuple[str, str, float, str]] = COUPLING_EDGES,
) -> DriveState:
    """
    让一维的涨落牵动其他维（一拍）。返回新的 DriveState。

    - level 模式：直接用「当前 source 值 × coeff」施加到 target。
    - delta 模式：算 (当前 source − 上一拍 source)，仅正向变化时施加 (delta × coeff)。
    - 施加完对所有维度做全局阻尼：每维向各自 baseline 回归一小步
      （target += COUPLING_DAMPING × (baseline − 当前值)），抑制自激/发散。
    - 最后统一 clamp 到 [0, 1]。

    prev_state 用于 delta 模式取「上一拍源值」。level 模式只看当前 state。
    """
    # 在一个可变 dict 上累加，避免边之间互相读到半更新的值
    acc: Dict[str, float] = dict(state.as_dict())

    for src, tgt, coeff, mode in edges:
        if src not in acc or tgt not in acc:
            continue
        if mode == "level":
            acc[tgt] += state.get(src) * coeff
        elif mode == "delta":
            rise = state.get(src) - prev_state.get(src)
            if rise > 0:
                acc[tgt] += rise * coeff
        # 未知 mode 静默跳过

    # 全局阻尼：每维向 baseline 回归一小步（防失控）
    for k in DRIVE_KEYS:
        acc[k] += COUPLING_DAMPING * (BASELINE[k] - acc[k])

    # 统一 clamp
    for k in DRIVE_KEYS:
        acc[k] = _clamp(acc[k])

    return DriveState.from_dict(acc)


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
    refractory: Optional[Dict[str, int]] = None,
    last_action: Optional[str] = None,
    action_repeat: int = 0,
    rng: Optional["_RandomLike"] = None,
) -> Intent:
    """
    决定「此刻最想做的事」：
    - fatigue ≥ FATIGUE_GATE → 不硬找事，返回 "rest"（歇息态自省）。
    - 处于不应期（refractory 里剩余拍数 > 0）的维度即使分最高也跳过，选次高的。
    - duty 若无未完成任务则不该被选中。
    - v2③ wildcard：总张力高、且（前两名胶着 或 最高动作卡死）时，
      从 WILDCARD_CANDIDATES 里随机抽一件（排除当前最高对应动作）做，
      返回 Intent.is_wildcard=True。调用方对 wildcard 结果**不应**调 satisfy。

    可选上下文：
    - refractory   : Dict[drive_key -> 剩余冷却拍数]，>0 的维度本拍跳过。
    - last_action  : 上一拍最终采用的 want_action（判卡死用）。
    - action_repeat: last_action 已连续保持的拍数（判卡死用）。
    - rng          : 随机源（有 .random() / .choice()），wildcard 抽签用；None 用模块 random。
    """
    # fatigue 闸优先（闸态不参与 wildcard / 不应期逻辑）
    if state.fatigue >= FATIGUE_GATE:
        return Intent(
            want_action="rest",
            drive_key="fatigue",
            reason=REST_REASON,
            score=state.fatigue,
            query_hint="",
            is_wildcard=False,
        )

    scores = compute_scores(state, thoughts)

    # duty 兜底：无未完成任务不参与选择
    if not has_pending_task:
        scores = {k: v for k, v in scores.items() if k != "duty"}

    # 不应期：冷却中的维度从候选里剔除（选次高）
    ref = refractory or {}
    eligible = {k: v for k, v in scores.items() if ref.get(k, 0) <= 0}
    # 若所有维度都在冷却，退回到全体（避免无可选；此时不应期整体失效一拍）
    pool = eligible if eligible else scores

    # 排序（稳定：分数相同按 RANKED_KEYS 顺序）
    ranked = sorted(pool, key=lambda k: (pool[k], -RANKED_KEYS.index(k)), reverse=True)
    best_key = ranked[0]
    best_score = pool[best_key]
    best_action = DRIVE_ACTION[best_key]

    # ---- v2③ wildcard 判定 ----
    # 总张力：参与排序的全部维度平均值（fatigue 已不在其中）
    tension = sum(scores.values()) / len(scores) if scores else 0.0
    tie = False
    if len(ranked) >= 2:
        tie = (pool[ranked[0]] - pool[ranked[1]]) < WILDCARD_TIE_MARGIN
    stuck = (last_action is not None
             and last_action == best_action
             and action_repeat >= WILDCARD_STUCK_TICKS)

    if tension >= WILDCARD_TENSION_THRESHOLD and (tie or stuck):
        _r = rng if rng is not None else _random
        # 候选集里排除「当前最高对应的动作」
        cands = [a for a in WILDCARD_CANDIDATES if a != best_action]
        if cands:
            picked = _r.choice(cands)
            return Intent(
                want_action=picked,
                drive_key=best_key,          # 记一下张力来源维，但动作不由它决定
                reason=WILDCARD_REASON,
                score=best_score,
                query_hint="",
                is_wildcard=True,
            )

    return Intent(
        want_action=best_action,
        drive_key=best_key,
        reason=DRIVE_REASON[best_key],
        score=best_score,
        query_hint=DRIVE_QUERY_HINT.get(best_key, ""),
        is_wildcard=False,
    )


# =============================================================================
# v2⑤ 自主心跳（张力 ↔ 间隔）
# =============================================================================

def compute_heartbeat_interval(
    state: DriveState,
    fatigue: float,
    local_hour: int,
    refractory: Optional[Dict[str, int]] = None,
) -> int:
    """
    根据当下张力 / 疲劳 / 时段算出下一次心跳醒来的间隔（秒）。纯函数。

    - tension = 参与排序维度（排除 fatigue）的均值。
    - interval = BASE × (1 + LOW_TENSION_REST×(1-tension)
                            − TENSION_GAIN×tension
                            + FATIGUE_GAIN×fatigue)
      直觉：张力高→醒得勤（间隔短）；疲劳高→拉长；低张力→额外歇息拉长。
    - clamp 到 [MIN, MAX]。
    - 勿扰时段（local_hour ∈ QUIET_HOURS，左闭右开）：interval = max(interval, QUIET_FLOOR)。
    - 全员在不应期（refractory 覆盖所有非 fatigue 维度）：interval × IDLE_MULT（没事可做就歇歇）。
      注意：先应用 idle 放大，再对勿扰 floor 兜底，最后仍夹在 [MIN, MAX] 内。

    fatigue 由调用方单独传入（与 state.fatigue 解耦，便于调用方传 display 里的疲劳值）。
    local_hour 为本地小时 0..23，由调用方传入（本模块不取系统时间）。
    """
    # 总张力：参与排序维度均值（fatigue 不算）
    tension = sum(state.get(k) for k in RANKED_KEYS) / len(RANKED_KEYS)
    tension = _clamp(tension)
    fatigue = _clamp(fatigue)

    factor = (1.0
              + HEARTBEAT_LOW_TENSION_REST * (1.0 - tension)
              - HEARTBEAT_TENSION_GAIN * tension
              + HEARTBEAT_FATIGUE_GAIN * fatigue)
    interval = HEARTBEAT_BASE_INTERVAL * factor

    # 全员不应期（没事可做）→ 拉长
    ref = refractory or {}
    if all(ref.get(k, 0) > 0 for k in RANKED_KEYS):
        interval *= HEARTBEAT_IDLE_MULT

    # 先 clamp 到 [MIN, MAX]
    interval = min(max(interval, HEARTBEAT_MIN_INTERVAL), HEARTBEAT_MAX_INTERVAL)

    # 勿扰时段：不低于 floor（floor 可能高于上面 clamp 后的值，故最后兜底）
    lo, hi = HEARTBEAT_QUIET_HOURS
    in_quiet = (lo <= local_hour < hi)
    if in_quiet:
        interval = max(interval, HEARTBEAT_QUIET_FLOOR)

    # 最终仍夹在 [MIN, MAX]（QUIET_FLOOR 已保证 ≤ MAX，稳妥再夹一次）
    interval = min(max(interval, HEARTBEAT_MIN_INTERVAL), HEARTBEAT_MAX_INTERVAL)
    return int(round(interval))


# =============================================================================
# v2④ 基线漂移（只作用于 attachment 想念地板）
# =============================================================================
# 🛑 设计红线：想念可以涨，但永远不许变成压人的东西。
# baseline 只影响 attachment 地板，不影响其他维度。
# 安全阀不许省略、不许绕过、不许在后续迭代中移除。

def drift_baseline(current_baseline: float, hours_since_interaction: float) -> float:
    """
    久没见→想得更浓：attachment 地板随「距上次互动的小时数」缓慢抬高。
        new = current_baseline + RISE_PER_HOUR × hours_since_interaction
    🛑 安全阀：结果必过 clamp(HOME, CAP)，任何路径不许突破 CAP(0.45)、不许跌破 HOME(0.30)。
    hours_since_interaction < 0 视为 0（不倒扣）。
    """
    hrs = max(0.0, hours_since_interaction)
    new = current_baseline + BASELINE_RISE_PER_HOUR * hrs
    # 🛑 硬封顶 + 保底：这一行是安全阀，不许省略、不许绕过。
    return _clamp(new, BASELINE_HOME, BASELINE_CAP)


def pullback_baseline(current_baseline: float) -> float:
    """
    主人一次互动，把抬高的 floor 拉回 65% 朝 HOME（一抱拉回大半）。
        result = current_baseline - PULLBACK × (current_baseline - HOME)
    🛑 安全阀：结果必过 clamp(HOME, CAP)；且数学上保证 result ≤ 传入值（不许反弹）、
       result ≥ HOME（不许跌破家）。
    """
    result = current_baseline - BASELINE_PULLBACK * (current_baseline - BASELINE_HOME)
    # 🛑 clamp 是安全阀，不许省略、不许绕过。
    return _clamp(result, BASELINE_HOME, BASELINE_CAP)


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
