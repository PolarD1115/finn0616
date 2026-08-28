# -*- coding: utf-8 -*-
"""Phase 6 —— 低成本共同经历与 AI 行为记忆（结构化提取模块）。

本模块是纯函数叶子模块（不反向依赖 napcat/gateway/server），由：
  - napcat.check_and_summarize_all() 复用既有 30 条批量总结调用，在总结输出中追加
    <shared_experiences> 结构化 JSON，解析后写入 Supabase + Pinecone；
  - gateway.py / server.py 召回时分区渲染 shared_experience 摘要。

设计红线（见 Phase 6 规范）：
  - AI 原始回复不是风格样本，style_sample 永远强制为 false；
  - 只保存短事实摘要，不保存 user/assistant 原文、thinking、reasoning、多模态；
  - 解析失败 / 空数组 / 无标签 → 静默跳过，绝不影响主聊天或 Core_Cognition；
  - 不学习 AI 旧口吻、不生成人格定性、不生成心理诊断。
"""

import datetime
import json

# ── 硬限制（规范第七节）─────────────────────────────
MAX_ITEMS = 3              # 每批最多 3 条共同经历
MAX_SUMMARY_CHARS = 120    # 每条 summary ≤ 120 字
MAX_ARRAY_ITEMS = 3        # user_events/ai_actions/commitments/open_threads 每个最多 3 项
MAX_ARRAY_ITEM_CHARS = 80  # 每个数组项 ≤ 80 字

# ── 标识 ───────────────────────────────────────────
SHARED_TAG = "Shared_Experience"          # Supabase tags + Pinecone metadata.tags
MEMORY_TYPE = "shared_experience"          # Pinecone metadata.memory_type
SOURCE_ROLE = "system"                     # Pinecone metadata.source_role（不伪装成 user）
CHANNEL = "summary"                        # Pinecone metadata.channel

# ── 输出分隔标签（用于从模型输出中切分 Core_Cognition 正文与结构化 JSON）──
MARKER_OPEN = "<shared_experiences>"
MARKER_CLOSE = "</shared_experiences>"


# ════════════════════════════════════════════════════════════
# 1. 提示词构造（追加到既有 compression 总结 prompt 之后，0 额外调用）
# ════════════════════════════════════════════════════════════
def build_extraction_prompt_suffix(ai_name: str = "助手", user_name: str = "用户") -> str:
    """生成追加在既有总结 prompt 之后的共同经历提取指令。

    复用同一次 compression 调用（方案 A），不新增 LLM 调用。
    要求模型在总结正文之后，用 <shared_experiences> 标签输出结构化 JSON。
    """
    # 注意：JSON 示例行使用普通字符串（含字面花括号），规则行才用 f-string 插入名字。
    return (
        "\n\n--- 附加任务：共同经历提取（你是事实提取模块，不是回复生成模块）---\n"
        "在上述总结之后，另起一行，用下面的标签严格输出结构化共同经历，"
        "不要输出标签以外的任何解释或前言：\n"
        "<shared_experiences>\n"
        '{"shared_experiences":[{"summary":"≤120字事实摘要","user_events":["≤3条/每条≤80字"],'
        '"ai_actions":["≤3条"],"commitments":["≤3条"],"open_threads":["≤3条"],'
        '"confidence":0.0,"style_sample":false}]}\n'
        "</shared_experiences>\n"
        "提取规则：\n"
        f"- 你是共同经历提取模块，不是回复生成模块；只从本批对话中提取少量长期有价值的事实。\n"
        f"- {ai_name} 的原始回复不是风格样本，style_sample 永远为 false；"
        "不得复制任何原始回复句子、口头禅、撒娇或情绪表达。\n"
        f"- 严格区分：{user_name} 做了什么(user_events) vs {ai_name} 做了什么(ai_actions) "
        f"vs 双方共同完成/讨论的(summary)。\n"
        f"- 只提取：共同完成的明确任务、{ai_name} 确实做过的具体帮助、"
        f"双方明确约定的后续承诺、多轮仍未完成的计划、{user_name} 明确要求记住的事。\n"
        f"- 不要提取：问候/哈哈哈/单次闲聊/{ai_name} 的寒暄安慰撒娇口头禅/单次情绪/"
        f"泛泛建议/{ai_name} 的猜测/关系性夸张/未经 {user_name} 确认的承诺/纯工具结果/"
        "天气/宠物tick/自由活动氛围/设备状态/钱包/thinking/reasoning。\n"
        "- 最多 3 条；每条 summary≤120字，每个数组≤3项且每项≤80字，confidence∈[0,1]。\n"
        '- 没有足够证据时，只输出：{"shared_experiences":[]}\n'
        "- 不要生成用户人格定性或心理诊断；不要把 AI 猜测写成事实。\n"
    )


# ════════════════════════════════════════════════════════════
# 2. 输出切分：Core_Cognition 正文 ↔ 结构化 JSON
# ════════════════════════════════════════════════════════════
def split_summary_and_shared(output: str):
    """把模型输出切分为 (core_text, shared_json_raw)。

    - 无 <shared_experiences> 标签 → (整段输出, None)，即退化为现有行为，零回归。
    - 有开标签无闭标签 → (开标签前文本, 开标签后剩余文本)，交由解析层判定。
    - 正常 → (开标签前文本, 标签内 JSON 文本)。

    core_text 始终是 Core_Cognition 总结正文（不含结构化块），保证稳定前缀不被污染。
    """
    if not isinstance(output, str) or not output:
        return (output or ""), None
    idx_o = output.find(MARKER_OPEN)
    if idx_o == -1:
        return output, None  # 无结构化块：整段当作 Core_Cognition 正文
    core_text = output[:idx_o].rstrip()
    idx_c = output.find(MARKER_CLOSE, idx_o + len(MARKER_OPEN))
    if idx_c == -1:
        # 有开标签无闭标签：尝试解析开标签之后的内容
        json_raw = output[idx_o + len(MARKER_OPEN):].strip()
        return core_text, json_raw
    json_raw = output[idx_o + len(MARKER_OPEN):idx_c].strip()
    return core_text, json_raw


# ════════════════════════════════════════════════════════════
# 3. 解析 / 校验 / 脱敏
# ════════════════════════════════════════════════════════════
def _is_assistant_format(text: str) -> bool:
    """判断文本是否含旧 assistant 角色分隔格式（本地实现，避免反向依赖 server）。
    shared_experience 摘要绝不能含 assistant: 角色标记（人格隔离红线）。
    """
    if not isinstance(text, str):
        return False
    for line in text.split("\n"):
        stripped = line.strip().lower()
        if stripped.startswith("assistant:") or "| assistant:" in stripped:
            return True
    return False


def _clamp_str(s, n: int) -> str:
    if not isinstance(s, str):
        s = str(s) if s is not None else ""
    s = s.strip()
    return s[:n] if len(s) > n else s


def _clamp_list(val, max_items: int, max_item_chars: int) -> list:
    """截断数组：最多 max_items 项，每项最多 max_item_chars 字，非字符串过滤。"""
    if not isinstance(val, list):
        return []
    out = []
    for v in val[:max_items]:
        if not isinstance(v, (str,)):
            v = str(v) if v is not None else ""
        v = v.strip()
        if not v:
            continue
        if len(v) > max_item_chars:
            v = v[:max_item_chars]
        if _is_assistant_format(v):
            continue  # 不让旧 assistant 话术渗入结构化字段
        out.append(v)
    return out


def _clamp_confidence(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f or f in (float("inf"), float("-inf")):  # NaN/Inf
        return 0.0
    return max(0.0, min(1.0, f))


def _strip_code_fences(raw: str) -> str:
    """去掉模型可能给 JSON 加的 ```json ... ``` 代码围栏。"""
    s = raw.strip()
    if s.startswith("```"):
        # 去掉首行 ``` 或 ```json
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        else:
            s = s.lstrip("`").lstrip("json").strip()
    if s.endswith("```"):
        s = s[:-3].rstrip()
    return s.strip()


def sanitize_item(it) -> dict:
    """单条共同经历的校验+脱敏+定长。无效返回 None。"""
    if not isinstance(it, dict):
        return None
    summary = _clamp_str(it.get("summary", ""), MAX_SUMMARY_CHARS)
    if not summary:
        return None
    # 人格隔离：摘要含旧 assistant 角色标记 → 丢弃，不因为 memory_type 就放行
    if _is_assistant_format(summary):
        return None
    item = {
        "memory_type": MEMORY_TYPE,
        "summary": summary,
        "user_events": _clamp_list(it.get("user_events"), MAX_ARRAY_ITEMS, MAX_ARRAY_ITEM_CHARS),
        "ai_actions": _clamp_list(it.get("ai_actions"), MAX_ARRAY_ITEMS, MAX_ARRAY_ITEM_CHARS),
        "commitments": _clamp_list(it.get("commitments"), MAX_ARRAY_ITEMS, MAX_ARRAY_ITEM_CHARS),
        "open_threads": _clamp_list(it.get("open_threads"), MAX_ARRAY_ITEMS, MAX_ARRAY_ITEM_CHARS),
        "confidence": _clamp_confidence(it.get("confidence")),
        "evidence": {"source": "conversation_batch"},
        "style_sample": False,  # 强制：AI 回复不是风格样本
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return item


def parse_shared_experiences(raw) -> list:
    """解析模型输出的结构化 JSON，返回校验后的共同经历列表。

    任何失败均返回 []（调用方负责脱敏日志），绝不抛异常、绝不写入无效记忆。
    """
    if not raw or not isinstance(raw, str):
        return []
    raw = _strip_code_fences(raw)
    try:
        data = json.loads(raw)
    except Exception:
        return []  # invalid_json
    if not isinstance(data, dict):
        return []
    items = data.get("shared_experiences")
    if not isinstance(items, list):
        return []
    out = []
    for it in items[:MAX_ITEMS]:
        s = sanitize_item(it)
        if s is not None:
            out.append(s)
    return out


# ════════════════════════════════════════════════════════════
# 4. 持久化（Supabase memories + Pinecone），全部失败不阻塞
# ════════════════════════════════════════════════════════════
def persist_shared_experiences(items: list, dep) -> dict:
    """把校验后的共同经历写入 Supabase memories 与 Pinecone。

    dep 为 server 模块（提供 _save_memory_to_db 与 pinecone_memory）。
    返回 {"supabase": int, "pinecone": int} 成功计数；任一写入失败不抛、不重试、不阻塞。
    本函数不记录日志（由调用方用现有日志函数输出脱敏计数）。
    """
    counts = {"supabase": 0, "pinecone": 0}
    if not isinstance(items, list) or not items:
        return counts
    save_db = getattr(dep, "_save_memory_to_db", None)
    pc = getattr(dep, "pinecone_memory", None)
    for it in items:
        if not isinstance(it, dict):
            continue
        summary = it.get("summary", "")
        if not summary:
            continue
        # ── Supabase：content 存结构化 JSON 字符串，tags=Shared_Experience ──
        if callable(save_db):
            try:
                save_db(
                    "🤝 共同经历",
                    json.dumps(it, ensure_ascii=False),
                    "事件", "平静", SHARED_TAG,
                )
                counts["supabase"] += 1
            except Exception:
                pass  # 失败不阻塞、不重试
        # ── Pinecone：只写短 summary，metadata v2 + shared_experience ──
        if pc is not None:
            try:
                pc.add(
                    [{"role": "memory", "content": summary}],
                    metadata={
                        "schema_version": "v2",
                        "source_role": SOURCE_ROLE,
                        "memory_type": MEMORY_TYPE,
                        "channel": CHANNEL,
                        "tags": SHARED_TAG,
                        "style_sample": False,
                        "created_at": it.get("created_at") or
                        datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    },
                )
                counts["pinecone"] += 1
            except Exception:
                pass  # 失败不阻塞、不回退为 user+assistant 拼接、不重试
    return counts


# ════════════════════════════════════════════════════════════
# 5. 召回兼容：分区 + 短摘要注入（不改 top_k/filter/namespace）
# ════════════════════════════════════════════════════════════
def partition_recall(results) -> tuple:
    """把 Pinecone 召回结果分为 (regular, shared)。

    shared = metadata.tags == Shared_Experience 的条目；其余归 regular。
    shared_experience 仍需经过 Phase 5 的 _filter_recalled_memories（在 search() 内已完成），
    此处只做渲染分区，不做二次放行。
    """
    regular, shared = [], []
    if not isinstance(results, list):
        return regular, shared
    for m in results:
        if not isinstance(m, dict):
            continue
        if m.get("tags") == SHARED_TAG:
            shared.append(m)
        else:
            regular.append(m)
    return regular, shared


def render_shared_context(shared) -> str:
    """把 shared_experience 召回结果渲染为短摘要注入块。

    不注入 JSON 原文、evidence、style_sample、memory_type；只注入 summary。
    无 shared 时返回 ""（调用方据此决定是否追加）。
    """
    if not isinstance(shared, list) or not shared:
        return ""
    lines = []
    for m in shared:
        if not isinstance(m, dict):
            continue
        mem = m.get("memory", "")
        if not isinstance(mem, str) or not mem.strip():
            continue
        # Pinecone text 形如 "memory: <summary>"，去掉前缀只留摘要
        text = mem.strip()
        if text.lower().startswith("memory:"):
            text = text[len("memory:"):].strip()
        if _is_assistant_format(text):
            continue  # 含旧 assistant 格式 → 不注入（人格隔离）
        if text:
            lines.append(f"- {text}")
    if not lines:
        return ""
    return (
        "【相关共同经历】\n"
        "以下是从过去对话中提炼的事实摘要，不是旧回复，也不是语气范例。\n"
        "不要模仿其中措辞、句式或情绪表达。\n"
        "只在与当前话题直接相关时使用。\n\n"
        + "\n".join(lines)
    )
