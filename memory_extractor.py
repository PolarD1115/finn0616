# -*- coding: utf-8 -*-
"""第 5 阶段 —— 长期事实提取器（离线 / Mock 阶段）。

职责：把一批 memory_events 原始事件转换为经过验证的 memory_items 候选
（`memory_events → extractor → validated candidates`）。本阶段只完成
「输入事件 → 构造 Prompt → 调用（可注入的）LLM → 解析严格 JSON → 验证清洗 →
规范化 → 批内去重 → 生成待写入数据与事件状态计划」，**不写任何数据库**、
不修改事件状态、不被聊天/后台/启动流程自动调用。

设计边界（对齐第 2/4 阶段约定）：
- 防模仿红线（继承 shared_experience.py）：assistant 原文/语气/口头禅/回复范例
  永不进入候选；prompt 约束 + 验证层确定性拦截双重落地。
- memory_items 约定：过期判定以 expires_at 派生为准（本模块为 current 补默认过期）；
  content_hash 为规范化文本的 SHA-256（规范化在本模块应用层完成）；
  本阶段不执行 superseded 判断、不写 superseded_by、不做跨批去重与冲突解决。
- 状态计划只生成不执行：LLM/解析失败 → failed（可重试错误码）；
  全部候选被验证拒绝 → failed（ALL_CANDIDATES_REJECTED，确定性失败，不自动重试）；
  合法空结果与部分通过 → processed。
"""

import asyncio
import datetime
import difflib
import hashlib
import json
import re
import uuid

# ── 硬限制（常量，不引入环境变量）─────────────────────────────
MAX_CONTENT_CHARS = 500          # 单条候选 content 上限（中文字符）
MAX_CANDIDATES = 10              # 一批最多保留的候选数
MAX_SOURCE_INDEXES = 5           # 单条候选最多引用的事件数
MAX_INPUT_EVENT_CHARS = 500      # Prompt 渲染时单条事件截断
MAX_INPUT_TOTAL_CHARS = 20000    # Prompt 事件区总长上限
CURRENT_DEFAULT_EXPIRY_HOURS = 72  # current 无 expires_at 时的保守默认（有限期，非无限）
CORE_MIN_CONFIDENCE = 0.9
CORE_MIN_IMPORTANCE = 8
OVER_INFERENCE_CONFIDENCE_FLOOR = 0.8  # long_term/core 含泛化限定词时要求的最低置信度
# 泛化限定词（第 11 阶段真实样本暴露）：仅作为「证据不足」信号与 confidence 联动，
# 不是机械黑名单——用户明确表达相应频率/条件/因果时模型会给出高置信度，不误伤。
GENERALIZATION_WORDS = ("容易", "经常", "总是", "尤其", "通常", "长期", "每当")
VERBATIM_COPY_RATIO = 0.7        # 与 assistant 原文相似度阈值
VERBATIM_COPY_MIN_SHARED_CHARS = 12  # 与 assistant 原文最大公共子串阈值
LOW_VALUE_MIN_CHARS = 16
DEFAULT_IMPORTANCE = 3
DEFAULT_CONFIDENCE = 0.5
ALLOWED_MEMORY_TYPES = ("core", "current", "long_term", "moment", "memo")
DEFAULT_STATUS = "pending_review"
CREATED_BY = "memory_extractor"
EXTRACTABLE_ROLES = ("user", "assistant")

# 显式记忆请求词（出现在被引用 user 事件中 → core 跳过降级并提升 importance）
EXPLICIT_MEMORY_REQUEST_WORDS = ("记住", "别忘了", "一定要记", "记下来")
# 事实信号词（问句/寒暄拒绝规则的豁免依据——含信号词的候选保留）
FACT_SIGNAL_WORDS = ("记住", "喜欢", "讨厌", "生日", "纪念日", "明天", "星期",
                     "地址", "电话", "名字", "考试", "工作", "学", "住", "买",
                     "约", "计划", "准备", "喜欢喝", "偏好")
# 低价值寒暄前缀（短内容 + 该前缀 → 拒绝）
LOW_VALUE_PREFIXES = ("哈哈", "你好", "晚安", "早上好", "晚上好", "早安", "在吗",
                      "在不在", "谢谢", "好的", "好哒", "嗯嗯", "拜拜", "再见",
                      "收到", "ok", "OK", "嗯", "哦", "行")
# 模型自称/填充语开头（这些词作为整句开头基本是 assistant 腔，而非事实陈述）
ASSISTANT_FILLER_PREFIXES = ("当然", "没问题", "放心", "别担心", "加油",
                             "没事", "没关系")

# 错误代码（last_error 只允许脱敏代码，不含正文/Prompt/完整异常）
ERR_LLM = "LLM_ERROR"
ERR_EMPTY = "EMPTY_RESPONSE"
ERR_JSON = "JSON_PARSE_ERROR"
ERR_VALIDATION = "VALIDATION_ERROR"
ERR_ALL_REJECTED = "ALL_CANDIDATES_REJECTED"

# 行首角色前缀（半/全角冒号都匹配；只锚定行首，普通中文词"回答"在句中不受影响）
_ROLE_PREFIX_RE = re.compile(
    r"^\s*(?:assistant|ai|system|tool|user|用户|助手|回复|回答)\s*[:：]",
    re.IGNORECASE | re.MULTILINE)
# assistant 自称格式 "我(某名称)："——不依赖具体 AI 名称（对齐 gateway 先例）
_ASSISTANT_SELF_RE = re.compile(r"^\s*我\([^)]*\)\s*[:：]", re.MULTILINE)
# <final> 内部包装（第 9 阶段发现：上游模型的 assistant 输出被 <final>...</final> 包裹）
_FINAL_WRAPPED_RE = re.compile(r"^\s*<\s*final\s*>(.*)<\s*/\s*final\s*>\s*$",
                               re.IGNORECASE | re.DOTALL)
_FINAL_OPEN_RE = re.compile(r"<\s*final\s*>", re.IGNORECASE)
_FINAL_CLOSE_RE = re.compile(r"<\s*/\s*final\s*>", re.IGNORECASE)
_FINAL_TAG_RE = re.compile(r"<\s*/?\s*final\s*>", re.IGNORECASE)


# ════════════════════════════════════════════════════════════
# 基础工具
# ════════════════════════════════════════════════════════════

def _normalize_text(text):
    """最小规范化：strip + 折叠连续空白。不做去词等激进规范化。"""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _parse_ts(value):
    """解析 ISO 时间字符串；非法或无时区返回 None（None 输入按未提供处理，由调用方区分）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else None


def _resolve_ts(cand, key):
    """返回 (True, datetime|None) 或 (False, None)。模型显式给出非法时间 → False。"""
    v = cand.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return True, None
    dt = _parse_ts(v)
    if dt is None:
        return False, None
    return True, dt


def _has_role_prefix(content):
    return bool(_ROLE_PREFIX_RE.search(content))


def _is_assistant_self_ref(content):
    return bool(_ASSISTANT_SELF_RE.search(content))


def _has_fact_signal(content):
    return any(w in content for w in FACT_SIGNAL_WORDS)


def _looks_like_question(content):
    c = content.rstrip()
    return c.endswith(("？", "?", "吗"))


def _is_low_value_greeting(content):
    return len(content) < LOW_VALUE_MIN_CHARS and content.startswith(LOW_VALUE_PREFIXES)


def _is_verbatim_copy(content, assistant_contents):
    """候选与任一 assistant 原文高度相似或共享长公共子串 → 判定为复制原始回复。"""
    for ac in assistant_contents:
        ac_n = _normalize_text(ac)
        if not ac_n:
            continue
        sm = difflib.SequenceMatcher(None, content, ac_n)
        match = sm.find_longest_match(0, len(content), 0, len(ac_n))
        if match.size >= VERBATIM_COPY_MIN_SHARED_CHARS:
            return True
        if sm.ratio() >= VERBATIM_COPY_RATIO:
            return True
    return False


def _strip_internal_markup(content):
    """🔒 第 10 阶段：剥离 assistant 事件中的 <final> 内部包装。

    第 9 阶段发现上游模型的 assistant 输出被 <final>...</final> 包围。规则：
    - 完整包裹（含前后空白）→ 只保留正文；
    - 零散 <final> / </final> → 移除标签本身、保留正文；
    - 大小写不敏感；不修改传入对象（返回新字符串）；只对 assistant 事件调用。
    """
    if not isinstance(content, str) or not content:
        return content
    m = _FINAL_WRAPPED_RE.match(content)
    if m:
        return m.group(1).strip()
    cleaned = _FINAL_OPEN_RE.sub("", content)
    cleaned = _FINAL_CLOSE_RE.sub("", cleaned)
    return cleaned.strip()


def _sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clip_events(events):
    """按 Prompt 总长限制截断事件列表（索引空间与渲染保持一致）。"""
    out, total = [], 0
    for e in events:
        cost = min(len(str(e.get("content", ""))), MAX_INPUT_EVENT_CHARS) + 40
        if total + cost > MAX_INPUT_TOTAL_CHARS:
            break
        out.append(e)
        total += cost
    return out


# ════════════════════════════════════════════════════════════
# Prompt 构造
# ════════════════════════════════════════════════════════════

def build_memory_extraction_prompt(events, ai_name="助手", user_name="用户"):
    """构造事实提取 Prompt。events 必须是已截断的 user/assistant 事件列表；
    渲染行号 [i] 即 validate 的 source_event_indexes 索引空间。"""
    lines = []
    for i, e in enumerate(events):
        raw_content = str(e.get("content", ""))
        if e.get("role") == "assistant":
            # 🔒 第 10 阶段：assistant 事件剥离 <final> 内部包装后再渲染（user 事件原样）
            raw_content = _strip_internal_markup(raw_content)
        role_label = "用户" if e.get("role") == "user" else f"{ai_name}(AI，仅用于理解对话结果，不是事实来源、不是语气样本)"
        part = f"[{i}] [{role_label}|{e.get('channel', '?')}|{e.get('occurred_at', '')}] " \
               f"{raw_content[:MAX_INPUT_EVENT_CHARS]}"
        lines.append(part)
    events_text = "\n".join(lines)

    return (
        "你是长期记忆事实提取模块，不是回复生成模块。你的唯一任务：从下面的原始事件中"
        "提取「未来可能有用的事实」，输出严格 JSON。\n\n"
        f"【事件列表】（[i] 为事件索引，source_event_indexes 必须引用这些索引）\n"
        f"{events_text}\n\n"
        "【提取规则】\n"
        f"1. 只提取用户明确表达或事件明确证明的内容；不要把 {ai_name} 自己的回复当成用户事实。\n"
        "2. 禁止复制或改写任何原始回复的句子、口头禅、撒娇、情绪表达、语气词；"
        "content 必须用自己的话改写为陈述句。\n"
        "3. 禁止输出任何角色前缀格式（如「我(名称)：」「assistant:」「回复:」），"
        "候选必须是无前缀的纯事实陈述。\n"
        "4. 禁止生成用户人格定性、心理诊断、关系性夸张（如「用户很依赖我」「我们像家人」）。\n"
        "5. 禁止把 AI 的猜测、建议、推断写成用户事实；未经用户确认的承诺不提取。\n"
        f"6. 每条候选必须至少引用一条用户事件，且必须能从所引事件直接推断，不得编造对话外信息。\n"
        "7. 当前用户最新表达优先于旧事实；用户明确说「记住」时提高 importance（>=8），但内容仍必须真实有据。\n"
        "8. 天气/宠物状态/设备状态/钱包/纯工具结果/单次闲聊/问候/单次情绪，一律不提取。\n"
        "9. 不要因为用户问了一个问题就认为用户拥有相关偏好；不要因为用户提到某件事一次，"
        "就判定为稳定人格特征。\n"
        f"10. 如果无法确认长期有效，不要归类为 core；如果事实只适用于短期，归类为 current 并设置 expires_at。\n"
        "11. 没有足够证据时输出空数组，绝不凑数、绝不生成记忆之外的内容。\n\n"
        "【过度推断红线】\n"
        "- 候选中的每个限定条件（原因、触发条件、频率、程度、时间跨度、长期稳定性、因果关系）"
        "都必须能在所引用的用户事件中找到明确证据。\n"
        "- 不得自行补充「经常 / 总是 / 容易 / 尤其 / 通常 / 长期 / 每当 / 因为 / 导致 / "
        "在……时会……」这类限定——除非用户明确表达了相应的频率、条件、原因或时间跨度。\n"
        "- 一次性的当前状态优先归类为 current（必须带 expires_at），不得泛化为长期规律；"
        "不得仅凭当时的场景或环境推导用户的长期体质、习惯或人格特征。\n"
        "- 证据不足时：删除无依据的限定、将候选降级为 current、或直接不生成该候选。\n"
        f"- 不得通过 {ai_name} 的回复补全用户未表达的原因、条件或规律。\n\n"
        "【memory_type 规则】（只允许 core / current / long_term / moment / memo）\n"
        f"- core：仅限{user_name}的姓名、明确的长期身份、明确的人际边界、"
        f"{user_name}明确要求长期记住的重要设定、重要纪念日；默认不要轻易输出 core。\n"
        "- current：最近正在做什么、近期情绪、当前计划、当前身体状态、短期未完成事项；"
        "必须给 expires_at（ISO 时间，含时区，不得无限期）。\n"
        "- long_term：稳定偏好、长期习惯、重要经历、长期计划、持续性关系事实、"
        "明确的工作或生活选择；一次性话题不要写成长期事实。\n"
        f"- moment：{user_name}和{ai_name}一起完成的重要事情、关系中的特殊时刻、"
        "重要的第一次、冲突与和好；必须是事实化短叙述，不得包含 "
        f"{ai_name} 的原文、语气或口头禅。\n"
        "- memo：当前窗口未完成事项、下次对话需要接续的任务、刚提出尚未完成的计划；"
        "不是完整聊天总结。\n\n"
        "【输出格式】只返回 JSON，不要 Markdown 代码围栏，不要任何解释：\n"
        '{"memories":[{"memory_type":"long_term","content":"事实化陈述",'
        '"subject_key":"english_snake_topic 或 null","importance":4,"confidence":0.9,'
        '"valid_at":"ISO时间或null","invalid_at":null,'
        '"expires_at":"ISO时间或null（current 必填）",'
        '"source_event_indexes":[0],"reason":"简短依据"}]}\n'
        f"- memories 最多 {MAX_CANDIDATES} 条；每条 content 不超过 {MAX_CONTENT_CHARS} 字。\n"
        f"- source_event_indexes 每条候选至少 1 个、最多 {MAX_SOURCE_INDEXES} 个，"
        "且至少一个指向用户事件。"
    )


# ════════════════════════════════════════════════════════════
# 严格 JSON 解析
# ════════════════════════════════════════════════════════════

def parse_memory_extraction_response(text):
    """严格解析模型输出。返回 (candidates_raw_list, None) 或 (None, error_code)。
    Markdown 代码围栏按任务约定直接拒绝（模型被明令禁止使用围栏）。"""
    if not isinstance(text, str) or not text.strip():
        return None, ERR_EMPTY
    s = text.strip()
    if s.startswith("```"):
        return None, ERR_JSON
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None, ERR_JSON
    if not isinstance(data, dict):
        return None, ERR_JSON
    memories = data.get("memories")
    if not isinstance(memories, list):
        return None, ERR_JSON
    for m in memories:
        if not isinstance(m, dict):
            return None, ERR_JSON
    # 一批最多保留 MAX_CANDIDATES 条（任务 §九.3）
    return memories[:MAX_CANDIDATES], None


# ════════════════════════════════════════════════════════════
# 单候选验证与规范化
# ════════════════════════════════════════════════════════════

def validate_and_normalize_candidate(cand, events, user_id, batch_id, now_utc):
    """验证并规范化单条候选。
    返回 (item_dict, None) 或 (None, 脱敏拒绝原因代码)。"""
    # 1. memory_type
    mt = cand.get("memory_type")
    if mt not in ALLOWED_MEMORY_TYPES:
        return None, "INVALID_MEMORY_TYPE"

    # 2. content
    content = cand.get("content")
    if not isinstance(content, str) or not content.strip():
        return None, "EMPTY_CONTENT"
    content = _normalize_text(content)
    if not content:
        return None, "EMPTY_CONTENT"
    if len(content) > MAX_CONTENT_CHARS:
        return None, "CONTENT_TOO_LONG"

    # 3. 数值字段
    imp = cand.get("importance", DEFAULT_IMPORTANCE)
    if isinstance(imp, bool) or not isinstance(imp, int) or not (1 <= imp <= 10):
        return None, "INVALID_IMPORTANCE"
    conf = cand.get("confidence", DEFAULT_CONFIDENCE)
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not (0 <= conf <= 1):
        return None, "INVALID_CONFIDENCE"

    # 4. 时间字段（显式给出但非法 → 拒绝；null/缺失 → None）
    ok, valid_at = _resolve_ts(cand, "valid_at")
    if not ok:
        return None, "INVALID_TIME"
    ok, invalid_at = _resolve_ts(cand, "invalid_at")
    if not ok:
        return None, "INVALID_TIME"
    ok, expires_at = _resolve_ts(cand, "expires_at")
    if not ok:
        return None, "INVALID_TIME"

    # 5. 来源索引（相对本批事件数组；去重、限数、范围内、至少一个 user 事件）
    idxs = cand.get("source_event_indexes")
    if not isinstance(idxs, list) or not idxs:
        return None, "MISSING_SOURCE_INDEXES"
    if len(idxs) > MAX_SOURCE_INDEXES:
        return None, "TOO_MANY_SOURCE_INDEXES"
    seen = set()
    for i in idxs:
        if isinstance(i, bool) or not isinstance(i, int):
            return None, "INVALID_SOURCE_INDEX"
        if i < 0 or i >= len(events):
            return None, "SOURCE_INDEX_OUT_OF_RANGE"
        seen.add(i)
    ref_events = [events[i] for i in sorted(seen)]
    user_refs = [e for e in ref_events if e.get("role") == "user"]
    if not user_refs:
        return None, "ASSISTANT_ONLY_SOURCE"

    # 6. 内容安全（确定性拦截，宁拒不误放）
    if _has_role_prefix(content):
        return None, "ROLE_PREFIX_CONTENT"
    if _is_assistant_self_ref(content):
        return None, "ASSISTANT_SELF_REF"
    if _FINAL_TAG_RE.search(content):
        return None, "INTERNAL_MARKUP"  # 🔒 候选不得残留 <final> 内部标记
    # verbatim 对比使用剥离 <final> 后的 assistant 文本，保证相似度基线一致
    assistant_contents = [_strip_internal_markup(str(e.get("content", ""))) for e in events
                          if e.get("role") == "assistant"]
    if assistant_contents and _is_verbatim_copy(content, assistant_contents):
        return None, "VERBATIM_COPY"
    has_signal = _has_fact_signal(content)
    if _looks_like_question(content) and not has_signal:
        return None, "QUESTION_NO_FACT"
    if _is_low_value_greeting(content) and not has_signal:
        return None, "LOW_VALUE_GREETING"
    if not re.search(r"[\u4e00-\u9fff a-zA-Z0-9]", content):
        return None, "NO_MEANINGFUL_CONTENT"

    # 7. core 高门槛（显式记忆请求覆盖：被引用 user 事件含记忆请求词 → 跳过降级并提升 importance）
    user_sources = " ".join(str(e.get("content", "")) for e in user_refs)
    explicit_request = any(w in user_sources for w in EXPLICIT_MEMORY_REQUEST_WORDS)
    if mt == "core":
        if explicit_request:
            imp = max(imp, CORE_MIN_IMPORTANCE)
        elif conf < CORE_MIN_CONFIDENCE or imp < CORE_MIN_IMPORTANCE:
            mt = "long_term"  # 降级，不拒绝（保留可审查的低层级候选）

    # 7.5 🔒 第 11 阶段：过度推断兜底——long_term/core 含泛化限定词但置信度不足时拒绝。
    #    第 9 阶段真实样本暴露：模型会把一次场景泛化为长期规律（如「一次手凉」→
    #    「长期容易手凉，尤其疲劳/冷环境时」）。泛化词本身不是黑名单（用户明确表达时
    #    模型会给出高置信度，见 B/E 场景）；低置信 + 泛化限定 = 模型自行补全了
    #    频率/触发条件/因果，证据不足 → 拒绝，交由后续批次或人工确认。
    if mt in ("long_term", "core") and conf < OVER_INFERENCE_CONFIDENCE_FLOOR:
        if any(w in content for w in GENERALIZATION_WORDS):
            return None, "OVER_INFERENCE"

    # 8. current 必须有限期：模型未给 → 补默认（从 max(valid_at, 最早来源事件时间) 起算，
    #    满足 DB CHECK expires_at >= valid_at）；模型给的值早于 valid_at → clamp 到 valid_at
    if mt == "current":
        ref_times = [_parse_ts(e.get("occurred_at")) for e in ref_events]
        ref_times = [t for t in ref_times if t is not None]
        base = min(ref_times) if ref_times else now_utc
        if valid_at is not None and valid_at > base:
            base = valid_at
        if expires_at is None:
            expires_at = base + datetime.timedelta(hours=CURRENT_DEFAULT_EXPIRY_HOURS)
        elif valid_at is not None and expires_at < valid_at:
            expires_at = valid_at

    # 9. 组装 memory_items 候选行（字段以第 4 阶段表结构为准；
    #    本阶段不做替代/冲突处理：invalid_at/superseded_by 恒空）
    source_event_ids = [str(e.get("id")) for e in ref_events]
    source = str(user_refs[0].get("channel") or "unknown")
    subject_key = cand.get("subject_key")
    subject_key = subject_key.strip() if isinstance(subject_key, str) and subject_key.strip() else None

    item = {
        "user_id": user_id,
        "memory_type": mt,
        "content": content,
        "content_hash": _sha256_hex(content),
        "status": DEFAULT_STATUS,
        "importance": imp,
        "confidence": round(float(conf), 4),
        "source": source,
        "source_event_ids": source_event_ids,
        "source_batch_id": batch_id,
        "subject_key": subject_key,
        "valid_at": valid_at.isoformat() if valid_at else None,
        "invalid_at": None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "last_confirmed_at": None,
        "superseded_by": None,
        "created_by": CREATED_BY,
        "metadata": {"explicit_memory_request": explicit_request},
    }
    return item, None


# ════════════════════════════════════════════════════════════
# 批内精确去重
# ════════════════════════════════════════════════════════════

def dedupe_candidates(items):
    """批内精确去重：content_hash 相同 → 保留 confidence 最高者（tie-break importance），
    并合并 source_event_ids（保序并集）。不做语义去重、不查库、不合并不同内容。"""
    best = {}
    order = []
    for it in items:
        h = it["content_hash"]
        if h not in best:
            best[h] = it
            order.append(h)
            continue
        cur = best[h]
        merged_ids = list(dict.fromkeys(list(cur["source_event_ids"]) + list(it["source_event_ids"])))
        winner = it if (it["confidence"], it["importance"]) >= (cur["confidence"], cur["importance"]) else cur
        loser = cur if winner is it else it
        merged = dict(winner)
        merged["source_event_ids"] = merged_ids
        merged["importance"] = max(winner["importance"], loser["importance"])
        best[h] = merged
    return [best[h] for h in order]


# ════════════════════════════════════════════════════════════
# 事件状态计划（只生成，不执行）
# ════════════════════════════════════════════════════════════

def prepare_event_status_updates(event_ids, batch_id, ok, error_code, now_utc):
    """生成 memory_events 状态更新计划（脱敏 last_error；本阶段绝不执行）。
    约定：ALL_CANDIDATES_REJECTED 属确定性失败，不自动重试（重试策略留待下一阶段设计）。"""
    return {
        "event_ids": list(event_ids),
        "processing_status": "processed" if ok else "failed",
        "processed_at": now_utc.isoformat() if ok else None,
        "attempt_count_increment": 1,
        "last_error": error_code,
        "batch_id": batch_id,
    }


# ════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════

def _fail_result(batch_id, error_code, event_ids, rejected=None):
    return {
        "ok": False,
        "error_code": error_code,
        "candidates": [],
        "rejected": list(rejected or []),
        "status_plan": prepare_event_status_updates(event_ids, batch_id, False, error_code,
                                                    datetime.datetime.now(datetime.timezone.utc)),
        "batch_id": batch_id,
    }


async def extract_memory_candidates(events, llm_call, user_id=None,
                                    ai_name="助手", user_name="用户"):
    """提取主入口。

    events:   memory_events 行列表（dict）；tool/system 等非 user/assistant 事件
              会被过滤出可提取集，但仍计入状态计划。
    llm_call: 同步 callable(prompt) -> str（可注入；真实实现见
              make_compression_llm_call，返回空串视为失败——与项目 ask_role_sync 约定一致）。
    user_id:  目标用户；缺省时取第一个事件的 user_id。

    返回 {ok, error_code, candidates, rejected, status_plan, batch_id}；
    永不抛出未处理异常、永不写库、candidates 均为通过全部验证的规范化数据。"""
    batch_id = str(uuid.uuid4())
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    if not isinstance(events, list) or not events:
        return _fail_result(batch_id, ERR_VALIDATION, [])
    all_ids = [str(e.get("id")) for e in events if isinstance(e, dict) and e.get("id")]
    extractable = [e for e in events
                   if isinstance(e, dict) and e.get("role") in EXTRACTABLE_ROLES]
    if not extractable:
        return _fail_result(batch_id, ERR_VALIDATION, all_ids)
    if user_id is None:
        user_id = str(extractable[0].get("user_id") or "unknown")
    clipped = _clip_events(extractable)
    prompt = build_memory_extraction_prompt(clipped, ai_name, user_name)

    # LLM 调用（可注入；异常与空串统一按失败处理，不向上抛）
    try:
        raw = await asyncio.to_thread(llm_call, prompt)
    except Exception as e:  # noqa: BLE001 —— 统一失败语义，见模块 docstring
        print(f"[memory_extractor] batch={batch_id[:8]} LLM 调用异常: {type(e).__name__}")
        return _fail_result(batch_id, ERR_LLM, all_ids)
    if not isinstance(raw, str) or not raw.strip():
        print(f"[memory_extractor] batch={batch_id[:8]} LLM 空响应")
        return _fail_result(batch_id, ERR_EMPTY, all_ids)

    rows, parse_err = parse_memory_extraction_response(raw)
    if parse_err:
        print(f"[memory_extractor] batch={batch_id[:8]} 解析失败: {parse_err}")
        return _fail_result(batch_id, parse_err, all_ids)

    items, rejected = [], []
    for cand in rows:
        item, reason = validate_and_normalize_candidate(cand, clipped, user_id, batch_id, now_utc)
        if item is not None:
            items.append(item)
        else:
            rejected.append(reason)
    items = dedupe_candidates(items)

    if not items and rejected:
        # 模型有输出但全部被验证拒绝 → 确定性失败（不自动重试；last_error 只存代码）
        print(f"[memory_extractor] batch={batch_id[:8]} 全部候选被拒绝: "
              f"{len(rejected)} 条（代码见 status_plan.last_error）")
        plan = prepare_event_status_updates(all_ids, batch_id, False, ERR_ALL_REJECTED, now_utc)
        return {"ok": False, "error_code": ERR_ALL_REJECTED, "candidates": [],
                "rejected": rejected, "status_plan": plan, "batch_id": batch_id}

    print(f"[memory_extractor] batch={batch_id[:8]} 候选={len(items)} "
          f"拒绝={len(rejected)} 事件={len(all_ids)}")
    plan = prepare_event_status_updates(all_ids, batch_id, True, None, now_utc)
    return {"ok": True, "error_code": None, "candidates": items,
            "rejected": rejected, "status_plan": plan, "batch_id": batch_id}


# ════════════════════════════════════════════════════════════
# 真实 LLM 调用工厂（本阶段不被任何自动流程调用）
# ════════════════════════════════════════════════════════════

def make_compression_llm_call():
    """返回基于项目 compression 角色池的同步 LLM 调用（惰性 import server，
    复用既有端点轮询与故障转移；temperature 跟随 compression 惯例 0.7）。
    本阶段仅供手动/受控试运行使用；ask_role_sync 全部端点失败时返回空串，
    由 extract_memory_candidates 统一按失败处理。"""
    def _call(prompt):
        import server  # 惰性导入，避免模块级副作用
        return server.ask_role_sync("compression", prompt, temperature=0.7)
    return _call
