# -*- coding: utf-8 -*-
"""第 37 阶段专项测试 —— lexical + vector 混合召回只读预览（RRF 融合）。

  POST /api/memory-hybrid-recall-preview
    → 请求体白名单仅 confirm/query/top_k，任何注入字段一律 400 且零调用；
    → 服务端 user_id 解析后，用现有 _get_embedding 对 trim 后查询文本恰嵌入
      一次（1024/finite/非零校验，复用第 31 阶段 validate_vector）；
    → 经 service_role 只读调用 match_memory_items 恰一次（match_count 服务端
      固定 10；p_user_id 服务端注入；不调用任何旧词面召回 RPC）；
    → 本阶段 lexical 是对 vector top-10 候选的二次排序，不是全量 active
      lexical 检索（零额外 SELECT；词面算法本体经 import 复用第 21 阶段）；
    → 服务端内部 memory_item_id 去重（绝不进入 HTTP/日志）；RRF（rrf_k=60，
      排名融合参数非阈值）融合排序；无固定分数权重；importance 仅最终稳定
      tie-break；threshold_applied 恒 false；
    → 零写入；不更新召回统计；无 Pinecone、无 LLM、无自动调度；不接正式
      上下文；不修改词面算法与 vector RPC。

全部 unittest + mock + 合成数据：不真实调用 provider、不真实执行 RPC、
不真实调用新接口、不连接真实 Supabase / Pinecone / LLM、不修改任何数据。

覆盖（任务书 A-I）：
  A 路由与鉴权（/api/* API_SECRET、仅 POST、OPTIONS、confirm/query/top_k、
    全部 18 个禁用注入字段拒绝、非法请求零 provider/RPC/DB）
  B vector 调用（provider 最多一次、RPC 最多一次、1024/finite/非零、
    p_user_id 服务端注入、match_count 固定 10 不受 top_k 影响、不调旧 RPC、
    不调 Pinecone/LLM、结构违规整体拒绝）
  C lexical 侧（仅对 vector 候选二次打分、零额外表访问、精确词面命中、
    无词面不命中、lexical 失败不伪装 vector 失败、二次排序声明）
  D ID 去重（同 ID 两侧一次、vector-only、vector+lexical、lexical 永不
    单独出现、不用 content/subject_key/hash 去重、HTTP 不返回 ID）
  E RRF（rrf_k=60、vector-only/双侧分值、公式只用排名、排名稳定、rrf 支配
    similarity、无固定权重、importance 仅 tie-break、无 threshold）
  F 状态与时间（active 保留、pending/rejected/superseded 丢弃、过期丢弃、
    无效时间丢弃、零 UPDATE）
  G 响应与脱敏（method 三元组、threshold_applied=false、writes_executed=false、
    字段白名单、禁用键扫描、query/ID/模型/SQL/异常正文不出日志、HTTP 状态映射）
  H 零写入与正式上下文隔离（模块/网关源码静态扫描 + 行为断言 + 算法复用
    身份断言 + 全路径脱敏扫描）
  （I 全量记忆回归由命令行单独运行）

运行： python -m unittest test_memory_hybrid_recall_phase37 -v
"""

import asyncio
import inspect
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import gateway
import memory_hybrid_recall as mhr
import memory_recall as mrec
import memory_vector_recall as mvrec
import memory_embedding as memb
import server as _srv
from test_memory_preview_phase10 import FakeReceive, FakeSend


# ==========================================
# 常量与脱敏标记
# ==========================================

_PATH = "/api/memory-hybrid-recall-preview"
_SECRET = "test-secret-marker-phase37"
# 查询与命中正文：共享 bigram（小满/上周）→ lexical_score > 0
_QUERY_MARKER = "查询文本隐私标记：小满上周做了什么"
_CONTENT_MARKER = "记忆正文隐私标记：小满周四去看了牙医"
# 与默认查询零词面重合的正文 → lexical_score == 0（vector-only）
_CONTENT_NOMATCH = "天气晴朗适合出门散步"
_USER_MARKER = "user-scope-377"
_ITEM_ID_MARKER = "a1b2c3d4-hybrid-uuid-0001"
_ITEM_ID_MARKER_2 = "b2c3d4e5-hybrid-uuid-0002"
_ITEM_ID_MARKER_3 = "c3d4e5f6-hybrid-uuid-0003"
_MODEL_MARKER = "MODEL-NAME-SECRET-123"
_SUBJECT_KEY_MARKER = "SUBJECT-KEY-SECRET-MARKER-37"
_HASH_MARKER = "HASH-SECRET-MARKER-37"
_EVENT_IDS_MARKER = "EVENT-ID-SECRET-MARKER-37"
_BATCH_ID_MARKER = "BATCH-ID-SECRET-MARKER-37"
_METADATA_MARKER = "METADATA-SECRET-MARKER-37"
_PROVIDER_SECRET_MARKER = "PROVIDER_RAW_ERROR_SECRET_MARKER"
_RPC_SECRET_MARKER = "RPC_RAW_ERROR_SECRET_MARKER"
_VEC_MARKER = 0.777123

_FUTURE_TS = "2099-01-01T00:00:00+00:00"
_PAST_TS = "2000-01-01T00:00:00+00:00"
_VALID_TS = "2026-08-30T12:00:00+00:00"


def _vec(dim, marker=None):
    """合成向量：全 finite、非零；marker 可供泄漏扫描。"""
    if marker is None:
        return [0.001 * (i % 97) for i in range(dim)]
    return [marker + 0.001 * i for i in range(dim)]


def _rpc_row(rid=_ITEM_ID_MARKER, similarity=0.82, content=_CONTENT_MARKER,
             subject_key=_SUBJECT_KEY_MARKER, importance=3, status=None,
             expires_at=None, extra_banned=True, **overrides):
    """RPC 返回行的合成形状（真实 RPC 的 10 个白名单列）。

    extra_banned=True 时附带真实 RPC 绝不返回的敏感列（user_id/embedding/
    embedding_model/content_hash/source_event_ids/source_batch_id/metadata/
    created_by/superseded_by），验证模块不信任、不回显任何额外字段；
    status 列真实 RPC 不返回，用于验证二次过滤的防御逻辑。
    """
    row = {"memory_item_id": rid,
           "content": content,
           "memory_type": "long_term",
           "importance": importance,
           "confidence": 0.7,
           "subject_key": subject_key,
           "valid_at": _VALID_TS,
           "expires_at": expires_at,
           "source": "web",
           "similarity": similarity}
    if status is not None:
        row["status"] = status
    if extra_banned:
        row.update({"user_id": _USER_MARKER,
                    "embedding": [_VEC_MARKER] * 4,
                    "embedding_model": _MODEL_MARKER,
                    "content_hash": _HASH_MARKER,
                    "source_event_ids": [_EVENT_IDS_MARKER],
                    "source_batch_id": _BATCH_ID_MARKER,
                    "metadata": {"m": _METADATA_MARKER},
                    "created_by": _ITEM_ID_MARKER_3,
                    "superseded_by": None})
    row.update(overrides)
    return row


def _default_body():
    return {"confirm": mhr.CONFIRM_TOKEN, "query": _QUERY_MARKER}


# ==========================================
# 假依赖（记录全部调用路径；绝不触网）
# ==========================================

class _FakeResult:
    """模拟 supabase-py execute() 返回（带 .data 属性）。"""

    def __init__(self, data):
        self.data = data


class _ForbiddenQuery:
    """直接表访问记录器：任何链式操作都记为 forbidden（本模块只允许 RPC）。"""

    def __init__(self, owner, table):
        self._owner = owner
        self._table = table

    def _rec(self, method, *a, **k):
        self._owner.forbidden.append((self._table, method))
        return self

    select = lambda self, *a, **k: self._rec("select")
    eq = lambda self, *a, **k: self._rec("eq")
    is_ = lambda self, *a, **k: self._rec("is_")
    order = lambda self, *a, **k: self._rec("order")
    limit = lambda self, *a, **k: self._rec("limit")
    insert = lambda self, *a, **k: self._rec("FORBIDDEN:insert")
    update = lambda self, *a, **k: self._rec("FORBIDDEN:update")
    delete = lambda self, *a, **k: self._rec("FORBIDDEN:delete")
    upsert = lambda self, *a, **k: self._rec("FORBIDDEN:upsert")

    def execute(self, *a, **k):
        self._owner.forbidden.append((self._table, "execute"))
        return _FakeResult([])


class _HybridFakeService:
    """handler 用假 service_role 客户端：只有 rpc 通路是真实的；
    任何直接表访问（table(...)）都被记录，测试断言其为空。"""

    def __init__(self, rpc_rows=(), rpc_exc=None, rpc_data=None):
        self.rpc_calls = []
        self.rpc_execute_count = 0
        self.rpc_rows = list(rpc_rows)
        self.rpc_exc = rpc_exc
        self.rpc_data = rpc_data
        self.table_calls = []
        self.forbidden = []

    def table(self, name):
        self.table_calls.append(name)
        return _ForbiddenQuery(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return _FakeRpcBuilder(self)


class _FakeRpcBuilder:
    """模拟 supabase-py 的 rpc(name, params) 构建器（仅 execute）。"""

    def __init__(self, owner):
        self._owner = owner

    def execute(self, *a, **k):
        self._owner.rpc_execute_count += 1
        if self._owner.rpc_exc is not None:
            raise self._owner.rpc_exc
        if self._owner.rpc_data is not None:
            return _FakeResult(self._owner.rpc_data)
        return _FakeResult(list(self._owner.rpc_rows))


class _RecordingEmbed:
    """记录调用并返回预定结果 / 抛预定异常的 embedding callable。"""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def __call__(self, text):
        self.calls.append(text)
        if self.exc is not None:
            raise self.exc
        return self.result


class _RecordingRpc:
    """注入模块的 RPC callable：记录 params，返回预定行 / 抛预定异常。
    data 为哨兵 _UNSET 时返回 rows；否则 .data 恰为传入值（可设 None 等）。"""

    _UNSET = object()

    def __init__(self, rows=(), exc=None, data=_UNSET):
        self.calls = []
        self.rows = list(rows)
        self.exc = exc
        self.data = data

    def __call__(self, params):
        self.calls.append(params)
        if self.exc is not None:
            raise self.exc
        if self.data is _RecordingRpc._UNSET:
            return _FakeResult(list(self.rows))
        return _FakeResult(self.data)


# ==========================================
# 调用辅助
# ==========================================

def _call_handler(body=None, raw=None, method="POST", embed=None, db=None,
                  user_id=_USER_MARKER):
    """直调 handler；返回 (send, logs, db, embed)。"""
    send = FakeSend()
    logs = []
    if raw is None:
        payload = _default_body()
        if body is not None:
            payload = body
        raw = json.dumps(payload).encode("utf-8")
    scope = {"method": method, "path": _PATH}
    if db is None:
        db = _HybridFakeService(rpc_rows=[_rpc_row()])
    if embed is None:
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
    env = {"API_SECRET": _SECRET}
    with patch.dict(os.environ, env), \
         patch.object(_srv, "_get_embedding", embed), \
         patch.object(_srv, "supabase_service", db), \
         patch.object(_srv, "_resolve_pinecone_user_id", lambda: user_id), \
         patch.object(gateway, "_log", lambda m: logs.append(m)):
        asyncio.run(gateway.HostFixMiddleware._handle_memory_hybrid_recall(
            None, scope, FakeReceive(raw), send))
    return send, logs, db, embed


def _run_module(rpc=None, embed=None, query=_QUERY_MARKER,
                user_id=_USER_MARKER, top_k=mhr.DEFAULT_TOP_K):
    """直调模块 run_hybrid_recall；返回 (result, log_line, embed, rpc)。"""
    if embed is None:
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
    if rpc is None:
        rpc = _RecordingRpc(rows=[_rpc_row()])
    result, log_line = asyncio.run(
        mhr.run_hybrid_recall(query, user_id, embed, rpc, top_k))
    return result, log_line, embed, rpc


def _run_rows(rows, query=_QUERY_MARKER, top_k=mhr.DEFAULT_TOP_K):
    """按给定 RPC 行直调模块；返回 (result, log_line)。"""
    result, log_line, _, _ = _run_module(
        rpc=_RecordingRpc(rows=rows), query=query, top_k=top_k)
    return result, log_line


def _mw_call(scope, body=b""):
    """完整中间件分发（测鉴权/CORS）。"""
    send = FakeSend()
    app = gateway.HostFixMiddleware(None)
    asyncio.run(app(scope, FakeReceive(body), send))
    return send


def _auth_scope(method="POST", with_auth=True):
    headers = ([(b"authorization", f"Bearer {_SECRET}".encode("utf-8"))]
               if with_auth else [])
    return {"type": "http", "path": _PATH, "method": method,
            "headers": headers}


def _resp_text(send):
    for m in send.msgs:
        if m.get("type") == "http.response.body":
            return m.get("body", b"").decode("utf-8")
    return ""


def _all_keys(obj):
    """递归收集响应中出现的全部字典键（扫禁用键）。"""
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            keys |= _all_keys(v)
    return keys


def _expected_lexical(query, content, subject_key):
    """用第 21 阶段算法本体独立计算期望词面分（与模块同源、测试侧独立调用）。"""
    return mrec._score_candidate(
        mrec._compact(mrec._normalize_text(query)),
        mrec._cjk_bigrams(mrec._normalize_text(query)),
        mrec._tokens(mrec._normalize_text(query)),
        mrec._normalize_text(content), mrec._compact(mrec._normalize_text(content)),
        mrec._cjk_bigrams(mrec._normalize_text(content)),
        mrec._tokens(mrec._normalize_text(content)),
        mrec._compact(mrec._normalize_text(subject_key)),
        mrec._cjk_bigrams(mrec._normalize_text(subject_key)),
        mrec._tokens(mrec._normalize_text(subject_key)))[0]


# ==========================================
# A. 路由与鉴权
# ==========================================

class TestRouteAuth(unittest.TestCase):

    def test_a_requires_api_secret(self):
        with patch.dict(os.environ, {"API_SECRET": _SECRET}):
            send = _mw_call(_auth_scope(with_auth=False),
                            json.dumps(_default_body()).encode())
        self.assertEqual(send.status, 401, "无鉴权头必须 401")
        self.assertEqual(send.body_json.get("error"),
                         "Unauthorized: Missing or invalid API key")

    def test_a_wrong_api_secret_rejected(self):
        headers = [(b"authorization", b"Bearer wrong-secret")]
        scope = {"type": "http", "path": _PATH, "method": "POST",
                 "headers": headers}
        with patch.dict(os.environ, {"API_SECRET": _SECRET}):
            send = _mw_call(scope, json.dumps(_default_body()).encode())
        self.assertEqual(send.status, 401)

    def test_a_empty_api_secret_rejected(self):
        with patch.dict(os.environ, {"API_SECRET": ""}):
            send = _mw_call(_auth_scope(with_auth=True),
                            json.dumps(_default_body()).encode())
        self.assertEqual(send.status, 503, "API_SECRET 为空必须拒绝而非放行")

    def test_a_options_preflight_without_auth(self):
        scope = {"type": "http", "path": _PATH, "method": "OPTIONS",
                 "headers": []}
        with patch.dict(os.environ, {"API_SECRET": _SECRET}):
            send = _mw_call(scope)
        self.assertEqual(send.status, 204, "OPTIONS 沿用全局 CORS 预检（免鉴权）")

    def test_a_post_only_zero_db_and_provider(self):
        for method in ("GET", "PUT", "DELETE", "HEAD"):
            with self.subTest(method=method):
                embed = _RecordingEmbed(result=_vec(1024))
                db = _HybridFakeService(rpc_rows=[_rpc_row()])
                send, logs, db, e = _call_handler(method=method, embed=embed,
                                                  db=db)
                self.assertEqual(send.status, 405)
                self.assertEqual(send.body_json.get("code"),
                                 "METHOD_NOT_ALLOWED")
                self.assertEqual(e.calls, [], "非 POST 绝不触碰 provider")
                self.assertEqual(db.rpc_calls, [], "非 POST 绝不调用 RPC")
                self.assertEqual(db.rpc_execute_count, 0)
                self.assertEqual(db.table_calls, [], "非 POST 绝不触碰数据库")
                self.assertEqual(db.forbidden, [], "非 POST 绝无表操作")
                self.assertFalse(send.body_json["retrieval"]["writes_executed"])

    def test_a_valid_secret_reaches_handler(self):
        db = _HybridFakeService(rpc_rows=[_rpc_row()])
        embed = _RecordingEmbed(result=_vec(1024))
        body = json.dumps(_default_body()).encode("utf-8")
        with patch.dict(os.environ, {"API_SECRET": _SECRET}), \
             patch.object(_srv, "_get_embedding", embed), \
             patch.object(_srv, "supabase_service", db), \
             patch.object(_srv, "_resolve_pinecone_user_id",
                          lambda: _USER_MARKER), \
             patch.object(gateway, "_log", lambda m: None):
            send = _mw_call(_auth_scope(), body)
        self.assertEqual(send.status, 200, "鉴权通过后进入混合召回预览 handler")
        self.assertEqual(send.body_json.get("code"),
                         "HYBRID_RECALL_PREVIEW_READY")
        self.assertEqual(embed.calls, [_QUERY_MARKER], "输入恒为查询文本")

    def test_a_route_registered_in_dispatch(self):
        src = inspect.getsource(gateway.HostFixMiddleware.__call__)
        self.assertEqual(src.count('"/api/memory-hybrid-recall-preview"'), 1,
                         "dispatch 恰好注册一次")

    def test_a_confirm_token_consistency(self):
        self.assertEqual(mhr.CONFIRM_TOKEN, "HYBRID_RECALL_PREVIEW_ONLY")
        handler_src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_hybrid_recall)
        self.assertIn('"HYBRID_RECALL_PREVIEW_ONLY"', handler_src)

    def test_a_confirm_missing(self):
        body = {"query": _QUERY_MARKER}
        send, logs, db, e = _call_handler(body=body)
        self.assertEqual(send.status, 400)
        self.assertEqual(send.body_json.get("code"), "INVALID_CONFIRMATION")
        self.assertEqual(e.calls, [], "缺 confirm 零 provider 调用")
        self.assertEqual(db.rpc_calls, [], "缺 confirm 零 RPC")

    def test_a_confirm_wrong(self):
        for token in ("VECTOR_RECALL_PREVIEW_ONLY", "RECALL_PREVIEW_ONLY",
                      "HYBRID_RECALL_PREVIEW", "hybrid_recall_preview_only"):
            with self.subTest(token=token):
                body = {"confirm": token, "query": _QUERY_MARKER}
                send, logs, db, e = _call_handler(body=body)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_CONFIRMATION")
                self.assertEqual(e.calls, [])
                self.assertEqual(db.rpc_calls, [])

    def test_a_injection_fields_all_rejected(self):
        banned_fields = ("user_id", "status", "memory_type", "threshold",
                         "provider", "model", "namespace", "include_pending",
                         "include_rejected", "include_expired",
                         "lexical_weight", "vector_weight", "rrf_k",
                         "item_id", "vector", "embedding", "write_back",
                         "update_recall_count")
        for field in banned_fields:
            with self.subTest(field=field):
                body = _default_body()
                body[field] = "CLIENT_INJECTED_MARKER"
                embed = _RecordingEmbed(result=_vec(1024))
                db = _HybridFakeService(rpc_rows=[_rpc_row()])
                send, logs, db2, e = _call_handler(body=body, embed=embed,
                                                   db=db)
                self.assertEqual(send.status, 400, f"{field} 必须拒绝")
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_HYBRID_RECALL_REQUEST")
                self.assertEqual(e.calls, [], f"{field} 注入绝不触碰 provider")
                self.assertEqual(db.rpc_calls, [], f"{field} 注入绝不调用 RPC")
                self.assertEqual(db.rpc_execute_count, 0)
                self.assertEqual(db.table_calls, [],
                                 f"{field} 注入绝不触碰数据库")

    def test_a_query_invalid(self):
        for bad in (None, 123, "", "   ", []):
            with self.subTest(repr(bad)):
                body = _default_body()
                body["query"] = bad
                embed = _RecordingEmbed(result=_vec(1024))
                db = _HybridFakeService(rpc_rows=[_rpc_row()])
                send, logs, db, e = _call_handler(body=body, embed=embed,
                                                  db=db)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_HYBRID_RECALL_REQUEST")
                self.assertEqual(e.calls, [], "非法 query 零 provider 调用")
                self.assertEqual(db.rpc_calls, [], "非法 query 零 RPC")

    def test_a_query_too_long(self):
        body = _default_body()
        body["query"] = "字" * 501
        embed = _RecordingEmbed(result=_vec(1024))
        db = _HybridFakeService(rpc_rows=[_rpc_row()])
        send, logs, db, e = _call_handler(body=body, embed=embed, db=db)
        self.assertEqual(send.status, 400)
        self.assertEqual(send.body_json.get("code"),
                         "INVALID_HYBRID_RECALL_REQUEST")
        self.assertEqual(e.calls, [], "超长 query 零 provider 调用")
        self.assertEqual(db.rpc_calls, [])

    def test_a_query_exactly_max_length_ok(self):
        body = _default_body()
        body["query"] = "字" * 500
        send, logs, db, e = _call_handler(body=body)
        self.assertEqual(send.status, 200, "恰好 500 字符必须放行")
        self.assertEqual(send.body_json.get("code"),
                         "HYBRID_RECALL_PREVIEW_READY")

    def test_a_top_k_invalid(self):
        for bad in (True, False, 0, 11, -1, "5", 5.0, None):
            with self.subTest(repr(bad)):
                body = _default_body()
                body["top_k"] = bad
                embed = _RecordingEmbed(result=_vec(1024))
                db = _HybridFakeService(rpc_rows=[_rpc_row()])
                send, logs, db, e = _call_handler(body=body, embed=embed,
                                                  db=db)
                self.assertEqual(send.status, 400, f"top_k={bad!r} 必须拒绝")
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_HYBRID_RECALL_REQUEST")
                self.assertEqual(e.calls, [], "非法 top_k 零 provider 调用")
                self.assertEqual(db.rpc_calls, [], "非法 top_k 零 RPC")

    def test_a_top_k_boundary_ok(self):
        for ok_val in (1, 10):
            with self.subTest(top_k=ok_val):
                body = _default_body()
                body["top_k"] = ok_val
                send, logs, db, e = _call_handler(body=body)
                self.assertEqual(send.status, 200)
                self.assertEqual(send.body_json.get("code"),
                                 "HYBRID_RECALL_PREVIEW_READY")

    def test_a_top_k_default_5(self):
        self.assertEqual(mhr.DEFAULT_TOP_K, 5)
        body = {"confirm": mhr.CONFIRM_TOKEN, "query": _QUERY_MARKER}
        send, logs, db, e = _call_handler(body=body)
        self.assertEqual(send.status, 200, "缺省 top_k 必须放行（默认 5）")
        self.assertEqual(send.body_json.get("code"),
                         "HYBRID_RECALL_PREVIEW_READY")

    def test_a_invalid_json_and_non_dict(self):
        embed = _RecordingEmbed(result=_vec(1024))
        db = _HybridFakeService(rpc_rows=[_rpc_row()])
        for raw in (b"not-json{{{", b"[1,2,3]", b'"str"'):
            with self.subTest(raw=raw):
                send, logs, db2, e = _call_handler(raw=raw, embed=embed,
                                                   db=db)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_HYBRID_RECALL_REQUEST")
                self.assertEqual(e.calls, [], "非法 body 零 provider 调用")
                self.assertEqual(db.rpc_calls, [], "非法 body 零 RPC")
                self.assertEqual(db.table_calls, [], "非法 body 零表访问")


# ==========================================
# B. vector 调用
# ==========================================

class TestVectorSide(unittest.TestCase):

    def test_b_provider_called_once_with_trimmed_query(self):
        result, log_line, embed, rpc = _run_module(query="  小满 在做什么  ")
        self.assertEqual(embed.calls, ["小满 在做什么  ".strip()],
                         "输入恒为 trim 后查询文本")
        self.assertEqual(len(embed.calls), 1, "provider 恰一次")
        self.assertTrue(result["stats"]["query_embedded"])

    def test_b_provider_called_once_with_many_rows(self):
        rows = [_rpc_row(rid=f"id-{i}", similarity=0.5,
                         content=_CONTENT_NOMATCH) for i in range(10)]
        result, log_line, embed, rpc = _run_module(
            rpc=_RecordingRpc(rows=rows))
        self.assertEqual(len(embed.calls), 1, "provider 最多一次（10 行亦然）")

    def test_b_rpc_called_once(self):
        result, log_line, embed, rpc = _run_module()
        self.assertEqual(len(rpc.calls), 1, "RPC 恰一次")
        self.assertTrue(result["ok"])

    def test_b_params_shape(self):
        result, log_line, embed, rpc = _run_module()
        params = rpc.calls[0]
        self.assertEqual(set(params.keys()),
                         {"query_embedding", "p_user_id", "match_count"})
        self.assertEqual(params["p_user_id"], _USER_MARKER,
                         "p_user_id 由服务端注入")
        self.assertEqual(params["match_count"], 10, "match_count 服务端固定 10")
        self.assertIsInstance(params["query_embedding"], list)
        self.assertEqual(len(params["query_embedding"]), 1024)
        self.assertTrue(all(isinstance(v, float) for v in
                            params["query_embedding"]))

    def test_b_match_count_fixed_regardless_top_k(self):
        for top_k in (1, 3, 10):
            with self.subTest(top_k=top_k):
                rows = [_rpc_row(rid=f"id-{i}", similarity=0.5,
                                 content=_CONTENT_NOMATCH)
                        for i in range(10)]
                result, log_line, embed, rpc = _run_module(
                    rpc=_RecordingRpc(rows=rows), top_k=top_k)
                self.assertEqual(rpc.calls[0]["match_count"], 10,
                                 "客户端 top_k 绝不影响 RPC 参数")
                self.assertEqual(len(result["items"]), top_k,
                                 "top_k 只截断预览列表")

    def test_b_invalid_vector_zero_rpc(self):
        cases = [
            ("empty", []),
            ("none", None),
            ("non_list", "vec"),
            ("non_numeric", [0.1, "x"]),
            ("nan", [float("nan")] * 1024),
            ("inf", [float("inf")] * 1024),
            ("dim512", [0.001] * 512),
            ("zero", [0.0] * 1024),
        ]
        for name, vec in cases:
            with self.subTest(case=name):
                result, log_line, embed, rpc = _run_module(
                    embed=_RecordingEmbed(result=vec))
                self.assertFalse(result["ok"], f"{name} 必须失败")
                self.assertEqual(rpc.calls, [], f"{name} 必须零 RPC")
                self.assertEqual(len(embed.calls), 1,
                                 "provider 恰一次（不重试）")
                self.assertFalse(result["stats"]["query_embedded"])
                self.assertIsNone(result["stats"]["dimension"])

    def test_b_provider_exception_no_retry(self):
        result, log_line, embed, rpc = _run_module(
            embed=_RecordingEmbed(
                exc=RuntimeError(f"{_PROVIDER_SECRET_MARKER} p")))
        self.assertEqual(result["code"], "INTERNAL_ERROR")
        self.assertEqual(rpc.calls, [], "provider 失败绝不触发 RPC")
        self.assertEqual(len(embed.calls), 1, "不自动重试")
        self.assertIn("exception_type=", log_line)
        self.assertNotIn(_PROVIDER_SECRET_MARKER, log_line,
                         "异常原文绝不入日志")

    def test_b_rpc_exception(self):
        result, log_line, embed, rpc = _run_module(
            rpc=_RecordingRpc(rows=[_rpc_row()],
                              exc=RuntimeError(f"{_RPC_SECRET_MARKER} r")))
        self.assertEqual(result["code"], "VECTOR_RPC_FAILED")
        self.assertEqual(len(rpc.calls), 1, "RPC 恰一次（无重试）")
        self.assertNotIn(_RPC_SECRET_MARKER, log_line, "异常原文不入日志")
        self.assertTrue(result["stats"]["query_embedded"])
        self.assertEqual(result["stats"]["dimension"], 1024)

    def test_b_rpc_response_invalid(self):
        cases = [
            ("non_list_data", _RecordingRpc(rows=[], data="nope")),
            ("data_none", _RecordingRpc(rows=[], data=None)),
            ("over_match_count", _RecordingRpc(
                rows=[_rpc_row(rid=f"id-{i}", similarity=0.5,
                               content=_CONTENT_NOMATCH)
                      for i in range(11)])),
            ("row_not_dict", _RecordingRpc(rows=[], data=["row"])),
            ("missing_id", _RecordingRpc(
                rows=[_rpc_row(rid=None)])),
            ("empty_id", _RecordingRpc(
                rows=[_rpc_row(rid="   ")])),
            ("missing_content", _RecordingRpc(
                rows=[_rpc_row(content="")])),
            ("bad_similarity", _RecordingRpc(
                rows=[_rpc_row(similarity="high")])),
            ("similarity_out_of_range", _RecordingRpc(
                rows=[_rpc_row(similarity=1.5)])),
        ]
        for name, rpc in cases:
            with self.subTest(case=name):
                result, log_line, embed, _ = _run_module(rpc=rpc)
                self.assertEqual(result["code"], "VECTOR_RPC_RESPONSE_INVALID",
                                 f"{name} 必须整体拒绝（不静默丢弃）")
                self.assertFalse(result["ok"])
                self.assertEqual(result["items"], [])

    def test_b_no_old_rpc_no_pinecone_no_llm(self):
        mod_src = inspect.getsource(mhr)
        for banned in ("match_memories", "match_active_memories",
                       "import pinecone", "from pinecone", "pinecone.",
                       "PineconeMemoryClient", "ask_role", "_ask_llm"):
            self.assertNotIn(banned, mod_src,
                             f"混合模块源码不得包含 {banned!r}")
        handler_src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_hybrid_recall)
        for banned in ("match_memories", "match_active_memories",
                       "import pinecone", "from pinecone",
                       "PineconeMemoryClient", "ask_role", "_ask_llm"):
            self.assertNotIn(banned, handler_src,
                             f"混合 handler 源码不得包含 {banned!r}")

    def test_b_rpc_name_and_service_role_source(self):
        self.assertEqual(mhr.RPC_NAME, "match_memory_items")
        handler_src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_hybrid_recall)
        self.assertIn("_srv_st.supabase_service.rpc(_mhr.RPC_NAME, params)",
                      handler_src, "只读 RPC 经 service_role 客户端构造")


# ==========================================
# C. lexical 侧
# ==========================================

class TestLexicalSide(unittest.TestCase):

    def test_c_exact_wordform_hit(self):
        """query 完整包含于 content → EXACT 信号触发，lexical 参与融合。"""
        rows = [_rpc_row(similarity=0.4)]
        result, log_line = _run_rows(rows, query="看了牙医")
        self.assertEqual(result["code"], "HYBRID_RECALL_PREVIEW_READY")
        item = result["items"][0]
        self.assertGreater(item["lexical_score"], 0.0)
        self.assertEqual(item["lexical_rank"], 1)
        self.assertEqual(item["retrieval_sources"], ["vector", "lexical"])
        expected = _expected_lexical("看了牙医", _CONTENT_MARKER,
                                     _SUBJECT_KEY_MARKER)
        self.assertEqual(item["lexical_score"], expected,
                         "词面分与第 21 阶段算法本体逐位一致")

    def test_c_no_wordform_no_lexical_rank(self):
        """零词面重合 → lexical_score=0、lexical_rank=null、lexical 贡献 0。"""
        rows = [_rpc_row(similarity=0.82, content=_CONTENT_NOMATCH)]
        result, log_line = _run_rows(rows)
        self.assertEqual(result["code"], "HYBRID_RECALL_PREVIEW_READY")
        item = result["items"][0]
        self.assertEqual(item["lexical_score"], 0.0)
        self.assertIsNone(item["lexical_rank"])
        self.assertEqual(item["retrieval_sources"], ["vector"])
        self.assertEqual(result["stats"]["lexical_candidates"], 0)
        self.assertLess(item["rrf_score"], 1.0 / (mhr.RRF_K + 1) + 1e-9)

    def test_c_lexical_only_over_rpc_candidates(self):
        """lexical 只对 RPC 候选二次打分：零额外表访问、零额外 SELECT。"""
        db = _HybridFakeService(rpc_rows=[_rpc_row()])
        send, logs, d, e = _call_handler(db=db)
        self.assertEqual(send.body_json.get("code"),
                         "HYBRID_RECALL_PREVIEW_READY")
        self.assertEqual(d.table_calls, [], "绝不直接读表（无全量词面检索）")
        self.assertEqual(d.forbidden, [])
        self.assertEqual(len(d.rpc_calls), 1, "除 RPC 外零数据库调用")

    def test_c_second_pass_declaration(self):
        """模块文档必须声明：lexical 是对 vector 候选的二次排序而非全量检索。"""
        doc = inspect.getdoc(mhr) or ""
        self.assertIn("二次排序", doc)
        self.assertIn("不是全量", doc)

    def test_c_lexical_failure_not_disguised(self):
        """lexical 打分失败 → INTERNAL_ERROR；绝不伪装成 vector 成功/失败。"""
        def _boom(*a, **k):
            raise RuntimeError(f"{_PROVIDER_SECRET_MARKER} lex")
        with patch.object(mhr, "_lexical_score", _boom):
            result, log_line, embed, rpc = _run_module()
        self.assertEqual(result["code"], "INTERNAL_ERROR")
        self.assertFalse(result["ok"])
        self.assertEqual(result["items"], [])
        # vector 侧确实已成功执行（query_embedded=true、RPC 恰一次），
        # 但响应绝不以 READY 伪装成功
        self.assertTrue(result["stats"]["query_embedded"])
        self.assertEqual(len(rpc.calls), 1)
        self.assertNotIn(_PROVIDER_SECRET_MARKER, log_line)

    def test_c_lexical_algorithm_identity(self):
        """算法本体必须是第 21 阶段模块同一对象（杜绝复制漂移）。"""
        self.assertIs(mhr._score_candidate, mrec._score_candidate)
        self.assertIs(mhr._normalize_text, mrec._normalize_text)
        self.assertIs(mhr._compact, mrec._compact)
        self.assertIs(mhr._cjk_bigrams, mrec._cjk_bigrams)
        self.assertIs(mhr._tokens, mrec._tokens)

    def test_c_trust_boundary_identity(self):
        """信任边界与二次过滤必须是第 35 阶段同一实现。"""
        self.assertIs(mhr._parse_rpc_row, mvrec._parse_rpc_row)
        self.assertIs(mhr._row_state, mvrec._row_state)

    def test_c_vector_validation_identity(self):
        """查询向量校验必须是第 31 阶段同一实现。"""
        self.assertIs(mhr.validate_vector, memb.validate_vector)


# ==========================================
# D. 内部 ID 去重
# ==========================================

class TestIdDedup(unittest.TestCase):

    def test_d_same_id_returned_once(self):
        """同一 memory_item_id 只返回一次（即使 RPC 返回两行）。"""
        rows = [_rpc_row(rid=_ITEM_ID_MARKER, similarity=0.9,
                         content=_CONTENT_NOMATCH),
                _rpc_row(rid=_ITEM_ID_MARKER, similarity=0.5,
                         content=_CONTENT_MARKER)]
        result, log_line = _run_rows(rows)
        self.assertEqual(result["code"], "HYBRID_RECALL_PREVIEW_READY")
        self.assertEqual(len(result["items"]), 1, "同 ID 只保留首个")
        self.assertEqual(result["stats"]["merged_candidates"], 1)
        self.assertIn("duplicate_filtered=1", log_line)

    def test_d_vector_only_source(self):
        rows = [_rpc_row(similarity=0.82, content=_CONTENT_NOMATCH)]
        result, _ = _run_rows(rows)
        item = result["items"][0]
        self.assertEqual(item["retrieval_sources"], ["vector"])
        self.assertIsNotNone(item["vector_rank"])
        self.assertIsNone(item["lexical_rank"])

    def test_d_vector_plus_lexical_source(self):
        rows = [_rpc_row(similarity=0.82)]
        result, _ = _run_rows(rows)
        item = result["items"][0]
        self.assertEqual(item["retrieval_sources"], ["vector", "lexical"])
        self.assertIsNotNone(item["vector_rank"])
        self.assertIsNotNone(item["lexical_rank"])

    def test_d_lexical_never_standalone(self):
        """lexical 是二次排序：任何带 lexical 来源的条目必带 vector 来源。"""
        rows = [_rpc_row(rid=_ITEM_ID_MARKER, similarity=0.9),
                _rpc_row(rid=_ITEM_ID_MARKER_2, similarity=0.3,
                         content=_CONTENT_NOMATCH),
                _rpc_row(rid=_ITEM_ID_MARKER_3, similarity=0.1,
                         content="小满去看了牙医")]
        result, _ = _run_rows(rows)
        self.assertGreater(len(result["items"]), 0)
        for item in result["items"]:
            if "lexical" in item["retrieval_sources"]:
                self.assertIn("vector", item["retrieval_sources"],
                              "lexical 永不脱离 vector 单独出现")

    def test_d_content_not_identity(self):
        """相同 content 不同 ID = 两条不同记忆（绝不按 content 去重）。"""
        rows = [_rpc_row(rid=_ITEM_ID_MARKER, similarity=0.9),
                _rpc_row(rid=_ITEM_ID_MARKER_2, similarity=0.8)]
        result, _ = _run_rows(rows)
        self.assertEqual(len(result["items"]), 2, "content 不是身份")

    def test_d_subject_and_hash_not_identity(self):
        """相同 subject_key / content_hash 不同 ID = 两条不同记忆。"""
        rows = [_rpc_row(rid=_ITEM_ID_MARKER, similarity=0.9,
                         subject_key="同一次体", content="甲正文"),
                _rpc_row(rid=_ITEM_ID_MARKER_2, similarity=0.8,
                         subject_key="同一次体", content="乙正文")]
        result, _ = _run_rows(rows)
        self.assertEqual(len(result["items"]), 2,
                         "subject_key/content_hash 不是身份")

    def test_d_internal_id_never_in_http(self):
        rows = [_rpc_row(), _rpc_row(rid=_ITEM_ID_MARKER_2, similarity=0.3,
                                     content=_CONTENT_NOMATCH)]
        send, logs, db, e = _call_handler(
            db=_HybridFakeService(rpc_rows=rows))
        text = _resp_text(send)
        for marker in (_ITEM_ID_MARKER, _ITEM_ID_MARKER_2, _ITEM_ID_MARKER_3):
            self.assertNotIn(marker, text, "内部 memory_item_id 绝不进 HTTP")
        for key in _all_keys(send.body_json):
            self.assertNotIn("item_id", key.lower())
            self.assertNotEqual(key.lower(), "id")


# ==========================================
# E. RRF 融合
# ==========================================

class TestRRF(unittest.TestCase):

    def test_e_rrf_k_constant_60(self):
        self.assertEqual(mhr.RRF_K, 60)

    def test_e_vector_only_rrf(self):
        rows = [_rpc_row(similarity=0.82, content=_CONTENT_NOMATCH)]
        result, _ = _run_rows(rows)
        item = result["items"][0]
        self.assertEqual(item["vector_rank"], 1)
        self.assertEqual(item["rrf_score"],
                         round(1.0 / (mhr.RRF_K + 1), 6))
        self.assertAlmostEqual(item["rrf_score"], 0.016393, places=6)

    def test_e_both_sides_rrf(self):
        """双侧候选：rrf = 1/(k+vector_rank) + 1/(k+lexical_rank)。"""
        rows = [_rpc_row(rid=_ITEM_ID_MARKER, similarity=0.9,
                         content=_CONTENT_NOMATCH),
                _rpc_row(rid=_ITEM_ID_MARKER_2, similarity=0.5)]
        result, _ = _run_rows(rows)
        by_importance_rank = {item["vector_rank"]: item
                              for item in result["items"]}
        a = by_importance_rank[1]
        b = by_importance_rank[2]
        self.assertEqual(a["retrieval_sources"], ["vector"])
        self.assertEqual(a["rrf_score"], round(1.0 / (mhr.RRF_K + 1), 6))
        self.assertEqual(b["retrieval_sources"], ["vector", "lexical"])
        self.assertEqual(b["lexical_rank"], 1)
        self.assertEqual(b["rrf_score"],
                         round(1.0 / (mhr.RRF_K + 2) + 1.0 / (mhr.RRF_K + 1),
                               6))

    def test_e_formula_uses_ranks_only(self):
        """RRF 公式只使用排名；不出现分数加权混合。"""
        src = inspect.getsource(mhr)
        self.assertIn("1.0 / (RRF_K + v_rank)", src)
        self.assertIn("1.0 / (RRF_K + l_rank)", src)
        for banned in ("0.7", "0.3", "weight", "lexical_weight",
                       "vector_weight"):
            self.assertNotIn(banned, src,
                             f"融合排序不得出现固定分数权重 {banned!r}")

    def test_e_rrf_governs_over_similarity(self):
        """rrf 支配排序：vector rank2+lexical rank1 > vector rank1 单侧，
        即使后者 similarity 更高。"""
        rows = [_rpc_row(rid=_ITEM_ID_MARKER, similarity=0.9,
                         content=_CONTENT_NOMATCH),
                _rpc_row(rid=_ITEM_ID_MARKER_2, similarity=0.5)]
        result, _ = _run_rows(rows)
        items = result["items"]
        self.assertEqual(items[0]["vector_rank"], 2,
                         "双侧融合分者排第一（尽管 similarity 较低）")
        self.assertEqual(items[0]["lexical_rank"], 1)
        self.assertEqual(items[1]["vector_rank"], 1)
        self.assertGreater(items[0]["rrf_score"], items[1]["rrf_score"])

    def test_e_similarity_preserved(self):
        rows = [_rpc_row(similarity=0.649424)]
        result, _ = _run_rows(rows)
        self.assertEqual(result["items"][0]["vector_similarity"], 0.649424)

    def test_e_lexical_score_is_algorithm_output(self):
        rows = [_rpc_row(similarity=0.82)]
        result, _ = _run_rows(rows)
        expected = _expected_lexical(_QUERY_MARKER, _CONTENT_MARKER,
                                     _SUBJECT_KEY_MARKER)
        self.assertGreater(expected, 0.0, "测试数据必须有词面重合")
        self.assertEqual(result["items"][0]["lexical_score"], expected)

    def test_e_stable_order_same_similarity(self):
        """同 similarity 保持 RPC 原序（稳定排序），vector_rank 依次递增。"""
        rows = [_rpc_row(rid=_ITEM_ID_MARKER, similarity=0.8, importance=5,
                         content=_CONTENT_NOMATCH),
                _rpc_row(rid=_ITEM_ID_MARKER_2, similarity=0.8, importance=3,
                         content=_CONTENT_NOMATCH)]
        result, _ = _run_rows(rows)
        items = result["items"]
        self.assertEqual([it["importance"] for it in items], [5, 3],
                         "同分保持 RPC 原序")
        self.assertEqual([it["vector_rank"] for it in items], [1, 2])

    def test_e_importance_only_final_tiebreak(self):
        """最终排序键固定为 (-rrf, -importance, vector_rank)；RPC 投影无
        updated_at，importance 只是最终稳定 tie-break。"""
        src = inspect.getsource(mhr)
        self.assertIn('key=lambda e: (-e["rrf"], -e["importance"],',
                      src)
        self.assertIn('e["vector_rank"]))', src)

    def test_e_no_threshold_low_similarity_returned(self):
        """不设相似度阈值：低相似度候选照常返回。"""
        rows = [_rpc_row(similarity=0.05, content=_CONTENT_NOMATCH)]
        result, _ = _run_rows(rows)
        self.assertEqual(result["code"], "HYBRID_RECALL_PREVIEW_READY")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["vector_similarity"], 0.05)

    def test_e_threshold_applied_false_everywhere(self):
        rows = [_rpc_row()]
        result, _ = _run_rows(rows)
        self.assertFalse(result["stats"]["threshold_applied"])
        self.assertFalse(result["retrieval"]["threshold_applied"])
        send, logs, db, e = _call_handler()
        self.assertFalse(send.body_json["stats"]["threshold_applied"])
        self.assertFalse(send.body_json["retrieval"]["threshold_applied"])

    def test_e_top_k_truncates_after_fusion(self):
        rows = [_rpc_row(rid=f"id-{i}", similarity=0.9 - 0.05 * i,
                         content=_CONTENT_NOMATCH) for i in range(8)]
        result, _ = _run_rows(rows, top_k=3)
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["stats"]["merged_candidates"], 8)
        self.assertEqual(result["stats"]["returned"], 3)
        self.assertEqual([it["vector_rank"] for it in result["items"]],
                         [1, 2, 3])


# ==========================================
# F. 状态与时间隔离
# ==========================================

class TestStateTime(unittest.TestCase):

    def test_f_no_status_key_kept(self):
        """真实 RPC 不返回 status 列：缺 status 的行以 SQL 过滤为准保留。"""
        rows = [_rpc_row(status=None)]
        result, _ = _run_rows(rows)
        self.assertEqual(result["code"], "HYBRID_RECALL_PREVIEW_READY")
        self.assertEqual(len(result["items"]), 1)

    def test_f_explicit_active_kept(self):
        rows = [_rpc_row(status="active")]
        result, _ = _run_rows(rows)
        self.assertEqual(len(result["items"]), 1)

    def test_f_non_active_dropped(self):
        for status in ("pending_review", "rejected", "superseded"):
            with self.subTest(status=status):
                rows = [_rpc_row(status=status)]
                result, log_line = _run_rows(rows)
                self.assertEqual(result["code"], "HYBRID_RECALL_NO_RESULTS")
                self.assertEqual(result["items"], [])
                self.assertIn("status_filtered=1", log_line)

    def test_f_expired_dropped(self):
        rows = [_rpc_row(expires_at=_PAST_TS)]
        result, log_line = _run_rows(rows)
        self.assertEqual(result["code"], "HYBRID_RECALL_NO_RESULTS")
        self.assertEqual(result["items"], [])
        self.assertIn("expired_filtered=1", log_line)

    def test_f_unexpired_kept(self):
        rows = [_rpc_row(expires_at=_FUTURE_TS)]
        result, _ = _run_rows(rows)
        self.assertEqual(len(result["items"]), 1)

    def test_f_unparseable_time_dropped(self):
        rows = [_rpc_row(expires_at="not-a-time")]
        result, log_line = _run_rows(rows)
        self.assertEqual(result["code"], "HYBRID_RECALL_NO_RESULTS")
        self.assertIn("invalid_time_filtered=1", log_line)

    def test_f_mixed_rows_counted(self):
        rows = [_rpc_row(rid=_ITEM_ID_MARKER, similarity=0.9,
                         content=_CONTENT_NOMATCH),
                _rpc_row(rid=_ITEM_ID_MARKER_2, similarity=0.8,
                         content=_CONTENT_NOMATCH, status="pending_review"),
                _rpc_row(rid=_ITEM_ID_MARKER_3, similarity=0.7,
                         content=_CONTENT_NOMATCH, expires_at=_PAST_TS)]
        result, log_line = _run_rows(rows)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["stats"]["vector_candidates"], 1)
        self.assertIn("status_filtered=1", log_line)
        self.assertIn("expired_filtered=1", log_line)

    def test_f_zero_update_ever(self):
        db = _HybridFakeService(rpc_rows=[_rpc_row()])
        send, logs, d, e = _call_handler(db=db)
        self.assertEqual(d.forbidden, [], "无 insert/update/delete/upsert")
        self.assertEqual(d.table_calls, [], "无任何直接表访问")
        self.assertEqual(len(d.rpc_calls), 1, "RPC 恰一次（只读）")


# ==========================================
# G. 响应与脱敏
# ==========================================

class TestResponseShape(unittest.TestCase):

    def test_g_method_triple(self):
        result, _ = _run_rows([_rpc_row()])
        r = result["retrieval"]
        self.assertEqual(r["method"], "rrf_hybrid_preview_v1")
        self.assertEqual(r["vector_method"], "pgvector_cosine_vector_recall_v1")
        self.assertEqual(r["lexical_method"], "deterministic_lexical_v1")

    def test_g_static_flags(self):
        result, _ = _run_rows([_rpc_row()])
        r = result["retrieval"]
        self.assertTrue(r["active_only"])
        self.assertTrue(r["expired_excluded"])
        self.assertTrue(r["user_scoped"])
        self.assertFalse(r["threshold_applied"])
        self.assertFalse(r["writes_executed"])

    def test_g_top_level_keys_exact(self):
        result, _ = _run_rows([_rpc_row()])
        self.assertEqual(set(result.keys()),
                         {"ok", "code", "stats", "retrieval", "items"})

    def test_g_stats_keys_exact(self):
        result, _ = _run_rows([_rpc_row()])
        self.assertEqual(set(result["stats"].keys()),
                         {"query_embedded", "dimension", "vector_candidates",
                          "lexical_candidates", "merged_candidates",
                          "returned", "threshold_applied"})

    def test_g_item_keys_exact(self):
        result, _ = _run_rows([_rpc_row()])
        self.assertEqual(set(result["items"][0].keys()),
                         {"recall_index", "rank", "memory_type", "content",
                          "importance", "confidence", "subject_key",
                          "valid_at", "expires_at", "source",
                          "vector_similarity", "lexical_score", "rrf_score",
                          "vector_rank", "lexical_rank",
                          "retrieval_sources"})

    def test_g_banned_keys_absent_everywhere(self):
        rows = [_rpc_row(), _rpc_row(rid=_ITEM_ID_MARKER_2, similarity=0.3,
                                     content=_CONTENT_NOMATCH)]
        result, _ = _run_rows(rows)
        keys = _all_keys(result)
        for banned in ("memory_item_id", "id", "user_id", "status",
                       "embedding", "embedding_model", "embedded_at",
                       "content_hash", "metadata", "superseded_by",
                       "created_by", "source_event_ids", "source_batch_id",
                       "similarity", "model", "provider"):
            self.assertNotIn(banned, keys, f"响应不得出现键 {banned!r}")

    def test_g_stats_values_ready(self):
        result, _ = _run_rows([_rpc_row()])
        s = result["stats"]
        self.assertTrue(s["query_embedded"])
        self.assertEqual(s["dimension"], 1024)
        self.assertEqual(s["vector_candidates"], 1)
        self.assertEqual(s["lexical_candidates"], 1)
        self.assertEqual(s["merged_candidates"], 1)
        self.assertEqual(s["returned"], 1)

    def test_g_no_results_shape(self):
        result, log_line = _run_rows([])
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "HYBRID_RECALL_NO_RESULTS")
        self.assertEqual(result["items"], [])
        self.assertTrue(result["stats"]["query_embedded"])
        self.assertEqual(result["stats"]["dimension"], 1024)
        self.assertEqual(result["stats"]["returned"], 0)
        self.assertFalse(result["retrieval"]["writes_executed"])

    def test_g_http_status_mapping(self):
        self.assertEqual(mhr.HTTP_STATUS_BY_CODE["HYBRID_RECALL_PREVIEW_READY"],
                         200)
        self.assertEqual(mhr.HTTP_STATUS_BY_CODE["HYBRID_RECALL_NO_RESULTS"],
                         200)
        self.assertEqual(mhr.HTTP_STATUS_BY_CODE["INVALID_HYBRID_RECALL_REQUEST"],
                         400)
        self.assertEqual(mhr.HTTP_STATUS_BY_CODE["INVALID_CONFIRMATION"], 400)
        self.assertEqual(mhr.HTTP_STATUS_BY_CODE["EMBEDDING_UNAVAILABLE"], 503)
        self.assertEqual(mhr.HTTP_STATUS_BY_CODE["EMBEDDING_DIMENSION_MISMATCH"],
                         503)
        self.assertEqual(mhr.HTTP_STATUS_BY_CODE["EMBEDDING_ZERO_VECTOR"], 503)
        self.assertEqual(mhr.HTTP_STATUS_BY_CODE["VECTOR_RPC_FAILED"], 500)
        self.assertEqual(mhr.HTTP_STATUS_BY_CODE["VECTOR_RPC_RESPONSE_INVALID"],
                         500)
        self.assertEqual(mhr.HTTP_STATUS_BY_CODE["INTERNAL_ERROR"], 500)

    def test_g_handler_status_end_to_end(self):
        cases = [
            ("ready", _HybridFakeService(rpc_rows=[_rpc_row()]),
             _RecordingEmbed(result=_vec(1024)), 200,
             "HYBRID_RECALL_PREVIEW_READY"),
            ("no_results", _HybridFakeService(rpc_rows=[]),
             _RecordingEmbed(result=_vec(1024)), 200,
             "HYBRID_RECALL_NO_RESULTS"),
            ("embedding_empty", _HybridFakeService(rpc_rows=[_rpc_row()]),
             _RecordingEmbed(result=[]), 503, "EMBEDDING_UNAVAILABLE"),
            ("dimension", _HybridFakeService(rpc_rows=[_rpc_row()]),
             _RecordingEmbed(result=_vec(512)), 503,
             "EMBEDDING_DIMENSION_MISMATCH"),
            ("rpc_error", _HybridFakeService(
                rpc_rows=[_rpc_row()],
                rpc_exc=RuntimeError("rpc down")), 
             _RecordingEmbed(result=_vec(1024)), 500, "VECTOR_RPC_FAILED"),
        ]
        for name, db, embed, status, code in cases:
            with self.subTest(case=name):
                send, logs, _, _ = _call_handler(db=db, embed=embed)
                self.assertEqual(send.status, status)
                self.assertEqual(send.body_json.get("code"), code)

    def test_g_query_and_ids_not_in_logs(self):
        """日志只含计数：查询原文、内部 ID、正文绝不出现在日志。"""
        send, logs, db, e = _call_handler(
            db=_HybridFakeService(rpc_rows=[_rpc_row()]))
        joined = "".join(logs)
        self.assertNotIn(_QUERY_MARKER, joined, "查询原文不入日志")
        self.assertNotIn(_CONTENT_MARKER, joined, "正文不入日志")
        self.assertNotIn(_ITEM_ID_MARKER, joined, "内部 ID 不入日志")
        self.assertIn("vector_candidates=1", joined)
        self.assertIn("returned=1", joined)


# ==========================================
# H. 零写入与正式上下文隔离
# ==========================================

class TestIsolation(unittest.TestCase):

    def test_h_module_source_has_no_forbidden_calls(self):
        src = inspect.getsource(mhr)
        for banned in ("import pinecone", "from pinecone", "pinecone.",
                       "PineconeMemoryClient", "import supabase",
                       "from supabase", "create_client",
                       ".delete(", ".upsert(", ".insert(", ".update(",
                       ".rpc(", "DELETE FROM", "TRUNCATE", "DROP TABLE",
                       "create_task", "ensure_future", "Timer(",
                       "threading", "time.sleep", "subprocess",
                       "ask_role", "_ask_llm",
                       "import server", "from server",
                       "import gateway", "from gateway",
                       "import os", "getenv", "os.environ",
                       "requests.", "httpx", "urlopen", "urllib",
                       "stable_system", "volatile_block",
                       "tool_loop", "compose_member_view",
                       "_inject_context", "_build_channel_context",
                       "memory_events",
                       "schedule", "cron", "print(",
                       "match_memories", "match_active_memories"):
            self.assertNotIn(banned, src,
                             f"memory_hybrid_recall 源码不得包含 {banned!r}")

    def test_h_module_reuses_existing_modules_only(self):
        src = inspect.getsource(mhr)
        self.assertIn("from memory_recall import", src,
                      "词面算法必须 import 复用第 21 阶段")
        self.assertIn("from memory_vector_recall import", src,
                      "信任边界必须 import 复用第 35 阶段")
        self.assertIn("from memory_embedding import", src,
                      "向量校验必须 import 复用第 31 阶段")

    def test_h_handler_source_has_no_forbidden_calls(self):
        src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_hybrid_recall)
        for banned in ("PineconeMemoryClient", "from pinecone",
                       "import pinecone", "pinecone.", "ask_role", "_ask_llm",
                       "create_task", "ensure_future", "Timer(",
                       "threading", "getenv", "os.environ",
                       "tool_loop", "compose_member_view",
                       "stable_system", "volatile_block",
                       "_inject_context", "_build_channel_context",
                       "memory_events", "schedule", "cron",
                       "match_active_memories", "match_memories'",
                       ".insert(", ".update(",
                       ".delete(", ".upsert("):
            self.assertNotIn(banned, src,
                             f"混合召回 handler 源码不得包含 {banned!r}")

    def test_h_handler_reuses_server_dependencies(self):
        src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_hybrid_recall)
        self.assertIn("_srv_st._get_embedding", src,
                      "复用现有 _get_embedding，不新建客户端")
        self.assertIn("_srv_st.supabase_service", src,
                      "复用 service_role 客户端")
        self.assertIn("_resolve_pinecone_user_id", src,
                      "user_id 由服务端统一解析")
        self.assertIn("_mhr.RPC_NAME", src, "只读 RPC 名来自模块常量")
        self.assertIn("_mhr.QUERY_MAX_LENGTH", src,
                      "query 上限来自模块常量，避免漂移")
        self.assertIn("_mhr.DEFAULT_TOP_K", src, "top_k 缺省来自模块常量")
        self.assertIn("HYBRID_RECALL_PREVIEW_ONLY", src)

    def test_h_behavior_zero_writes(self):
        db = _HybridFakeService(rpc_rows=[_rpc_row()])
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, d, e = _call_handler(db=db, embed=embed)
        self.assertEqual(send.body_json.get("code"),
                         "HYBRID_RECALL_PREVIEW_READY")
        self.assertEqual(d.forbidden, [], "无 insert/update/delete/upsert")
        self.assertEqual(d.table_calls, [], "无任何直接表访问")
        self.assertEqual(len(d.rpc_calls), 1, "RPC 恰一次（只读）")
        self.assertEqual(len(e.calls), 1, "provider 恰一次")
        self.assertFalse(send.body_json["retrieval"]["writes_executed"])

    def test_h_module_prints_nothing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _run_module()
            _run_rows([])
            _run_module(embed=_RecordingEmbed(result=[]))
            _run_module(rpc=_RecordingRpc(
                rows=[_rpc_row()],
                exc=RuntimeError("boom")))
        self.assertEqual(buf.getvalue(), "", "模块不得直接打印任何内容")


# ==========================================
# G+. 全路径脱敏扫描
# ==========================================

class TestNoLeakage(unittest.TestCase):

    _MARKERS = (_QUERY_MARKER, _USER_MARKER, _ITEM_ID_MARKER,
                _ITEM_ID_MARKER_2, _ITEM_ID_MARKER_3, _MODEL_MARKER,
                _HASH_MARKER, _EVENT_IDS_MARKER, _BATCH_ID_MARKER,
                _METADATA_MARKER, _PROVIDER_SECRET_MARKER,
                _RPC_SECRET_MARKER, "SHOULD-NOT-LEAK", "DOUBAO",
                "DOUBAO_EMBEDDING_EP", "siliconflow", "api.siliconflow",
                "Bearer", "api_key", "Authorization", "rest/v1",
                "memory_items", "content_hash", "traceback",
                "superseded_by", "source_event_ids", "source_batch_id",
                "embedding_model")

    def _leak_scan(self, send, logs):
        text = _resp_text(send) + "".join(logs)
        for marker in self._MARKERS:
            self.assertNotIn(marker, text, f"泄漏标记 {marker!r} 出现在响应/日志")
        self.assertNotIn("confirm", text.lower(), "请求体内容不得回显")
        return text

    def test_g_success_path_no_leakage(self):
        # 默认 RPC 行携带全部敏感额外字段（user_id/embedding/模型名/hash/
        # 事件ID/批ID/metadata/created_by），响应与日志都不得回显敏感值；
        # content 本身是预览载荷（与第 21/35 阶段一致），不在扫描标记内
        send, logs, db, e = _call_handler()
        text = self._leak_scan(send, logs)
        self.assertIn("HYBRID_RECALL_PREVIEW_READY", text)

    def test_g_failure_paths_no_leakage(self):
        scenarios = [
            ("no_results", _HybridFakeService(rpc_rows=[]),
             _RecordingEmbed(result=_vec(1024))),
            ("embedding_empty", _HybridFakeService(rpc_rows=[_rpc_row()]),
             _RecordingEmbed(result=[])),
            ("provider_error", _HybridFakeService(rpc_rows=[_rpc_row()]),
             _RecordingEmbed(exc=RuntimeError(
                 f"{_PROVIDER_SECRET_MARKER} p"))),
            ("rpc_error", _HybridFakeService(
                rpc_rows=[_rpc_row()],
                rpc_exc=RuntimeError(f"{_RPC_SECRET_MARKER} rpc")),
             _RecordingEmbed(result=_vec(1024))),
            ("rpc_invalid", _HybridFakeService(rpc_rows=[], rpc_data="bad"),
             _RecordingEmbed(result=_vec(1024))),
        ]
        for name, db, embed in scenarios:
            with self.subTest(case=name):
                send, logs, _, _ = _call_handler(db=db, embed=embed)
                self._leak_scan(send, logs)

    def test_g_response_carries_content_payload(self):
        """与第 21/35 阶段一致：预览条目返回 content 正文本身。"""
        result, _ = _run_rows([_rpc_row()])
        self.assertEqual(result["items"][0]["content"], _CONTENT_MARKER)
        self.assertEqual(result["items"][0]["subject_key"],
                         _SUBJECT_KEY_MARKER)


if __name__ == "__main__":
    unittest.main()
