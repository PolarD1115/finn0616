# -*- coding: utf-8 -*-
"""第 41 阶段专项测试 —— active 记忆上下文注入 · Mock 设计（零生产接入）。

被测模块 memory_context_injection.py 的设计契约：
  → 混合召回能力只经注入的 async callable 进入（契约 recall_fn(query_text,
    server_user_id) -> 第 37 阶段 result dict）；本测试把**真实的**
    run_hybrid_recall 与假 embedding / 假 RPC 绑定后注入，证明复用第 37 阶段
    算法本体（零复制）且全链路零真实外部调用；
  → 只注入 active：状态排除（pending_review/rejected/superseded）与首次过期
    过滤由召回层保证（逐类断言），注入层独立复验过期/时间可解析性；
  → 限量 top 3（去重先于截断）；跨来源去重（归一化精确相等或双向子串包含，
    无相似度阈值合并）；恰一条 system 事实参考消息（绝不伪装 user/assistant）；
  → 失败安全：召回失败/空/结构违规/解析异常一律"无注入"且绝不抛错；
  → 脱敏：日志与返回值绝不含 user_id / 内部 ID / hash / 查询原文 / 异常原文 /
    正文（正文只出现在最终注入消息内容里）；
  → 零生产接入：gateway.py / server.py 等全部既有 .py 源码中不出现本模块名，
    _inject_context 零改动（静态扫描断言）。

全部 unittest + mock + 合成数据：不真实调用 provider、不真实执行 RPC、
不连接真实 Supabase / Pinecone / LLM、不读取环境变量、不修改任何数据。
本测试不 import gateway / server（零生产链路依赖）。

覆盖（任务书【测试必须覆盖】A-H）：
  A 只注入 active：pending_review / rejected / superseded / 过期 / 时间不可解析
    逐类排除（经真实第 37 阶段召回链 + 注入层独立时间复验）
  B 限量：默认 top 3 截断（5 条候选）、max_items=2/1、非法 max_items 回退 3、
    去重先于截断
  C 跨来源去重：与画像/总结/Pinecone/历史流水重复被去掉（精确/双向子串/带
    项目符号的画像行/大小写与标点归一化/超短串防误杀）、批内去重、契约违规
    dedup 输入整体拒绝（且零召回调用）
  D 注入形态：恰一条 system 消息、含"事实参考+禁止模仿"指令原文、块结构
    逐行断言、正文截断上限、无 user/assistant 伪装、无内部 ID 泄漏进块
  E 失败安全：召回抛异常/返回 None/非 dict/ok 非 True（含不安全错误码不外泄）/
    items 缺失或非法/全部无效/query 与 user_id 非法（零召回调用）/
    recall_fn 不可调用——全部返回无注入且不抛错
  F 脱敏：全部路径日志扫描（user_id/内部 ID/hash/查询原文/正文/provider 与
    RPC 异常原文均不出现；正文只出现在注入块内）
  G 零真实外部调用：假 embedding / 假 RPC 恰调用预期次数、p_user_id 为服务端
    注入值、match_count 固定、recall_fn 收到 trim 后参数
  H 零生产接入：模块 import 白名单（AST 级）、无 print/无环境变量访问、
    全部既有 .py 源码不含本模块名（gateway.py / server.py 必在扫描集内）

运行： python -m unittest test_memory_context_injection_phase41 -v
"""

import ast
import asyncio
import datetime
import inspect
import os
import unittest

import memory_context_injection as mci
import memory_hybrid_recall as mhr
import memory_vector_recall as mvrec


# ==========================================
# 常量与脱敏标记
# ==========================================

# 查询与记忆正文零词面重合（无共享 bigram / token / 紧凑子串），
# 保证混合召回排序退化为纯 similarity 序，限量断言可精确预测
_QUERY_MARKER = "今天穿什么衣服出门比较合适"
_USER_MARKER = "user-scope-410"
_QEMB_MARKER = 0.414141
_ITEM_ID_MARKERS = [f"a1b2c3d4-ctx-uuid-{i:04d}" for i in range(1, 7)]
_SUBJECT_KEY = "SUBJ_ALPHA_MEMO"
_MODEL_MARKER = "MODEL-NAME-SECRET-41"
_HASH_MARKER = "HASH-SECRET-MARKER-41"
_EVENT_IDS_MARKER = "EVENT-ID-SECRET-MARKER-41"
_BATCH_ID_MARKER = "BATCH-ID-SECRET-MARKER-41"
_METADATA_MARKER = "METADATA-SECRET-MARKER-41"
_PROVIDER_SECRET_MARKER = "PROVIDER_RAW_ERROR_SECRET_41"
_RPC_SECRET_MARKER = "RPC_RAW_ERROR_SECRET_41"

_FUTURE_TS = "2099-01-01T00:00:00+00:00"
_PAST_TS = "2000-01-01T00:00:00+00:00"

# 五条候选（similarity 严格降序；正文与查询零词面重合）
_C1 = "用户对花生严重过敏"
_C2 = "用户家里养了一只猫"
_C3 = "用户是后端工程师"
_C4 = "用户住在杭州"
_C5 = "用户每天六点起床"


def _vec(dim=1024):
    """合成查询向量：全 finite、非零、1024 维（过第 31 阶段 validate_vector）。"""
    return [_QEMB_MARKER + 0.001 * (i % 13) for i in range(dim)]


def _rpc_row(rid=None, similarity=0.82, content=_C1, expires_at=None,
             status=None, extra_banned=True):
    """RPC 返回行的合成形状（真实 RPC 的 10 个白名单列 + 防御用 status 列）。

    extra_banned=True 时附带真实 RPC 绝不返回的敏感列（user_id/embedding/
    embedding_model/content_hash/source_event_ids/source_batch_id/metadata/
    superseded_by），供脱敏扫描验证模块不回显任何额外字段。
    """
    if rid is None:
        rid = _ITEM_ID_MARKERS[0]
    row = {"memory_item_id": rid,
           "content": content,
           "memory_type": "long_term",
           "importance": 3,
           "confidence": 0.7,
           "subject_key": _SUBJECT_KEY,
           "valid_at": "2026-08-01T00:00:00+00:00",
           "expires_at": expires_at,
           "source": "web",
           "similarity": similarity}
    if status is not None:
        row["status"] = status
    if extra_banned:
        row.update({"user_id": _USER_MARKER,
                    "embedding": [_QEMB_MARKER] * 4,
                    "embedding_model": _MODEL_MARKER,
                    "content_hash": _HASH_MARKER,
                    "source_event_ids": [_EVENT_IDS_MARKER],
                    "source_batch_id": _BATCH_ID_MARKER,
                    "metadata": {"m": _METADATA_MARKER},
                    "superseded_by": None})
    return row


# ==========================================
# 假依赖（记录全部调用；绝不触网）
# ==========================================

class _FakeResult:
    """模拟 supabase-py execute() 返回（带 .data 属性）。"""

    def __init__(self, data):
        self.data = data


class _RecordingEmbed:
    """记录调用的 embedding callable（生产路径 server._get_embedding 的替身）。"""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result if result is not None else _vec()
        self.exc = exc

    def __call__(self, text):
        self.calls.append(text)
        if self.exc is not None:
            raise self.exc
        return self.result


class _RecordingRpc:
    """注入模块的只读 RPC callable：记录 params，返回预定行 / 抛预定异常。"""

    def __init__(self, rows=(), exc=None):
        self.calls = []
        self.rows = list(rows)
        self.exc = exc

    def __call__(self, params):
        self.calls.append(params)
        if self.exc is not None:
            raise self.exc
        return _FakeResult(list(self.rows))


def _make_recall_fn(rpc_rows, embed=None, rpc=None, top_k=10):
    """构造注入式召回 callable：绑定**真实的**第 37 阶段 run_hybrid_recall
    （复用其向量+词面 RRF 算法本体，零复制）与假 embedding / 假 RPC。

    返回的 callable 上挂 embed / rpc / last_result 供调用次数与召回层过滤
    计数断言。全链路零真实外部调用。
    """
    if embed is None:
        embed = _RecordingEmbed()
    if rpc is None:
        rpc = _RecordingRpc(rows=rpc_rows)
    holder = {"last_result": None}

    async def recall_fn(query_text, user_id):
        holder["last_args"] = (query_text, user_id)
        result, log_line = await mhr.run_hybrid_recall(
            query_text, user_id, embed, rpc, top_k=top_k)
        holder["last_result"] = result
        holder["last_log"] = log_line
        return result

    recall_fn.embed = embed
    recall_fn.rpc = rpc
    recall_fn.hybrid_identity = mhr.run_hybrid_recall
    recall_fn.state = holder
    return recall_fn


def _static_recall_fn(items, ok=True, code=mci.METHOD_NAME.upper(),
                      exc=None):
    """直接返回预定 result dict 的召回 callable（测注入层自身的过滤/失败路径）。

    items 元素为第 37 阶段 items 形状的合成 dict；ok=False 时模拟召回层失败。
    """

    async def recall_fn(query_text, user_id):
        if exc is not None:
            raise exc
        return {"ok": ok, "code": code, "stats": {}, "retrieval": {},
                "items": list(items)}

    recall_fn.embed = None
    recall_fn.rpc = None
    recall_fn.state = {"last_result": None}
    return recall_fn


_UNSET = object()


def _build(query=_QUERY_MARKER, user_id=_USER_MARKER, recall_fn=_UNSET,
           existing=None, max_items=mci.DEFAULT_MAX_INJECTED, now=None):
    """直调模块 build_active_memory_injection；返回 (message, log_line)。
    recall_fn 用哨兵区分"未提供"与"显式传 None"（后者用于依赖校验测试）。"""
    if recall_fn is _UNSET:
        recall_fn = _make_recall_fn([_rpc_row()])
    message, log_line = asyncio.run(mci.build_active_memory_injection(
        query, user_id, recall_fn, existing_context_texts=existing,
        max_items=max_items, now=now))
    return message, log_line


def _block_lines(message):
    return message["content"].split("\n")


def _injected_contents(message):
    """从注入块提取各条正文（去掉 '• ' 前缀）。"""
    return [line[2:] for line in _block_lines(message)[2:]]


def _clean_log_assertions(log_line):
    """日志脱敏断言（全部路径共用）：绝无 user_id / 内部 ID / hash / 模型名 /
    查询原文 / 正文 / 异常原文 / 事件与批 ID / 敏感列名。"""
    markers = [_USER_MARKER, _QUERY_MARKER, _HASH_MARKER, _MODEL_MARKER,
               _EVENT_IDS_MARKER, _BATCH_ID_MARKER, _METADATA_MARKER,
               _PROVIDER_SECRET_MARKER, _RPC_SECRET_MARKER, str(_QEMB_MARKER),
               "Bearer", "traceback", "memory_items", "content_hash",
               "embedding_model", "superseded_by", "source_event_ids"]
    markers.extend(_ITEM_ID_MARKERS)
    markers.extend([_C1, _C2, _C3, _C4, _C5])
    for marker in markers:
        assert marker not in log_line, f"泄漏标记 {marker!r} 出现在日志"
    return log_line


# ==========================================
# A. 只注入 active（逐类排除）
# ==========================================

class TestActiveOnly(unittest.TestCase):

    def test_a_active_row_injected_with_exact_content(self):
        recall = _make_recall_fn([_rpc_row(content=_C1, status="active")])
        message, log = _build(recall_fn=recall)
        self.assertIsNotNone(message)
        self.assertEqual(_injected_contents(message), [_C1])
        self.assertIn("injected=1", log)
        _clean_log_assertions(log)

    def test_a_pending_review_excluded(self):
        recall = _make_recall_fn([_rpc_row(content=_C1,
                                           status="pending_review")])
        message, log = _build(recall_fn=recall)
        self.assertIsNone(message)
        self.assertIn("recall_items=0", log)
        self.assertIn("injected=0", log)

    def test_a_rejected_excluded(self):
        recall = _make_recall_fn([_rpc_row(content=_C1, status="rejected")])
        message, log = _build(recall_fn=recall)
        self.assertIsNone(message)

    def test_a_superseded_excluded(self):
        recall = _make_recall_fn([_rpc_row(content=_C1, status="superseded")])
        message, log = _build(recall_fn=recall)
        self.assertIsNone(message)

    def test_a_expired_excluded(self):
        recall = _make_recall_fn([_rpc_row(content=_C1,
                                           expires_at=_PAST_TS)])
        message, log = _build(recall_fn=recall)
        self.assertIsNone(message)

    def test_a_unparseable_time_excluded(self):
        recall = _make_recall_fn([_rpc_row(content=_C1,
                                           expires_at="not-a-timestamp")])
        message, log = _build(recall_fn=recall)
        self.assertIsNone(message)

    def test_a_future_expiry_kept(self):
        recall = _make_recall_fn([_rpc_row(content=_C1,
                                           expires_at=_FUTURE_TS)])
        message, log = _build(recall_fn=recall)
        self.assertIsNotNone(message)
        self.assertEqual(_injected_contents(message), [_C1])

    def test_a_mixed_rows_only_active_survives_by_category(self):
        """1 条 active + 3 条非 active 状态 + 1 条过期 + 1 条时间不可解析：
        注入只含 active；召回层过滤计数逐类核对（状态排除由召回层保证）。"""
        rows = [
            _rpc_row(rid=_ITEM_ID_MARKERS[0], content=_C1, similarity=0.91,
                     status="active"),
            _rpc_row(rid=_ITEM_ID_MARKERS[1], content="候选甲不应注入",
                     similarity=0.90, status="pending_review"),
            _rpc_row(rid=_ITEM_ID_MARKERS[2], content="候选乙不应注入",
                     similarity=0.89, status="rejected"),
            _rpc_row(rid=_ITEM_ID_MARKERS[3], content="候选丙不应注入",
                     similarity=0.88, status="superseded"),
            _rpc_row(rid=_ITEM_ID_MARKERS[4], content="候选丁不应注入",
                     similarity=0.87, expires_at=_PAST_TS),
            _rpc_row(rid=_ITEM_ID_MARKERS[5], content="候选戊不应注入",
                     similarity=0.86, expires_at="broken-time"),
        ]
        recall = _make_recall_fn(rows)
        message, log = _build(recall_fn=recall)
        self.assertIsNotNone(message)
        self.assertEqual(_injected_contents(message), [_C1])
        # 状态/过期/时间过滤发生在召回层：逐类核对第 37 阶段日志行计数
        recall_log = recall.state["last_log"]
        self.assertIn("status_filtered=3", recall_log)
        self.assertIn("expired_filtered=1", recall_log)
        self.assertIn("invalid_time_filtered=1", recall_log)
        self.assertIn("injected=1", log)
        for banned in ("候选甲", "候选乙", "候选丙", "候选丁", "候选戊"):
            self.assertNotIn(banned, message["content"])

    def test_a_no_status_key_row_kept(self):
        """真实 RPC 不返回 status 列：无 status 字段的行按 SQL 过滤结果保留。"""
        recall = _make_recall_fn([_rpc_row(content=_C1, status=None)])
        message, _ = _build(recall_fn=recall)
        self.assertIsNotNone(message)
        self.assertEqual(_injected_contents(message), [_C1])


# ==========================================
# B. 限量 top 3（去重先于截断）
# ==========================================

class TestLimit(unittest.TestCase):

    def _five_rows(self):
        sims = (0.91, 0.89, 0.87, 0.85, 0.83)
        contents = (_C1, _C2, _C3, _C4, _C5)
        return [_rpc_row(rid=_ITEM_ID_MARKERS[i], content=contents[i],
                         similarity=sims[i]) for i in range(5)]

    def test_b_default_limit_three_of_five(self):
        recall = _make_recall_fn(self._five_rows())
        message, log = _build(recall_fn=recall)
        self.assertIsNotNone(message)
        self.assertEqual(_injected_contents(message), [_C1, _C2, _C3])
        self.assertIn("limit=3", log)
        self.assertIn("injected=3", log)

    def test_b_limit_two(self):
        recall = _make_recall_fn(self._five_rows())
        message, _ = _build(recall_fn=recall, max_items=2)
        self.assertEqual(_injected_contents(message), [_C1, _C2])

    def test_b_limit_one(self):
        recall = _make_recall_fn(self._five_rows())
        message, _ = _build(recall_fn=recall, max_items=1)
        self.assertEqual(_injected_contents(message), [_C1])

    def test_b_limit_above_candidates_returns_all(self):
        recall = _make_recall_fn(self._five_rows())
        message, _ = _build(recall_fn=recall, max_items=99)
        self.assertEqual(_injected_contents(message), [_C1, _C2, _C3, _C4,
                                                       _C5])

    def test_b_invalid_max_items_falls_back_to_default(self):
        for bad in (0, -1, True, False, "3", 2.5, None, [3]):
            with self.subTest(max_items=bad):
                recall = _make_recall_fn(self._five_rows())
                message, log = _build(recall_fn=recall, max_items=bad)
                self.assertIsNotNone(message)
                self.assertEqual(_injected_contents(message),
                                 [_C1, _C2, _C3])
                self.assertIn("limit=3", log)

    def test_b_dedup_runs_before_truncation(self):
        """5 条候选中 2 条与既有画像重复：先去重再截断，注入剩下 3 条按序。"""
        rows = [_rpc_row(rid=_ITEM_ID_MARKERS[0], content=_C1,
                         similarity=0.95),
                _rpc_row(rid=_ITEM_ID_MARKERS[1], content=_C2,
                         similarity=0.90),
                _rpc_row(rid=_ITEM_ID_MARKERS[2], content=_C3,
                         similarity=0.85),
                _rpc_row(rid=_ITEM_ID_MARKERS[3], content=_C4,
                         similarity=0.80),
                _rpc_row(rid=_ITEM_ID_MARKERS[4], content=_C5,
                         similarity=0.75)]
        existing = ["用户是后端工程师，写代码的",   # 与 _C3 同一事实
                    "用户对花生严重过敏，吃花生会休克"]  # 与 _C1 同一事实
        recall = _make_recall_fn(rows)
        message, log = _build(recall_fn=recall, existing=existing)
        self.assertIsNotNone(message)
        self.assertEqual(_injected_contents(message), [_C2, _C4, _C5])
        self.assertIn("dedup_existing_removed=2", log)
        self.assertIn("injected=3", log)

    def test_b_default_constant_is_three(self):
        self.assertEqual(mci.DEFAULT_MAX_INJECTED, 3)


# ==========================================
# C. 跨来源去重（简单可解释：归一化精确相等 / 双向子串包含）
# ==========================================

class TestCrossSourceDedup(unittest.TestCase):

    def _one_row(self, content=_C1, similarity=0.9):
        return _make_recall_fn([_rpc_row(content=content,
                                         similarity=similarity)])

    def test_c_exact_match_vs_profile_removed(self):
        recall = self._one_row(_C3)
        message, log = _build(recall_fn=recall, existing=["用户是后端工程师"])
        self.assertIsNone(message)
        self.assertIn("dedup_existing_removed=1", log)

    def test_c_memory_contained_in_profile_removed(self):
        recall = self._one_row(_C1)
        message, log = _build(recall_fn=recall,
                              existing=["用户对花生严重过敏，吃花生会休克"])
        self.assertIsNone(message)
        self.assertIn("dedup_existing_removed=1", log)

    def test_c_profile_contained_in_memory_removed(self):
        recall = self._one_row(_C1)
        message, _ = _build(recall_fn=recall, existing=["对花生严重过敏"])
        self.assertIsNone(message)

    def test_c_punctuated_profile_line_removed(self):
        """画像行带项目符号/标点（_inject_context 的 '• {val}' 形态）同样命中。"""
        recall = self._one_row(_C2)
        message, _ = _build(recall_fn=recall, existing=["• 用户家里养了一只猫。"])
        self.assertIsNone(message)

    def test_c_case_and_punct_normalized_match(self):
        recall = self._one_row("User likes Cats!!")
        message, _ = _build(recall_fn=recall, existing=["user likes cats"])
        self.assertIsNone(message)

    def test_c_history_flow_line_removed(self):
        """历史流水来源：记忆正文是已注入历史用户侧内容的子串。"""
        recall = self._one_row("加班到十点")
        message, _ = _build(recall_fn=recall, existing=["今天加班到十点才回家"])
        self.assertIsNone(message)

    def test_c_each_source_type_deduped(self):
        """画像 / Core_Cognition 总结 / Pinecone / 历史流水四来源各去掉一条，
        无关候选保留（一次构建内混合四来源基底）。"""
        rows = [_rpc_row(rid=_ITEM_ID_MARKERS[0], content=_C1, similarity=0.94),
                _rpc_row(rid=_ITEM_ID_MARKERS[1], content=_C2, similarity=0.93),
                _rpc_row(rid=_ITEM_ID_MARKERS[2], content=_C3, similarity=0.92),
                _rpc_row(rid=_ITEM_ID_MARKERS[3], content=_C4, similarity=0.91),
                _rpc_row(rid=_ITEM_ID_MARKERS[4], content="用户喜欢蓝颜色",
                         similarity=0.90)]
        existing = ["• 用户对花生严重过敏，吃花生会休克",   # 画像
                    "- 用户家里养了一只猫",                 # Core_Cognition 总结
                    "- 用户是后端工程师",                   # Pinecone 深层记忆
                    "用户住在杭州"]                         # 历史流水用户侧
        recall = _make_recall_fn(rows)
        message, log = _build(recall_fn=recall, existing=existing)
        self.assertIsNotNone(message)
        self.assertEqual(_injected_contents(message), ["用户喜欢蓝颜色"])
        self.assertIn("dedup_existing_removed=4", log)
        self.assertIn("injected=1", log)

    def test_c_unrelated_existing_keeps_memory(self):
        recall = self._one_row(_C1)
        message, _ = _build(recall_fn=recall, existing=["用户喜欢的城市是成都"])
        self.assertIsNotNone(message)
        self.assertEqual(_injected_contents(message), [_C1])

    def test_c_short_substring_guard_no_false_positive(self):
        """被包含侧归一化长度 < DEDUP_MIN_SUBSTR_LEN 时不判重（防超短串误杀）。"""
        recall = self._one_row(_C2)  # "用户家里养了一只猫"
        message, _ = _build(recall_fn=recall, existing=["猫"])
        self.assertIsNotNone(message)

    def test_c_substring_boundary_exactly_min_len_deduped(self):
        """被包含侧归一化长度恰为 DEDUP_MIN_SUBSTR_LEN(5) 时判重（闭区间），
        短于 5 时不判重。"""
        self.assertEqual(mci.DEDUP_MIN_SUBSTR_LEN, 5)
        recall = self._one_row(_C1)  # 归一化 "用户对花生严重过敏"
        message, _ = _build(recall_fn=recall, existing=["对花生严重"])
        self.assertIsNone(message)   # 被包含侧 5 字 → 判重
        recall2 = self._one_row(_C1)
        message2, _ = _build(recall_fn=recall2, existing=["花生严重"])
        self.assertIsNotNone(message2)  # 被包含侧 4 字 → 不判重

    def test_c_intra_batch_duplicate_kept_once(self):
        """两条不同 ID、同正文的候选只保留排名靠前的一条。"""
        rows = [_rpc_row(rid=_ITEM_ID_MARKERS[0], content=_C2,
                         similarity=0.92),
                _rpc_row(rid=_ITEM_ID_MARKERS[1], content=_C2,
                         similarity=0.80)]
        recall = _make_recall_fn(rows)
        message, log = _build(recall_fn=recall)
        self.assertIsNotNone(message)
        self.assertEqual(_injected_contents(message), [_C2])
        self.assertIn("dedup_batch_removed=1", log)

    def test_c_contract_violating_existing_rejected_without_recall(self):
        """dedup 输入契约违规（非 str 元素 / 非可接受容器）整体拒绝：
        宁可无注入也不跳过去重，且绝不发起召回调用。"""
        for bad in ([1, 2], ["ok", 42], 42, {"a": 1}, 3.14, b"bytes"):
            with self.subTest(existing=bad):
                recall = _make_recall_fn([_rpc_row()])
                message, log = _build(recall_fn=recall, existing=bad)
                self.assertIsNone(message)
                self.assertIn("stage=dedup_input", log)
                self.assertIn("INVALID_DEDUP_INPUT", log)
                self.assertNotIn("injected=", log)
                self.assertIsNone(recall.state.get("last_args"))

    def test_c_acceptable_existing_forms(self):
        """None / 单条 str / 空 list / 全 str tuple 均为合法基底。"""
        for existing in (None, "", "某段已注入文本", [], ("用户喜欢的城市是成都",)):
            with self.subTest(existing=existing):
                recall = self._one_row(_C1)
                message, log = _build(recall_fn=recall, existing=existing)
                self.assertIsNotNone(message)
                self.assertEqual(_injected_contents(message), [_C1])


# ==========================================
# D. 注入形态（system 事实参考，绝不伪装 user/assistant）
# ==========================================

class TestInjectionShape(unittest.TestCase):

    def setUp(self):
        self.recall = _make_recall_fn([
            _rpc_row(rid=_ITEM_ID_MARKERS[0], content=_C1, similarity=0.93),
            _rpc_row(rid=_ITEM_ID_MARKERS[1], content=_C2, similarity=0.91)])
        self.message, self.log = _build(recall_fn=self.recall)

    def test_d_mandated_instruction_constant_verbatim(self):
        self.assertEqual(
            mci.INJECTION_INSTRUCTION,
            "以下是关于用户的事实参考，仅供回答时参考，禁止模仿其措辞、语气，"
            "禁止当作对话范例复述。")

    def test_d_single_system_message(self):
        self.assertIsInstance(self.message, dict)
        self.assertEqual(set(self.message.keys()), {"role", "content"})
        self.assertEqual(self.message["role"], "system")

    def test_d_block_structure_exact(self):
        lines = _block_lines(self.message)
        self.assertEqual(lines[0], mci.INJECTION_BLOCK_TITLE)
        self.assertEqual(lines[1],
                         mci.INJECTION_INSTRUCTION + mci.INJECTION_IGNORE_NOTE)
        self.assertEqual(lines[2:], [f"• {_C1}", f"• {_C2}"])

    def test_d_block_contains_reference_and_prohibition(self):
        content = self.message["content"]
        self.assertIn(mci.INJECTION_INSTRUCTION, content)
        self.assertIn(mci.INJECTION_BLOCK_TITLE, content)
        self.assertIn(mci.INJECTION_IGNORE_NOTE, content)

    def test_d_no_role_disguise_in_block(self):
        """块是参考身份而非对话消息：不含 user/assistant 角色伪装痕迹。"""
        content = self.message["content"]
        self.assertNotIn('"role"', content)
        self.assertNotIn("assistant", content)
        self.assertNotIn("role:", content)

    def test_d_internal_ids_never_in_block(self):
        content = self.message["content"]
        for marker in (_USER_MARKER, _MODEL_MARKER, _HASH_MARKER):
            self.assertNotIn(marker, content)
        for rid in _ITEM_ID_MARKERS:
            self.assertNotIn(rid, content)

    def test_d_content_length_cap(self):
        long_content = "长" * 400
        recall = _make_recall_fn([_rpc_row(content=long_content)])
        message, _ = _build(recall_fn=recall)
        self.assertIsNotNone(message)
        injected = _injected_contents(message)
        self.assertEqual(len(injected[0]), mci.MAX_ITEM_CONTENT_CHARS)
        self.assertEqual(mci.MAX_ITEM_CONTENT_CHARS, 300)

    def test_d_return_is_tuple_with_none_or_message(self):
        msg, log = _build()
        self.assertTrue(msg is None or isinstance(msg, dict))
        self.assertIsInstance(log, str)

    def test_d_method_name_declared(self):
        self.assertEqual(mci.METHOD_NAME,
                         "active_memory_context_injection_v1")


# ==========================================
# E. 失败安全（召回失败/空/结构违规 → 无注入，绝不抛错）
# ==========================================

class TestFailSafe(unittest.TestCase):

    def _counting_recall(self):
        calls = {"n": 0}

        async def recall_fn(query_text, user_id):
            calls["n"] += 1
            return {"ok": True, "code": "READY", "items": []}

        recall_fn.calls = calls
        return recall_fn

    def test_e_recall_exception_no_injection_no_raise(self):
        async def boom(query_text, user_id):
            raise RuntimeError(f"{_RPC_SECRET_MARKER} boom")

        message, log = _build(recall_fn=boom)
        self.assertIsNone(message)
        self.assertIn("stage=recall_call", log)
        self.assertIn("exception_type=RuntimeError", log)
        self.assertNotIn(_RPC_SECRET_MARKER, log)
        self.assertNotIn("boom", log)

    def test_e_recall_result_garbage_no_injection(self):
        """直接构造各类违规返回（非 dict / 空字典）→ 无注入且不抛错。"""
        async def ret_none(q, u):
            return None

        async def ret_str(q, u):
            return "garbage"

        async def ret_int(q, u):
            return 42

        async def ret_list(q, u):
            return [1, 2]

        async def ret_empty_dict(q, u):
            return {}

        for name, fn in (("none", ret_none), ("str", ret_str),
                         ("int", ret_int), ("list", ret_list),
                         ("empty_dict", ret_empty_dict)):
            with self.subTest(case=name):
                message, log = _build(recall_fn=fn)
                self.assertIsNone(message)
                self.assertIn("stage=recall_result", log)
                _clean_log_assertions(log)

    def test_e_ok_false_with_safe_code_logged(self):
        recall = _static_recall_fn([], ok=False, code="VECTOR_RPC_FAILED")
        message, log = _build(recall_fn=recall)
        self.assertIsNone(message)
        self.assertIn("error=VECTOR_RPC_FAILED", log)

    def test_e_ok_false_with_unsafe_code_not_echoed(self):
        """结构违规结果的 code 字段绝不回显进日志（白名单形态之外一律通用码）。"""
        recall = _static_recall_fn([], ok=False,
                                   code="leak secret USER-scope-410 now!!")
        message, log = _build(recall_fn=recall)
        self.assertIsNone(message)
        self.assertNotIn("leak secret", log)
        self.assertNotIn(_USER_MARKER, log)

    def test_e_ok_not_strictly_true_rejected(self):
        """ok=1 / "true" 等：契约要求 ok is True，其余一律无注入。"""
        for bad_ok in (1, "true", None):
            with self.subTest(ok=bad_ok):
                recall = _static_recall_fn(
                    [{"content": _C1}], ok=bad_ok)
                message, log = _build(recall_fn=recall)
                self.assertIsNone(message)
                self.assertIn("stage=recall_result", log)

    def test_e_items_shape_violations(self):
        # items 键缺失 / None / 非 list：召回结果结构违规，无注入
        for name, result in (("missing", {"ok": True, "code": "X"}),
                             ("none", {"ok": True, "code": "X",
                                       "items": None}),
                             ("not_list", {"ok": True, "code": "X",
                                           "items": "x"})):
            with self.subTest(case=name):
                async def fn(q, u, _r=result):
                    return _r

                message, log = _build(recall_fn=fn)
                self.assertIsNone(message)
                self.assertIn("stage=recall_result", log)
                _clean_log_assertions(log)
        # items 为空列表 / 条目全部无效：走计数路径，injected=0
        scenarios = [
            ("empty", {"ok": True, "code": "X", "items": []}),
            ("non_dict_entries", {"ok": True, "code": "X",
                                  "items": ["x", 1, None, ["y"]]}),
            ("content_not_str", {"ok": True, "code": "X",
                                 "items": [{"content": 42}]}),
            ("content_empty", {"ok": True, "code": "X",
                               "items": [{"content": ""}]}),
            ("content_spaces", {"ok": True, "code": "X",
                                "items": [{"content": "   "}]}),
        ]
        for name, result in scenarios:
            with self.subTest(case=name):
                async def fn(q, u, _r=result):
                    return _r

                message, log = _build(recall_fn=fn)
                self.assertIsNone(message)
                self.assertIn("injected=0", log)
                _clean_log_assertions(log)

    def test_e_mixed_items_only_valid_survive(self):
        items = [{"content": _C1}, "garbage", {"content": ""},
                 {"content": 42}, {"content": "   "}]
        recall = _static_recall_fn(items)
        message, log = _build(recall_fn=recall)
        self.assertIsNotNone(message)
        self.assertEqual(_injected_contents(message), [_C1])
        self.assertIn("invalid_dropped=4", log)

    def test_e_invalid_query_no_recall_call(self):
        recall = self._counting_recall()
        for bad in ("", "   ", 42, None, ["q"]):
            with self.subTest(query=bad):
                message, log = _build(query=bad, recall_fn=recall)
                self.assertIsNone(message)
                self.assertIn("stage=request_check", log)
        self.assertEqual(recall.calls["n"], 0)

    def test_e_invalid_user_id_no_recall_call(self):
        recall = self._counting_recall()
        for bad in ("", "   ", 42, None):
            with self.subTest(user_id=bad):
                message, log = _build(user_id=bad, recall_fn=recall)
                self.assertIsNone(message)
                self.assertIn("stage=request_check", log)
        self.assertEqual(recall.calls["n"], 0)

    def test_e_recall_fn_not_callable(self):
        for bad in (None, "nope", 42):
            with self.subTest(recall_fn=bad):
                message, log = _build(recall_fn=bad)
                self.assertIsNone(message)
                self.assertIn("stage=dependency_check", log)

    def test_e_real_chain_embedding_failure(self):
        """假 embedding 抛异常 → 召回层失败 → 无注入；embedding 恰 1 次、
        RPC 恰 0 次，异常原文不进日志。"""
        embed = _RecordingEmbed(exc=RuntimeError(_PROVIDER_SECRET_MARKER))
        recall = _make_recall_fn([_rpc_row()], embed=embed)
        message, log = _build(recall_fn=recall)
        self.assertIsNone(message)
        self.assertEqual(len(embed.calls), 1)
        self.assertEqual(len(recall.rpc.calls), 0)
        self.assertNotIn(_PROVIDER_SECRET_MARKER, log)

    def test_e_real_chain_embedding_invalid_vector(self):
        embed = _RecordingEmbed(result=[])
        recall = _make_recall_fn([_rpc_row()], embed=embed)
        message, log = _build(recall_fn=recall)
        self.assertIsNone(message)
        self.assertEqual(len(embed.calls), 1)
        self.assertEqual(len(recall.rpc.calls), 0)

    def test_e_real_chain_rpc_failure(self):
        """假 RPC 抛异常 → 无注入；embedding 恰 1 次、RPC 恰 1 次，不重试。"""
        rpc = _RecordingRpc(rows=[_rpc_row()],
                            exc=RuntimeError(_RPC_SECRET_MARKER))
        recall = _make_recall_fn([_rpc_row()], rpc=rpc)
        message, log = _build(recall_fn=recall)
        self.assertIsNone(message)
        self.assertEqual(len(recall.embed.calls), 1)
        self.assertEqual(len(rpc.calls), 1)
        self.assertNotIn(_RPC_SECRET_MARKER, log)

    def test_e_all_deduped_still_no_injection(self):
        """全部候选与既有上下文重复：无注入，但召回（含 embedding/RPC）已按
        契约各恰调用一次。"""
        recall = self._one_row_helper(_C1)
        message, log = _build(recall_fn=recall, existing=["用户对花生严重过敏"])
        self.assertIsNone(message)
        self.assertIn("dedup_existing_removed=1", log)
        self.assertIn("injected=0", log)
        self.assertEqual(len(recall.embed.calls), 1)
        self.assertEqual(len(recall.rpc.calls), 1)

    def _one_row_helper(self, content):
        return _make_recall_fn([_rpc_row(content=content)])


# ==========================================
# F. 日志与返回值脱敏（正文只出现在注入块内）
# ==========================================

class TestSanitization(unittest.TestCase):

    def test_f_success_log_clean_content_only_in_block(self):
        recall = _make_recall_fn([
            _rpc_row(rid=_ITEM_ID_MARKERS[0], content=_C1, similarity=0.93)])
        message, log = _build(recall_fn=recall)
        _clean_log_assertions(log)
        self.assertIn(_C1, message["content"])  # 正文只在该在的地方
        self.assertNotIn(_C1, log)

    def test_f_failure_paths_log_clean(self):
        cases = []
        async def boom(q, u):
            raise RuntimeError(f"{_RPC_SECRET_MARKER} x")
        cases.append(("recall_raise", _build(recall_fn=boom)))
        cases.append(("empty_recall",
                      _build(recall_fn=_make_recall_fn([]))))
        cases.append(("bad_query", _build(query="")))
        cases.append(("bad_user", _build(user_id="")))
        cases.append(("bad_recall_fn", _build(recall_fn=None)))
        cases.append(("bad_dedup_input", _build(existing=[1])))
        for name, (message, log) in cases:
            with self.subTest(case=name):
                self.assertIsNone(message)
                _clean_log_assertions(log)

    def test_f_no_results_log_has_counters_only(self):
        recall = _make_recall_fn([])
        message, log = _build(recall_fn=recall)
        self.assertIsNone(message)
        self.assertIn("recall_items=0", log)
        self.assertIn("injected=0", log)
        _clean_log_assertions(log)


# ==========================================
# G. 零真实外部调用（恰调用次数 + 契约参数）
# ==========================================

class TestCallableContract(unittest.TestCase):

    def test_g_real_hybrid_recall_reused_not_copied(self):
        """测试注入的召回 callable 绑定的就是第 37 阶段算法本体。"""
        recall = _make_recall_fn([_rpc_row()])
        self.assertIs(recall.hybrid_identity, mhr.run_hybrid_recall)
        self.assertEqual(mhr.run_hybrid_recall.__module__,
                         "memory_hybrid_recall")

    def test_g_embedding_called_once_with_trimmed_query(self):
        recall = _make_recall_fn([_rpc_row()])
        _build(query=f"  {_QUERY_MARKER}  ", recall_fn=recall)
        self.assertEqual(len(recall.embed.calls), 1)
        self.assertEqual(recall.embed.calls[0], _QUERY_MARKER)

    def test_g_rpc_called_once_with_server_user_id(self):
        """p_user_id 恒为服务端注入值（客户端无提交入口）；match_count 固定；
        查询向量为 1024 维 float 列表。"""
        recall = _make_recall_fn([_rpc_row()])
        _build(user_id=f"  {_USER_MARKER}  ", recall_fn=recall)
        self.assertEqual(len(recall.rpc.calls), 1)
        params = recall.rpc.calls[0]
        self.assertEqual(params["p_user_id"], _USER_MARKER)
        self.assertEqual(params["match_count"], mvrec.MATCH_COUNT)
        emb = params["query_embedding"]
        self.assertIsInstance(emb, list)
        self.assertEqual(len(emb), 1024)
        self.assertTrue(all(isinstance(v, float) for v in emb))

    def test_g_recall_fn_receives_trimmed_args(self):
        recall = _make_recall_fn([_rpc_row()])
        _build(query=f"  {_QUERY_MARKER}  ", user_id=f"  {_USER_MARKER}  ",
               recall_fn=recall)
        self.assertEqual(recall.state["last_args"],
                         (_QUERY_MARKER, _USER_MARKER))

    def test_g_call_counts_per_scenario(self):
        """恰调用预期次数：成功 1+1；请求非法 0+0；embedding 失败 1+0；
        RPC 失败 1+1；注入层直拒（dedup 违规）0+0。"""
        recall = _make_recall_fn([_rpc_row()])
        _build(recall_fn=recall)
        self.assertEqual(len(recall.embed.calls), 1)
        self.assertEqual(len(recall.rpc.calls), 1)

        recall0 = _make_recall_fn([_rpc_row()])
        _build(query="", recall_fn=recall0)
        self.assertEqual(len(recall0.embed.calls), 0)
        self.assertEqual(len(recall0.rpc.calls), 0)

        embed = _RecordingEmbed(exc=RuntimeError("x"))
        recall1 = _make_recall_fn([_rpc_row()], embed=embed)
        _build(recall_fn=recall1)
        self.assertEqual(len(recall1.embed.calls), 1)
        self.assertEqual(len(recall1.rpc.calls), 0)

        rpc = _RecordingRpc(rows=[_rpc_row()], exc=RuntimeError("x"))
        recall2 = _make_recall_fn([_rpc_row()], rpc=rpc)
        _build(recall_fn=recall2)
        self.assertEqual(len(recall2.embed.calls), 1)
        self.assertEqual(len(recall2.rpc.calls), 1)

        recall3 = _make_recall_fn([_rpc_row()])
        _build(existing=[1], recall_fn=recall3)
        self.assertEqual(len(recall3.embed.calls), 0)
        self.assertEqual(len(recall3.rpc.calls), 0)

    def test_g_static_recall_makes_no_external_calls(self):
        """注入层直测路径（静态 result）不触任何 embedding/RPC。"""
        recall = _static_recall_fn([{"content": _C1}])
        _build(recall_fn=recall)
        self.assertIsNone(recall.embed)
        self.assertIsNone(recall.rpc)


# ==========================================
# H. 注入层独立时间复验 + 零生产接入静态扫描
# ==========================================

class TestInjectionLayerTimeRecheck(unittest.TestCase):
    """状态排除由召回层保证；注入层对 expires_at 独立复验（防御性时间过滤），
    以静态 result 直测，不依赖召回层。"""

    _NOW = datetime.datetime(2026, 9, 9, 12, 0, 0,
                             tzinfo=datetime.timezone.utc)

    def test_h_past_expires_at_dropped(self):
        recall = _static_recall_fn([{"content": _C1,
                                     "expires_at": "2026-01-01T00:00:00+00:00"}])
        message, log = _build(recall_fn=recall, now=self._NOW)
        self.assertIsNone(message)
        self.assertIn("expired_dropped=1", log)

    def test_h_future_expires_at_kept(self):
        recall = _static_recall_fn([{"content": _C1,
                                     "expires_at": "2027-01-01T00:00:00+00:00"}])
        message, _ = _build(recall_fn=recall, now=self._NOW)
        self.assertIsNotNone(message)

    def test_h_expires_at_equal_now_dropped(self):
        recall = _static_recall_fn([{"content": _C1,
                                     "expires_at": self._NOW.isoformat()}])
        message, _ = _build(recall_fn=recall, now=self._NOW)
        self.assertIsNone(message)

    def test_h_unparseable_expires_at_dropped(self):
        recall = _static_recall_fn([{"content": _C1,
                                     "expires_at": "day-after-tomorrow"}])
        message, log = _build(recall_fn=recall, now=self._NOW)
        self.assertIsNone(message)
        self.assertIn("invalid_time_dropped=1", log)

    def test_h_none_expires_at_kept(self):
        recall = _static_recall_fn([{"content": _C1, "expires_at": None}])
        message, _ = _build(recall_fn=recall, now=self._NOW)
        self.assertIsNotNone(message)

    def test_h_datetime_expires_at(self):
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        future = datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc)
        recall_past = _static_recall_fn([{"content": _C1, "expires_at": past}])
        self.assertIsNone(_build(recall_fn=recall_past, now=self._NOW)[0])
        recall_future = _static_recall_fn([{"content": _C1,
                                            "expires_at": future}])
        self.assertIsNotNone(_build(recall_fn=recall_future,
                                    now=self._NOW)[0])

    def test_h_default_now_drops_far_past(self):
        recall = _static_recall_fn([{"content": _C1,
                                     "expires_at": "2000-01-01T00:00:00+00:00"}])
        message, _ = _build(recall_fn=recall)  # now 缺省 = 当前 UTC
        self.assertIsNone(message)


class TestZeroProductionWiring(unittest.TestCase):
    """零生产接入：模块纯度 + 全部既有 .py 源码不含本模块名。"""

    _ALLOWED_IMPORT_ROOTS = {"datetime", "re", "memory_vector_recall"}
    _BANNED_CALL_NAMES = {"print", "eval", "exec", "compile", "__import__"}
    _BANNED_ATTR_NAMES = {"environ", "getenv"}

    def _module_tree(self):
        return ast.parse(inspect.getsource(mci))

    def test_h_import_whitelist_ast(self):
        roots = set()
        for node in ast.walk(self._module_tree()):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    roots.add(node.module.split(".")[0])
        self.assertEqual(roots, self._ALLOWED_IMPORT_ROOTS)

    def test_h_no_banned_calls_or_env_access(self):
        for node in ast.walk(self._module_tree()):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    self.assertNotIn(func.id, self._BANNED_CALL_NAMES)
                if isinstance(func, ast.Attribute):
                    self.assertNotIn(func.attr, self._BANNED_ATTR_NAMES)
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, self._BANNED_ATTR_NAMES)

    def test_h_entry_point_is_coroutine_function(self):
        self.assertTrue(
            inspect.iscoroutinefunction(mci.build_active_memory_injection))

    def test_h_no_existing_file_references_new_module(self):
        """既有顶层 .py 源码中，本模块名只允许出现在 gateway.py（第42阶段授权：
        _inject_context 门控接线 + 只读 preview 接口）、server.py（阶段 B3 授权：
        _build_channel_context 渠道注入接线）与本阶段/第42阶段测试文件里；
        tool_loop.py / heartbeat.py 等其余文件仍必须零引用。"""
        base = os.path.dirname(os.path.abspath(__file__))
        # 第42阶段起 gateway.py 为授权接线点；阶段 B3 起 server.py 为授权接线点；
        # 两个阶段测试文件自身引用模块名
        allowed_files = {"memory_context_injection.py",
                         "test_memory_context_injection_phase41.py",
                         "test_memory_context_injection_phase42.py",
                         "test_channel_context_injection_phaseB3.py",
                         "gateway.py",
                         "server.py"}
        scanned = []
        for name in os.listdir(base):
            if not name.endswith(".py") or name in allowed_files:
                continue
            path = os.path.join(base, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
            self.assertNotIn("memory_context_injection", text,
                             f"{name} 引用了本阶段新模块（越权接线）")
            scanned.append(name)
        for required in ("tool_loop.py",
                         "heartbeat.py"):
            self.assertIn(required, scanned)
        self.assertGreater(len(scanned), 50)


if __name__ == "__main__":
    unittest.main()
