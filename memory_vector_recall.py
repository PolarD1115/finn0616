# -*- coding: utf-8 -*-
"""第 35 阶段：用户自然语言查询的向量召回只读预览（受保护、手动、零写入）。

背景：第 30 阶段已建成 memory_items 向量基础设施（vector(1024) 三列 + triplet
CHECK + HNSW cosine 索引 + service_role-only 的 active-only 余弦召回 RPC），
第 31 阶段回填执行器、第 33/34 阶段自匹配预览均已通过生产验收（生产实测
VECTOR_SELF_MATCH_READY，top1_similarity=1.0，VECTOR_RPC_GATE_PASS），证明
memory_items content → 当前 embedding provider → 1024 维查询向量 → service_role
RPC → pgvector cosine 查询 → Top1 自匹配的基础链路正常。

尚未验证的是：用户自然语言查询能否通过 embedding 找到相关 active 记忆、相似度
分布如何、不相关查询是否误召回、多条 active 时的排序质量。本模块提供手动触发
的只读召回预览执行体：用户提交一条自然语言查询 → 服务端用现有 _get_embedding
恰嵌入一次 → 经 service_role 恰调用一次第 30 阶段 active-only 余弦召回 RPC →
内存二次过滤（状态/过期/时间可解析性/去重）→ 返回脱敏候选列表。接口不直接
接入聊天上下文，仅供人工判断召回质量。

职责边界（gateway handler 只负责路由/鉴权/请求校验/user_id 解析/依赖注入/
构造 service_role RPC callable）：
  1. 防御性复验 query（字符串、trim 非空、≤500 字符）与 top_k（整数非 bool、
     1~10），非法直接拒绝：零 provider 调用、零 RPC、零数据库访问；
  2. 调用注入的 embedding callable（生产路径 server._get_embedding）恰一次，
     输入恒为 trim 后的用户查询文本；最多一次，不自动重试；
  3. 校验查询向量（复用第 31 阶段 memory_embedding.validate_vector 同一实现）：
     空 / 非 list / 元素不可转 float / NaN、Inf / 维度非 1024 / 全零，逐一拒绝；
  4. 以 list[float]（PostgREST vector 参数官方 JSON 数组 wire 格式）经注入的
     只读 RPC callable 恰调用一次第 30 阶段 match_memory_items；match_count
     服务端固定 10（客户端 top_k 只截断预览列表，绝不作为 RPC 参数，避免请求
     形态影响数据库候选集与查询计划）；p_user_id 为服务端解析值；绝不使用
     anon 客户端、绝不调用任何旧词面召回 RPC；
  5. 校验 RPC 返回结构（信任边界）：必须是列表、行数 ≤ match_count、每行是
     dict 且含非空 memory_item_id、非空 content、similarity 可转 float /
     finite / [-1,1] 容差内；任一结构违规 → 整体 VECTOR_RPC_RESPONSE_INVALID
     （与第 33 阶段同一严格语义：结构违规不静默丢弃，防止掩盖契约破坏）；
  6. 内存二次过滤（数据库条件失效时的最后防线；逐行保守丢弃并计数、不中断、
     不暴露被丢弃行的任何内容）：显式携带非 active 状态字段的行丢弃；expires_at
     已过期丢弃；expires_at 存在但无法解析保守丢弃；memory_item_id 重复只保留
     首个（RPC 距离序中的首个）并计数。不把高 importance 当作召回理由（本模块
     根本不按 importance 过滤），不把低 similarity 自动解释为错误；
  7. 存活候选按 similarity 降序（稳定排序，同分保持 RPC 原序）后截断 top_k；
     rank / recall_index 为最终列表中的 1 基序号；内部 memory_item_id 仅在
     服务端内存中用于去重与调试核对，绝不进入 HTTP 响应与日志；
  8. 不设任何相似度硬过滤阈值：RPC 返回并存活过二次过滤的候选全部作为预览
     候选返回，threshold_applied 恒为 false；低相似度候选照常返回，是否可用
     由人工判断（仅 1 条 active 自匹配样本不足以校准正式阈值）；
  9. 返回安全响应结构 + 一条安全日志行（不含查询原文、正文、ID、user_id、
     模型名、provider、向量、RPC 原始行、hash、来源、密钥、SQL、异常原文）。

硬性边界：
  - 对数据库仅一次只读 RPC（经注入 callable）；模块自身不持有任何数据库客户端、
    不做任何直接表查询；无任何写入方法；不删除任何数据；
  - 不更新召回次数 / last_recalled_at / updated_at 等任何列；
  - 不读取、不复制、不写入 Pinecone；不调用 LLM；不新建 embedding 客户端；
  - 不读取环境变量；不自动调度（无异步任务派发、无定时器、无线程循环）；
  - 不接正式聊天上下文；不接入词面召回模块；不实现词面+向量混合；不修改
    词面召回；
  - similarity 仅表示 pgvector cosine 相似度，不是记忆置信度、不是回答正确
    概率；返回条目不代表会被注入正式上下文。
"""

import asyncio
import datetime
import math

# 复用第 31 阶段查询向量校验语义（同一实现，避免复制粘贴漂移）
from memory_embedding import validate_vector, EXPECTED_EMBEDDING_DIMENSION

# 客户端确认令牌（gateway handler 校验同一字面量；导出供测试引用）
CONFIRM_TOKEN = "VECTOR_RECALL_PREVIEW_ONLY"

# 第 30 阶段只读 RPC 名（service_role-only；绝不调用任何旧词面召回 RPC）
RPC_NAME = "match_memory_items"

# match_count 服务端固定值（RPC 侧约束 1..10）：客户端 top_k 只截断预览列表，
# 绝不作为 RPC 参数
MATCH_COUNT = 10

# top_k：预览列表截断上限（服务端校验范围；缺省 5）
DEFAULT_TOP_K = 5
TOP_K_MIN = 1
TOP_K_MAX = 10

# query 长度上限（trim 后字符数）
QUERY_MAX_LENGTH = 500

_ACTIVE_STATUS = "active"

# similarity 合法范围容差（浮点余弦理论值域 [-1,1]，允许微小越界）
_SIM_TOLERANCE = 1e-6

# 稳定错误码
CODE_READY = "VECTOR_RECALL_PREVIEW_READY"
CODE_NO_RESULTS = "NO_VECTOR_RECALL_RESULTS"
CODE_INVALID_REQUEST = "INVALID_VECTOR_RECALL_REQUEST"
CODE_INVALID_CONFIRMATION = "INVALID_CONFIRMATION"
CODE_UNAVAILABLE = "EMBEDDING_UNAVAILABLE"
CODE_RESPONSE_INVALID = "EMBEDDING_RESPONSE_INVALID"
CODE_NON_FINITE = "EMBEDDING_NON_FINITE_VALUES"
CODE_DIMENSION_MISMATCH = "EMBEDDING_DIMENSION_MISMATCH"
CODE_ZERO_VECTOR = "EMBEDDING_ZERO_VECTOR"
CODE_RPC_FAILED = "VECTOR_RPC_FAILED"
CODE_RPC_RESPONSE_INVALID = "VECTOR_RPC_RESPONSE_INVALID"
CODE_INTERNAL = "INTERNAL_ERROR"

# gateway handler 的 HTTP 状态映射（请求校验 400 由 handler 自己处理）：
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


def _result(ok, code, query_embedded, dimension, rpc_returned, returned,
            status_filtered, expired_filtered, invalid_time_filtered,
            duplicate_filtered, items=None):
    """统一响应形状（任务书第七节字段一一对应，无额外键）。

    retrieval 为静态声明：active-only / 过期排除 / 用户隔离由 RPC SQL 固定
    过滤 + 本模块二次过滤共同保证；threshold_applied 恒 false（本阶段不设
    任何硬相似度阈值）；writes_executed 恒 false（本模块无任何写方法）。
    """
    return {
        "ok": ok,
        "code": code,
        "stats": {
            "query_embedded": query_embedded,
            "dimension": dimension,
            "rpc_returned": rpc_returned,
            "returned": returned,
            "status_filtered": status_filtered,
            "expired_filtered": expired_filtered,
            "invalid_time_filtered": invalid_time_filtered,
            "duplicate_filtered": duplicate_filtered,
        },
        "retrieval": {
            "method": "pgvector_cosine_vector_recall_v1",
            "active_only": True,
            "expired_excluded": True,
            "user_scoped": True,
            "threshold_applied": False,
            "writes_executed": False,
        },
        "items": items if items is not None else [],
    }


def _utc_now():
    """当前 UTC aware 时间。"""
    return datetime.datetime.now(datetime.timezone.utc)


def _log_fail(stage, code, extra=""):
    """失败日志：只含 stage / 安全错误码 / 少量计数与维度；绝无查询原文、
    正文、ID、user_id、模型名、向量、RPC 行、异常原文。"""
    line = f"⚠️ 向量召回预览失败：stage={stage} error={code}"
    if extra:
        line += f" {extra}"
    return line


def _parse_utc(value):
    """expires_at 的宽容解析：datetime 直用（naive 补 UTC），ISO 字符串尝试
    fromisoformat；其余（含解析失败）返回 None。"""
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.datetime.fromisoformat(
                value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _safe_text(value):
    return value if isinstance(value, str) else None


def _safe_ts(value):
    """时间戳字段脱敏透传：字符串原样、datetime 转 ISO、其余置 None。"""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return None


def _safe_int(value):
    """整数字段脱敏透传（bool 不是 int 语义）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _safe_float(value):
    """数值字段脱敏透传：可转有限 float 才保留。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _parse_rpc_row(row):
    """RPC 行结构校验（信任边界）。返回 (internal, None) 或 (None, 原因)。

    internal 只含服务端内部字段：memory_item_id（仅用于内存去重与调试核对，
    绝不外发）、content、similarity（全精度，供排序）。其余字段不信任、
    不保留；输出白名单字段在最终条目组装时另行从原始行脱敏提取。
    """
    if not isinstance(row, dict):
        return None, "row_type"
    mid = row.get("memory_item_id")
    if mid is None or not str(mid).strip():
        return None, "memory_item_id"
    content = row.get("content")
    if not isinstance(content, str) or not content.strip():
        return None, "content"
    try:
        sim = float(row.get("similarity"))
    except (TypeError, ValueError, OverflowError):
        return None, "similarity"
    if not math.isfinite(sim):
        return None, "similarity"
    if sim < -1.0 - _SIM_TOLERANCE or sim > 1.0 + _SIM_TOLERANCE:
        return None, "similarity"
    return {"memory_item_id": str(mid).strip(),
            "content": content,
            "similarity": sim}, None


def _row_state(raw_row, now_utc):
    """内存二次过滤（数据库条件失效时的最后防线；逐行保守判断）。
    返回 (ok, drop_reason)：
    - (True, "")：保留；
    - (False, "status")：显式携带非 active 状态字段；
    - (False, "expired")：expires_at 已过期；
    - (False, "time_invalid")：expires_at 存在但无法解析（保守丢弃）。

    第 30 阶段 RPC SQL 已固定过滤 status=active 与未过期且 embedding 非空，
    此处仅防御异常返回；行未显式携带 status 字段时以 SQL 过滤为准。"""
    status = raw_row.get("status")
    if status is not None and status != _ACTIVE_STATUS:
        return False, "status"
    raw_exp = raw_row.get("expires_at")
    if raw_exp is None:
        return True, ""
    exp = _parse_utc(raw_exp)
    if exp is None:
        return False, "time_invalid"
    if exp <= now_utc:
        return False, "expired"
    return True, ""


async def run_recall(query, server_user_id, embedding_fn, rpc_fn,
                     top_k=DEFAULT_TOP_K):
    """用户查询向量召回只读预览执行体
    （gateway /api/memory-vector-recall-preview 调用）。

    query:           用户自然语言查询文本（handler 已校验；此处防御性复验）。
    server_user_id:  服务端统一解析的 user_id（gateway 解析后传入，客户端无
                     任何提交入口）。
    embedding_fn:    生产路径为 server._get_embedding（恰调用一次，输入恒为
                     trim 后查询文本；失败统一返回空，不重试）。
    rpc_fn:          注入的只读 RPC callable（生产路径为 gateway 构造的
                     service_role 客户端调用，签名 rpc_fn(params) → 响应对象，
                     响应含 .data 行列表；恰调用一次，无重试）。
    top_k:           预览列表截断上限（1~10；绝不影响 RPC 参数）。

    返回 (result, log_line)：result 为可直接作为 HTTP JSON 响应体的安全结构
    （不含内部 ID / user_id / 向量 / 模型名 / provider / 异常原文）；
    log_line 为一条只含计数的安全日志行。
    """
    # 0. 依赖与请求防御性复验（全部通过前不触 provider、不触 RPC）
    if not isinstance(server_user_id, str) or not server_user_id.strip():
        return (_result(False, CODE_INTERNAL, False, None, 0, 0, 0, 0, 0, 0),
                _log_fail("dependency_check", CODE_INTERNAL))
    server_user_id = server_user_id.strip()
    if not callable(embedding_fn) or not callable(rpc_fn):
        return (_result(False, CODE_INTERNAL, False, None, 0, 0, 0, 0, 0, 0),
                _log_fail("dependency_check", CODE_INTERNAL))
    if not isinstance(query, str):
        return (_result(False, CODE_INVALID_REQUEST, False, None,
                        0, 0, 0, 0, 0, 0),
                _log_fail("request_check", CODE_INVALID_REQUEST))
    query_text = query.strip()
    if not query_text or len(query_text) > QUERY_MAX_LENGTH:
        return (_result(False, CODE_INVALID_REQUEST, False, None,
                        0, 0, 0, 0, 0, 0),
                _log_fail("request_check", CODE_INVALID_REQUEST,
                          f"query_chars={len(query_text)}"))
    if (isinstance(top_k, bool) or not isinstance(top_k, int)
            or not (TOP_K_MIN <= top_k <= TOP_K_MAX)):
        return (_result(False, CODE_INVALID_REQUEST, False, None,
                        0, 0, 0, 0, 0, 0),
                _log_fail("request_check", CODE_INVALID_REQUEST))

    # 1. provider 恰调用一次：输入恒为 trim 后查询文本；不自动重试
    try:
        raw = await asyncio.to_thread(embedding_fn, query_text)
    except Exception as e:  # noqa: BLE001
        return (_result(False, CODE_INTERNAL, False, None, 0, 0, 0, 0, 0, 0),
                _log_fail("provider_call", CODE_INTERNAL,
                          f"exception_type={type(e).__name__}"))

    # 2. 查询向量严格校验（空/类型/数值/finite/维度/零向量）
    values, failure = validate_vector(raw)
    if failure is not None:
        code, actual_dim = failure
        return (_result(False, code, False, None, 0, 0, 0, 0, 0, 0),
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
                        EXPECTED_EMBEDDING_DIMENSION, 0, 0, 0, 0, 0, 0),
                _log_fail("rpc_call", CODE_RPC_FAILED,
                          f"exception_type={type(e).__name__} "
                          f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))

    # 4. RPC 返回结构校验（信任边界；任一违规整体拒绝，不静默丢弃）
    rpc_rows = getattr(rpc_res, "data", None)
    if not isinstance(rpc_rows, list):
        return (_result(False, CODE_RPC_RESPONSE_INVALID, True,
                        EXPECTED_EMBEDDING_DIMENSION, 0, 0, 0, 0, 0, 0),
                _log_fail("rpc_response", CODE_RPC_RESPONSE_INVALID,
                          f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))
    if len(rpc_rows) > MATCH_COUNT:
        return (_result(False, CODE_RPC_RESPONSE_INVALID, True,
                        EXPECTED_EMBEDDING_DIMENSION, len(rpc_rows),
                        0, 0, 0, 0, 0),
                _log_fail("rpc_response", CODE_RPC_RESPONSE_INVALID,
                          f"rows={len(rpc_rows)} reason=row_count "
                          f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))
    pairs = []
    for row in rpc_rows:
        internal, bad = _parse_rpc_row(row)
        if bad is not None:
            return (_result(False, CODE_RPC_RESPONSE_INVALID, True,
                            EXPECTED_EMBEDDING_DIMENSION, len(rpc_rows),
                            0, 0, 0, 0, 0),
                    _log_fail("rpc_response", CODE_RPC_RESPONSE_INVALID,
                              f"rows={len(rpc_rows)} reason={bad} "
                              f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))
        pairs.append((internal, row))

    # 5. 内存二次过滤（逐行保守丢弃并计数；被丢弃行不暴露任何内容）
    now_utc = _utc_now()
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

    # 6. similarity 降序（稳定排序，同分保持 RPC 原序）→ top_k 截断 →
    #    白名单脱敏条目（内部 ID 绝不外发）
    kept.sort(key=lambda pair: pair[0]["similarity"], reverse=True)
    items = []
    for idx, (internal, raw_row) in enumerate(kept[:top_k]):
        items.append({
            "recall_index": idx + 1,
            "rank": idx + 1,
            "memory_type": _safe_text(raw_row.get("memory_type")),
            "content": internal["content"],
            "importance": _safe_int(raw_row.get("importance")),
            "confidence": _safe_float(raw_row.get("confidence")),
            "subject_key": _safe_text(raw_row.get("subject_key")),
            "valid_at": _safe_ts(raw_row.get("valid_at")),
            "expires_at": _safe_ts(raw_row.get("expires_at")),
            "source": _safe_text(raw_row.get("source")),
            "similarity": round(internal["similarity"], 6),
        })

    counters = (f"status_filtered={status_filtered} "
                f"expired_filtered={expired_filtered} "
                f"invalid_time_filtered={invalid_time_filtered} "
                f"duplicate_filtered={duplicate_filtered}")
    if not items:
        return (_result(True, CODE_NO_RESULTS, True,
                        EXPECTED_EMBEDDING_DIMENSION, len(rpc_rows), 0,
                        status_filtered, expired_filtered,
                        invalid_time_filtered, duplicate_filtered),
                f"🔍 向量召回预览：embedded=true "
                f"rpc_returned={len(rpc_rows)} returned=0 {counters}")
    return (_result(True, CODE_READY, True, EXPECTED_EMBEDDING_DIMENSION,
                    len(rpc_rows), len(items), status_filtered,
                    expired_filtered, invalid_time_filtered,
                    duplicate_filtered, items),
            f"🔍 向量召回预览：embedded=true rpc_returned={len(rpc_rows)} "
            f"returned={len(items)} {counters}")
