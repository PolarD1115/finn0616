# -*- coding: utf-8 -*-
"""第 31 阶段：active 记忆向量手动回填执行器（一次一条、受保护、零自动化）。

背景：memory_items 已具备 vector(1024) 向量基础设施（第 30 阶段 additive
migration：embedding / embedding_model / embedded_at 三列 + 全空或全非空
triplet CHECK + HNSW cosine 索引 + service_role-only match_memory_items RPC），
但当前生产仅有的 active 记忆三列全空，向量召回尚不可用。本模块提供手动触发
的单条回填执行体：把该 active 记忆自身的事实化 content 重新嵌入并原子写回。

职责边界（gateway handler 只负责路由/鉴权/请求校验/依赖注入）：
  1. 服务端强制条件选择一条待回填 active：user_id = 服务端解析用户、
     status = active、embedding IS NULL，created_at 升序最旧优先，limit 1；
  2. 内存二次确认该行确属可回填（active / 无 embedding / content 非空 /
     user scope 一致），全部通过前不触碰 provider；
  3. 调用注入的 embedding callable（生产路径 server._get_embedding）恰一次，
     输入恒为该行数据库 content（客户端无任何文本提交入口）；
  4. 严格校验向量（与第 28 阶段诊断同规则）：list/tuple、元素可转 float、
     全 finite、维度恰为 1024、非零向量；
  5. 规范化为 list[float] 写入格式（PostgREST 对 vector 列的官方 JSON 数组
     表示，与 Supabase 官方 Vector columns 文档存储示例同一 wire 路径，
     已在生产库以只读表达式实证：JSON 数组文本形式可直接 cast 为 vector、
     维度不匹配由数据库整体原子拒绝）；
  6. 单条条件 UPDATE 原子写入 embedding / embedding_model / embedded_at
     恰三列（条件含 id + user_id + status=active + embedding IS NULL），
     默认 representation 返回并验证行数恰为 1；triplet CHECK 保证三列
     同一语句内同写，半写会被数据库整句拒绝；
  7. 返回安全响应结构 + 一条安全日志行（不含正文、向量、模型名、provider、
     endpoint、key、user_id、item id、hash、来源、异常原文、SQL）。

硬性边界：
  - 对数据库仅 SELECT + 单条条件 UPDATE；不删除、不 upsert、不调 RPC；
  - 不读取、不复制、不写入任何旧向量存储（Pinecone / 旧向量表 / 旧链路）；
  - 不调用 LLM；不新建 embedding 客户端；不自动重试 provider；不批量；
  - 不读取环境变量（模型标识由 gateway 只读现有配置后传入，仅写不返回）；
  - 不修改 status / content / updated_at / last_confirmed_at / valid_at /
    invalid_at / expires_at / source_event_ids / source_batch_id /
    content_hash / subject_key / metadata / importance / confidence / source；
  - 不接入正式聊天上下文；不自动调度（无异步任务派发、无 Timer、无线程循环）。

幂等依据：embedding IS NULL 条件。回填成功后再次调用会因选不到行而返回
NO_ACTIVE_MEMORIES_NEED_EMBEDDING（provider 零调用、零写入）；并发竞争时
后到的 UPDATE 因该条件命中 0 行而返回 MEMORY_EMBEDDING_STATE_CHANGED，
不覆盖已有向量。本阶段不实现分布式锁、队列或 advisory lock。
"""

import asyncio
import datetime
import math

# 客户端确认令牌（gateway handler 校验同一字面量；导出供测试引用）
CONFIRM_TOKEN = "BACKFILL_ONE_ACTIVE_MEMORY"

# 生产 embedding 输出维度（第 29 阶段生产诊断 EMBEDDING_DIMENSION_CONFIRMED）
EXPECTED_EMBEDDING_DIMENSION = 1024

_ACTIVE_STATUS = "active"

# SELECT 列：至少需要内部 id + content；user_id/status/embedding 供内存二次
# 确认。绝不读取 content_hash / source_event_ids / source_batch_id /
# metadata / superseded_by / importance / confidence 等其余列。
_SELECT_COLUMNS = "id,content,user_id,status,embedding"

# 稳定错误码
CODE_BACKFILLED = "MEMORY_EMBEDDING_BACKFILLED"
CODE_NO_CANDIDATES = "NO_ACTIVE_MEMORIES_NEED_EMBEDDING"
CODE_STATE_CHANGED = "MEMORY_EMBEDDING_STATE_CHANGED"
CODE_MODEL_NOT_CONFIGURED = "EMBEDDING_MODEL_NOT_CONFIGURED"
CODE_UNAVAILABLE = "EMBEDDING_UNAVAILABLE"
CODE_RESPONSE_INVALID = "EMBEDDING_RESPONSE_INVALID"
CODE_NON_FINITE = "EMBEDDING_NON_FINITE_VALUES"
CODE_DIMENSION_MISMATCH = "EMBEDDING_DIMENSION_MISMATCH"
CODE_ZERO_VECTOR = "EMBEDDING_ZERO_VECTOR"
CODE_QUERY_FAILED = "MEMORY_EMBEDDING_QUERY_FAILED"
CODE_CANDIDATE_INVALID = "MEMORY_EMBEDDING_CANDIDATE_INVALID"
CODE_UPDATE_INVALID = "MEMORY_EMBEDDING_UPDATE_INVALID"
CODE_UPDATE_FAILED = "MEMORY_EMBEDDING_UPDATE_FAILED"
CODE_SERVICE_UNAVAILABLE = "MEMORY_EMBEDDING_SERVICE_UNAVAILABLE"
CODE_INTERNAL = "INTERNAL_ERROR"

# gateway handler 的 HTTP 状态映射（请求校验 400 由 handler 自己处理）
HTTP_STATUS_BY_CODE = {
    CODE_BACKFILLED: 200,
    CODE_NO_CANDIDATES: 200,
    CODE_STATE_CHANGED: 409,
    CODE_MODEL_NOT_CONFIGURED: 503,
    CODE_UNAVAILABLE: 503,
    CODE_RESPONSE_INVALID: 503,
    CODE_NON_FINITE: 503,
    CODE_DIMENSION_MISMATCH: 503,
    CODE_ZERO_VECTOR: 503,
    CODE_QUERY_FAILED: 500,
    CODE_CANDIDATE_INVALID: 500,
    CODE_UPDATE_INVALID: 500,
    CODE_UPDATE_FAILED: 500,
    CODE_SERVICE_UNAVAILABLE: 503,
    CODE_INTERNAL: 500,
}


def _base_result(ok, code, selected, updated,
                 provider_calls, database_reads, database_writes):
    """非成功路径统一响应形状（成功路径见 _success_result）。

    execution 如实声明副作用：database_writes 计"已发出的 UPDATE 语句数"
    （0 行命中也是一次语句执行），database_reads 计 SELECT 次数；
    Pinecone 与 LLM 恒为未触碰。
    """
    return {
        "ok": ok,
        "code": code,
        "stats": {"selected": selected, "updated": updated},
        "execution": {
            "provider_calls": provider_calls,
            "database_reads": database_reads,
            "database_writes": database_writes,
            "pinecone_touched": False,
            "llm_touched": False,
        },
    }


def _success_result(dimension):
    """回填成功响应。write_result 为静态声明：payload 恰含三列，且不含
    status / content，因此记忆状态与正文声明为未改变。"""
    return {
        "ok": True,
        "code": CODE_BACKFILLED,
        "stats": {"selected": 1, "updated": 1, "dimension": dimension},
        "write_result": {
            "embedding_written": True,
            "embedding_model_written": True,
            "embedded_at_written": True,
            "memory_status_changed": False,
            "memory_content_changed": False,
        },
        "execution": {
            "provider_calls": 1,
            "database_reads": 1,
            "database_writes": 1,
            "pinecone_touched": False,
            "llm_touched": False,
        },
    }


def _utc_now_iso():
    """当前 UTC aware ISO 时间（与 timestamptz 列兼容）。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _log_fail(stage, code, extra=""):
    line = f"⚠️ active记忆向量回填失败：stage={stage} error={code}"
    if extra:
        line += f" {extra}"
    return line


def validate_vector(raw):
    """严格复用第 28 阶段诊断规则校验 provider 响应。

    判定顺序：空 → 容器类型 → 元素可转 float → finite → 维度 → 非零。
    返回 (values, None) 或 (None, (code, actual_dimension))；
    actual_dimension 仅在已能确定长度时有意义，否则为 0。

    元素类型合法性（可转 float）优先于数值合法性（finite）判定，保证
    "非法元素" 恒报 EMBEDDING_RESPONSE_INVALID、"NaN/Inf" 恒报
    EMBEDDING_NON_FINITE_VALUES，与位置无关。
    """
    # 1. 空结果：_get_embedding 把多种失败统一返回空，不猜测具体原因
    if raw is None:
        return None, (CODE_UNAVAILABLE, 0)
    if not isinstance(raw, (list, tuple)):
        return None, (CODE_RESPONSE_INVALID, 0)
    if len(raw) == 0:
        return None, (CODE_UNAVAILABLE, 0)
    # 2. 元素类型校验：每项必须可转换 float（bool/Decimal 等可转类型放行）
    try:
        values = [float(v) for v in raw]
    except (TypeError, ValueError, OverflowError):
        return None, (CODE_RESPONSE_INVALID, 0)
    # 3. 数值校验：任一 NaN / ±Inf 即拒绝
    if not all(math.isfinite(v) for v in values):
        return None, (CODE_NON_FINITE, len(values))
    # 4. 维度校验：必须恰为生产确认的 1024 维
    if len(values) != EXPECTED_EMBEDDING_DIMENSION:
        return None, (CODE_DIMENSION_MISMATCH, len(values))
    # 5. 零向量判定（sum(v*v) > 0 的无溢出等价形式）：全部元素为 0 的向量
    #    对 cosine 无意义，不得写入
    if all(v == 0.0 for v in values):
        return None, (CODE_ZERO_VECTOR, len(values))
    return values, None


async def run_backfill(supabase_service, server_user_id, embedding_fn,
                       embedding_model):
    """active 记忆向量手动回填执行体（gateway /api/memory-embedding-backfill 调用）。

    supabase_service: server.supabase_service（service_role；
                      仅 SELECT + 单条条件 UPDATE 两种操作）。
    server_user_id: 服务端统一解析的 user_id（gateway 解析后传入，
                    客户端无任何提交入口）。
    embedding_fn:   生产路径为 server._get_embedding（恰调用一次，
                    输入恒为库内 content；失败统一返回 []，不重试）。
    embedding_model: 当前配置的 embedding 模型标识（gateway 只读现有配置后
                     传入；仅写入 embedding_model 列，不返回、不打印）。

    返回 (result, log_line)：result 为可直接作为 HTTP JSON 响应体的安全结构；
    log_line 为一条不含正文/向量/模型名/user_id/item id/异常原文的安全日志行
    （由调用方 _log）。
    """
    if supabase_service is None:
        return (_base_result(False, CODE_SERVICE_UNAVAILABLE, 0, 0, 0, 0, 0),
                _log_fail("dependency_check", CODE_SERVICE_UNAVAILABLE))
    if not isinstance(server_user_id, str) or not server_user_id.strip():
        return (_base_result(False, CODE_SERVICE_UNAVAILABLE, 0, 0, 0, 0, 0),
                _log_fail("dependency_check", CODE_SERVICE_UNAVAILABLE))
    server_user_id = server_user_id.strip()

    if not callable(embedding_fn):
        return (_base_result(False, CODE_INTERNAL, 0, 0, 0, 0, 0),
                _log_fail("callable_check", CODE_INTERNAL))
    if not isinstance(embedding_model, str) or not embedding_model.strip():
        # 模型标识未配置：此时 _get_embedding 亦必然返回空结果，
        # 先行给出更准确的错误（零查询、零 provider 调用）
        return (_base_result(False, CODE_MODEL_NOT_CONFIGURED, 0, 0, 0, 0, 0),
                _log_fail("model_config", CODE_MODEL_NOT_CONFIGURED))
    embedding_model = embedding_model.strip()

    # 1. 服务端强制条件选择一条最旧待回填 active（客户端不可指定任何条件）
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items")
            .select(_SELECT_COLUMNS)
            .eq("user_id", server_user_id)
            .eq("status", _ACTIVE_STATUS)
            .is_("embedding", None)
            .order("created_at", desc=False)
            .limit(1)
            .execute())
        rows = [r for r in (getattr(res, "data", None) or []) if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001 —— 数据库异常只返回脱敏代码
        return (_base_result(False, CODE_QUERY_FAILED, 0, 0, 0, 1, 0),
                _log_fail("select", CODE_QUERY_FAILED,
                          f"exception_type={type(e).__name__}"))

    if not rows:
        return (_base_result(True, CODE_NO_CANDIDATES, 0, 0, 0, 1, 0),
                "🧬 active记忆向量回填：selected=0 updated=0 dimension=none")

    # 2. 内存二次确认（数据库条件失效时的最后防线；全部通过前不触 provider）。
    #    查询层有 limit 1，这里恒只取第一条，不批量。
    row = rows[0]
    if (row.get("status") != _ACTIVE_STATUS
            or row.get("user_id") != server_user_id
            or not isinstance(row.get("content"), str)
            or not row["content"].strip()
            or row.get("id") is None):
        return (_base_result(False, CODE_CANDIDATE_INVALID, 1, 0, 0, 1, 0),
                _log_fail("candidate_check", CODE_CANDIDATE_INVALID))
    if row.get("embedding") is not None:
        # 该行已有向量（并发已回填等）：状态已变化，不覆盖
        return (_base_result(False, CODE_STATE_CHANGED, 1, 0, 0, 1, 0),
                _log_fail("candidate_check", CODE_STATE_CHANGED))

    content = row["content"]
    item_id = row["id"]

    # 3. provider 恰调用一次：输入恒为该行事实化 content；不自动重试
    try:
        raw = await asyncio.to_thread(embedding_fn, content)
    except Exception as e:  # noqa: BLE001
        return (_base_result(False, CODE_INTERNAL, 1, 0, 1, 1, 0),
                _log_fail("provider_call", CODE_INTERNAL,
                          f"exception_type={type(e).__name__}"))

    # 4. 严格校验（空/类型/数值/finite/维度/零向量）
    values, failure = validate_vector(raw)
    if failure is not None:
        code, actual_dim = failure
        extra = ""
        if code == CODE_DIMENSION_MISMATCH:
            extra = f"expected={EXPECTED_EMBEDDING_DIMENSION} actual={actual_dim}"
        return (_base_result(False, code, 1, 0, 1, 1, 0),
                _log_fail("embedding", code, extra))

    # 5. 单条条件 UPDATE：payload 恰三列；条件含 id + user_id + status +
    #    embedding IS NULL；整句原子（triplet CHECK 拒绝即整句失败，无半写）
    payload = {
        "embedding": values,  # list[float]：PostgREST vector 列官方 JSON 数组格式
        "embedding_model": embedding_model,
        "embedded_at": _utc_now_iso(),
    }
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items")
            .update(payload)
            .eq("id", item_id)
            .eq("user_id", server_user_id)
            .eq("status", _ACTIVE_STATUS)
            .is_("embedding", None)
            .execute())
        updated_rows = [r for r in (getattr(res, "data", None) or [])
                        if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001 —— 只记异常类型，不外泄数据库异常原文
        return (_base_result(False, CODE_UPDATE_FAILED, 1, 0, 1, 1, 1),
                _log_fail("update", CODE_UPDATE_FAILED,
                          f"exception_type={type(e).__name__}"))

    if len(updated_rows) == 1:
        return (_success_result(EXPECTED_EMBEDDING_DIMENSION),
                f"🧬 active记忆向量回填：selected=1 updated=1 "
                f"dimension={EXPECTED_EMBEDDING_DIMENSION}")
    if updated_rows:
        # id 为主键，多行返回在正常数据下不可能；防御性拒绝且不声称成功
        return (_base_result(False, CODE_UPDATE_INVALID, 1, 0, 1, 1, 1),
                _log_fail("update", CODE_UPDATE_INVALID,
                          f"rows={len(updated_rows)}"))
    # 0 行：并发已回填或状态/归属改变 → 不重试、不覆盖
    return (_base_result(False, CODE_STATE_CHANGED, 1, 0, 1, 1, 1),
            _log_fail("update", CODE_STATE_CHANGED, "rows=0"))
