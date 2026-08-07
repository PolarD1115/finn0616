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
      → [DESIRE_COUPLING 开] apply_coupling（维度间联动一拍）
      → desire_engine.pick_intent()
      → 存回状态 → 返回 DesireSnapshot

gating（灰度原则，默认关）：
    DESIRE_DRIVEN=false（默认）：只算 + 只存快照，返回 intent 供观测，
                                但调用方**不应**用它覆盖行为。
    DESIRE_DRIVEN=true         ：调用方按 intent.want_action 覆盖「想做的事」。

状态存储（desire_state 表，key-value 结构，与 user_facts 同构）：
    ⚠️ 原先寄居在 user_facts，因高频读写拖累 prompt 缓存前缀、且被画像查询误当"用户画像"
       注入上下文，v3.8 起独立到 desire_state 表。表名见本模块 STATE_TABLE 常量。
    desire_emotion_state   16 维情感引擎完整状态 JSON
    desire_drive_state     8 维驱动条快照 JSON（含上一拍值，用于算 pulse 增量）
    desire_events_queue    待情感引擎消费的事件队列 JSON
    desire_refractory      不应期计数 Dict[str,int]（v2②）
    desire_last_action     上一拍最终 want_action（v2③ 卡死判定）
    desire_action_repeat   连续相同动作的拍数（v2③ 卡死判定）
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
REFRACTORY_KEY = "desire_refractory"          # v2② 不应期计数 Dict[str,int]
LAST_ACTION_KEY = "desire_last_action"        # v2③ 上一拍 want_action
ACTION_REPEAT_KEY = "desire_action_repeat"    # v2③ 连续相同动作拍数
NEXT_HEARTBEAT_KEY = "desire_next_heartbeat_at"  # v2⑤ 下次心跳醒来的时间戳（毫秒）
ATTACHMENT_BASELINE_KEY = "desire_attachment_baseline"  # v2④ attachment 当前地板值（初始=HOME）
LAST_TICK_KEY = "desire_last_tick_at"          # 上次 tick 的时间戳（毫秒），算 decay 的 dt
LAST_SLEEP_WAKE_KEY = "desire_last_sleep_wake_ms"  # v2⑥ 已处理过的设备睡眠 sleepWakeupMs（去重，防重复注入同一觉）

# pulse 增量：主人来的情感冲击 vs 自经历（对齐攻略：自经历更小）
PULSE_FROM_USER = 0.18
PULSE_FROM_SELF = 0.10


def is_desire_driven() -> bool:
    """总闸：欲望是否真覆盖行为。默认关（只读可看不动手）。"""
    return os.environ.get("DESIRE_DRIVEN", "false").strip().lower() in ("1", "true", "yes", "on")


def is_desire_coupling() -> bool:
    """v2① 耦合网开关。默认关（关着时 tick 跳过 apply_coupling）。"""
    return os.environ.get("DESIRE_COUPLING", "false").strip().lower() in ("1", "true", "yes", "on")


def is_heartbeat_autonomy() -> bool:
    """v2⑤ 自主心跳开关。默认关（关着时返回固定 BASE 间隔，不做动态计算）。"""
    return os.environ.get("HEARTBEAT_AUTONOMY", "false").strip().lower() in ("1", "true", "yes", "on")


def is_baseline_drift() -> bool:
    """v2④ 基线漂移开关。默认关（关着时 attachment 地板固定在 HOME，不漂移）。"""
    return os.environ.get("DESIRE_BASELINE_DRIFT", "false").strip().lower() in ("1", "true", "yes", "on")


def is_sleep_from_device() -> bool:
    """
    v2⑥ 设备睡眠同步开关。默认关。
    开：心跳 tick 前从 device_data 最新一条读真实睡眠（sleepStartMs/sleepWakeupMs），
        检测到「新的一觉」时向情感引擎注入 sleep_start + sleep_end 事件，
        让疲惫值按真实睡眠时长回血 / 起床点重算。
    关：不读设备，疲惫仍走「清醒时长 + 昼夜节律」的默认估算。
    """
    return os.environ.get("SLEEP_FROM_DEVICE", "false").strip().lower() in ("1", "true", "yes", "on")


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


# 引擎状态专用表（原先寄居在 user_facts，因其高频读写拖累 prompt 缓存前缀而独立出来）。
# 结构与 user_facts 对齐（key/value/confidence），额外多一列 updated_at 便于观测 tick 频率。
STATE_TABLE = "desire_state"


def _load_fact(key: str) -> Optional[Any]:
    sb = _get_supabase()
    if not sb:
        return None
    try:
        r = sb.table(STATE_TABLE).select("value").eq("key", key).execute()
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
        sb.table(STATE_TABLE).upsert(
            {
                "key": key,
                "value": json.dumps(value, ensure_ascii=False),
                "confidence": 1.0,
                "updated_at": _dt_now_iso(),
            },
            on_conflict="key",
        ).execute()
    except Exception as e:
        print(f"[Desire] save {key} failed: {e}")


def _dt_now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


DESIRE_TRACE_TAG = "Desire_Trace"   # 曲线留痕专用标签；importance=1，两天后被深梦清理协程自动扫掉


def _save_desire_trace(drive: Dict[str, float], intent: de.Intent,
                       att_baseline: float, now_ms: float) -> None:
    """
    往 memories 追加一条驱动条快照留痕。
    DRIVE_STATE_KEY 是覆盖写、历史全丢；整定系数需要曲线，这里逐拍落一条。
    - tags=Desire_Trace，importance=1（STREAM 级）→ heartbeat._perform_deep_dreaming
      里 importance < 4 的清理协程两天后自动扫掉，不堆积。
    - content 存 8 维值 + intent 的 JSON，供离线取数画曲线。
    """
    sb = _get_supabase()
    if not sb:
        return
    try:
        import datetime as _dt
        payload = {
            "drive": drive,
            "intent": {
                "want_action": intent.want_action,
                "drive_key": intent.drive_key,
                "score": intent.score,
                "is_wildcard": intent.is_wildcard,
            },
            "attachment_baseline": att_baseline,
        }
        sb.table("memories").insert({
            "title": "💗 欲望驱动留痕",
            "content": json.dumps(payload, ensure_ascii=False),
            "category": "流水",
            "mood": "平静",
            "tags": DESIRE_TRACE_TAG,
            "importance": 1,
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"[Desire] save trace failed: {e}")


# =============================================================================
# 消息语义分类（LLM）—— 情感引擎的事件入口
# =============================================================================
# emotion_engine 只消费 {label, confidence}，不自己分类。这里补上「生产」这一环：
# 调一个便宜的 LLM 把用户消息判成 16 类之一 + 置信度，再塞进事件队列。
# 设计原则：
#   - 失败绝不阻塞正常回复：任何异常 → 降级为 ("neutral", 0.5)。
#   - 用便宜模型（默认 silicon1），分类不值得烧主对话模型的钱。
#   - 只做语义判断，不做关键词匹配（标签本身就是语义级的）。

# 16 个合法标签（必须与 emotion_engine.LABEL_DELTAS 的键完全一致）
CLASSIFY_LABELS = tuple(ee.LABEL_DELTAS.keys())

# 分类用的 LLM provider。默认 deepseek（V4 Flash，便宜快）。
# 可用环境变量 CLASSIFY_PROVIDER 覆盖（如改回 silicon1 / main_chat）。
def _classify_provider() -> str:
    return os.environ.get("CLASSIFY_PROVIDER", "deepseek").strip() or "deepseek"

_CLASSIFY_SYSTEM = (
    "你是一个消息情感分类器。给定用户发来的一条（或合并的多条）消息，"
    "判断它相对于亲密伴侣关系的情感基调，从下面固定的标签集合里选**恰好一个**最贴切的：\n"
    "- affectionate 亲昵示爱 / playful 玩闹调侃 / vulnerable 示弱倾诉 / reassuring 安抚承诺\n"
    "- cold 冷淡敷衍 / conflict 争执冲突 / distant 疏远回避 / struggling 自己正陷入困境\n"
    "- intimate_reference 提及亲密 / intimate_event 描述亲密行为\n"
    "- neutral 中性日常 / hostile 敌意攻击\n"
    "- fear_separation 害怕分离被抛弃 / fear_death 害怕生死 / fear_concern 担心对方安危 / fear_general 泛化的害怕不安\n"
    "只输出一行 JSON，不要任何多余文字：\n"
    '{"label": "上面标签之一的英文名", "confidence": 0.0到1.0之间的小数}\n'
    "confidence 表示你有多确定：语气明确给 0.8~1.0，含糊或中性给 0.4~0.6。"
)


def _thinking_extra_body(model_name: str) -> Dict[str, Any]:
    """
    DeepSeek V4 系列（v4-flash / v4-pro / deepseek-chat 等）默认开启 thinking，
    分类任务不需要思维链，显式关闭以省钱提速。
    官方写法：OpenAI SDK 下 extra_body={"thinking": {"type": "disabled"}}。
    仅对 deepseek 系模型下发该字段，其他模型不传（免得报未知参数）。
    """
    m = (model_name or "").lower()
    if "deepseek" in m or "v4-flash" in m or "v4-pro" in m:
        return {"thinking": {"type": "disabled"}}
    return {}


def classify_message_sync(text: str, provider: Optional[str] = None) -> Dict[str, Any]:
    """
    同步版消息分类（供 asyncio.to_thread 包裹调用）。
    返回 {"label": <str>, "confidence": <float>}；任何失败降级为中性。
    text 为空 → 直接返回中性，不调模型。

    provider 默认取环境变量 CLASSIFY_PROVIDER（默认 "deepseek"）。
    DeepSeek V4 默认开 thinking，本函数会显式关闭（见 _thinking_extra_body）。
    """
    text = (text or "").strip()
    if not text:
        return {"label": "neutral", "confidence": 0.5}

    prov = provider or _classify_provider()
    try:
        from server import _get_llm_client
        client = _get_llm_client(prov)
        # 分类模型没配就回退主对话模型；再没有就降级中性
        if not client:
            client = _get_llm_client("main_chat")
        if not client:
            return {"label": "neutral", "confidence": 0.5}

        model_name = getattr(client, "custom_model_name", "gpt-3.5-turbo")
        # 截断超长消息，分类只需前若干字符即可判基调
        snippet = text[:500]
        create_kwargs: Dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": snippet},
            ],
            "temperature": 0.0,
        }
        # DeepSeek V4 关闭 thinking（非思考模式支持 temperature，安全）
        extra = _thinking_extra_body(model_name)
        if extra:
            create_kwargs["extra_body"] = extra
        resp = client.chat.completions.create(**create_kwargs)
        raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        return _parse_classification(raw)
    except Exception as e:
        print(f"[Desire] classify failed, fallback neutral: {e}")
        return {"label": "neutral", "confidence": 0.5}


def _parse_classification(raw: str) -> Dict[str, Any]:
    """从 LLM 原始输出里抠出 {label, confidence}，容错到底：解析不出就中性。"""
    import re as _re
    if not raw:
        return {"label": "neutral", "confidence": 0.5}
    # 剥离可能的 <think> 块与 ```json 围栏
    raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL | _re.IGNORECASE)
    m = _re.search(r"\{.*\}", raw, flags=_re.DOTALL)
    label = "neutral"
    confidence = 0.5
    if m:
        try:
            obj = json.loads(m.group(0))
            label = str(obj.get("label", "neutral")).strip()
            confidence = float(obj.get("confidence", 0.5))
        except Exception:
            pass
    # 校验标签合法，非法则降级中性（confidence 也压低）
    if label not in CLASSIFY_LABELS:
        label = "neutral"
        confidence = min(confidence, 0.5)
    # 夹到 [0,1]
    confidence = max(0.0, min(1.0, confidence))
    return {"label": label, "confidence": confidence}


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


async def record_user_message(text: str, message_id: Optional[str] = None) -> None:
    """
    收到用户消息时调用（异步，供消息处理路径 await）：
      分类（LLM，跑在线程池里不阻塞事件循环）→ enqueue 一个 msg_user 事件（带分类结果）。
    任何失败都吞掉、只打日志——分类挂了绝不能影响正常回复。
    """
    import asyncio as _asyncio
    try:
        classified = await _asyncio.to_thread(classify_message_sync, text)
        await _asyncio.to_thread(
            enqueue_event, "msg_user",
            {"message_id": message_id, "text_len": len((text or ""))},
            classified,
        )
        print(f"💗 [欲望驱动] 用户消息已分类入队 label={classified['label']} "
              f"conf={classified['confidence']:.2f}")
    except Exception as e:
        print(f"💗 [欲望驱动] record_user_message 跳过：{e}")


async def record_assistant_message(message_id: Optional[str] = None) -> None:
    """
    AI 回复发出后调用（异步）：enqueue 一个 msg_assistant 事件。
    情感引擎用它开「未回复线程」计时（超时逐级加焦虑）。失败吞掉。
    """
    import asyncio as _asyncio
    try:
        await _asyncio.to_thread(
            enqueue_event, "msg_assistant", {"message_id": message_id}, None
        )
    except Exception as e:
        print(f"💗 [欲望驱动] record_assistant_message 跳过：{e}")


# =============================================================================
# v2⑥ 设备睡眠同步（device_data → sleep_start / sleep_end 事件）
# =============================================================================
# 设备端每晚给出「一觉」的汇总：sleepStartMs / sleepWakeupMs（epoch 毫秒）+ 总时长。
# 情感引擎的 sleep_start / sleep_end 事件正好吃这个格式的时间戳。
# 本函数把「设备记录的真实睡眠」翻成一对事件塞进队列，让疲惫按真实睡眠回血。
#
# 设计原则（对齐项目一贯的灰度 + 防御风格）：
#   - 默认关（SLEEP_FROM_DEVICE）；关着时整段跳过。
#   - 去重：记住已处理的 sleepWakeupMs，同一觉不重复注入。
#   - 合理性校验：时长必须落在 [MIN, MAX] 小时；起 < 止；醒来不在未来。异常直接丢弃。
#   - 事件按时间顺序 prepend 到队列头部（睡觉发生在这拍其它事件之前）。
#   - 任何异常都吞掉、只打日志：设备读挂了绝不能影响正常心跳。

SLEEP_MIN_HOURS = 1.0    # 短于此视为噪声（小憩/误报），丢弃
SLEEP_MAX_HOURS = 16.0   # 长于此视为脏数据，丢弃
SLEEP_FUTURE_TOLERANCE_MS = 6 * 3600_000.0  # 醒来时间最多可比 now 晚这么多（容设备/时区小偏差）


def _health_data_of_latest_device_row() -> Optional[Dict[str, Any]]:
    """
    读 device_data 最新一条的 health_data（JSON 字符串 → dict）。
    无库 / 无数据 / 解析失败 → None。
    ⚠️ 设备数据是外部不可信来源，这里只按已知字段名取值，不执行其中任何内容。
    """
    sb = _get_supabase()
    if not sb:
        return None
    try:
        r = (sb.table("device_data").select("health_data")
             .order("id", desc=True).limit(1).execute())
        rows = r.data or []
        if not rows:
            return None
        raw = rows[0].get("health_data")
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
    except Exception as e:
        print(f"😴 [设备睡眠] 读取 device_data 失败：{e}")
    return None


def sync_sleep_from_device(now_ms: Optional[float] = None) -> bool:
    """
    从设备最新健康数据同步「一觉」到情感引擎事件队列。

    返回 True 表示本次真的注入了一对睡眠事件；False 表示没有可同步的新睡眠
    （开关关 / 无数据 / 已处理过 / 校验不过）。

    注入的事件（prepend 到队列头，保证时间序在其它事件之前）：
      sleep_start  timestamp = sleepStartMs
      sleep_end    timestamp = sleepWakeupMs
    情感引擎 process_event 会据此把 last_sleep_duration_hours 设为真实时长、
    last_wake_at 设为真实醒来时刻，疲惫从真实起床点重算。
    """
    if not is_sleep_from_device():
        return False
    if now_ms is None:
        now_ms = time.time() * 1000.0

    health = _health_data_of_latest_device_row()
    if not health:
        return False

    start_ms = health.get("sleepStartMs")
    wake_ms = health.get("sleepWakeupMs")
    if not isinstance(start_ms, (int, float)) or not isinstance(wake_ms, (int, float)):
        return False
    start_ms = float(start_ms)
    wake_ms = float(wake_ms)

    # 合理性校验：起 < 止、时长在 [MIN, MAX] 小时、醒来不在（过分的）未来
    if wake_ms <= start_ms:
        print(f"😴 [设备睡眠] 起止异常（wake<=start），丢弃：start={start_ms} wake={wake_ms}")
        return False
    dur_h = (wake_ms - start_ms) / ee.MS_PER_HOUR
    if dur_h < SLEEP_MIN_HOURS or dur_h > SLEEP_MAX_HOURS:
        print(f"😴 [设备睡眠] 时长 {dur_h:.1f}h 超出 [{SLEEP_MIN_HOURS},{SLEEP_MAX_HOURS}]，丢弃")
        return False
    if wake_ms > now_ms + SLEEP_FUTURE_TOLERANCE_MS:
        print(f"😴 [设备睡眠] 醒来时间在未来，疑似脏数据，丢弃：wake={wake_ms} now={now_ms}")
        return False

    # 去重：同一觉（相同 sleepWakeupMs）只注入一次
    last_wake = _load_fact(LAST_SLEEP_WAKE_KEY)
    if isinstance(last_wake, (int, float)) and abs(float(last_wake) - wake_ms) < 1000.0:
        return False

    # 组装一对事件（sleep_start 在前、sleep_end 在后），prepend 到队列头
    start_iso = ee._ms_to_iso(start_ms)
    wake_iso = ee._ms_to_iso(wake_ms)
    rid = os.urandom(3).hex()
    sleep_events = [
        {
            "event_id": f"evt_sleep_start_{int(wake_ms)}_{rid}",
            "timestamp": start_iso,
            "type": "sleep_start",
            "payload": {"source": "device", "duration_hours": round(dur_h, 2)},
        },
        {
            "event_id": f"evt_sleep_end_{int(wake_ms)}_{rid}",
            "timestamp": wake_iso,
            "type": "sleep_end",
            "payload": {"source": "device", "duration_hours": round(dur_h, 2)},
        },
    ]
    q = _load_fact(EVENTS_QUEUE_KEY) or []
    q = sleep_events + q  # prepend：睡觉发生在这拍其它事件之前
    _save_fact(EVENTS_QUEUE_KEY, q)
    _save_fact(LAST_SLEEP_WAKE_KEY, wake_ms)
    print(f"😴 [设备睡眠] 已注入一觉：{start_iso} → {wake_iso}（{dur_h:.1f}h）")
    return True


# =============================================================================
# 快照数据
# =============================================================================

class DesireSnapshot:
    """一拍的结果，供心跳 / 前端使用。"""

    def __init__(self, driven: bool, drive: Dict[str, float],
                 scores: Dict[str, float], intent: de.Intent,
                 display: Dict[str, float],
                 refractory: Optional[Dict[str, int]] = None,
                 heartbeat_interval: Optional[int] = None,
                 next_heartbeat_at: Optional[float] = None,
                 attachment_baseline: Optional[float] = None):
        self.driven = driven          # DESIRE_DRIVEN 是否开
        self.drive = drive            # 8 维驱动条当前值
        self.scores = scores          # 各维召唤力（fatigue 不计）
        self.intent = intent          # pick_intent 结果
        self.display = display        # 16 维情感 display（来源）
        self.refractory = refractory or {}   # v2② 当前冷却中的维度 -> 剩余拍数
        self.heartbeat_interval = heartbeat_interval  # v2⑤ 下次心跳间隔（秒）
        self.next_heartbeat_at = next_heartbeat_at    # v2⑤ 下次醒来时间戳（毫秒）
        self.attachment_baseline = attachment_baseline  # v2④ attachment 当前地板

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
                "is_wildcard": self.intent.is_wildcard,
            },
            "display": self.display,
            "refractory": self.refractory,
            "heartbeat_interval": self.heartbeat_interval,
            "next_heartbeat_at": self.next_heartbeat_at,
            "attachment_baseline": self.attachment_baseline,
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

    # 0) 不应期 / 卡死跨拍状态。
    #    consume_events=True 才真正「推进一拍」：refractory 计数 -1、写回 last_action/repeat。
    #    只读观测（consume_events=False）用加载值算 intent，但不推进、不写回（免污染计数）。
    refractory = _load_fact(REFRACTORY_KEY) or {}
    if consume_events:
        refractory = de.tick_refractory(refractory)
    last_action = _load_fact(LAST_ACTION_KEY)  # str 或 None
    try:
        action_repeat = int(_load_fact(ACTION_REPEAT_KEY) or 0)
    except Exception:
        action_repeat = 0

    # 1) 加载情感状态 + 事件队列
    emo_state = _load_fact(EMOTION_STATE_KEY)
    if not emo_state:
        emo_state = ee.create_initial_state(now_ms, tz)

    # 1.2) v2⑥ 设备睡眠同步（默认关）：在读事件队列之前注入，本拍即消费。
    #      只在真正推进一拍时同步（consume_events=True）；只读观测不动队列。
    #      内部含开关判断 + 去重 + 校验，任何异常吞掉不影响心跳。
    if consume_events:
        try:
            sync_sleep_from_device(now_ms)
        except Exception as _se:
            print(f"😴 [设备睡眠] 同步跳过：{_se}")

    events: List[Dict[str, Any]] = []
    if consume_events:
        events = _load_fact(EVENTS_QUEUE_KEY) or []

    # 1.5) v2④ 基线漂移（只作用于 attachment 地板；默认关）。
    # 🛑 设计红线：想念可以涨，但永远不许变成压人的东西。只影响 attachment 地板。
    #    - 开关关：地板固定在 HOME，不漂移。
    #    - 开关开：按「距上次互动的小时数」抬高地板（drift_baseline 内含 clamp 安全阀）；
    #             本拍若有 msg_user 事件（主人来了），一抱拉回大半（pullback_baseline）。
    #    只读观测（consume_events=False）不写回，避免污染地板。
    try:
        att_baseline = float(_load_fact(ATTACHMENT_BASELINE_KEY))
    except (TypeError, ValueError):
        att_baseline = de.BASELINE_HOME

    if is_baseline_drift():
        # 距上次互动多久（小时）。用情感引擎里维护的 last_interaction_at。
        last_it = emo_state.get("last_interaction_at")
        if last_it:
            hours_since = max(0.0, (now_ms - ee._ms(last_it)) / ee.MS_PER_HOUR)
        else:
            hours_since = 0.0
        att_baseline = de.drift_baseline(att_baseline, hours_since)
        # 本拍收到用户消息 → 拉回（在 drift 之后应用，"一抱拉回大半"）
        has_user_msg = any(ev.get("type") == "msg_user" for ev in events)
        if has_user_msg:
            att_baseline = de.pullback_baseline(att_baseline)
    else:
        # 开关关：地板固定在 HOME（安全默认）
        att_baseline = de.BASELINE_HOME

    # 2) 情感引擎推进（纯函数）→ 拿 16 维 display
    new_emo = ee.tick_evolve(emo_state, events, now_ms, tz_offset=tz)
    display = new_emo["display"]

    # 3) 16 维 → 8 维映射
    mapped = de.map_from_emotions(display, has_pending_task=has_pending_task)

    # 4) pulse：把「本拍情感相对上一拍的正向冲击」喂进驱动条（边际递减）
    #    上一拍驱动值从 DRIVE_STATE_KEY 读；首次则以 mapped 作基线（无 pulse）。
    prev = _load_fact(DRIVE_STATE_KEY)
    if prev and isinstance(prev.get("drive"), dict):
        prev_drive_state = de.DriveState.from_dict(prev["drive"])

        # 先按真实经过时间做自然衰减，再叠 pulse。
        # 缺这一步驱动条只涨不跌，只能靠 satisfy 硬砸，曲线会变成锯齿。
        # ⚠️ prev_drive_state 保持衰减「前」的值：耦合网 delta 模式靠它算源的前值，
        #    用衰减后的值会把 delta 吃掉。这里另用 drive_state 承接衰减结果。
        last_tick = _load_fact(LAST_TICK_KEY)
        if isinstance(last_tick, (int, float)):
            dt = max(0.0, (now_ms - float(last_tick)) / 1000.0)
            drive_state = de.decay(prev_drive_state, dt)
        else:
            drive_state = prev_drive_state
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
        # 首拍：直接以映射值为基线；耦合的 delta 模式无「上一拍」，以自身作 prev（delta=0）
        prev_drive_state = mapped
        drive_state = mapped

    # 4.5) v2① 耦合网（灰度，默认关）：pulse 之后、pick_intent 之前。
    #      delta 模式取「上一拍存储的驱动快照」prev_drive_state 作为源的前值。
    if is_desire_coupling():
        drive_state = de.apply_coupling(drive_state, prev_drive_state)

    # 4.6) v2④ 地板兜底：attachment 不许低于当前地板 att_baseline（这就是「地板」的意义）。
    # 🛑 只作用于 attachment 一维；开关关时 att_baseline == HOME，仍保证不跌破 HOME。
    #    放在 pulse + 耦合之后：无论前面把 attachment 拉到多低，都被地板托住。
    if drive_state.attachment < att_baseline:
        drive_state = de.replace(drive_state, attachment=att_baseline)

    # 5) pick_intent（带不应期跳过 + wildcard 判定）
    intent = de.pick_intent(
        drive_state,
        has_pending_task=has_pending_task,
        refractory=refractory,
        last_action=last_action,
        action_repeat=action_repeat,
    )
    scores = de.compute_scores(drive_state)

    # 更新连续相同动作拍数（wildcard 卡死判定用）
    if intent.want_action == last_action:
        action_repeat += 1
    else:
        action_repeat = 1
    last_action = intent.want_action

    # 5.5) v2⑤ 自主心跳：算下次醒来的间隔。
    #      开关关（默认）→ 固定 BASE 间隔，不做动态计算。
    if is_heartbeat_autonomy():
        local_hr = int(ee.local_hour(now_ms, tz))
        heartbeat_interval = de.compute_heartbeat_interval(
            drive_state,
            fatigue=display.get("fatigue", drive_state.fatigue),
            local_hour=local_hr,
            refractory=refractory,
        )
    else:
        heartbeat_interval = de.HEARTBEAT_BASE_INTERVAL
    next_heartbeat_at = now_ms + heartbeat_interval * 1000.0

    # 6) 存回状态
    if consume_events:
        _save_fact(EMOTION_STATE_KEY, new_emo)
        _save_fact(EVENTS_QUEUE_KEY, [])  # 清空已消费队列
        _save_fact(REFRACTORY_KEY, refractory)
        _save_fact(LAST_ACTION_KEY, last_action)
        _save_fact(ACTION_REPEAT_KEY, action_repeat)
        _save_fact(NEXT_HEARTBEAT_KEY, next_heartbeat_at)
        _save_fact(ATTACHMENT_BASELINE_KEY, att_baseline)  # v2④ 写回当前地板
        _save_fact(LAST_TICK_KEY, now_ms)                  # decay 的 dt 基准
        _save_desire_trace(drive_state.as_dict(), intent, att_baseline, now_ms)  # 曲线留痕（两天后自动清）
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
        refractory=refractory,
        heartbeat_interval=heartbeat_interval,
        next_heartbeat_at=next_heartbeat_at,
        attachment_baseline=att_baseline,
    )


def satisfy_action(action: str) -> None:
    """
    做完某 want_action 后，对驱动条做针对性回落并让对应维度进入不应期。
    调用方在真正执行了 intent.want_action 对应的行为后调用。

    ⚠️ wildcard（is_wildcard=True）触发的动作**不应**调用本函数
       （"说不上来就突然想"，不可归因），由调用方判断后跳过。
    """
    prev = _load_fact(DRIVE_STATE_KEY)
    if not prev or not isinstance(prev.get("drive"), dict):
        return
    drive_state = de.DriveState.from_dict(prev["drive"])
    refractory = _load_fact(REFRACTORY_KEY) or {}

    # 乘性回落 + 主驱动进入不应期
    drive_state, refractory = de.satisfy_with_refractory(drive_state, action, refractory)

    # 🛑 v2④ 地板兜底：satisfy 不许把 attachment 挖穿地板。
    #    intimacy 动作的相关维度正好是 attachment（乘 SATISFY_REL_FACTOR），
    #    没有这道兜底会从下面把想念地板踩穿——想念的地板不该被「刚做完亲密」踩下去。
    try:
        att_baseline = float(_load_fact(ATTACHMENT_BASELINE_KEY))
    except (TypeError, ValueError):
        att_baseline = de.BASELINE_HOME
    if drive_state.attachment < att_baseline:
        drive_state = de.replace(drive_state, attachment=att_baseline)

    _save_fact(DRIVE_STATE_KEY, {
        "drive": drive_state.as_dict(),
        "snapshot_at": ee._ms_to_iso(time.time() * 1000.0),
    })
    _save_fact(REFRACTORY_KEY, refractory)


def read_snapshot(now_ms: Optional[float] = None,
                  has_pending_task: bool = False) -> DesireSnapshot:
    """只读观测：不消费事件、不改情感状态（供 /state 接口 / 前端面板）。"""
    return tick(now_ms=now_ms, has_pending_task=has_pending_task, consume_events=False)


# =============================================================================
# v2⑤ 自主心跳调度辅助
# =============================================================================

def seconds_until_next_heartbeat(now_ms: Optional[float] = None) -> Optional[int]:
    """
    心跳调度器用：读上一拍存的 desire_next_heartbeat_at，算出「还需 sleep 多少秒」。

    - HEARTBEAT_AUTONOMY 关：返回 None（调用方回退到固定间隔）。
    - 没有存过 next_heartbeat_at（首次）：返回 None。
    - 已过期（now ≥ next）：返回 0（该醒了）。
    - 否则返回剩余秒数，并夹在 [MIN, MAX] 内（防脏数据把调度器卡死）。
    """
    if not is_heartbeat_autonomy():
        return None
    nxt = _load_fact(NEXT_HEARTBEAT_KEY)
    if not isinstance(nxt, (int, float)):
        return None
    if now_ms is None:
        now_ms = time.time() * 1000.0
    remaining = (nxt - now_ms) / 1000.0
    if remaining <= 0:
        return 0
    return int(min(max(remaining, de.HEARTBEAT_MIN_INTERVAL), de.HEARTBEAT_MAX_INTERVAL))


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
