# -*- coding: utf-8 -*-
"""第 21 阶段 —— active 长期记忆只读召回预览。

职责：供 gateway 的受保护 POST /api/memory-recall-preview 调用——用户手动提交
一条查询，服务端只读查询当前统一 user_id 下 status=active 的 memory_items，
排除已过期条目，用确定性词面相关性（deterministic lexical relevance）在内存
排序，返回脱敏召回预览。

边界红线（与第 10/17/19 阶段一脉相承）：
- 数据库操作仅限 memory_items 的 SELECT；绝不写入任何数据（无增改删/
  UPSERT/RPC），不更新任何统计字段，不触碰事件账本表；
- 不调用 LLM、不建 embedding、不接入 pgvector、不操作 Pinecone；
- 不接入正式聊天上下文（本阶段 active 仍不进上下文），不自动调度、无后台线程；
- user_id 一律取服务端统一解析规则（server._resolve_pinecone_user_id），
  客户端无 user_id / status / 过滤字段白名单之外的一切入口；
- 永不返回 pending_review / rejected / superseded / expired（查询条件
  status=active 之外，模块还在内存对返回行做二次 active 过滤，并对
  expires_at <= 当前 UTC 的条目保守剔除——解析失败的时间同样跳过，
  不通过 UPDATE 把任何条目标成 expired）；
- 响应与日志脱敏：不返回/不打印 item ID / user_id / content_hash /
  source_event_ids / source_batch_id / metadata / superseded_by / created_by /
  SQL / 数据库异常原文；日志只输出计数。

相关性声明：本阶段没有可用的 embedding 检索链路，本模块实现的是确定性词面
（lexical）相关性排序——不是语义检索、不是向量召回；分数只用于预览排序，
不是概率，不是 embedding 相似度。

取数上限：无向量索引，先拉取最多 MAX_ACTIVE_CANDIDATES（代码常量）条 active
候选再在内存排序；超过该上限时可能漏掉较旧但相关的记忆（importance DESC、
updated_at DESC 取数序缓解），本阶段不为理论上的大规模数据增加基础设施。
"""

import asyncio
import datetime
import re
import unicodedata

# ════════════════════════════════════════════════════════════
# 常量（代码常量，不新增环境变量）
# ════════════════════════════════════════════════════════════

CONFIRM_RECALL_PREVIEW = "RECALL_PREVIEW_ONLY"

DEFAULT_TOP_K = 5
MIN_TOP_K = 1
MAX_TOP_K = 10
MAX_QUERY_CHARS = 500
MAX_ACTIVE_CANDIDATES = 200

METHOD_NAME = "deterministic_lexical_v1"

CODE_READY = "RECALL_PREVIEW_READY"
CODE_NO_MATCH = "NO_RELEVANT_ACTIVE_MEMORIES"
CODE_QUERY_FAILED = "RECALL_QUERY_FAILED"
CODE_SERVICE_UNAVAILABLE = "RECALL_SERVICE_UNAVAILABLE"
CODE_INVALID_REQUEST = "INVALID_RECALL_REQUEST"

_ACTIVE_STATUS = "active"

# SELECT 列：仅响应字段 + updated_at（排序 tie-break）+ status（内存二次
# active 过滤）。绝不查询 id / user_id / content_hash / source_event_ids /
# source_batch_id / metadata / superseded_by / created_by / last_confirmed_at。
_SELECT_COLUMNS = ("memory_type,content,importance,confidence,subject_key,"
                   "valid_at,expires_at,source,updated_at,status")

# 响应字段白名单（无任何 ID / user_id / hash / 来源 / batch / metadata /
# superseded_by / created_by / updated_at）
_ITEM_RESPONSE_FIELDS = ("memory_type", "content", "importance", "confidence",
                         "subject_key", "valid_at", "expires_at", "source")

# ── 确定性词面相关性权重（分数 0~1，只用于预览排序）──
_W_EXACT = 0.50        # query 完整包含于 content
_W_FRAGMENT = 0.25     # content 的中文核心片段（≥3 字连续段）包含于 query
_W_SUBJECT = 0.25      # subject_key 与 query 存在完整子串关系
_W_BIGRAM = 0.20       # 中文 bigram 重合（按比例）
_W_TOKEN = 0.20        # 英文/数字 token 重合（按比例）
_BIGRAM_CAP = 8        # 比例分母上限：query bigram 数超过 8 按 8 计
_TOKEN_CAP = 5         # 比例分母上限：query token 数超过 5 按 5 计

_RETRIEVAL_DECLARATION = {"method": METHOD_NAME, "semantic_search": False,
                          "writes_executed": False}

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ════════════════════════════════════════════════════════════
# 文本规范化与词面特征
# ════════════════════════════════════════════════════════════

def _normalize_text(text):
    """Unicode NFKC → 小写 → 非[中文/字母/数字]替换为空格 → 折叠空白。

    不引入分词库、不新增依赖；中文标点/空白/全角字符经 NFKC 与替换后
    形成干净边界。
    """
    if not isinstance(text, str):
        return ""
    s = unicodedata.normalize("NFKC", text).lower()
    kept = []
    for ch in s:
        if "\u4e00" <= ch <= "\u9fff" or (ch.isascii() and ch.isalnum()):
            kept.append(ch)
        else:
            kept.append(" ")
    return " ".join("".join(kept).split())


def _compact(norm):
    return "".join(norm.split())


def _cjk_bigrams(norm):
    """中文 bigram 集合（按连续中文段内部切分，不跨段）。"""
    bigrams = set()
    for run in _CJK_RUN_RE.findall(norm):
        for i in range(len(run) - 1):
            bigrams.add(run[i:i + 2])
    return bigrams


def _tokens(norm):
    """有效英文/数字 token 集合（长度 ≥2，停用过短 token 防单字母泛滥）。"""
    return {t for t in _TOKEN_RE.findall(norm) if len(t) >= 2}


def _containment_allowed(compact):
    """完整子串关系准入：含中文的 compact ≥2 字即可；纯 ASCII 需 ≥3。

    理由：去空白后的 compact 拼接（如 "a b"→"ab"）在纯 ASCII 场景会跨词
    边界制造单字母级噪声；中文无词边界问题，2 字即有区分度（宁拒不放）。
    """
    if len(compact) >= 3:
        return True
    return len(compact) == 2 and any(
        "\u4e00" <= ch <= "\u9fff" for ch in compact)


def _parse_ts(value):
    """把 DB 时间值解析为 aware UTC datetime；无法解析返回 None。

    PostgREST 返回 ISO 字符串（可能带 Z / 偏移）；字符串无时区按 UTC 解释；
    解析失败交由调用方保守处理（过期判断跳过、排序沉底）。
    """
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        s = value.strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


# ════════════════════════════════════════════════════════════
# 确定性词面相关性（deterministic_lexical_v1）
# ════════════════════════════════════════════════════════════

def _score_candidate(q_compact, q_bi, q_tok,
                     c_norm, c_compact, c_bi, c_tok,
                     s_compact, s_bi, s_tok):
    """返回 (score, match_reasons)。score ∈ [0,1]，同输入结果恒定。

    任一信号触发即满足最低命中条件；完全没有词面重合的 active 记忆
    score=0、reasons=[]，不会因 importance 高而返回。
    """
    pts = 0.0
    reasons = []

    # 1. query 完整包含于 content（最强信号）
    if _containment_allowed(q_compact) and q_compact in c_compact:
        pts += _W_EXACT
        reasons.append("EXACT_QUERY_IN_CONTENT")

    # 2. content 的中文核心片段（≥3 字连续段）包含于 query
    if _containment_allowed(q_compact):
        for run in _CJK_RUN_RE.findall(c_norm):
            if len(run) >= 3 and run in q_compact:
                pts += _W_FRAGMENT
                reasons.append("CONTENT_FRAGMENT_IN_QUERY")
                break

    # 3. subject_key 与 query 完整子串关系（任一方向）
    if (_containment_allowed(q_compact) and _containment_allowed(s_compact)
            and (s_compact in q_compact or q_compact in s_compact)):
        pts += _W_SUBJECT
        reasons.append("SUBJECT_KEY_MATCH")

    # 4. 中文 bigram 重合（按 query bigram 命中比例，分母封顶）
    if q_bi:
        inter = q_bi & (c_bi | s_bi)
        if inter:
            ratio = min(1.0, len(inter) / min(len(q_bi), _BIGRAM_CAP))
            pts += _W_BIGRAM * ratio
            reasons.append("CHINESE_BIGRAM_OVERLAP")

    # 5. 英文/数字 token 重合（按 query token 命中比例，分母封顶）
    if q_tok:
        inter = q_tok & (c_tok | s_tok)
        if inter:
            ratio = min(1.0, len(inter) / min(len(q_tok), _TOKEN_CAP))
            pts += _W_TOKEN * ratio
            reasons.append("TOKEN_OVERLAP")

    return round(min(1.0, pts), 4), reasons


# ════════════════════════════════════════════════════════════
# 响应构造（脱敏）
# ════════════════════════════════════════════════════════════

def _error_response(code, stats=None):
    """脱敏错误响应：只含 ok/code/stats 安全计数，绝不含 query/正文/ID/
    user_id/hash/SQL/数据库异常原文。"""
    return {"ok": False, "code": code, "stats": dict(stats or {})}


# ════════════════════════════════════════════════════════════
# 只读召回（POST /api/memory-recall-preview 的执行体）
# ════════════════════════════════════════════════════════════

async def run_recall(supabase_service, server_user_id, query, top_k=DEFAULT_TOP_K):
    """active 记忆只读召回预览（gateway 的 /api/memory-recall-preview 调用）。

    supabase_service: server.supabase_service（service_role；仅 SELECT）。
    server_user_id: 服务端统一解析的 user_id（gateway 从 server 模块解析后传入，
                    客户端无任何提交入口）。
    query: 已由 gateway 校验的查询文本（1~500 字符；此处再防御校验）。
    top_k: 已由 gateway 校验的 1~10 整数（此处再夹取防御）。

    流程：只读查询 active（服务端强制 user_id + status 条件，最多 200 条）
      → 内存二次 active 过滤 → 过期/无效时间过滤 → 确定性词面打分
      → score desc → importance desc → updated_at desc → 稳定序 → top_k 截断。
    返回 API 安全响应 dict——零写入、无 LLM、无 Pinecone、无 embedding。
    """
    stats = {"active_fetched": 0, "status_filtered": 0, "expired_filtered": 0,
             "invalid_time_filtered": 0, "matched": 0, "returned": 0}

    if supabase_service is None:
        return _error_response(CODE_SERVICE_UNAVAILABLE, stats)
    if not isinstance(server_user_id, str) or not server_user_id.strip():
        return _error_response(CODE_SERVICE_UNAVAILABLE, stats)
    server_user_id = server_user_id.strip()

    # 模块层防御校验（gateway 已先行校验；保证直接调用同样不查库）
    if not isinstance(query, str):
        return _error_response(CODE_INVALID_REQUEST, stats)
    query = query.strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        return _error_response(CODE_INVALID_REQUEST, stats)
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        return _error_response(CODE_INVALID_REQUEST, stats)
    top_k = max(MIN_TOP_K, min(MAX_TOP_K, top_k))

    # 1. 只读查询 active 候选（服务端强制隔离；最多 200 条，
    #    importance DESC + updated_at DESC 取数序缓解上限截断漏召）
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items")
            .select(_SELECT_COLUMNS)
            .eq("user_id", server_user_id)
            .eq("status", _ACTIVE_STATUS)
            .order("importance", desc=True)
            .order("updated_at", desc=True)
            .limit(MAX_ACTIVE_CANDIDATES)
            .execute())
        rows = [r for r in (getattr(res, "data", None) or []) if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001 —— 数据库异常只返回脱敏代码
        print(f"⚠️ 记忆召回预览失败：stage=active_query "
              f"error={type(e).__name__} fetched=0")
        return _error_response(CODE_QUERY_FAILED, stats)

    stats["active_fetched"] = len(rows)

    # 2. 规范化查询词面特征
    q_norm = _normalize_text(query)
    q_compact = _compact(q_norm)
    q_bi = _cjk_bigrams(q_norm)
    q_tok = _tokens(q_norm)

    # 3. 内存过滤 + 打分（即使查询层条件失效，这里仍只保留 active 未过期）
    now = datetime.datetime.now(datetime.timezone.utc)
    scored = []
    for order_index, row in enumerate(rows):
        if row.get("status") != _ACTIVE_STATUS:
            stats["status_filtered"] += 1
            continue
        exp_raw = row.get("expires_at")
        exp_missing = exp_raw is None or (
            isinstance(exp_raw, str) and not exp_raw.strip())
        if not exp_missing:
            exp = _parse_ts(exp_raw)
            if exp is None:
                # 时间解析失败保守跳过（不改状态、不写库）
                stats["invalid_time_filtered"] += 1
                continue
            if exp <= now:
                stats["expired_filtered"] += 1
                continue
        c_norm = _normalize_text(row.get("content"))
        s_norm = _normalize_text(row.get("subject_key"))
        score, reasons = _score_candidate(
            q_compact, q_bi, q_tok,
            c_norm, _compact(c_norm), _cjk_bigrams(c_norm), _tokens(c_norm),
            _compact(s_norm), _cjk_bigrams(s_norm), _tokens(s_norm))
        if not reasons:
            continue  # 完全无词面重合：不因 importance 高而返回
        updated_ts = _parse_ts(row.get("updated_at"))
        scored.append({
            "order_index": order_index,
            "score": score,
            "reasons": reasons,
            "importance": float(row.get("importance") or 0),
            "updated_ts": updated_ts.timestamp() if updated_ts else 0.0,
            "row": row,
        })

    # 4. 排序：文本相关性优先 → importance 仅 tie-break → updated_at 最终
    #    tie-break → 原始取数序稳定收尾；再 top_k 截断
    scored.sort(key=lambda c: (-c["score"], -c["importance"],
                               -c["updated_ts"], c["order_index"]))
    stats["matched"] = len(scored)
    chosen = scored[:top_k]
    stats["returned"] = len(chosen)

    if not chosen:
        print(f"🔎 active记忆召回预览：fetched={stats['active_fetched']} "
              f"matched=0 returned=0")
        return {"ok": True, "code": CODE_NO_MATCH, "stats": stats,
                "retrieval": dict(_RETRIEVAL_DECLARATION), "items": []}

    items = []
    for i, c in enumerate(chosen, start=1):
        row = c["row"]
        items.append({
            "recall_index": i,
            **{f: row.get(f) for f in _ITEM_RESPONSE_FIELDS},
            "score": c["score"],
            "match_reasons": list(c["reasons"]),
        })

    print(f"🔎 active记忆召回预览：fetched={stats['active_fetched']} "
          f"matched={stats['matched']} returned={stats['returned']}")

    return {"ok": True, "code": CODE_READY, "stats": stats,
            "retrieval": dict(_RETRIEVAL_DECLARATION), "items": items}
