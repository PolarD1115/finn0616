# -*- coding: utf-8 -*-
"""第 28 阶段：生产 embedding 维度安全诊断（纯函数模块，零副作用）。

背景：为 memory_items 设计独立向量召回前，唯一阻塞项是
EMBEDDING_DIMENSION_NOT_CONFIRMED —— 生产运行时 `_get_embedding()`
的实际输出维度尚未确认（历史 vector(1024) 不能证明当前 provider 仍输出 1024 维）。

职责边界：
  1. 保存固定合成探针常量（中英混合短文本，无隐私、不从请求体/数据库/环境读取）；
  2. 接收调用方注入的 embedding callable（生产路径为 server._get_embedding）；
  3. 对该 callable 最多调用一次（恒以固定探针文本为参数）；
  4. 严格校验返回结构：list/tuple、元素可转 float、全部 finite；
  5. 计算维度并判断是否满足 pgvector HNSW vector 类型 2000 维上限；
  6. 返回安全诊断结构 + 一条安全日志行（不含向量、模型名、provider URL、
     API Key、环境变量、异常原文、traceback、探针文本本身）。

硬性边界（零副作用）：
  - 不 import server / gateway，不访问 Supabase / Pinecone / LLM；
  - 不读取任何环境变量；不发起网络请求；不自动重试；不自动调度；
  - 不打印任何内容（日志行由 gateway handler 负责 _log）；
  - 不修改数据库：本模块与数据库无任何交互，也不创建第二个 embedding 客户端。

`_get_embedding()` 契约事实（本模块防御性覆盖）：多种失败统一返回 []，
因此空结果一律 EMBEDDING_UNAVAILABLE，不猜测是未配置还是请求失败。
"""

import math

# 固定合成探针：中英混合，远低于 server._MAX_EMBED_TEXT_CHARS（6000）截断上限。
# 只用于确认当前 provider 路径的响应结构与维度；维度不应受文本语言影响。
PROBE_TEXT = "记忆向量维度探针。Memory embedding dimension probe."

# 客户端确认令牌（与 gateway handler 校验值一致；单独导出供测试引用）
CONFIRM_TOKEN = "PROBE_EMBEDDING_DIMENSION"

# pgvector 0.8.0 官方文档：vector 类型 HNSW 索引维度上限 2000
# （halfvec 可至 4000；本阶段不自动切换，超限另行设计）。
HNSW_VECTOR_DIMENSION_LIMIT = 2000

# 稳定错误码
CODE_READY = "EMBEDDING_DIMENSION_READY"
CODE_UNSUPPORTED = "EMBEDDING_DIMENSION_UNSUPPORTED_FOR_VECTOR_HNSW"
CODE_UNAVAILABLE = "EMBEDDING_UNAVAILABLE"
CODE_RESPONSE_INVALID = "EMBEDDING_RESPONSE_INVALID"
CODE_NON_FINITE = "EMBEDDING_NON_FINITE_VALUES"
CODE_INTERNAL = "INTERNAL_ERROR"

# gateway handler 的 HTTP 状态映射（请求校验 400 由 handler 自己处理）
HTTP_STATUS_BY_CODE = {
    CODE_READY: 200,
    # 维度超 HNSW vector 上限：诊断本身成功，仍返回真实维度，由 code 表达不可用
    CODE_UNSUPPORTED: 200,
    CODE_UNAVAILABLE: 503,
    CODE_RESPONSE_INVALID: 503,
    CODE_NON_FINITE: 503,
    CODE_INTERNAL: 500,
}

_EMPTY_DIAGNOSTICS = {
    "dimension": None,
    "all_values_numeric": None,
    "all_values_finite": None,
    "hnsw_vector_dimension_supported": None,
}


def _result(ok: bool, code: str, diagnostics: dict, provider_calls: int) -> dict:
    """统一响应形状。execution 固定声明零数据库/Pinecone/LLM 副作用。"""
    return {
        "ok": ok,
        "code": code,
        "diagnostics": diagnostics,
        "execution": {
            "provider_calls": provider_calls,
            "database_reads": 0,
            "database_writes": 0,
            "pinecone_touched": False,
            "llm_touched": False,
        },
    }


def _ok_log(diagnostics: dict) -> str:
    """成功（含维度超上限）日志行：只含 ok / dimension / finite。"""
    return (f"🧭 embedding维度诊断：ok=true dimension={diagnostics['dimension']} "
            f"finite={str(bool(diagnostics['all_values_finite'])).lower()}")


def run_dimension_probe(embedding_fn):
    """对注入的 embedding callable 执行一次维度探针。

    返回 (result, log_line)：
      result   —— 安全诊断结构（可直接作为 HTTP JSON 响应体）；
      log_line —— 一条不含敏感信息的安全日志行（由调用方 _log）。

    保证：
      - embedding_fn 至多被调用一次，参数恒为固定合成探针 PROBE_TEXT；
      - 任何失败路径都不返回向量内容、向量 preview、统计特征、模型名、
        provider 信息、环境变量、异常 message 或 traceback；
      - 元素类型合法性（可转 float）优先于数值合法性（finite）判定，
        保证 "非法元素" 恒报 EMBEDDING_RESPONSE_INVALID、
        "NaN/Inf" 恒报 EMBEDDING_NON_FINITE_VALUES，与位置无关。
    """
    if not callable(embedding_fn):
        return (_result(False, CODE_INTERNAL, dict(_EMPTY_DIAGNOSTICS), 0),
                f"⚠️ embedding维度诊断失败：code={CODE_INTERNAL} stage=callable_check")

    # provider 调用发起即计 1（callable 抛异常同样计入：调用已尝试）
    try:
        raw = embedding_fn(PROBE_TEXT)
    except Exception as exc:  # 仅记录异常类型，绝不记录 message
        return (_result(False, CODE_INTERNAL, dict(_EMPTY_DIAGNOSTICS), 1),
                f"⚠️ embedding维度诊断失败：code={CODE_INTERNAL} "
                f"stage=provider_call exception_type={type(exc).__name__}")

    # 1. 空结果（None / [] / 空 tuple 等）：_get_embedding 把多种失败统一为空，
    #    不猜测具体原因
    if raw is None:
        return (_result(False, CODE_UNAVAILABLE, dict(_EMPTY_DIAGNOSTICS), 1),
                f"⚠️ embedding维度诊断失败：code={CODE_UNAVAILABLE}")

    # 2. 返回类型错误（注意：str/dict/set/int 都不是合法向量容器）
    if not isinstance(raw, (list, tuple)):
        return (_result(False, CODE_RESPONSE_INVALID, dict(_EMPTY_DIAGNOSTICS), 1),
                f"⚠️ embedding维度诊断失败：code={CODE_RESPONSE_INVALID}")

    if len(raw) == 0:
        return (_result(False, CODE_UNAVAILABLE, dict(_EMPTY_DIAGNOSTICS), 1),
                f"⚠️ embedding维度诊断失败：code={CODE_UNAVAILABLE}")

    # 3. 元素类型校验：每个元素必须可转换为 float（bool/Decimal 等可转类型放行）
    try:
        values = [float(v) for v in raw]
    except (TypeError, ValueError, OverflowError):
        return (_result(False, CODE_RESPONSE_INVALID, dict(_EMPTY_DIAGNOSTICS), 1),
                f"⚠️ embedding维度诊断失败：code={CODE_RESPONSE_INVALID}")

    # 4. 数值校验：全部 finite（NaN / +Inf / -Inf 任一出现即拒绝）
    all_finite = all(math.isfinite(v) for v in values)
    dimension = len(values)

    if not all_finite:
        return (_result(False, CODE_NON_FINITE, dict(_EMPTY_DIAGNOSTICS), 1),
                f"⚠️ embedding维度诊断失败：code={CODE_NON_FINITE}")

    # 5/6. 维度与 pgvector HNSW vector 上限判断（诊断成功；超限由 code 表达）
    hnsw_supported = dimension <= HNSW_VECTOR_DIMENSION_LIMIT
    code = CODE_READY if hnsw_supported else CODE_UNSUPPORTED
    diagnostics = {
        "dimension": dimension,
        "all_values_numeric": True,
        "all_values_finite": True,
        "hnsw_vector_dimension_supported": hnsw_supported,
    }
    return (_result(True, code, diagnostics, 1), _ok_log(diagnostics))
