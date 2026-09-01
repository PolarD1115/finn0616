# -*- coding: utf-8 -*-
"""第 37 阶段：lexical + vector 混合召回只读预览（RRF 融合；手动、零写入）。

背景：第 35 阶段用户自然语言向量召回已在生产真实通过（VECTOR_RECALL_PREVIEW_
READY：query_embedded=true、dimension=1024、rpc_returned=1、命中预期 active、
threshold_applied=false）；第 21/22 阶段词面召回已单独验证——精确词面可命中、
同义词面可能漏召回、词面门槛不降低。本阶段提供手动触发的混合召回预览执行体：
对查询恰嵌入一次 → 经 service_role 恰调用一次 match_memory_items 取得 active
向量候选 → 用同一批候选的 content/subject_key 以 deterministic_lexical_v1
做词面二次打分 → 按内部 memory_item_id 在服务端内存合并去重 → RRF
（Reciprocal Rank Fusion）融合排名 → 返回脱敏候选列表。仅供人工判断混合召回
质量，不接入聊天上下文。

lexical 侧边界声明：本阶段 lexical 是对 vector RPC top-10 候选的二次排序，
不是全量 active lexical 检索——词面强命中但向量未进入 RPC top-10 的记忆不会被
本预览返回。这是当前最小实现（零额外 SELECT、零额外表查询）；若未来需要全量
词面参与，必须单独增加受限 active SELECT 并说明上限与性能。

融合排序：rrf_score = 1/(RRF_K + vector_rank) + 1/(RRF_K + lexical_rank)；
RRF_K=60（排名融合参数，不是相似度阈值）。只使用排名参与 RRF，绝不使用
w1*similarity + w2*score 之类的固定分数权重（未经校准的权重被明确禁止）；
一侧没有的候选该侧贡献为 0；vector_similarity 与 lexical_score 保留原值；
rrf_score 只用于预览排序，不自动判定记忆正确，不设任何相似度阈值
（threshold_applied 恒 false，低相似度候选照常返回，由人工判断）。
lexical_score=0（无任何词面重合证据）的候选不进入 lexical 排名
（lexical_rank=null、lexical 贡献 0），与词面召回模块"完全无词面重合不返回"
的既有语义一致。

职责边界（与第 21/35 阶段同构）：
  1. 防御性复验 query（字符串、trim 非空、≤500 字符）与 top_k（整数非 bool、
     1~10），非法直接拒绝：零 provider 调用、零 RPC、零数据库访问；
  2. 调用注入的 embedding callable（生产路径 server._get_embedding）恰一次；
  3. 校验查询向量（复用第 31 阶段 validate_vector 同一实现）；
  4. 经注入的只读 RPC callable 恰调用一次 match_memory_items（match_count
     服务端固定 10；p_user_id 服务端解析值；RPC 固定 active-only/未过期/
     embedding 非空；绝不调用任何旧词面召回 RPC）；
  5. 校验 RPC 返回结构（信任边界，复用第 35 阶段同一实现）：结构违规整体
     拒绝，不静默丢弃；
  6. 内存二次过滤（复用第 35 阶段同一实现）：内部 memory_item_id 重复只保留
     首个并计数；显式非 active 状态/已过期/时间不可解析逐行保守丢弃并计数；
  7. 词面二次打分：lexical 算法本体（规范化与 _score_candidate）经 import
     复用第 21 阶段模块，杜绝复制漂移；不修改词面算法；不修改 vector RPC；
  8. RRF 排名融合后按 rrf_score 降序 → importance 仅最终稳定 tie-break →
     稳定序收尾，截断 top_k；内部 memory_item_id 仅在服务端内存用于去重与
     排名，绝不进入 HTTP 响应与日志；
  9. 不写任何数据库（无增改删/UPSERT），不更新 recall_count/last_recalled_at/
     updated_at；不读取、不复制、不写入 Pinecone；不调用 LLM；不新建
     embedding 客户端；不读取环境变量；无自动调度；不接正式聊天上下文；
 10. 返回安全响应结构 + 一条安全日志行（不含查询原文、正文、ID、user_id、
     模型名、provider、向量、RPC 原始行、hash、来源、密钥、SQL、异常原文）。

RPC 投影说明：第 30 阶段迁移固定返回 10 列，不含 updated_at，故最终稳定
tie-break 只能用 importance + 稳定序（vector similarity 序 / RPC 原序）。
"""

import asyncio
import datetime

# ── 算法与信任边界复用（同一实现，杜绝复制漂移；不修改被复用模块）──
# 词面算法本体来自第 21 阶段（deterministic_lexical_v1 不变）
from memory_recall import (_normalize_text, _compact, _cjk_bigrams, _tokens,
                           _score_candidate)
# 信任边界 / 二次过滤 / 输出脱敏 / RPC 常量来自第 35 阶段
from memory_vector_recall import (RPC_NAME, MATCH_COUNT, _parse_rpc_row,
                                  _row_state, _safe_text, _safe_ts, _safe_int,
                                  _safe_float)
# 查询向量校验来自第 31 阶段
from memory_embedding import validate_vector, EXPECTED_EMBEDDING_DIMENSION

# 客户端确认令牌（gateway handler 校验同一字面量；导出供测试引用）
CONFIRM_TOKEN = "HYBRID_RECALL_PREVIEW_ONLY"

# 融合方法声明
METHOD_NAME = "rrf_hybrid_preview_v1"
VECTOR_METHOD_NAME = "pgvector_cosine_vector_recall_v1"
LEXICAL_METHOD_NAME = "deterministic_lexical_v1"

# RRF 排名融合参数（排序参数，不是相似度阈值；未经校准不调整）
RRF_K = 60

# top_k / query 上限（与第 35 阶段同值同语义：top_k 只截断预览列表，
# 绝不影响 RPC 参数）
DEFAULT_TOP_K = 5
TOP_K_MIN = 1
TOP_K_MAX = 10
QUERY_MAX_LENGTH = 500

# 稳定错误码（embedding/RPC 错误与第 35 阶段同一分类，日志前缀区分阶段）
CODE_READY = "HYBRID_RECALL_PREVIEW_READY"
CODE_NO_RESULTS = "HYBRID_RECALL_NO_RESULTS"
CODE_INVALID_REQUEST = "INVALID_HYBRID_RECALL_REQUEST"
CODE_INVALID_CONFIRMATION = "INVALID_CONFIRMATION"
CODE_UNAVAILABLE = "EMBEDDING_UNAVAILABLE"
CODE_RESPONSE_INVALID = "EMBEDDING_RESPONSE_INVALID"
CODE_NON_FINITE = "EMBEDDING_NON_FINITE_VALUES"
CODE_DIMENSION_MISMATCH = "EMBEDDING_DIMENSION_MISMATCH"
CODE_ZERO_VECTOR = "EMBEDDING_ZERO_VECTOR"
CODE_RPC_FAILED = "VECTOR_RPC_FAILED"
CODE_RPC_RESPONSE_INVALID = "VECTOR_RPC_RESPONSE_INVALID"
CODE_INTERNAL = "INTERNAL_ERROR"

# gateway handler 的 HTTP 状态映射（与第 35 阶段同构）：
# 无结果 → 200（空召回是正常诊断结果，ok=true）；请求非法 → 400；
# 模型/provider 不可用 → 503；RPC/内部错误 → 500。
HTTP_STATUS_BY_CODE = {
    CODE_READY: 200,
    CODE_NO_RESULTS: 200,
    CODE_INVALID_REQUEST: 400,
    CODE_INVALID_CONFIRMATION: 400,
    CODE_UNAVAILABLE: 503,
    CODE_RESPONSE_INVALID: 503,
    CODE_NON_FINITE: 503,
    CODE_DIMENSION_MISMATCH: 503,
    CODE_ZERO_VECTOR: 503,
    CODE_RPC_FAILED: 500,
    CODE_RPC_RESPONSE_INVALID: 500,
    CODE_INTERNAL: 500,
}


def _result(ok, code, query_embedded, dimension, vector_candidates,
            lexical_candidates, merged_candidates, returned, items=None):
    """统一响应形状（stats 恰 7 键；retrieval 为静态声明）。

    active-only / 过期排除 / 用户隔离由 RPC SQL 固定过滤 + 模块二次过滤共同
    保证；threshold_applied 恒 false（本阶段不设任何硬相似度阈值）；
    writes_executed 恒 false（本模块无任何写方法）。
    """
    return {
        "ok": ok,
        "code": code,
        "stats": {
            "query_embedded": query_embedded,
            "dimension": dimension,
            "vector_candidates": vector_candidates,
            "lexical_candidates": lexical_candidates,
            "merged_candidates": merged_candidates,
            "returned": returned,
            "threshold_applied": False,
        },
        "retrieval": {
            "method": METHOD_NAME,
            "vector_method": VECTOR_METHOD_NAME,
            "lexical_method": LEXICAL_METHOD_NAME,
            "active_only": True,
            "expired_excluded": True,
            "user_scoped": True,
            "threshold_applied": False,
            "writes_executed": False,
        },
        "items": items if items is not None else [],
    }


def _empty_stats(query_embedded=False, dimension=None):
    return (query_embedded, dimension, 0, 0, 0, 0)


def _log_fail(stage, code, extra=""):
    """失败日志：只含 stage / 安全错误码 / 少量计数与维度；绝无查询原文、
    正文、ID、user_id、模型名、向量、RPC 行、异常原文。"""
    line = f"⚠️ 混合召回预览失败：stage={stage} error={code}"
    if extra:
        line += f" {extra}"
    return line


def _lexical_score(query_text, content, subject_key):
    """对单条候选做 deterministic_lexical_v1 词面打分（算法本体经 import
    复用第 21 阶段模块；输入输出语义与词面召回完全一致）。

    返回 (score, reasons)；score ∈ [0,1]，reasons 为空表示完全无词面重合。
    """
    q_norm = _normalize_text(query_text)
    q_compact = _compact(q_norm)
    q_bi = _cjk_bigrams(q_norm)
    q_tok = _tokens(q_norm)
    c_norm = _normalize_text(content)
    s_norm = _normalize_text(subject_key)
    score, reasons = _score_candidate(
        q_compact, q_bi, q_tok,
        c_norm, _compact(c_norm), _cjk_bigrams(c_norm), _tokens(c_norm),
        _compact(s_norm), _cjk_bigrams(s_norm), _tokens(s_norm))
    return score, reasons


async def run_hybrid_recall(query, server_user_id, embedding_fn, rpc_fn,
                            top_k=DEFAULT_TOP_K):
    """lexical + vector 混合召回只读预览执行体
    （gateway /api/memory-hybrid-recall-preview 调用）。

    query:           用户自然语言查询文本（handler 已校验；此处防御性复验）。
    server_user_id:  服务端统一解析的 user_id（客户端无任何提交入口）。
    embedding_fn:    生产路径为 server._get_embedding（恰调用一次）。
    rpc_fn:          注入的只读 RPC callable（生产路径为 gateway 构造的
                     service_role 客户端调用；恰调用一次，无重试）。
    top_k:           预览列表截断上限（1~10；绝不影响 RPC 参数）。

    返回 (result, log_line)：result 为可直接作为 HTTP JSON 响应体的安全结构
    （不含内部 ID / user_id / 向量 / 模型名 / provider / 异常原文）；
    log_line 为一条只含计数的安全日志行。
    """
    # 0. 依赖与请求防御性复验（全部通过前不触 provider、不触 RPC、不做词面）
    if not isinstance(server_user_id, str) or not server_user_id.strip():
        return (_result(False, CODE_INTERNAL, *_empty_stats()),
                _log_fail("dependency_check", CODE_INTERNAL))
    server_user_id = server_user_id.strip()
    if not callable(embedding_fn) or not callable(rpc_fn):
        return (_result(False, CODE_INTERNAL, *_empty_stats()),
                _log_fail("dependency_check", CODE_INTERNAL))
    if not isinstance(query, str):
        return (_result(False, CODE_INVALID_REQUEST, *_empty_stats()),
                _log_fail("request_check", CODE_INVALID_REQUEST))
    query_text = query.strip()
    if not query_text or len(query_text) > QUERY_MAX_LENGTH:
        return (_result(False, CODE_INVALID_REQUEST, *_empty_stats()),
                _log_fail("request_check", CODE_INVALID_REQUEST,
                          f"query_chars={len(query_text)}"))
    if (isinstance(top_k, bool) or not isinstance(top_k, int)
            or not (TOP_K_MIN <= top_k <= TOP_K_MAX)):
        return (_result(False, CODE_INVALID_REQUEST, *_empty_stats()),
                _log_fail("request_check", CODE_INVALID_REQUEST))

    # 1. provider 恰调用一次：输入恒为 trim 后查询文本；不自动重试
    try:
        raw = await asyncio.to_thread(embedding_fn, query_text)
    except Exception as e:  # noqa: BLE001
        return (_result(False, CODE_INTERNAL, *_empty_stats()),
                _log_fail("provider_call", CODE_INTERNAL,
                          f"exception_type={type(e).__name__}"))

    # 2. 查询向量严格校验（空/类型/数值/finite/维度/零向量）
    values, failure = validate_vector(raw)
    if failure is not None:
        code, actual_dim = failure
        return (_result(False, code, *_empty_stats()),
                _log_fail("embedding", code,
                          f"provider_calls=1 rpc_calls=0 "
                          f"actual_dim={actual_dim}"))

    # 3. 只读 RPC 恰调用一次：list[float] 为 PostgREST vector 参数官方 JSON
    #    数组格式；match_count 服务端固定；p_user_id 服务端解析值
    params = {"query_embedding": values,
              "p_user_id": server_user_id,
              "match_count": MATCH_COUNT}
    try:
        rpc_res = await asyncio.to_thread(rpc_fn, params)
    except Exception as e:  # noqa: BLE001 —— RPC 异常只记类型，不外泄原文
        return (_result(False, CODE_RPC_FAILED, True,
                        EXPECTED_EMBEDDING_DIMENSION, 0, 0, 0, 0),
                _log_fail("rpc_call", CODE_RPC_FAILED,
                          f"exception_type={type(e).__name__} "
                          f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))

    # 4. RPC 返回结构校验（信任边界；任一违规整体拒绝，不静默丢弃）
    rpc_rows = getattr(rpc_res, "data", None)
    if not isinstance(rpc_rows, list):
        return (_result(False, CODE_RPC_RESPONSE_INVALID, True,
                        EXPECTED_EMBEDDING_DIMENSION, 0, 0, 0, 0),
                _log_fail("rpc_response", CODE_RPC_RESPONSE_INVALID,
                          f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))
    if len(rpc_rows) > MATCH_COUNT:
        return (_result(False, CODE_RPC_RESPONSE_INVALID, True,
                        EXPECTED_EMBEDDING_DIMENSION, 0, 0, 0, 0),
                _log_fail("rpc_response", CODE_RPC_RESPONSE_INVALID,
                          f"rows={len(rpc_rows)} reason=row_count "
                          f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))
    pairs = []
    for row in rpc_rows:
        internal, bad = _parse_rpc_row(row)
        if bad is not None:
            return (_result(False, CODE_RPC_RESPONSE_INVALID, True,
                            EXPECTED_EMBEDDING_DIMENSION, 0, 0, 0, 0),
                    _log_fail("rpc_response", CODE_RPC_RESPONSE_INVALID,
                              f"rows={len(rpc_rows)} reason={bad} "
                              f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))
        pairs.append((internal, row))

    # 5. 内存二次过滤（复用第 35 阶段同一实现；逐行保守丢弃并计数；
    #    内部 memory_item_id 去重只保留首个；被丢弃行不暴露任何内容）
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    kept = []
    status_filtered = expired_filtered = invalid_time_filtered = 0
    duplicate_filtered = 0
    seen_ids = set()
    for internal, raw_row in pairs:
        mid = internal["memory_item_id"]
        if mid in seen_ids:
            duplicate_filtered += 1
            continue
        seen_ids.add(mid)
        ok, reason = _row_state(raw_row, now_utc)
        if not ok:
            if reason == "status":
                status_filtered += 1
            elif reason == "expired":
                expired_filtered += 1
            else:
                invalid_time_filtered += 1
            continue
        kept.append((internal, raw_row))

    vector_candidates = len(kept)

    if not kept:
        return (_result(True, CODE_NO_RESULTS, True,
                        EXPECTED_EMBEDDING_DIMENSION, 0, 0, 0, 0),
                f"🔀 混合召回预览：embedded=true rpc_returned={len(rpc_rows)} "
                f"vector_candidates=0 lexical_candidates=0 merged=0 "
                f"returned=0 status_filtered={status_filtered} "
                f"expired_filtered={expired_filtered} "
                f"invalid_time_filtered={invalid_time_filtered} "
                f"duplicate_filtered={duplicate_filtered}")

    # 6. vector 排名：similarity 降序（稳定排序，同分保持 RPC 原序），
    #    1 基排名；每条存活候选都有 vector_rank（本阶段 lexical 是对同一批
    #    vector 候选的二次排序，不存在仅 lexical 侧的候选）
    vector_order = sorted(range(len(kept)),
                          key=lambda i: -kept[i][0]["similarity"])
    vector_rank_by_idx = {}
    for rank_pos, idx in enumerate(vector_order, start=1):
        vector_rank_by_idx[idx] = rank_pos

    # 7. lexical 二次打分（对同一批 vector 候选；词面算法本体复用第 21 阶段）。
    #    lexical 是纯内存计算，失败（防御意外异常）整体返回 INTERNAL_ERROR，
    #    绝不伪装成 vector 侧成功或 vector 侧失败
    lex_scores = []
    try:
        for internal, raw_row in kept:
            score, _reasons = _lexical_score(query_text, internal["content"],
                                             raw_row.get("subject_key"))
            lex_scores.append(score)
    except Exception as e:  # noqa: BLE001 —— 异常只记类型，不外泄原文
        return (_result(False, CODE_INTERNAL, True,
                        EXPECTED_EMBEDDING_DIMENSION, vector_candidates, 0,
                        vector_candidates, 0),
                _log_fail("lexical_scoring", CODE_INTERNAL,
                          f"exception_type={type(e).__name__} "
                          f"vector_candidates={vector_candidates}"))

    # lexical 排名：仅 lexical_score > 0（有词面重合证据）的候选参与；
    # score 降序 → importance 降序 → RPC 原序稳定收尾（RPC 投影无 updated_at，
    # 不能作为 tie-break），1 基排名
    lex_idx = [i for i, s in enumerate(lex_scores) if s > 0]
    lexical_candidates = len(lex_idx)
    lex_idx.sort(key=lambda i: (-lex_scores[i],
                                -float(kept[i][1].get("importance") or 0), i))
    lexical_rank_by_idx = {}
    for rank_pos, idx in enumerate(lex_idx, start=1):
        lexical_rank_by_idx[idx] = rank_pos

    # 8. RRF 融合：只使用排名；一侧没有的候选该侧贡献为 0；
    #    vector_similarity / lexical_score 保留原值
    merged = []
    for idx, (internal, raw_row) in enumerate(kept):
        v_rank = vector_rank_by_idx[idx]
        l_rank = lexical_rank_by_idx.get(idx)
        rrf = (1.0 / (RRF_K + v_rank) if v_rank is not None else 0.0)
        if l_rank is not None:
            rrf += 1.0 / (RRF_K + l_rank)
        sources = ["vector"] + (["lexical"] if l_rank is not None else [])
        merged.append({
            "idx": idx,
            "internal": internal,
            "raw": raw_row,
            "similarity": internal["similarity"],
            "lexical_score": lex_scores[idx],
            "vector_rank": v_rank,
            "lexical_rank": l_rank,
            "rrf": rrf,
            "importance": float(raw_row.get("importance") or 0),
            "sources": sources,
        })

    # 9. 最终排序：rrf_score 降序 → importance 仅最终稳定 tie-break（RPC
    #    投影无 updated_at）→ vector similarity 序稳定收尾；再 top_k 截断
    merged.sort(key=lambda e: (-e["rrf"], -e["importance"],
                               e["vector_rank"]))
    chosen = merged[:top_k]

    # 10. 白名单脱敏条目（内部 ID 绝不外发）
    items = []
    for i, e in enumerate(chosen, start=1):
        raw = e["raw"]
        items.append({
            "recall_index": i,
            "rank": i,
            "memory_type": _safe_text(raw.get("memory_type")),
            "content": e["internal"]["content"],
            "importance": _safe_int(raw.get("importance")),
            "confidence": _safe_float(raw.get("confidence")),
            "subject_key": _safe_text(raw.get("subject_key")),
            "valid_at": _safe_ts(raw.get("valid_at")),
            "expires_at": _safe_ts(raw.get("expires_at")),
            "source": _safe_text(raw.get("source")),
            "vector_similarity": round(e["similarity"], 6),
            "lexical_score": e["lexical_score"],
            "rrf_score": round(e["rrf"], 6),
            "vector_rank": e["vector_rank"],
            "lexical_rank": e["lexical_rank"],
            "retrieval_sources": list(e["sources"]),
        })

    counters = (f"status_filtered={status_filtered} "
                f"expired_filtered={expired_filtered} "
                f"invalid_time_filtered={invalid_time_filtered} "
                f"duplicate_filtered={duplicate_filtered}")
    if not items:
        return (_result(True, CODE_NO_RESULTS, True,
                        EXPECTED_EMBEDDING_DIMENSION, vector_candidates,
                        lexical_candidates, vector_candidates, 0),
                f"🔀 混合召回预览：embedded=true "
                f"rpc_returned={len(rpc_rows)} "
                f"vector_candidates={vector_candidates} "
                f"lexical_candidates={lexical_candidates} "
                f"merged={vector_candidates} returned=0 {counters}")
    return (_result(True, CODE_READY, True, EXPECTED_EMBEDDING_DIMENSION,
                    vector_candidates, lexical_candidates, vector_candidates,
                    len(items), items),
            f"🔀 混合召回预览：embedded=true rpc_returned={len(rpc_rows)} "
            f"vector_candidates={vector_candidates} "
            f"lexical_candidates={lexical_candidates} "
            f"merged={vector_candidates} returned={len(items)} {counters}")
