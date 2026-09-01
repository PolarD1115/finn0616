# -*- coding: utf-8 -*-
"""第 35 阶段专项测试 —— 用户查询向量召回只读预览。

  POST /api/memory-vector-recall-preview
    → 请求体白名单仅 confirm/query/top_k，任何注入字段一律 400 且零调用；
    → 服务端 user_id 解析后，用现有 _get_embedding 对 trim 后查询文本恰嵌入
      一次（1024/finite/非零校验，复用第 31 阶段 validate_vector）；
    → 经 service_role 只读调用 match_memory_items 恰一次（match_count 服务端
      固定 10，客户端 top_k 只截断预览列表，绝不影响 RPC 参数）；
    → 模块内存二次过滤（显式非 active 状态 / 已过期 / 时间不可解析逐行保守
      丢弃并计数；内部 memory_item_id 去重，仅服务端内存使用）；
    → 按 similarity 降序（稳定）截断 top_k，白名单脱敏条目返回；
    → 不设相似度硬阈值（threshold_applied=false）；零写入；无 Pinecone、
      无 LLM、无自动调度；不接正式上下文；不接词面召回。

全部 unittest + mock + 合成数据：不真实调用 provider、不真实执行 RPC、
不真实调用新接口、不连接真实 Supabase / Pinecone / LLM、不修改任何数据。

覆盖（任务书 A-G）：
  A 路由与鉴权（/api/* API_SECRET、仅 POST、OPTIONS、confirm/query/top_k/
    全部注入字段拒绝、非法请求零 provider/RPC/DB）
  B embedding（1024 成功、空/非list/非数值/NaN/Inf/维度错误/零向量/异常、
    恰一次不重试、非法向量零 RPC）
  C RPC（service_role 来源、参数形状、list[float]、match_count 固定不受
    top_k 影响、p_user_id 服务端注入、不调旧 RPC、恰一次、空结果、合法
    结果、缺字段/非法 similarity/非 finite/超量、内部 ID 不出 HTTP）
  D 状态与过期（active 保留、pending_review/rejected/superseded 丢弃、
    过期丢弃、未过期保留、时间非法保守丢弃、混合计数、零 UPDATE）
  E 用户隔离（客户端 user_id 拒绝、服务端 user_id 注入、不跨用户、
    响应不出 user_id）
  F 响应（similarity 语义、threshold_applied=false、method、四项静态声明、
    条目白名单键、排序与 top_k 截断、禁用键扫描、HTTP 状态映射）
  G 零写入与隔离（模块/网关源码静态扫描 + 行为断言 + 全路径脱敏扫描）
  （H 全量记忆回归由命令行单独运行）

运行： python -m unittest test_memory_vector_recall_phase35 -v
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
import memory_vector_recall as mvr
import server as _srv
from test_memory_preview_phase10 import FakeReceive, FakeSend


# ==========================================
# 常量与脱敏标记
# ==========================================

_PATH = "/api/memory-vector-recall-preview"
_SECRET = "test-secret-marker-phase35"
# 注入脱敏扫描的独特标记（断言响应/日志不外泄）
_QUERY_MARKER = "查询文本隐私标记：小满上周四做了什么"
_CONTENT_MARKER = "记忆正文隐私标记：小满周四去看了牙医"
_USER_MARKER = "user-scope-355"
_ITEM_ID_MARKER = "a1b2c3d4-recall-uuid-0001"
_OTHER_ID_MARKER = "ffff1111-other-uuid-9999"
_MODEL_MARKER = "MODEL-NAME-SECRET-123"
_SUBJECT_KEY_MARKER = "SUBJECT-KEY-SECRET-MARKER-35"
_HASH_MARKER = "HASH-SECRET-MARKER-35"
_EVENT_IDS_MARKER = "EVENT-ID-SECRET-MARKER-35"
_BATCH_ID_MARKER = "BATCH-ID-SECRET-MARKER-35"
_METADATA_MARKER = "METADATA-SECRET-MARKER-35"
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


def _rpc_row(rid=_ITEM_ID_MARKER, similarity=0.82, status="active",
             expires_at=None, extra_banned=True, **overrides):
    """RPC 返回行的合成形状（真实 RPC 的 10 个白名单列）。

    extra_banned=True 时附带真实 RPC 绝不返回的敏感列（user_id/embedding/
    embedding_model/content_hash/source_event_ids/source_batch_id/metadata/
    created_by/superseded_by），用于验证模块不信任、不回显任何额外字段；
    status 列真实 RPC 不返回，用于验证二次过滤的防御逻辑。
    """
    row = {"memory_item_id": rid,
           "content": _CONTENT_MARKER,
           "memory_type": "long_term",
           "importance": 3,
           "confidence": 0.7,
           "subject_key": _SUBJECT_KEY_MARKER,
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
                    "created_by": _OTHER_ID_MARKER,
                    "superseded_by": None})
    row.update(overrides)
    return row


def _ok_rpc_rows():
    return [_rpc_row()]


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


class _RecallFakeService:
    """handler 用假 service_role 客户端：只有 rpc 通路是真实的；
    任何直接表访问（table(...)）都被记录，测试断言其为空。"""

    def __init__(self, rpc_rows=(), rpc_exc=None):
        self.rpc_calls = []          # [(name, params)]
        self.rpc_execute_count = 0
        self.rpc_rows = list(rpc_rows)
        self.rpc_exc = rpc_exc
        self.table_calls = []        # 直接表访问（必须为空）
        self.forbidden = []          # 被禁表操作

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
        self._data = data

    def __call__(self, params):
        self.calls.append(params)
        if self.exc is not None:
            raise self.exc
        return _FakeResult(self.rows if self._data is self._UNSET else self._data)


# ==========================================
# 调用辅助
# ==========================================

def _default_body():
    return {"confirm": mvr.CONFIRM_TOKEN, "query": _QUERY_MARKER}


def _call_handler(body=None, raw=None, method="POST", embed=None, db=None,
                  user_id=_USER_MARKER, query=None):
    """直调 handler；返回 (send, logs, db, embed)。"""
    send = FakeSend()
    logs = []
    if raw is None:
        payload = _default_body()
        if query is not None:
            payload["query"] = query
        if body is not None:
            payload = body
        raw = json.dumps(payload).encode("utf-8")
    scope = {"method": method, "path": _PATH}
    if db is None:
        db = _RecallFakeService(rpc_rows=_ok_rpc_rows())
    if embed is None:
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
    env = {"API_SECRET": _SECRET}
    with patch.dict(os.environ, env), \
         patch.object(_srv, "_get_embedding", embed), \
         patch.object(_srv, "supabase_service", db), \
         patch.object(_srv, "_resolve_pinecone_user_id", lambda: user_id), \
         patch.object(gateway, "_log", lambda m: logs.append(m)):
        asyncio.run(gateway.HostFixMiddleware._handle_memory_vector_recall(
            None, scope, FakeReceive(raw), send))
    return send, logs, db, embed


def _run_module(rpc=None, embed=None, query=_QUERY_MARKER,
                user_id=_USER_MARKER, top_k=mvr.DEFAULT_TOP_K):
    """直调模块 run_recall；返回 (result, log_line, embed, rpc)。"""
    if embed is None:
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
    if rpc is None:
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
    result, log_line = asyncio.run(
        mvr.run_recall(query, user_id, embed, rpc, top_k))
    return result, log_line, embed, rpc


def _mw_call(scope, body=b""):
    """完整中间件分发（测鉴权/CORS）。"""
    send = FakeSend()
    app = gateway.HostFixMiddleware(None)
    asyncio.run(app(scope, FakeReceive(body), send))
    return send


def _auth_scope(method="POST", with_auth=True):
    headers = ([(b"authorization", f"Bearer {_SECRET}".encode("utf-8"))]
               if with_auth else [])
    return {"type": "http", "path": _PATH, "method": method, "headers": headers}


def _resp_text(send):
    for m in send.msgs:
        if m.get("type") == "http.response.body":
            return m.get("body", b"").decode("utf-8")
    return ""


def _invalid_body(field):
    body = _default_body()
    body[field] = "CLIENT_INJECTED_MARKER"
    return body


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
                db = _RecallFakeService(rpc_rows=_ok_rpc_rows())
                send, logs, db, e = _call_handler(method=method, embed=embed,
                                                  db=db)
                self.assertEqual(send.status, 405)
                self.assertEqual(send.body_json.get("code"),
                                 "METHOD_NOT_ALLOWED")
                self.assertEqual(e.calls, [], "非 POST 绝不触碰 provider")
                self.assertEqual(db.rpc_calls, [], "非 POST 绝不调用 RPC")
                self.assertEqual(db.rpc_execute_count, 0)
                self.assertEqual(db.table_calls, [], "非 POST 绝不触碰数据库")
                self.assertFalse(send.body_json["retrieval"]["writes_executed"])

    def test_a_valid_secret_reaches_handler(self):
        db = _RecallFakeService(rpc_rows=_ok_rpc_rows())
        embed = _RecordingEmbed(result=_vec(1024))
        body = json.dumps(_default_body()).encode("utf-8")
        with patch.dict(os.environ, {"API_SECRET": _SECRET}), \
             patch.object(_srv, "_get_embedding", embed), \
             patch.object(_srv, "supabase_service", db), \
             patch.object(_srv, "_resolve_pinecone_user_id",
                          lambda: _USER_MARKER), \
             patch.object(gateway, "_log", lambda m: None):
            send = _mw_call(_auth_scope(), body)
        self.assertEqual(send.status, 200, "鉴权通过后进入召回预览 handler")
        self.assertEqual(send.body_json.get("code"),
                         "VECTOR_RECALL_PREVIEW_READY")
        self.assertEqual(embed.calls, [_QUERY_MARKER], "输入恒为查询文本")

    def test_a_route_registered_in_dispatch(self):
        src = inspect.getsource(gateway.HostFixMiddleware.__call__)
        self.assertIn("/api/memory-vector-recall-preview", src)

    def test_a_confirm_token_consistency(self):
        self.assertEqual(mvr.CONFIRM_TOKEN, "VECTOR_RECALL_PREVIEW_ONLY")
        handler_src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_vector_recall)
        self.assertIn('"VECTOR_RECALL_PREVIEW_ONLY"', handler_src)


class TestRequestValidation(unittest.TestCase):

    def test_a_confirm_missing(self):
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(body={"query": _QUERY_MARKER},
                                          embed=embed)
        self.assertEqual(send.status, 400)
        self.assertEqual(send.body_json.get("code"), "INVALID_CONFIRMATION")
        self.assertEqual(e.calls, [], "confirm 缺失不得调用 provider")
        self.assertEqual(db.rpc_calls, [], "confirm 缺失不得调用 RPC")
        self.assertEqual(db.table_calls, [], "confirm 缺失不得触碰数据库")

    def test_a_confirm_wrong(self):
        for confirm in ("vector_recall_preview_only", "VECTOR", "", 123,
                        None, True, ["VECTOR_RECALL_PREVIEW_ONLY"]):
            with self.subTest(confirm=confirm):
                send, logs, db, e = _call_handler(body={"confirm": confirm,
                                                        "query": _QUERY_MARKER})
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_CONFIRMATION")
                self.assertEqual(e.calls, [])
                self.assertEqual(db.rpc_calls, [])
                self.assertEqual(db.table_calls, [])

    def test_a_injection_fields_all_rejected(self):
        # 客户端试图注入 user_id/status/memory_type/threshold/provider/model/
        # namespace/include_*/write_back/item_id/vector/embedding/force/batch
        # 等任何额外字段 → 400，绝不触 provider、绝不触 RPC、绝不触数据库
        for field in ("user_id", "status", "memory_type", "threshold",
                      "provider", "model", "namespace", "include_pending",
                      "include_rejected", "include_expired", "write_back",
                      "item_id", "vector", "embedding", "force", "batch",
                      "id", "text", "content"):
            with self.subTest(field=field):
                embed = _RecordingEmbed(result=_vec(1024))
                db = _RecallFakeService(rpc_rows=_ok_rpc_rows())
                send, logs, db2, e = _call_handler(body=_invalid_body(field),
                                                   embed=embed, db=db)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_VECTOR_RECALL_REQUEST")
                self.assertEqual(e.calls, [])
                self.assertEqual(db.rpc_calls, [])
                self.assertEqual(db.table_calls, [])

    def test_a_query_invalid(self):
        cases = {
            "missing": None,
            "not_str": 123,
            "empty": "",
            "whitespace": "   \n\t ",
        }
        for name, query in cases.items():
            with self.subTest(case=name):
                embed = _RecordingEmbed(result=_vec(1024))
                body = _default_body()
                if query is not None:
                    body["query"] = query
                else:
                    body.pop("query")
                send, logs, db, e = _call_handler(body=body, embed=embed)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_VECTOR_RECALL_REQUEST")
                self.assertEqual(e.calls, [])
                self.assertEqual(db.rpc_calls, [])
                self.assertEqual(db.table_calls, [])

    def test_a_query_too_long(self):
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(query="查" * (mvr.QUERY_MAX_LENGTH + 1),
                                          embed=embed)
        self.assertEqual(send.status, 400)
        self.assertEqual(send.body_json.get("code"),
                         "INVALID_VECTOR_RECALL_REQUEST")
        self.assertEqual(e.calls, [])
        self.assertEqual(db.rpc_calls, [])

    def test_a_query_exactly_max_length_ok(self):
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(query="记" * mvr.QUERY_MAX_LENGTH,
                                          embed=embed)
        self.assertEqual(send.status, 200)
        self.assertEqual(send.body_json.get("code"),
                         "VECTOR_RECALL_PREVIEW_READY")
        self.assertEqual(len(e.calls), 1)
        self.assertEqual(len(db.rpc_calls), 1)

    def test_a_top_k_invalid(self):
        for top_k in ("5", 5.0, True, False, None, 0, 11, -1, [5], {"k": 5}):
            with self.subTest(top_k=top_k):
                embed = _RecordingEmbed(result=_vec(1024))
                body = _default_body()
                body["top_k"] = top_k
                send, logs, db, e = _call_handler(body=body, embed=embed)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_VECTOR_RECALL_REQUEST")
                self.assertEqual(e.calls, [], "top_k 非法绝不调用 provider")
                self.assertEqual(db.rpc_calls, [], "top_k 非法绝不调用 RPC")

    def test_a_top_k_boundary_ok(self):
        for top_k in (1, 10):
            with self.subTest(top_k=top_k):
                embed = _RecordingEmbed(result=_vec(1024))
                body = _default_body()
                body["top_k"] = top_k
                send, logs, db, e = _call_handler(body=body, embed=embed)
                self.assertEqual(send.status, 200)
                self.assertEqual(send.body_json.get("code"),
                                 "VECTOR_RECALL_PREVIEW_READY")

    def test_a_top_k_default_5(self):
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(embed=embed)
        self.assertEqual(send.status, 200)
        self.assertEqual(len(db.rpc_calls), 1, "top_k 缺省仍恰一次 RPC")

    def test_a_invalid_json_and_non_dict(self):
        for raw in (b"{not json", b"[1,2,3]", b'"str"', b"null", b"123"):
            with self.subTest(raw=raw):
                embed = _RecordingEmbed(result=_vec(1024))
                send, logs, db, e = _call_handler(raw=raw, embed=embed)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_VECTOR_RECALL_REQUEST")
                self.assertEqual(e.calls, [])
                self.assertEqual(db.rpc_calls, [])
                self.assertEqual(db.table_calls, [])


# ==========================================
# B. provider 查询向量
# ==========================================

class TestQueryEmbedding(unittest.TestCase):

    def test_b_provider_called_once_with_trimmed_query(self):
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
        result, log_line, e, r = _run_module(
            embed=embed, query=f"  {_QUERY_MARKER}  \n")
        self.assertEqual(e.calls, [_QUERY_MARKER], "恰一次且输入为 trim 后文本")
        self.assertEqual(result["stats"]["query_embedded"], True)
        self.assertEqual(result["stats"]["dimension"], 1024)

    def test_b_valid_vector_reaches_rpc(self):
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, e, r = _run_module(embed=embed, rpc=rpc)
        self.assertEqual(len(rpc.calls), 1)
        self.assertEqual(rpc.calls[0]["query_embedding"], _vec(1024))
        self.assertTrue(all(isinstance(v, float) and not isinstance(v, bool)
                            for v in rpc.calls[0]["query_embedding"]))

    def _bad_vector_case(self, vec, expected_code):
        embed = _RecordingEmbed(result=vec)
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, e, r = _run_module(embed=embed, rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], expected_code)
        self.assertEqual(rpc.calls, [], "向量非法绝不调用 RPC")
        self.assertEqual(len(e.calls), 1, "provider 恰一次不重试")
        self.assertEqual(result["stats"]["query_embedded"], False)
        self.assertEqual(result["items"], [])

    def test_b_empty_and_none(self):
        self._bad_vector_case([], "EMBEDDING_UNAVAILABLE")
        self._bad_vector_case(None, "EMBEDDING_UNAVAILABLE")

    def test_b_non_list(self):
        self._bad_vector_case({"a": 1}, "EMBEDDING_RESPONSE_INVALID")
        self._bad_vector_case("0.1,0.2", "EMBEDDING_RESPONSE_INVALID")
        self._bad_vector_case(1024, "EMBEDDING_RESPONSE_INVALID")

    def test_b_non_numeric_element(self):
        self._bad_vector_case(["x"] * 1024, "EMBEDDING_RESPONSE_INVALID")
        self._bad_vector_case([None] * 1024, "EMBEDDING_RESPONSE_INVALID")

    def test_b_non_finite(self):
        bad = _vec(1024)
        bad[10] = float("nan")
        self._bad_vector_case(bad, "EMBEDDING_NON_FINITE_VALUES")
        bad2 = _vec(1024)
        bad2[10] = float("inf")
        self._bad_vector_case(bad2, "EMBEDDING_NON_FINITE_VALUES")

    def test_b_dimension_mismatch(self):
        for dim in (768, 1536, 1023, 1025):
            with self.subTest(dim=dim):
                self._bad_vector_case(_vec(dim), "EMBEDDING_DIMENSION_MISMATCH")

    def test_b_zero_vector(self):
        self._bad_vector_case([0.0] * 1024, "EMBEDDING_ZERO_VECTOR")

    def test_b_provider_exception_no_retry(self):
        embed = _RecordingEmbed(exc=RuntimeError(f"{_PROVIDER_SECRET_MARKER} p"))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, e, r = _run_module(embed=embed, rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INTERNAL_ERROR")
        self.assertEqual(len(e.calls), 1, "异常后绝不重试")
        self.assertEqual(rpc.calls, [])
        self.assertNotIn(_PROVIDER_SECRET_MARKER, json.dumps(result))
        self.assertNotIn(_PROVIDER_SECRET_MARKER, log_line)


# ==========================================
# C. RPC 调用与返回校验
# ==========================================

class TestRpcCall(unittest.TestCase):

    def test_c_rpc_name_and_service_role_source(self):
        self.assertEqual(mvr.RPC_NAME, "match_memory_items")
        handler_src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_vector_recall)
        self.assertIn("supabase_service.rpc(_mvr.RPC_NAME", handler_src,
                      "handler 经 service_role 客户端构造 RPC callable")
        self.assertNotIn("match_memories'", handler_src)
        self.assertNotIn("match_active_memories", handler_src)
        module_src = inspect.getsource(mvr)
        self.assertNotIn("match_memories", module_src)
        self.assertNotIn("match_active_memories", module_src)

    def test_c_params_shape(self):
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, e, r = _run_module(rpc=rpc)
        self.assertEqual(len(rpc.calls), 1, "每请求 RPC 恰一次")
        params = rpc.calls[0]
        self.assertEqual(set(params.keys()),
                         {"query_embedding", "p_user_id", "match_count"})
        self.assertEqual(params["match_count"], 10, "match_count 服务端固定 10")
        self.assertEqual(params["p_user_id"], _USER_MARKER,
                         "p_user_id 为服务端解析值")
        self.assertEqual(len(params["query_embedding"]), 1024)

    def test_c_match_count_immutable_to_top_k(self):
        for top_k in (1, 5, 10):
            with self.subTest(top_k=top_k):
                rpc = _RecordingRpc(rows=_ok_rpc_rows())
                result, log_line, e, r = _run_module(rpc=rpc, top_k=top_k)
                self.assertEqual(rpc.calls[0]["match_count"], 10,
                                 "top_k 绝不影响 RPC 参数")

    def test_c_ready(self):
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, e, r = _run_module(rpc=rpc)
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "VECTOR_RECALL_PREVIEW_READY")
        self.assertEqual(result["stats"], {
            "query_embedded": True, "dimension": 1024,
            "rpc_returned": 1, "returned": 1,
            "status_filtered": 0, "expired_filtered": 0,
            "invalid_time_filtered": 0, "duplicate_filtered": 0})
        self.assertEqual(result["retrieval"], {
            "method": "pgvector_cosine_vector_recall_v1",
            "active_only": True, "expired_excluded": True,
            "user_scoped": True, "threshold_applied": False,
            "writes_executed": False})
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["recall_index"], 1)
        self.assertEqual(item["rank"], 1)
        self.assertEqual(item["memory_type"], "long_term")
        self.assertEqual(item["content"], _CONTENT_MARKER)
        self.assertEqual(item["importance"], 3)
        self.assertEqual(item["confidence"], 0.7)
        self.assertEqual(item["subject_key"], _SUBJECT_KEY_MARKER)
        self.assertEqual(item["valid_at"], _VALID_TS)
        self.assertIsNone(item["expires_at"])
        self.assertEqual(item["source"], "web")
        self.assertEqual(item["similarity"], 0.82)

    def test_c_empty_results(self):
        rpc = _RecordingRpc(rows=[])
        result, log_line, e, r = _run_module(rpc=rpc)
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "NO_VECTOR_RECALL_RESULTS")
        self.assertEqual(result["stats"]["rpc_returned"], 0)
        self.assertEqual(result["stats"]["returned"], 0)
        self.assertEqual(result["items"], [])

    def test_c_non_list_data(self):
        for data in (None, {"rows": []}, "rows", 123):
            with self.subTest(data=data):
                rpc = _RecordingRpc(data=data)
                result, log_line, e, r = _run_module(rpc=rpc)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "VECTOR_RPC_RESPONSE_INVALID")

    def test_c_row_count_over_match_count(self):
        rpc = _RecordingRpc(rows=[_rpc_row(f"{_ITEM_ID_MARKER}-{i}", 0.5)
                                  for i in range(mvr.MATCH_COUNT + 1)])
        result, log_line, e, r = _run_module(rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VECTOR_RPC_RESPONSE_INVALID")

    def test_c_row_missing_id(self):
        rpc = _RecordingRpc(rows=[{"similarity": 0.82,
                                   "content": _CONTENT_MARKER}])
        result, log_line, e, r = _run_module(rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VECTOR_RPC_RESPONSE_INVALID")

    def test_c_row_missing_or_empty_content(self):
        for content in (None, "", "   ", 123):
            with self.subTest(content=content):
                rpc = _RecordingRpc(rows=[{"memory_item_id": _ITEM_ID_MARKER,
                                           "content": content,
                                           "similarity": 0.82}])
                result, log_line, e, r = _run_module(rpc=rpc)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "VECTOR_RPC_RESPONSE_INVALID")

    def test_c_row_not_dict(self):
        rpc = _RecordingRpc(rows=["not-a-dict"])
        result, log_line, e, r = _run_module(rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VECTOR_RPC_RESPONSE_INVALID")

    def test_c_bad_similarity(self):
        cases = {
            "non_numeric": {"similarity": "high"},
            "missing": None,
            "nan": {"similarity": float("nan")},
            "inf": {"similarity": float("inf")},
            "out_of_range": {"similarity": 1.5},
        }
        for name, override in cases.items():
            with self.subTest(case=name):
                row = {"memory_item_id": _ITEM_ID_MARKER,
                       "content": _CONTENT_MARKER}
                if override is not None:
                    row.update(override)
                rpc = _RecordingRpc(rows=[row])
                result, log_line, e, r = _run_module(rpc=rpc)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "VECTOR_RPC_RESPONSE_INVALID")

    def test_c_similarity_boundary_tolerated(self):
        for sim in (-1.0, 1.0):
            with self.subTest(sim=sim):
                rpc = _RecordingRpc(rows=[_rpc_row(similarity=sim)])
                result, log_line, e, r = _run_module(rpc=rpc)
                self.assertTrue(result["ok"])
                self.assertEqual(result["items"][0]["similarity"], sim)

    def test_c_internal_id_never_in_http(self):
        rpc = _RecordingRpc(rows=[_rpc_row(_OTHER_ID_MARKER, 0.9),
                                  _rpc_row(_ITEM_ID_MARKER, 0.8)])
        result, log_line, e, r = _run_module(rpc=rpc)
        self.assertTrue(result["ok"])
        text = json.dumps(result, ensure_ascii=False) + log_line
        self.assertNotIn(str(_ITEM_ID_MARKER), text, "内部 ID 不得出 HTTP/日志")
        self.assertNotIn(_OTHER_ID_MARKER, text, "内部 ID 不得出 HTTP/日志")

    def test_c_rpc_exception_no_retry(self):
        rpc = _RecordingRpc(exc=RuntimeError(f"{_RPC_SECRET_MARKER} rpc boom"))
        result, log_line, e, r = _run_module(rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VECTOR_RPC_FAILED")
        self.assertEqual(len(rpc.calls), 1, "RPC 异常后不重试")
        self.assertEqual(result["stats"]["query_embedded"], True)
        self.assertEqual(result["stats"]["dimension"], 1024)
        self.assertNotIn(_RPC_SECRET_MARKER, json.dumps(result))
        self.assertNotIn(_RPC_SECRET_MARKER, log_line)


# ==========================================
# D. 状态与过期二次过滤
# ==========================================

class TestStatusAndExpiry(unittest.TestCase):

    def _run_rows(self, rows):
        rpc = _RecordingRpc(rows=rows)
        result, log_line, e, r = _run_module(rpc=rpc)
        return result, log_line

    def test_d_active_kept_with_explicit_status(self):
        result, log_line = self._run_rows([_rpc_row(status="active")])
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "VECTOR_RECALL_PREVIEW_READY")
        self.assertEqual(result["stats"]["returned"], 1)
        self.assertEqual(result["stats"]["status_filtered"], 0)

    def test_d_no_status_key_kept(self):
        result, log_line = self._run_rows([_rpc_row(status=None)])
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "VECTOR_RECALL_PREVIEW_READY")
        self.assertEqual(result["stats"]["returned"], 1)

    def test_d_non_active_statuses_dropped(self):
        for status in ("pending_review", "rejected", "superseded",
                       "archived", "expired"):
            with self.subTest(status=status):
                result, log_line = self._run_rows([_rpc_row(status=status)])
                self.assertTrue(result["ok"])
                self.assertEqual(result["code"], "NO_VECTOR_RECALL_RESULTS")
                self.assertEqual(result["stats"]["rpc_returned"], 1)
                self.assertEqual(result["stats"]["returned"], 0)
                self.assertEqual(result["stats"]["status_filtered"], 1)
                self.assertEqual(result["items"], [])

    def test_d_expired_dropped(self):
        result, log_line = self._run_rows(
            [_rpc_row(expires_at=_PAST_TS, extra_banned=False)])
        self.assertEqual(result["code"], "NO_VECTOR_RECALL_RESULTS")
        self.assertEqual(result["stats"]["expired_filtered"], 1)
        self.assertEqual(result["stats"]["returned"], 0)

    def test_d_unexpired_kept(self):
        result, log_line = self._run_rows(
            [_rpc_row(expires_at=_FUTURE_TS, extra_banned=False)])
        self.assertEqual(result["code"], "VECTOR_RECALL_PREVIEW_READY")
        self.assertEqual(result["stats"]["returned"], 1)
        self.assertEqual(result["items"][0]["expires_at"], _FUTURE_TS)

    def test_d_unparseable_time_dropped(self):
        result, log_line = self._run_rows(
            [_rpc_row(expires_at="not-a-timestamp", extra_banned=False)])
        self.assertEqual(result["code"], "NO_VECTOR_RECALL_RESULTS")
        self.assertEqual(result["stats"]["invalid_time_filtered"], 1)
        self.assertEqual(result["stats"]["returned"], 0)

    def test_d_mixed_rows_counted(self):
        rows = [_rpc_row(_ITEM_ID_MARKER, 0.9, status="active"),
                _rpc_row(_OTHER_ID_MARKER, 0.8, status="pending_review"),
                _rpc_row(f"{_OTHER_ID_MARKER}-2", 0.7, status=None,
                         expires_at=_PAST_TS, extra_banned=False),
                _rpc_row(_ITEM_ID_MARKER, 0.6, status="active")]
        result, log_line = self._run_rows(rows)
        self.assertEqual(result["code"], "VECTOR_RECALL_PREVIEW_READY")
        self.assertEqual(result["stats"]["rpc_returned"], 4)
        self.assertEqual(result["stats"]["returned"], 1)
        self.assertEqual(result["stats"]["status_filtered"], 1)
        self.assertEqual(result["stats"]["expired_filtered"], 1)
        self.assertEqual(result["stats"]["duplicate_filtered"], 1)
        self.assertEqual(result["items"][0]["similarity"], 0.9)

    def test_d_zero_update_ever(self):
        db = _RecallFakeService(rpc_rows=_ok_rpc_rows())
        send, logs, d, e = _call_handler(db=db)
        self.assertEqual(d.table_calls, [], "绝不直接访问任何表")
        self.assertEqual(d.forbidden, [], "无 insert/update/delete/upsert")
        self.assertFalse(send.body_json["retrieval"]["writes_executed"])


# ==========================================
# E. 用户隔离
# ==========================================

class TestUserIsolation(unittest.TestCase):

    def test_e_client_user_id_rejected(self):
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(
            body=_invalid_body("user_id"), embed=embed)
        self.assertEqual(send.status, 400)
        self.assertEqual(e.calls, [])
        self.assertEqual(db.rpc_calls, [])

    def test_e_server_user_id_passed_to_rpc(self):
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, e, r = _run_module(rpc=rpc, user_id=_USER_MARKER)
        self.assertEqual(rpc.calls[0]["p_user_id"], _USER_MARKER)

    def test_e_no_cross_user_in_params(self):
        # 即便客户端试图注入另一 user 值，参数中的 user_id 也只可能是服务端值
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, e, r = _run_module(rpc=rpc)
        self.assertEqual(rpc.calls[0]["p_user_id"], _USER_MARKER)
        self.assertNotIn("CLIENT_INJECTED_MARKER", json.dumps(rpc.calls))

    def test_e_response_never_contains_user_id(self):
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, e, r = _run_module(rpc=rpc)
        text = json.dumps(result, ensure_ascii=False) + log_line
        self.assertNotIn(_USER_MARKER, text, "响应/日志不得包含 user_id")


# ==========================================
# F. 响应形状与语义
# ==========================================

_ITEM_KEYS = {"recall_index", "rank", "memory_type", "content", "importance",
              "confidence", "subject_key", "valid_at", "expires_at",
              "source", "similarity"}
_BANNED_KEYS = {"memory_item_id", "user_id", "embedding", "embedding_model",
                "content_hash", "source_event_ids", "source_batch_id",
                "metadata", "created_by", "status", "superseded_by",
                "prompt", "provider", "sql", "traceback"}


def _all_keys(obj):
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            keys |= _all_keys(v)
    return keys


class TestResponseShape(unittest.TestCase):

    def _run_rows(self, rows, top_k=mvr.DEFAULT_TOP_K):
        rpc = _RecordingRpc(rows=rows)
        result, log_line, e, r = _run_module(rpc=rpc, top_k=top_k)
        return result, log_line

    def test_f_threshold_applied_false_everywhere(self):
        scenarios = [
            self._run_rows(_ok_rpc_rows()),
            self._run_rows([]),
            self._run_rows([_rpc_row(status="pending_review")]),
        ]
        bad_vec = _RecordingEmbed(result=_vec(768))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line = asyncio.run(
            mvr.run_recall(_QUERY_MARKER, _USER_MARKER, bad_vec, rpc))
        scenarios.append((result, log_line))
        for result, _ in scenarios:
            self.assertFalse(result["retrieval"]["threshold_applied"],
                             "任何路径都不得声称已应用阈值")

    def test_f_method_and_static_flags(self):
        for result, _ in (self._run_rows(_ok_rpc_rows()),
                          self._run_rows([])):
            self.assertEqual(result["retrieval"]["method"],
                             "pgvector_cosine_vector_recall_v1")
            self.assertTrue(result["retrieval"]["active_only"])
            self.assertTrue(result["retrieval"]["expired_excluded"])
            self.assertTrue(result["retrieval"]["user_scoped"])
            self.assertFalse(result["retrieval"]["writes_executed"])

    def test_f_item_whitelist_keys(self):
        result, _ = self._run_rows(_ok_rpc_rows())
        self.assertEqual(set(result["items"][0].keys()), _ITEM_KEYS)

    def test_f_banned_keys_absent_everywhere(self):
        for result, _ in (self._run_rows(_ok_rpc_rows()),
                          self._run_rows([])):
            keys = _all_keys(result)
            self.assertEqual(keys & _BANNED_KEYS, set(),
                             f"响应出现禁用键: {keys & _BANNED_KEYS}")

    def test_f_similarity_is_cosine_only(self):
        # similarity 键存在且为数值；响应中不存在 score/probability 等
        # 语义混淆字段；similarity 仅表示 pgvector cosine 相似度
        result, _ = self._run_rows([_rpc_row(similarity=0.8234567891)])
        item = result["items"][0]
        self.assertIsInstance(item["similarity"], float)
        self.assertEqual(item["similarity"], 0.823457)
        for banned in ("score", "probability", "recall_score",
                       "match_score", "relevance"):
            self.assertNotIn(banned, item)

    def test_f_fields_default_none_when_missing(self):
        sparse = {"memory_item_id": _ITEM_ID_MARKER,
                  "content": _CONTENT_MARKER, "similarity": 0.5}
        result, _ = self._run_rows([sparse])
        item = result["items"][0]
        self.assertEqual(item["content"], _CONTENT_MARKER)
        for key in ("memory_type", "importance", "confidence", "subject_key",
                    "valid_at", "expires_at", "source"):
            self.assertIsNone(item[key], f"缺失字段 {key} 应为 None")

    def test_f_bool_and_garbage_fields_rejected_to_none(self):
        weird = _rpc_row(extra_banned=False, importance=True,
                         confidence="high", memory_type=["x"],
                         subject_key={"k": 1}, valid_at=123, source=4.5)
        result, _ = self._run_rows([weird])
        item = result["items"][0]
        self.assertIsNone(item["importance"])
        self.assertIsNone(item["confidence"])
        self.assertIsNone(item["memory_type"])
        self.assertIsNone(item["subject_key"])
        self.assertIsNone(item["valid_at"])
        self.assertIsNone(item["source"])

    def test_f_rank_sorted_by_similarity_desc(self):
        rows = [_rpc_row("id-low", 0.5, extra_banned=False),
                _rpc_row("id-high", 0.9, extra_banned=False),
                _rpc_row("id-mid", 0.7, extra_banned=False)]
        result, _ = self._run_rows(rows, top_k=3)
        sims = [it["similarity"] for it in result["items"]]
        self.assertEqual(sims, [0.9, 0.7, 0.5])
        self.assertEqual([it["rank"] for it in result["items"]], [1, 2, 3])
        self.assertEqual([it["recall_index"] for it in result["items"]],
                         [1, 2, 3])

    def test_f_top_k_truncates_items_only(self):
        rows = [_rpc_row(f"id-{i}", 0.5 + 0.01 * i, extra_banned=False)
                for i in range(5)]
        result, _ = self._run_rows(rows, top_k=2)
        self.assertEqual(result["stats"]["rpc_returned"], 5)
        self.assertEqual(result["stats"]["returned"], 2)
        self.assertEqual(len(result["items"]), 2)

    def test_f_top_k_larger_than_results(self):
        result, _ = self._run_rows(_ok_rpc_rows(), top_k=10)
        self.assertEqual(result["stats"]["rpc_returned"], 1)
        self.assertEqual(result["stats"]["returned"], 1)

    def test_f_http_status_mapping(self):
        expected = {
            "VECTOR_RECALL_PREVIEW_READY": 200,
            "NO_VECTOR_RECALL_RESULTS": 200,
            "INVALID_VECTOR_RECALL_REQUEST": 400,
            "INVALID_CONFIRMATION": 400,
            "EMBEDDING_UNAVAILABLE": 503,
            "EMBEDDING_RESPONSE_INVALID": 503,
            "EMBEDDING_NON_FINITE_VALUES": 503,
            "EMBEDDING_DIMENSION_MISMATCH": 503,
            "EMBEDDING_ZERO_VECTOR": 503,
            "VECTOR_RPC_FAILED": 500,
            "VECTOR_RPC_RESPONSE_INVALID": 500,
            "INTERNAL_ERROR": 500,
        }
        for code, status in expected.items():
            self.assertEqual(mvr.HTTP_STATUS_BY_CODE.get(code), status,
                             f"{code} 的 HTTP 状态映射错误")

    def test_f_handler_status_end_to_end(self):
        cases = [
            (_RecallFakeService(rpc_rows=[]), _RecordingEmbed(result=_vec(1024)),
             200, "NO_VECTOR_RECALL_RESULTS"),
            (_RecallFakeService(rpc_rows=_ok_rpc_rows()),
             _RecordingEmbed(result=[]), 503, "EMBEDDING_UNAVAILABLE"),
            (_RecallFakeService(rpc_rows=_ok_rpc_rows(),
                                rpc_exc=RuntimeError("rpc down")),
             _RecordingEmbed(result=_vec(1024)), 500, "VECTOR_RPC_FAILED"),
            (_RecallFakeService(rpc_rows=[{"memory_item_id": "x"}]),
             _RecordingEmbed(result=_vec(1024)), 500,
             "VECTOR_RPC_RESPONSE_INVALID"),
        ]
        for db, embed, status, code in cases:
            with self.subTest(code=code):
                send, logs, d, e = _call_handler(db=db, embed=embed)
                self.assertEqual(send.status, status)
                self.assertEqual(send.body_json.get("code"), code)


# ==========================================
# G. 零写入与隔离
# ==========================================

class TestIsolation(unittest.TestCase):

    def test_g_module_source_has_no_forbidden_calls(self):
        src = inspect.getsource(mvr)
        # 注意：RPC 经注入 callable 调用，模块源码不得出现客户端 .rpc( 链；
        # pinecone/LLM 按导入与客户端类名匹配；不得引用词面召回模块
        for banned in ("import pinecone", "from pinecone",
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
                       "schedule", "cron", "print(",
                       "match_memories", "match_active_memories",
                       "memory_recall"):
            self.assertNotIn(banned, src,
                             f"memory_vector_recall 源码不得包含 {banned!r}")

    def test_g_module_never_reads_env(self):
        src = inspect.getsource(mvr)
        self.assertNotIn("DOUBAO", src,
                         "模块不读环境变量；模型/provider 事项不进入本模块")

    def test_g_handler_source_has_no_forbidden_calls(self):
        src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_vector_recall)
        # 本阶段唯一例外：service_role 只读 RPC（.rpc( 允许）；
        # 词面召回 / Pinecone / LLM / 调度 / 环境变量读取一律禁止
        for banned in ("PineconeMemoryClient", "from pinecone",
                       "import pinecone", "ask_role", "_ask_llm",
                       "create_task", "ensure_future", "Timer(",
                       "threading", "getenv", "os.environ",
                       "tool_loop", "compose_member_view",
                       "stable_system", "volatile_block",
                       "schedule", "cron",
                       "match_active_memories", "match_memories'",
                       "memory_recall", ".insert(", ".update(",
                       ".delete(", ".upsert("):
            self.assertNotIn(banned, src,
                             f"recall handler 源码不得包含 {banned!r}")

    def test_g_handler_reuses_server_dependencies(self):
        src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_vector_recall)
        self.assertIn("_srv_st._get_embedding", src,
                      "复用现有 _get_embedding，不新建客户端")
        self.assertIn("_srv_st.supabase_service", src,
                      "复用 service_role 客户端")
        self.assertIn("_resolve_pinecone_user_id", src,
                      "user_id 由服务端统一解析")
        self.assertIn("_mvr.RPC_NAME", src, "只读 RPC 名来自模块常量")
        self.assertIn("_mvr.QUERY_MAX_LENGTH", src,
                      "query 上限来自模块常量，避免漂移")
        self.assertIn("_mvr.DEFAULT_TOP_K", src, "top_k 缺省来自模块常量")
        self.assertIn("VECTOR_RECALL_PREVIEW_ONLY", src)

    def test_g_behavior_zero_writes(self):
        db = _RecallFakeService(rpc_rows=_ok_rpc_rows())
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, d, e = _call_handler(db=db, embed=embed)
        self.assertEqual(send.body_json.get("code"),
                         "VECTOR_RECALL_PREVIEW_READY")
        self.assertEqual(d.forbidden, [], "无 insert/update/delete/upsert")
        self.assertEqual(d.table_calls, [], "无任何直接表访问")
        self.assertEqual(len(d.rpc_calls), 1, "RPC 恰一次（只读）")
        self.assertFalse(send.body_json["retrieval"]["writes_executed"])
        self.assertFalse(send.body_json["retrieval"]["threshold_applied"])

    def test_g_module_prints_nothing(self):
        buf = io.StringIO()
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        with redirect_stdout(buf):
            asyncio.run(mvr.run_recall(_QUERY_MARKER, _USER_MARKER,
                                       embed, rpc))
            asyncio.run(mvr.run_recall(
                _QUERY_MARKER, _USER_MARKER, _RecordingEmbed(result=[]),
                _RecordingRpc(rows=_ok_rpc_rows())))
            asyncio.run(mvr.run_recall(
                _QUERY_MARKER, _USER_MARKER, _RecordingEmbed(result=_vec(1024)),
                _RecordingRpc(rows=[])))
        self.assertEqual(buf.getvalue(), "", "模块不得直接打印任何内容")


# ==========================================
# G+. 全路径脱敏扫描
# ==========================================

class TestNoLeakage(unittest.TestCase):

    _MARKERS = (_QUERY_MARKER, _USER_MARKER, str(_ITEM_ID_MARKER),
                _OTHER_ID_MARKER, _MODEL_MARKER, _HASH_MARKER,
                _EVENT_IDS_MARKER, _BATCH_ID_MARKER, _METADATA_MARKER,
                _PROVIDER_SECRET_MARKER, _RPC_SECRET_MARKER,
                "SHOULD-NOT-LEAK", "DOUBAO", "DOUBAO_EMBEDDING_EP",
                "siliconflow", "api.siliconflow", "Bearer", "api_key",
                "Authorization", "rest/v1", "memory_items",
                "content_hash", "traceback", "superseded_by",
                "source_event_ids", "source_batch_id", "embedding_model")

    def _leak_scan(self, send, logs):
        text = _resp_text(send) + "".join(logs)
        for marker in self._MARKERS:
            self.assertNotIn(marker, text, f"泄漏标记 {marker!r} 出现在响应/日志")
        self.assertNotIn("confirm", text.lower(), "请求体内容不得回显")
        return text

    def test_g_success_path_no_leakage(self):
        # 默认 RPC 行携带全部敏感额外字段（user_id/embedding/模型名/hash/
        # 事件ID/批ID/metadata/created_by），响应与日志都不得回显
        send, logs, db, e = _call_handler()
        self._leak_scan(send, logs)

    def test_g_failure_paths_no_leakage(self):
        scenarios = [
            ("no_results", _RecallFakeService(rpc_rows=[]),
             _RecordingEmbed(result=_vec(1024))),
            ("embedding_empty", _RecallFakeService(rpc_rows=_ok_rpc_rows()),
             _RecordingEmbed(result=[])),
            ("provider_error", _RecallFakeService(rpc_rows=_ok_rpc_rows()),
             _RecordingEmbed(exc=RuntimeError(f"{_PROVIDER_SECRET_MARKER} p"))),
            ("rpc_error", _RecallFakeService(
                rpc_rows=_ok_rpc_rows(),
                rpc_exc=RuntimeError(f"{_RPC_SECRET_MARKER} rpc")),
             _RecordingEmbed(result=_vec(1024))),
            ("rpc_invalid", _RecallFakeService(
                rpc_rows=[{"memory_item_id": "x"}]),
             _RecordingEmbed(result=_vec(1024))),
            ("filtered", _RecallFakeService(
                rpc_rows=[_rpc_row(status="pending_review")]),
             _RecordingEmbed(result=_vec(1024))),
        ]
        for name, db, embed in scenarios:
            with self.subTest(scenario=name):
                send, logs, d, e = _call_handler(db=db, embed=embed)
                self._leak_scan(send, logs)


if __name__ == "__main__":
    unittest.main()
