# -*- coding: utf-8 -*-
"""第 41 阶段：active 记忆上下文注入 · Mock 设计（零生产接入）。

背景：第 40 阶段已验收混合召回多候选排序（HYBRID_MULTI_GATE_PASS），但 active
记忆尚未接入真实聊天上下文。本模块是接入前的**注入逻辑设计体 + Mock 测试对象**：
把「召回 → 过滤 → 去重 → 限量 → 拼装 system 事实参考块」做成纯逻辑函数，供
第 42 阶段手动 preview 真实接线复用。**本阶段零生产接入**：不修改
gateway._inject_context，不被任何聊天路径调用（仅被本阶段测试调用），不部署。

职责边界（全部为已定决策的直接实现）：
  1. 召回源解耦：混合召回能力（第 37 阶段 memory_hybrid_recall 的向量+词面
     RRF）**只经注入的 async callable 进入本模块**，契约 recall_fn(query_text,
     server_user_id) -> 第 37 阶段 result dict（ok/code/stats/retrieval/items）。
     本模块绝不 import memory_hybrid_recall、绝不复制其算法代码；生产接线
     （第 42 阶段）由调用方把 run_hybrid_recall 与 server._get_embedding、
     service_role RPC callable 绑定后注入。
  2. 只注入 active：状态排除（pending_review/rejected/superseded）与首次过期
     过滤由第 37 阶段召回层保证（RPC SQL 固定 active-only + _row_state 二次
     过滤，本模块不重做状态判断）；本模块对召回条目做**防御性时间复验**
     （expires_at 已过 → 丢弃；expires_at 存在但不可解析 → 保守丢弃；None =
     永不过期），解析复用第 35 阶段 _parse_utc 同一实现，杜绝复制漂移。
     user_id 只能来自服务端调用方参数（server_user_id），本模块没有任何客户端
     提交入口，该值原样传给 recall_fn（生产路径即 RPC 的 p_user_id）。
  3. 限量：默认最多注入 top 3 条（max_items 参数，默认 DEFAULT_MAX_INJECTED=3）。
     去重在截断**之前**完成（先去掉与既有上下文重复的候选，再取前 N），避免
     重复条目浪费名额。max_items 非法（非整数/bool/小于 1）时回退默认值 3，
     不报错、不中断。
  4. 跨来源去重：注入内容与 _inject_context 现有来源（user_facts 画像、
     Core_Cognition 总结、Pinecone 深层记忆、历史流水）做内容级去重——由调用方
     把这些来源的既有文本以 existing_context_texts 传入（可迭代字符串，每元素
     一段已注入文本），本模块不查询任何数据库。策略为**简单可解释**的归一化
     匹配：lowercase + 去全部空白 + 仅保留字母数字/CJK 后，归一化相等、或双向
     子串包含（被包含侧归一化长度 ≥ DEDUP_MIN_SUBSTR_LEN，防止"猫"这类超短
     串误杀整条记忆）即视为同一事实，只保留一处；召回列表内部同正文也只保留
     排名靠前的一条。**不引入相似度阈值合并**（已被明确否决）；契约违规的
     dedup 输入（非 str 元素/非可接受容器类型）整体拒绝，宁可无注入也不跳过去重。
  5. 注入形态：恰一条独立 system 消息 {"role": "system", "content": 块}，以
     "事实参考"身份注入，绝不伪装成 user/assistant 消息。块结构（示意）：

       【长期记忆 · 事实参考】
       以下是关于用户的事实参考，仅供回答时参考，禁止模仿其措辞、语气，禁止
       当作对话范例复述。若某条与当前话题无关，直接忽略即可。
       • 记忆正文1
       • 记忆正文2
       • 记忆正文3

     每条正文 strip 后按 MAX_ITEM_CONTENT_CHARS 截断（与画像 150/历史 500 的
     既有截断风格一致）。
  6. 无关查询保护（风险如实声明）：本阶段**不设相似度阈值**（未校准，被明确
     禁止），召回层也不设（threshold_applied 恒 false），因此弱相关/无关查询
     仍可能召回并注入 top 3 条记忆。当前防线只有三个：注入块是"参考"身份而非
     "事实断言"（块内明示仅供回答参考、可忽略）；限量 top 3；每条记忆本身
     经过人工 approve 才进入 active（质量下限由审批环节兜底）。**根本把关在
     第 42 阶段的人工 preview**：接线前先以真实数据人工核对注入质量，确认可
     接受后才允许进入真实聊天链路。
  7. 失败安全：任何一步失败——query/user_id 非法、recall_fn 不可调用、召回
     抛异常、返回结构违规、items 为空/全部无效、去重后为空——一律返回
     (None, 安全日志) 即"无注入"，绝不抛异常打断聊天主链路，绝不注入残缺
     内容。外层再有兜底 except Exception（只放过 asyncio.CancelledError 一类
     BaseException，让取消语义正常上抛）。
  8. 脱敏：日志与返回值绝不含 user_id、内部 item ID、hash、异常原文、查询
     原文；正文只出现在最终注入的 system 消息内容里（那是它该去的地方）。
     失败日志只含 stage、安全错误码（仅放行 [A-Z0-9_]{1,64} 形态的召回层
     错误码常量，其余一律以通用码代替，防止结构违规结果把任意文本带进日志）
     与计数。

硬性边界：不 import gateway / server / memory_hybrid_recall；不读环境变量；
不触网络（无 requests/urllib/httpx/openai/supabase/pinecone）；不持有任何
数据库客户端；无写入方法；无自动调度；不 print（日志行由调用方决定去向）。
"""

import datetime
import re

# expires_at 宽容解析复用第 35 阶段同一实现（与召回层语义一致，杜绝复制漂移）
from memory_vector_recall import _parse_utc

# 注入方法声明
METHOD_NAME = "active_memory_context_injection_v1"

# 限量默认值（已定决策：默认最多注入 top 3 条）
DEFAULT_MAX_INJECTED = 3

# 单条记忆正文注入上限（strip 后截断；与画像 150 / 历史 500 的既有截断风格一致）
MAX_ITEM_CONTENT_CHARS = 300

# 双向子串包含去重时，被包含侧的最小归一化长度（防止超短串误杀整条记忆）
DEDUP_MIN_SUBSTR_LEN = 5

# 注入块固定文案（导出供测试与第 42 阶段人工 preview 核对）
INJECTION_BLOCK_TITLE = "【长期记忆 · 事实参考】"
INJECTION_INSTRUCTION = (
    "以下是关于用户的事实参考，仅供回答时参考，禁止模仿其措辞、语气，"
    "禁止当作对话范例复述。")
INJECTION_IGNORE_NOTE = "若某条与当前话题无关，直接忽略即可。"

# 稳定错误码（仅本模块日志使用；绝不回显调用方任意文本）
CODE_NOTHING_TO_INJECT = "MEMORY_CONTEXT_NOTHING_TO_INJECT"
CODE_INVALID_REQUEST = "INVALID_INJECTION_REQUEST"
CODE_DEPENDENCY_INVALID = "INVALID_INJECTION_DEPENDENCY"
CODE_RECALL_FAILED = "RECALL_FAILED"
CODE_RECALL_RESULT_INVALID = "RECALL_RESULT_INVALID"
CODE_DEDUP_INPUT_INVALID = "INVALID_DEDUP_INPUT"
CODE_INTERNAL = "INTERNAL_ERROR"

# 召回层错误码白名单形态：只放行大写字母/数字/下划线、≤64 字符的常量形态
# （第 35/37 阶段全部错误码均符合），防止结构违规结果把任意文本带进日志
_SAFE_CODE_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


def _safe_error_code(value):
    """错误码脱敏：仅放行常量形态的错误码，其余返回 None。"""
    if isinstance(value, str) and _SAFE_CODE_RE.match(value):
        return value
    return None


def _log_fail(stage, code, extra=""):
    """失败日志：只含 stage / 安全错误码 / 计数；绝无查询原文、正文、ID、
    user_id、异常原文。"""
    line = f"⚠️ 长期记忆注入失败（无注入）：stage={stage} error={code}"
    if extra:
        line += f" {extra}"
    return line


def _dedup_norm(text):
    """去重归一化：lowercase + 去全部空白 + 仅保留字母数字/CJK。

    简单可解释（无相似度阈值）：归一化后相等或双向子串包含即视为同一事实。
    """
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def _is_duplicate(cand_norm, norm_list):
    """归一化精确相等 或 双向子串包含（被包含侧 ≥ DEDUP_MIN_SUBSTR_LEN）。"""
    for other in norm_list:
        if not other:
            continue
        if cand_norm == other:
            return True
        if len(cand_norm) >= DEDUP_MIN_SUBSTR_LEN and cand_norm in other:
            return True
        if len(other) >= DEDUP_MIN_SUBSTR_LEN and other in cand_norm:
            return True
    return False


def _build_existing_norms(existing_context_texts):
    """把调用方传入的既有上下文文本（画像/总结/Pinecone/历史流水）归一化。

    返回 (norm_list, ok)。None 视为空基底（合法）；单条 str 合法；
    list/tuple/set 且元素全为 str 合法；其余契约违规整体拒绝（保守无注入，
    宁可不注入也不跳过去重）。
    """
    if existing_context_texts is None:
        return [], True
    if isinstance(existing_context_texts, str):
        texts = [existing_context_texts]
    elif isinstance(existing_context_texts, (list, tuple, set)):
        texts = list(existing_context_texts)
        for t in texts:
            if not isinstance(t, str):
                return None, False
    else:
        return None, False
    return [_dedup_norm(t) for t in texts], True


def _effective_now(now):
    """注入时刻：aware datetime 直用，naive 视为 UTC，非法回退当前 UTC。"""
    if isinstance(now, datetime.datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=datetime.timezone.utc)
        return now
    return datetime.datetime.now(datetime.timezone.utc)


def _extract_contents(items, now_utc):
    """从召回 items 提取候选正文 + 注入层防御性时间复验。

    返回 (contents, invalid_dropped, expired_dropped, invalid_time_dropped)。
    条目非 dict / content 非字符串 / strip 后为空 → invalid；expires_at 存在
    但不可解析 → invalid_time（保守丢弃）；expires_at 已过 → expired；
    expires_at 为 None/缺失 = 永不过期 → 保留。
    """
    contents = []
    invalid_dropped = 0
    expired_dropped = 0
    invalid_time_dropped = 0
    for item in items:
        if not isinstance(item, dict):
            invalid_dropped += 1
            continue
        content = item.get("content")
        if not isinstance(content, str):
            invalid_dropped += 1
            continue
        content = content.strip()
        if not content:
            invalid_dropped += 1
            continue
        raw_exp = item.get("expires_at")
        if raw_exp is not None:
            exp = _parse_utc(raw_exp)
            if exp is None:
                invalid_time_dropped += 1
                continue
            if exp <= now_utc:
                expired_dropped += 1
                continue
        contents.append(content[:MAX_ITEM_CONTENT_CHARS])
    return contents, invalid_dropped, expired_dropped, invalid_time_dropped


def _injection_block(contents):
    """拼装 system 事实参考块（结构见模块 docstring 示意）。"""
    lines = [INJECTION_BLOCK_TITLE,
             INJECTION_INSTRUCTION + INJECTION_IGNORE_NOTE]
    for c in contents:
        lines.append(f"• {c}")
    return "\n".join(lines)


async def build_active_memory_injection(query, server_user_id, recall_fn,
                                        existing_context_texts=None,
                                        max_items=DEFAULT_MAX_INJECTED,
                                        now=None):
    """active 记忆上下文注入构建体（第 41 阶段 Mock 设计；第 42 阶段手动
    preview 接线复用；本阶段不被任何聊天路径调用）。

    query:                  当前用户查询文本（trim 后传给 recall_fn）。
    server_user_id:         服务端统一解析的 user_id（本模块唯一的 user_id
                            来源，原样传给 recall_fn；无任何客户端提交入口）。
    recall_fn:              注入的 async callable，契约
                            (query_text, server_user_id) -> 第 37 阶段
                            run_hybrid_recall 的 result dict。生产接线由
                            调用方绑定 server._get_embedding 与 service_role
                            RPC；本模块绝不 import 召回模块。
    existing_context_texts: 已注入上下文文本（画像/总结/Pinecone/历史流水），
                            None / 单条 str / 全 str 的 list/tuple/set。
    max_items:              注入上限，默认 3；非法回退默认值。
    now:                    注入时刻（aware datetime；测试注入用），默认
                            当前 UTC。

    返回 (message_or_None, log_line)：
      message_or_None —— 恰一条 {"role": "system", "content": 块}，"无注入"
      时为 None；调用方原样插入 messages 即可。
      log_line        —— 一条只含计数与安全错误码的日志行（不含正文/ID/
      user_id/查询原文/hash/异常原文），去向由调用方决定。
    """
    # 外层兜底：任何未预期异常一律降级为"无注入"，绝不打断聊天主链路
    #（只放行 CancelledError 等 BaseException，保持取消语义）
    try:
        # 0. 请求与依赖防御性复验（全部通过前不触 recall_fn）
        if not isinstance(query, str) or not query.strip():
            return (None, _log_fail("request_check", CODE_INVALID_REQUEST))
        if (not isinstance(server_user_id, str)
                or not server_user_id.strip()):
            return (None, _log_fail("request_check", CODE_INVALID_REQUEST))
        if not callable(recall_fn):
            return (None, _log_fail("dependency_check",
                                    CODE_DEPENDENCY_INVALID))
        if (isinstance(max_items, bool) or not isinstance(max_items, int)
                or max_items < 1):
            max_items = DEFAULT_MAX_INJECTED  # 配置错误回退默认值，不中断
        now_utc = _effective_now(now)

        # 1. 去重基底（契约违规整体拒绝：宁可不注入也不跳过去重）
        existing_norms, dedup_ok = _build_existing_norms(existing_context_texts)
        if not dedup_ok:
            return (None, _log_fail("dedup_input", CODE_DEDUP_INPUT_INVALID))

        # 2. 混合召回恰调用一次（能力经注入 callable 进入，本模块无任何
        #    provider/RPC/数据库访问）
        try:
            result = await recall_fn(query.strip(), server_user_id.strip())
        except Exception as e:  # noqa: BLE001 —— 异常只记类型，不外泄原文
            return (None, _log_fail("recall_call", CODE_RECALL_FAILED,
                                    f"exception_type={type(e).__name__}"))

        # 3. 召回结果结构复验（信任边界；ok 非 True / 结构违规 → 无注入）
        if not isinstance(result, dict):
            return (None, _log_fail("recall_result",
                                    CODE_RECALL_RESULT_INVALID))
        if result.get("ok") is not True:
            code = _safe_error_code(result.get("code")) or CODE_RECALL_FAILED
            return (None, _log_fail("recall_result", code))
        items = result.get("items")
        if not isinstance(items, list):
            return (None, _log_fail("recall_result",
                                    CODE_RECALL_RESULT_INVALID))
        recall_items = len(items)

        # 4. 候选提取 + 注入层时间复验（状态排除由召回层保证，见 docstring）
        contents, invalid_dropped, expired_dropped, invalid_time_dropped = \
            _extract_contents(items, now_utc)
        if not contents:
            return (None, f"🧠 长期记忆注入：无注入 recall_items={recall_items}"
                          f" invalid_dropped={invalid_dropped}"
                          f" expired_dropped={expired_dropped}"
                          f" invalid_time_dropped={invalid_time_dropped}"
                          f" injected=0 limit={max_items}")

        # 5. 跨来源去重（先于限量截断）+ 批内去重；归一化后为空的候选按无效丢弃
        accepted = []
        accepted_norms = []
        dedup_existing_removed = 0
        dedup_batch_removed = 0
        for content in contents:
            cand_norm = _dedup_norm(content)
            if not cand_norm:
                invalid_dropped += 1
                continue
            if _is_duplicate(cand_norm, existing_norms):
                dedup_existing_removed += 1
                continue
            if _is_duplicate(cand_norm, accepted_norms):
                dedup_batch_removed += 1
                continue
            accepted.append(content)
            accepted_norms.append(cand_norm)
        chosen = accepted[:max_items]

        counters = (f"recall_items={recall_items}"
                    f" dedup_existing_removed={dedup_existing_removed}"
                    f" dedup_batch_removed={dedup_batch_removed}"
                    f" invalid_dropped={invalid_dropped}"
                    f" expired_dropped={expired_dropped}"
                    f" invalid_time_dropped={invalid_time_dropped}"
                    f" limit={max_items}")
        if not chosen:
            return (None, f"🧠 长期记忆注入：无注入 {counters} injected=0")

        # 6. 拼装恰一条 system 事实参考消息（绝不伪装 user/assistant）
        message = {"role": "system",
                   "content": _injection_block(chosen)}
        return (message,
                f"🧠 长期记忆注入：{counters} injected={len(chosen)}")
    except Exception as e:  # noqa: BLE001 —— 兜底保险，只记异常类型
        return (None, _log_fail("internal", CODE_INTERNAL,
                                f"exception_type={type(e).__name__}"))
