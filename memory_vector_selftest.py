# -*- coding: utf-8 -*-
"""第 33 阶段：memory_items 向量 RPC 自匹配只读预览（受保护、手动、零写入）。

背景：第 30 阶段已建成 memory_items 向量基础设施（vector(1024) 三列 + triplet
CHECK + HNSW cosine 索引 + service_role-only 的 active-only 余弦召回 RPC），
第 31 阶段回填执行器已通过生产验收（当前唯一 active 记忆三列全非空、1024 维、
finite、非零）。尚未验证的是：该 RPC 的真实 PostgREST 调用通路、Python
list[float] 作为 RPC vector 参数的实际行为、当前 provider 对同一正文重新生成
的查询向量能否命中已存向量、RPC 内部 memory_item_id 能否用于服务端核对。

为什么用"active 记忆自身 content 的重新嵌入"作查询向量：固定无关探针只适合
确认维度——探针与记忆无关时，RPC 返回空不代表失败，返回唯一 active 也可能
只是 match_count 强制返回，相似度高低没有预期，无法判断内部 ID 合并是否正确。
自匹配设计只验证 vector self-match / RPC plumbing，不代表同义召回、自然语言
召回或 AI 伴侣记忆质量。

职责边界（gateway handler 只负责路由/鉴权/请求校验/依赖注入/构造 RPC callable）：
  1. 服务端只读选择一条候选：user_id = 服务端解析用户、status = active、
     embedding IS NOT NULL，created_at 升序最旧优先，limit 1；
     只读取 id/content/user_id/status/expires_at/embedding_model/embedded_at，
     绝不读取 embedding 向量值本身；
  2. 内存二次过滤（数据库条件失效时的最后防线）：行是 dict、id 非空、content
     非空字符串、user scope 一致、status=active、embedding_model 非空字符串、
     embedded_at 非空、expires_at 为空或晚于当前 UTC；时间无法解析时以
     ACTIVE_MEMORY_TIME_INVALID 终止（零 provider、零 RPC）；
  3. 模型一致性核对（trim 后比较）：当前配置模型为空 → 不查库（依赖检查阶段
     直接终止）；与库内 embedding_model 不一致 → 不调 provider、不调 RPC、
     不覆盖旧向量、不自动重嵌、不返回两个模型名。不同模型即使同为 1024 维，
     向量空间也可能完全不可比——维度一致不代表可查询；
  4. 调用注入的 embedding callable（生产路径 server._get_embedding）恰一次，
     输入恒为该行数据库 content；最多一次，不自动重试；
  5. 校验查询向量（复用第 31 阶段 memory_embedding.validate_vector 同一实现）：
     空 / 非 list / 元素不可转 float / NaN、Inf / 维度非 1024 / 全零，逐一拒绝；
  6. 以 list[float]（PostgREST vector 参数官方 JSON 数组 wire 格式，与 Supabase
     官方 Vector columns / Database Functions 文档 rpc 参数示例同一表示；错误时
     数据库 RAISE EXCEPTION 立即终止事务并向上以 APIError 呈现）经注入的
     RPC callable（生产路径 server.supabase_service.rpc 只读调用第 30 阶段
     match_memory_items，match_count 固定 5，客户端不可控制）恰调用一次；
     每请求最多一次，绝不使用 anon client、绝不调用任何旧词面召回 RPC；
  7. 校验 RPC 返回：必须是列表、行数不超 match_count、每行含非空
     memory_item_id 与可转 float / finite / [-1,1] 容差内 的 similarity；
     不信任、不保留任何其余字段；HTTP 不返回任何原始 RPC 行；
  8. Top1 内部 ID 核对：与被选中行的内部 id 全等比较（比较仅在服务端内存中
     进行，HTTP 与日志均不返回该 ID）；空结果 / Top1 不一致 / Top1 一致但
     similarity 低于 0.99 分别给出独立诊断码——同模型同正文重嵌对已存向量的
     cosine 理论上应非常接近 1，低于阈值提示 provider 非确定性、模型版本漂移、
     存储精度变化或输入处理差异（本阶段只读，不自动失败写入任何状态）；
  9. 返回安全响应结构 + 一条安全日志行（不含正文、ID、user_id、模型名、向量、
     RPC 行、hash、来源、密钥、SQL、异常原文）。

硬性边界：
  - 对数据库仅 SELECT + 只读 RPC 两种操作；无任何写入方法；不删除任何数据；
  - 不更新召回次数 / last_recalled_at / updated_at 等任何列；
  - 不读取、不复制、不写入 Pinecone；不调用 LLM；不新建 embedding 客户端；
  - 不读取环境变量（模型标识由 gateway 只读现有配置后传入，仅用于比对，
    不返回、不打印）；不自动调度（无异步任务派发、无定时器、无线程循环）；
  - 不接正式聊天上下文；不接入 lexical+vector 混合；不修改词面召回。
"""

import asyncio
import datetime
import math

# 复用第 31 阶段查询向量校验语义（同一实现，避免复制粘贴漂移）
from memory_embedding import validate_vector, EXPECTED_EMBEDDING_DIMENSION

# 客户端确认令牌（gateway handler 校验同一字面量；导出供测试引用）
CONFIRM_TOKEN = "VECTOR_SELFTEST_PREVIEW_ONLY"

# 第 30 阶段只读 RPC 名（service_role-only；绝不调用任何旧词面召回 RPC）
RPC_NAME = "match_memory_items"

# match_count 固定值（RPC 侧约束 1..10；客户端不可控制）
MATCH_COUNT = 5

# 自匹配相似度阈值：同模型同正文重嵌，cosine 理论上应非常接近 1；
# 不硬性要求等于 1，但低于该阈值视为 VECTOR_SELF_MATCH_LOW_SIMILARITY
MIN_SELF_SIMILARITY = 0.99

_ACTIVE_STATUS = "active"

# SELECT 列：id/content 用于自匹配核对；其余列用于内存二次过滤与模型一致性。
# 绝不读取 embedding 向量值、content_hash / source_event_ids / source_batch_id /
# metadata / superseded_by / importance / confidence 等列。
_SELECT_COLUMNS = "id,content,user_id,status,expires_at,embedding_model,embedded_at"

# similarity 合法范围容差（浮点余弦理论值域 [-1,1]，允许微小越界）
_SIM_TOLERANCE = 1e-6

# 稳定错误码
CODE_READY = "VECTOR_SELF_MATCH_READY"
CODE_NO_CANDIDATES = "NO_ACTIVE_EMBEDDED_MEMORIES"
CODE_TIME_INVALID = "ACTIVE_MEMORY_TIME_INVALID"
CODE_MODEL_NOT_CONFIGURED = "EMBEDDING_MODEL_NOT_CONFIGURED"
CODE_MODEL_MISMATCH = "EMBEDDING_MODEL_MISMATCH"
CODE_UNAVAILABLE = "EMBEDDING_UNAVAILABLE"
CODE_RESPONSE_INVALID = "EMBEDDING_RESPONSE_INVALID"
CODE_NON_FINITE = "EMBEDDING_NON_FINITE_VALUES"
CODE_DIMENSION_MISMATCH = "EMBEDDING_DIMENSION_MISMATCH"
CODE_ZERO_VECTOR = "EMBEDDING_ZERO_VECTOR"
CODE_RPC_FAILED = "VECTOR_RPC_FAILED"
CODE_RPC_RESPONSE_INVALID = "VECTOR_RPC_RESPONSE_INVALID"
CODE_NO_RESULTS = "VECTOR_SELF_MATCH_NO_RESULTS"
CODE_TOP1_MISMATCH = "VECTOR_SELF_MATCH_TOP1_MISMATCH"
CODE_LOW_SIMILARITY = "VECTOR_SELF_MATCH_LOW_SIMILARITY"
CODE_INTERNAL = "INTERNAL_ERROR"

# gateway handler 的 HTTP 状态映射（请求校验 400 由 handler 自己处理）：
# 无候选/时间不可解析 → 200；模型/provider 不可用 → 503；RPC/内部错误 → 500；
# Top1 不一致或低相似度是诊断结果而非服务器异常 → 200 + ok=false。
HTTP_STATUS_BY_CODE = {
    CODE_READY: 200,
    CODE_NO_CANDIDATES: 200,
    CODE_TIME_INVALID: 200,
    CODE_MODEL_NOT_CONFIGURED: 503,
    CODE_MODEL_MISMATCH: 503,
    CODE_UNAVAILABLE: 503,
    CODE_RESPONSE_INVALID: 503,
    CODE_NON_FINITE: 503,
    CODE_DIMENSION_MISMATCH: 503,
    CODE_ZERO_VECTOR: 503,
    CODE_RPC_FAILED: 500,
    CODE_RPC_RESPONSE_INVALID: 500,
    CODE_NO_RESULTS: 200,
    CODE_TOP1_MISMATCH: 200,
    CODE_LOW_SIMILARITY: 200,
    CODE_INTERNAL: 500,
}


def _result(ok, code, selected, rpc_returned, top1_match, top1_similarity,
            dimension, provider_calls, database_reads):
    """统一响应形状。database_writes 恒为 0（本模块无任何写方法）；
    database_reads 计 SELECT + 只读 RPC 次数；Pinecone 与 LLM 恒未触碰。"""
    return {
        "ok": ok,
        "code": code,
        "stats": {
            "selected": selected,
            "rpc_returned": rpc_returned,
            "top1_match": top1_match,
            "top1_similarity": top1_similarity,
            "dimension": dimension,
        },
        "retrieval": {
            "method": "pgvector_cosine_selftest_v1",
            "active_only": True,
            "expired_excluded": True,
            "user_scoped": True,
            "writes_executed": False,
        },
        "execution": {
            "provider_calls": provider_calls,
            "database_reads": database_reads,
            "database_writes": 0,
            "pinecone_touched": False,
            "llm_touched": False,
        },
    }


def _utc_now():
    """当前 UTC aware 时间。"""
    return datetime.datetime.now(datetime.timezone.utc)


def _log_fail(stage, code, extra=""):
    """失败日志：只含 stage / 安全错误码 / 少量计数；绝无正文、ID、模型名、
    向量、RPC 行、异常原文。"""
    line = f"⚠️ 向量自匹配预览失败：stage={stage} error={code}"
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


def _candidate_state(row, server_user_id, now_utc):
    """内存二次过滤。返回 (ok, time_invalid)：
    - (True, False)：候选可用；
    - (False, True)：expires_at 存在但无法解析 → ACTIVE_MEMORY_TIME_INVALID；
    - (False, False)：任一条件不满足 → 视为无候选。"""
    if not isinstance(row, dict):
        return False, False
    if row.get("id") is None or not str(row.get("id")).strip():
        return False, False
    content = row.get("content")
    if not isinstance(content, str) or not content.strip():
        return False, False
    if row.get("user_id") != server_user_id:
        return False, False
    if row.get("status") != _ACTIVE_STATUS:
        return False, False
    model = row.get("embedding_model")
    if not isinstance(model, str) or not model.strip():
        return False, False
    if row.get("embedded_at") is None:
        return False, False
    raw_exp = row.get("expires_at")
    if raw_exp is None:
        return True, False
    exp = _parse_utc(raw_exp)
    if exp is None:
        return False, True
    if exp <= now_utc:
        return False, False
    return True, False


def _validate_rpc_rows(rows):
    """校验 RPC 返回行。返回 (parsed, None) 或 (None, 失败原因字符串)。
    只读取 memory_item_id 与 similarity；不信任、不保留其余字段。"""
    if len(rows) > MATCH_COUNT:
        return None, "row_count"
    parsed = []
    for r in rows:
        if not isinstance(r, dict):
            return None, "row_type"
        mid = r.get("memory_item_id")
        if mid is None or not str(mid).strip():
            return None, "memory_item_id"
        try:
            sim = float(r.get("similarity"))
        except (TypeError, ValueError, OverflowError):
            return None, "similarity"
        if not math.isfinite(sim):
            return None, "similarity"
        if sim < -1.0 - _SIM_TOLERANCE or sim > 1.0 + _SIM_TOLERANCE:
            return None, "similarity"
        parsed.append({"memory_item_id": str(mid).strip(), "similarity": sim})
    return parsed, None


async def run_selftest(supabase_service, server_user_id, embedding_fn,
                       configured_model, rpc_fn):
    """memory_items 向量 RPC 自匹配预览执行体
    （gateway /api/memory-vector-selftest-preview 调用）。

    supabase_service: server.supabase_service（service_role；仅 SELECT 一种
                      直接表操作；只读 RPC 经 rpc_fn 注入调用）。
    server_user_id:   服务端统一解析的 user_id（gateway 解析后传入，
                      客户端无任何提交入口）。
    embedding_fn:     生产路径为 server._get_embedding（恰调用一次，输入恒为
                      库内 content；失败统一返回空，不重试）。
    configured_model: 当前配置的 embedding 模型标识（gateway 只读现有环境配置
                      后传入；仅用于与库内 embedding_model 比对，不返回、
                      不打印）。
    rpc_fn:           注入的只读 RPC callable（生产路径为 gateway 构造的
                      service_role 客户端调用，签名 rpc_fn(params) → 响应对象，
                      响应含 .data 行列表；恰调用一次，无重试）。

    返回 (result, log_line)：result 为可直接作为 HTTP JSON 响应体的安全结构；
    log_line 为一条不含正文/ID/user_id/模型名/向量/RPC 行/异常原文的安全日志行。
    """
    # 0. 依赖检查（全部通过前不查库、不触 provider）
    if supabase_service is None:
        return (_result(False, CODE_INTERNAL, 0, 0, False, None, None, 0, 0),
                _log_fail("dependency_check", CODE_INTERNAL))
    if not isinstance(server_user_id, str) or not server_user_id.strip():
        return (_result(False, CODE_INTERNAL, 0, 0, False, None, None, 0, 0),
                _log_fail("dependency_check", CODE_INTERNAL))
    server_user_id = server_user_id.strip()
    if not callable(embedding_fn) or not callable(rpc_fn):
        return (_result(False, CODE_INTERNAL, 0, 0, False, None, None, 0, 0),
                _log_fail("dependency_check", CODE_INTERNAL))
    if not isinstance(configured_model, str) or not configured_model.strip():
        # 当前模型未配置：provider 亦必然不可用，先行终止（零查库、零调用）
        return (_result(False, CODE_MODEL_NOT_CONFIGURED, 0, 0, False, None,
                        None, 0, 0),
                _log_fail("model_config", CODE_MODEL_NOT_CONFIGURED))
    configured_model = configured_model.strip()

    # 1. 服务端强制条件选择一条最旧 active 且 embedding 非空的候选
    #    （客户端不可指定任何条件；不读取 embedding 向量值）
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items")
            .select(_SELECT_COLUMNS)
            .eq("user_id", server_user_id)
            .eq("status", _ACTIVE_STATUS)
            .not_.is_("embedding", None)
            .order("created_at", desc=False)
            .limit(1)
            .execute())
        rows = [r for r in (getattr(res, "data", None) or []) if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001 —— 数据库异常只返回脱敏代码
        return (_result(False, CODE_INTERNAL, 0, 0, False, None, None, 0, 1),
                _log_fail("select", CODE_INTERNAL,
                          f"exception_type={type(e).__name__}"))

    selected = len(rows)
    if not rows:
        return (_result(True, CODE_NO_CANDIDATES, 0, 0, False, None, None, 0, 1),
                "🧪 向量自匹配预览：selected=0 rpc_returned=0 "
                "top1_match=false similarity=none")

    # 2. 内存二次过滤（含过期与时间可解析性判断）
    row = rows[0]
    ok, time_invalid = _candidate_state(row, server_user_id, _utc_now())
    if time_invalid:
        return (_result(False, CODE_TIME_INVALID, 1, 0, False, None, None, 0, 1),
                _log_fail("candidate_check", CODE_TIME_INVALID,
                          "selected=1 returned=0 dimension=none"))
    if not ok:
        return (_result(False, CODE_NO_CANDIDATES, 1, 0, False, None, None, 0, 1),
                _log_fail("candidate_check", CODE_NO_CANDIDATES,
                          "selected=1 returned=0 dimension=none"))

    content = row["content"]
    item_id = str(row["id"]).strip()

    # 3. 模型一致性核对（trim 后比较）：不一致绝不调 provider、绝不调 RPC、
    #    绝不覆盖旧向量；不返回两个模型名
    stored_model = row["embedding_model"].strip()
    if stored_model != configured_model:
        return (_result(False, CODE_MODEL_MISMATCH, 1, 0, False, None, None, 0, 1),
                _log_fail("model_check", CODE_MODEL_MISMATCH,
                          "selected=1 returned=0 dimension=none"))

    # 4. provider 恰调用一次：输入恒为该行事实化 content；不自动重试
    try:
        raw = await asyncio.to_thread(embedding_fn, content)
    except Exception as e:  # noqa: BLE001
        return (_result(False, CODE_INTERNAL, 1, 0, False, None, None, 1, 1),
                _log_fail("provider_call", CODE_INTERNAL,
                          f"exception_type={type(e).__name__}"))

    # 5. 查询向量严格校验（空/类型/数值/finite/维度/零向量）
    values, failure = validate_vector(raw)
    if failure is not None:
        code, actual_dim = failure
        extra = f"selected=1 returned=0 actual_dim={actual_dim}"
        return (_result(False, code, 1, 0, False, None, None, 1, 1),
                _log_fail("embedding", code, extra))

    # 6. 只读 RPC 恰调用一次：list[float] 为 PostgREST vector 参数官方
    #    JSON 数组格式；match_count 固定；service_role 客户端
    params = {"query_embedding": values,
              "p_user_id": server_user_id,
              "match_count": MATCH_COUNT}
    try:
        rpc_res = await asyncio.to_thread(rpc_fn, params)
    except Exception as e:  # noqa: BLE001 —— RPC 异常只记类型，不外泄原文
        return (_result(False, CODE_RPC_FAILED, 1, 0, False, None,
                        EXPECTED_EMBEDDING_DIMENSION, 1, 2),
                _log_fail("rpc_call", CODE_RPC_FAILED,
                          f"exception_type={type(e).__name__} selected=1 "
                          f"returned=0 dimension={EXPECTED_EMBEDDING_DIMENSION}"))

    # 7. RPC 返回校验（必须是列表、行数上限、逐行 ID 与 similarity）
    rpc_rows = getattr(rpc_res, "data", None)
    if not isinstance(rpc_rows, list):
        return (_result(False, CODE_RPC_RESPONSE_INVALID, 1, 0, False, None,
                        EXPECTED_EMBEDDING_DIMENSION, 1, 2),
                _log_fail("rpc_response", CODE_RPC_RESPONSE_INVALID,
                          f"selected=1 returned=0 "
                          f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))
    parsed, bad = _validate_rpc_rows(rpc_rows)
    if bad is not None:
        return (_result(False, CODE_RPC_RESPONSE_INVALID, 1, len(rpc_rows),
                        False, None, EXPECTED_EMBEDDING_DIMENSION, 1, 2),
                _log_fail("rpc_response", CODE_RPC_RESPONSE_INVALID,
                          f"selected=1 returned={len(rpc_rows)} reason={bad} "
                          f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))

    # 8. Top1 内部 ID 核对（仅在服务端内存中比较；绝不返回该 ID）
    if not parsed:
        return (_result(False, CODE_NO_RESULTS, 1, 0, False, None,
                        EXPECTED_EMBEDDING_DIMENSION, 1, 2),
                _log_fail("self_match", CODE_NO_RESULTS,
                          f"selected=1 returned=0 "
                          f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))
    top1 = parsed[0]
    sim = round(top1["similarity"], 6)
    if top1["memory_item_id"] != item_id:
        return (_result(False, CODE_TOP1_MISMATCH, 1, len(parsed), False, sim,
                        EXPECTED_EMBEDDING_DIMENSION, 1, 2),
                _log_fail("self_match", CODE_TOP1_MISMATCH,
                          f"selected=1 returned={len(parsed)} "
                          f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))
    if sim < MIN_SELF_SIMILARITY:
        return (_result(False, CODE_LOW_SIMILARITY, 1, len(parsed), True, sim,
                        EXPECTED_EMBEDDING_DIMENSION, 1, 2),
                _log_fail("self_match", CODE_LOW_SIMILARITY,
                          f"selected=1 returned={len(parsed)} "
                          f"dimension={EXPECTED_EMBEDDING_DIMENSION}"))

    # 9. 自匹配成立
    return (_result(True, CODE_READY, 1, len(parsed), True, sim,
                    EXPECTED_EMBEDDING_DIMENSION, 1, 2),
            f"🧪 向量自匹配预览：selected=1 rpc_returned={len(parsed)} "
            f"top1_match=true similarity={sim}")
