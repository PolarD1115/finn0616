# -*- coding: utf-8 -*-
"""第 33 阶段专项测试 —— memory_items 向量 RPC 自匹配只读预览。

  POST /api/memory-vector-selftest-preview
    → 服务端只读选定最旧 active 且 embedding 非空的一条（created_at 升序，limit 1）；
    → 内存二次过滤（status/user/content/model/embedded_at/expires_at）；
    → 核对当前配置模型与库内 embedding_model 一致（trim 后），不一致零 provider/RPC；
    → 用其 content 恰调用一次 server._get_embedding，校验 1024/finite/非零；
    → 经 service_role 只读调用 match_memory_items（match_count 固定 5）恰一次；
    → 校验 RPC 返回行与 Top1 内部 ID（内存中比较，绝不返回该 ID）；
    → similarity ≥ 0.99 才 VECTOR_SELF_MATCH_READY；
    → 响应/日志不含 ID/正文/user_id/模型名/向量/RPC 原始行/hash/异常原文；
    → 零写入、无 Pinecone、无 LLM、无自动调度、不接正式上下文。

全部 unittest + mock + 合成数据：不真实调用 provider、不真实执行 RPC、
不真实调用新接口、不连接真实 Supabase / Pinecone / LLM、不修改任何数据。

覆盖（任务书 A-I）：
  A 路由与鉴权（/api/* API_SECRET、仅 POST、OPTIONS、非法方法零 DB/provider/RPC）
  B 请求校验（confirm、全部注入字段拒绝、非法请求零副作用）
  C 候选选择（强制条件/升序/limit 1、内存二次过滤、过期、时间不可解析、无候选）
  D 模型一致性（未配置、库内空、trim 一致、不一致零 provider/RPC、不返回模型名）
  E provider（1024 成功、空/非list/非数值/NaN/Inf/维度错误/零向量/异常、恰一次）
  F RPC（参数形状、service_role、恰一次、旧 RPC 未用、空结果/非list/行数超限/
       缺 ID/similarity 非法/Top1 mismatch/正确/低于 0.99/RPC 异常）
  G HTTP 脱敏（成功+失败路径全量泄漏扫描，含 RPC 额外字段不回显）
  H 零写入隔离（源码静态 + 行为断言）
  （I 全量记忆回归由命令行单独运行）

运行： python -m unittest test_memory_vector_selftest_phase33 -v
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
import memory_vector_selftest as mvs
import server as _srv
from test_memory_preview_phase10 import FakeReceive, FakeSend


# ==========================================
# 常量与脱敏标记
# ==========================================

_PATH = "/api/memory-vector-selftest-preview"
_SECRET = "test-secret-marker-phase33"
# 注入脱敏扫描的独特标记（断言响应/日志不外泄）
_MODEL_MARKER = "MODEL-NAME-SECRET-123"
_MODEL_MARKER_2 = "OTHER-MODEL-SECOND-456"
_USER_MARKER = "user-scope-333"
_CONTENT_MARKER = "记忆内容隐私标记：小满周四要去看牙医"
_ITEM_ID_MARKER = "a1b2c3d4-selftest-uuid-0001"
_TOP1_ID_MARKER = "a1b2c3d4-selftest-uuid-0001"
_OTHER_ID_MARKER = "ffff1111-other-uuid-9999"
_VEC_MARKER = 0.777123
_SUBJECT_KEY_MARKER = "SUBJECT-KEY-SECRET-MARKER"
_PROVIDER_SECRET_MARKER = "PROVIDER_RAW_ERROR_SECRET_MARKER"
_RPC_SECRET_MARKER = "RPC_RAW_ERROR_SECRET_MARKER"
_DB_SECRET_MARKER = "DB_RAW_ERROR_SECRET_MARKER"

_FUTURE_TS = "2099-01-01T00:00:00+00:00"
_PAST_TS = "2000-01-01T00:00:00+00:00"
_EMBEDDED_AT = "2026-08-30T12:00:00+00:00"


def _vec(dim, marker=None):
    """合成向量：全 finite、非零；marker 可供泄漏扫描。"""
    if marker is None:
        return [0.001 * (i % 97) for i in range(dim)]
    return [marker + 0.001 * i for i in range(dim)]


def _row(content=_CONTENT_MARKER, user=_USER_MARKER, status="active",
         rid=_ITEM_ID_MARKER, expires_at=None, model=_MODEL_MARKER,
         embedded_at=_EMBEDDED_AT):
    """memory_items 候选行的合成最小形状（仅含模块 SELECT 的 7 列）。"""
    return {"id": rid, "content": content, "user_id": user, "status": status,
            "expires_at": expires_at, "embedding_model": model,
            "embedded_at": embedded_at}


def _rpc_row(rid=_TOP1_ID_MARKER, similarity=0.9999, extra=False):
    """RPC 返回行的合成形状；extra=True 时附带真实 RPC 会返回的其余字段，
    用于验证模块不信任、不回显额外字段。"""
    row = {"memory_item_id": rid, "similarity": similarity}
    if extra:
        row["content"] = _CONTENT_MARKER
        row["subject_key"] = _SUBJECT_KEY_MARKER
        row["source"] = "SHOULD-NOT-LEAK"
    return row


# ==========================================
# 假 service_role 客户端（记录全部调用路径；绝不触网）
# ==========================================

class _FakeResult:
    """模拟 supabase-py execute() 返回（带 .data 属性）。"""

    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """链式记录器：记录每个方法；被禁操作只记录、统一断言。"""

    def __init__(self, owner, table):
        self._owner = owner
        self._table = table
        self._path = []

    def _rec(self, method, *args, **kwargs):
        self._path.append((method, args, kwargs))
        return self

    @property
    def not_(self):
        return self

    def select(self, *a, **k): return self._rec("select", *a, **k)
    def eq(self, *a, **k): return self._rec("eq", *a, **k)
    def neq(self, *a, **k): return self._rec("neq", *a, **k)
    def is_(self, *a, **k): return self._rec("is_", *a, **k)
    def order(self, *a, **k): return self._rec("order", *a, **k)
    def limit(self, *a, **k): return self._rec("limit", *a, **k)

    def insert(self, *a, **k): return self._rec("FORBIDDEN:insert", *a, **k)
    def update(self, *a, **k): return self._rec("FORBIDDEN:update", *a, **k)
    def delete(self, *a, **k): return self._rec("FORBIDDEN:delete", *a, **k)
    def upsert(self, *a, **k): return self._rec("FORBIDDEN:upsert", *a, **k)

    def execute(self, *a, **k):
        self._path.append(("execute", (), {}))
        self._owner.calls.append((self._table, list(self._path)))
        return self._owner._respond_table(self._table, list(self._path))


class _FakeRpcBuilder:
    """模拟 supabase-py 的 rpc(name, params) 构建器（仅 execute）。"""

    def __init__(self, owner):
        self._owner = owner

    def execute(self, *a, **k):
        self._owner.rpc_execute_count += 1
        if self._owner.rpc_exc is not None:
            raise self._owner.rpc_exc
        return _FakeResult(list(self._owner.rpc_rows))


class _SelftestFakeService:
    """按 table-select / rpc 分流的假 service_role 客户端。"""

    def __init__(self, select_rows=(), select_exc=None,
                 rpc_rows=(), rpc_exc=None):
        self.calls = []              # [(table, path)] 每次 SELECT 执行
        self.table_calls = []        # 每次 table() 访问
        self.forbidden = []          # 捕获到的被禁表操作
        self.select_rows = list(select_rows)
        self.select_exc = select_exc
        self.rpc_rows = list(rpc_rows)
        self.rpc_exc = rpc_exc
        self.rpc_calls = []          # [(name, params)] 每次 rpc() 调用
        self.rpc_execute_count = 0

    def table(self, name):
        self.table_calls.append(name)
        return _FakeQuery(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return _FakeRpcBuilder(self)

    def _respond_table(self, table, path):
        kinds = [p[0] for p in path]
        for kind in ("insert", "delete", "upsert", "update"):
            if f"FORBIDDEN:{kind}" in kinds:
                self.forbidden.append(kind)
        if kinds and kinds[0] == "select":
            if self.select_exc is not None:
                raise self.select_exc
            return _FakeResult(list(self.select_rows))
        return _FakeResult([])


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


_UNSET = object()


class _RecordingRpc:
    """注入模块的 RPC callable：记录 params，返回预定行 / 抛预定异常。
    data 为哨兵 _UNSET 时返回 rows；否则 .data 恰为传入值（可设 None 等）。"""

    def __init__(self, rows=(), exc=None, data=_UNSET):
        self.calls = []
        self.rows = list(rows)
        self.exc = exc
        self._data = data

    def __call__(self, params):
        self.calls.append(params)
        if self.exc is not None:
            raise self.exc
        return _FakeResult(self.rows if self._data is _UNSET else self._data)


# ==========================================
# 调用辅助
# ==========================================

def _ok_rpc_rows():
    return [_rpc_row(_TOP1_ID_MARKER, 0.9999, extra=True)]


def _call_handler(body=None, raw=None, method="POST", embed=None, db=None,
                  model=_MODEL_MARKER, user_id=_USER_MARKER):
    """直调 handler；返回 (send, logs, db, embed)。"""
    send = FakeSend()
    logs = []
    if raw is None:
        raw = (json.dumps({"confirm": mvs.CONFIRM_TOKEN}).encode("utf-8")
               if body is None else json.dumps(body).encode("utf-8"))
    scope = {"method": method, "path": _PATH}
    if db is None:
        db = _SelftestFakeService(select_rows=[_row()], rpc_rows=_ok_rpc_rows())
    if embed is None:
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
    env = {"API_SECRET": _SECRET, "DOUBAO_EMBEDDING_EP": model or ""}
    with patch.dict(os.environ, env), \
         patch.object(_srv, "_get_embedding", embed), \
         patch.object(_srv, "supabase_service", db), \
         patch.object(_srv, "_resolve_pinecone_user_id", lambda: user_id), \
         patch.object(gateway, "_log", lambda m: logs.append(m)):
        asyncio.run(gateway.HostFixMiddleware._handle_memory_vector_selftest(
            None, scope, FakeReceive(raw), send))
    return send, logs, db, embed


def _run_module(db=None, embed=None, rpc=None, model=_MODEL_MARKER,
                user_id=_USER_MARKER, select_rows=None):
    """直调模块 run_selftest；返回 (result, log_line, db, embed, rpc)。"""
    if db is None:
        db = _SelftestFakeService(
            select_rows=[_row()] if select_rows is None else select_rows)
    if embed is None:
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
    if rpc is None:
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
    result, log_line = asyncio.run(mvs.run_selftest(
        db, user_id, embed, model, rpc))
    return result, log_line, db, embed, rpc


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


def _ops(db, index=0):
    """第 index 次 SELECT 执行的方法序列 [(method, args, kwargs)]。"""
    return db.calls[index][1]


def _op(db, name, index=0):
    """第 index 次出现的名为 name 的方法调用（在同一次执行的路径内）。"""
    hits = [m for m in _ops(db, 0) if m[0] == name]
    return hits[index] if index < len(hits) else None


def _invalid_body(field):
    return {"confirm": mvs.CONFIRM_TOKEN, field: "CLIENT_INJECTED_MARKER"}


# ==========================================
# A. 路由与鉴权
# ==========================================

class TestRouteAuth(unittest.TestCase):

    def test_a_requires_api_secret(self):
        with patch.dict(os.environ, {"API_SECRET": _SECRET}):
            send = _mw_call(_auth_scope(with_auth=False),
                            json.dumps({"confirm": mvs.CONFIRM_TOKEN}).encode())
        self.assertEqual(send.status, 401, "无鉴权头必须 401")
        self.assertEqual(send.body_json.get("error"),
                         "Unauthorized: Missing or invalid API key")

    def test_a_wrong_api_secret_rejected(self):
        headers = [(b"authorization", b"Bearer wrong-secret")]
        scope = {"type": "http", "path": _PATH, "method": "POST",
                 "headers": headers}
        with patch.dict(os.environ, {"API_SECRET": _SECRET}):
            send = _mw_call(scope,
                            json.dumps({"confirm": mvs.CONFIRM_TOKEN}).encode())
        self.assertEqual(send.status, 401)

    def test_a_empty_api_secret_rejected(self):
        with patch.dict(os.environ, {"API_SECRET": ""}):
            send = _mw_call(_auth_scope(with_auth=True),
                            json.dumps({"confirm": mvs.CONFIRM_TOKEN}).encode())
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
                db = _SelftestFakeService(select_rows=[_row()])
                send, logs, db, e = _call_handler(method=method, embed=embed,
                                                  db=db)
                self.assertEqual(send.status, 405)
                self.assertEqual(send.body_json.get("code"),
                                 "METHOD_NOT_ALLOWED")
                self.assertEqual(e.calls, [], "非 POST 绝不触碰 provider")
                self.assertEqual(db.table_calls, [], "非 POST 绝不触碰数据库")
                self.assertEqual(db.rpc_calls, [], "非 POST 绝不调用 RPC")
                self.assertEqual(send.body_json["execution"]["provider_calls"], 0)
                self.assertEqual(send.body_json["execution"]["database_reads"], 0)
                self.assertEqual(send.body_json["execution"]["database_writes"], 0)

    def test_a_valid_secret_reaches_handler(self):
        db = _SelftestFakeService(select_rows=[_row()], rpc_rows=_ok_rpc_rows())
        embed = _RecordingEmbed(result=_vec(1024))
        body = json.dumps({"confirm": mvs.CONFIRM_TOKEN}).encode("utf-8")
        env = {"API_SECRET": _SECRET, "DOUBAO_EMBEDDING_EP": _MODEL_MARKER}
        with patch.dict(os.environ, env), \
             patch.object(_srv, "_get_embedding", embed), \
             patch.object(_srv, "supabase_service", db), \
             patch.object(_srv, "_resolve_pinecone_user_id",
                          lambda: _USER_MARKER), \
             patch.object(gateway, "_log", lambda m: None):
            send = _mw_call(_auth_scope(), body)
        self.assertEqual(send.status, 200, "鉴权通过后进入自匹配 handler")
        self.assertEqual(send.body_json.get("code"), "VECTOR_SELF_MATCH_READY")
        self.assertEqual(embed.calls, [_CONTENT_MARKER], "输入恒为库内 content")

    def test_a_route_registered_in_dispatch(self):
        src = inspect.getsource(gateway.HostFixMiddleware.__call__)
        self.assertIn("/api/memory-vector-selftest-preview", src)

    def test_a_confirm_token_consistency(self):
        self.assertEqual(mvs.CONFIRM_TOKEN, "VECTOR_SELFTEST_PREVIEW_ONLY")
        handler_src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_vector_selftest)
        self.assertIn('"VECTOR_SELFTEST_PREVIEW_ONLY"', handler_src)


# ==========================================
# B. 请求校验
# ==========================================

class TestRequestValidation(unittest.TestCase):

    def test_b_confirm_missing(self):
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(body={}, embed=embed)
        self.assertEqual(send.status, 400)
        self.assertEqual(send.body_json.get("code"), "INVALID_CONFIRMATION")
        self.assertEqual(e.calls, [], "confirm 缺失不得调用 provider")
        self.assertEqual(db.table_calls, [], "confirm 缺失不得查询数据库")
        self.assertEqual(db.rpc_calls, [], "confirm 缺失不得调用 RPC")

    def test_b_confirm_wrong(self):
        for confirm in ("vector_selftest_preview_only", "VECTOR", "", 123,
                        None, True, ["VECTOR_SELFTEST_PREVIEW_ONLY"]):
            with self.subTest(confirm=confirm):
                send, logs, db, e = _call_handler(body={"confirm": confirm})
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_CONFIRMATION")
                self.assertEqual(e.calls, [])
                self.assertEqual(db.table_calls, [])
                self.assertEqual(db.rpc_calls, [])

    def test_b_injection_fields_all_rejected(self):
        # 客户端试图注入 query/正文/item_id/user_id/向量/模型/provider/top_k/
        # threshold/status/include_pending/write/update/backfill 等任何额外
        # 字段 → 400，绝不查库、绝不调 provider、绝不调 RPC
        for field in ("query", "content", "item_id", "user_id", "vector",
                      "embedding", "model", "provider", "top_k", "threshold",
                      "status", "include_pending", "write", "update",
                      "backfill", "id", "text"):
            with self.subTest(field=field):
                embed = _RecordingEmbed(result=_vec(1024))
                db = _SelftestFakeService(select_rows=[_row()],
                                          rpc_rows=_ok_rpc_rows())
                send, logs, db2, e = _call_handler(body=_invalid_body(field),
                                                   embed=embed, db=db)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_SELFTEST_REQUEST")
                self.assertEqual(e.calls, [])
                self.assertEqual(db.table_calls, [])
                self.assertEqual(db.rpc_calls, [])

    def test_b_invalid_json_and_non_dict(self):
        for raw in (b"{not json", b"[1,2,3]", b'"str"', b"null", b"123"):
            with self.subTest(raw=raw):
                embed = _RecordingEmbed(result=_vec(1024))
                send, logs, db, e = _call_handler(raw=raw, embed=embed)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_SELFTEST_REQUEST")
                self.assertEqual(e.calls, [])
                self.assertEqual(db.table_calls, [])
                self.assertEqual(db.rpc_calls, [])


# ==========================================
# C. 候选选择
# ==========================================

class TestCandidateSelection(unittest.TestCase):

    def test_c_select_path_conditions(self):
        result, log_line, db, embed, rpc = _run_module()
        self.assertEqual(db.table_calls, ["memory_items"])
        self.assertEqual(_op(db, "select")[1][0], mvs._SELECT_COLUMNS)
        self.assertEqual(_op(db, "eq", 0), ("eq", ("user_id", _USER_MARKER), {}))
        self.assertEqual(_op(db, "eq", 1),
                         ("eq", ("status", "active"), {}))
        self.assertEqual(_op(db, "is_"),
                         ("is_", ("embedding", None), {}))
        self.assertEqual(_op(db, "order"),
                         ("order", ("created_at",), {"desc": False}))
        self.assertEqual(_op(db, "limit"), ("limit", (1,), {}))
        self.assertEqual(db.forbidden, [], "SELECT 链上不得出现任何写方法")

    def test_c_no_rows_no_provider_no_rpc(self):
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(
            select_rows=[], embed=embed, rpc=rpc)
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "NO_ACTIVE_EMBEDDED_MEMORIES")
        self.assertEqual(result["stats"]["selected"], 0)
        self.assertEqual(embed.calls, [], "无候选绝不调用 provider")
        self.assertEqual(rpc.calls, [], "无候选绝不调用 RPC")
        self.assertEqual(result["execution"]["database_reads"], 1)

    def test_c_memory_filter_rejects(self):
        scenarios = {
            "wrong_user": _row(user="OTHER-USER"),
            "wrong_status": _row(status="archived"),
            "blank_content": _row(content="   "),
            "content_not_str": _row(content=12345),
            "blank_model": _row(model="   "),
            "model_none": _row(model=None),
            "embedded_at_missing": _row(embedded_at=None),
            "id_missing": _row(rid=None),
        }
        for name, row in scenarios.items():
            with self.subTest(scenario=name):
                embed = _RecordingEmbed(result=_vec(1024))
                rpc = _RecordingRpc(rows=_ok_rpc_rows())
                result, log_line, db, e, r = _run_module(
                    select_rows=[row], embed=embed, rpc=rpc)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "NO_ACTIVE_EMBEDDED_MEMORIES",
                                 f"{name} 应按无候选处理")
                self.assertEqual(embed.calls, [], f"{name} 绝不调用 provider")
                self.assertEqual(rpc.calls, [], f"{name} 绝不调用 RPC")
                self.assertEqual(result["execution"]["database_reads"], 1)

    def test_c_expired_row_rejected(self):
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(
            select_rows=[_row(expires_at=_PAST_TS)], embed=embed, rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "NO_ACTIVE_EMBEDDED_MEMORIES")
        self.assertEqual(embed.calls, [])
        self.assertEqual(rpc.calls, [])

    def test_c_unexpired_row_passes(self):
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(
            select_rows=[_row(expires_at=_FUTURE_TS)], embed=embed, rpc=rpc)
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "VECTOR_SELF_MATCH_READY")

    def test_c_time_invalid_blocks_provider_and_rpc(self):
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(
            select_rows=[_row(expires_at="not-a-timestamp")],
            embed=embed, rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ACTIVE_MEMORY_TIME_INVALID")
        self.assertEqual(embed.calls, [], "时间不可解析绝不调用 provider")
        self.assertEqual(rpc.calls, [], "时间不可解析绝不调用 RPC")

    def test_c_select_exception(self):
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(
            db=_SelftestFakeService(
                select_exc=RuntimeError(f"{_DB_SECRET_MARKER} boom")),
            embed=embed, rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INTERNAL_ERROR")
        self.assertEqual(embed.calls, [])
        self.assertEqual(rpc.calls, [])
        self.assertEqual(result["execution"]["database_reads"], 1)


# ==========================================
# D. 模型一致性
# ==========================================

class TestModelConsistency(unittest.TestCase):

    def test_d_model_not_configured_zero_db(self):
        db = _SelftestFakeService(select_rows=[_row()])
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, d, e, r = _run_module(db=db, embed=embed, rpc=rpc,
                                                model="   ")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "EMBEDDING_MODEL_NOT_CONFIGURED")
        self.assertEqual(db.table_calls, [], "模型未配置零查库")
        self.assertEqual(embed.calls, [], "模型未配置零 provider")
        self.assertEqual(rpc.calls, [], "模型未配置零 RPC")
        self.assertEqual(result["execution"]["database_reads"], 0)

    def test_d_trim_equal_passes(self):
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(
            select_rows=[_row(model=_MODEL_MARKER)],
            embed=embed, rpc=rpc, model=f"  {_MODEL_MARKER}  ")
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "VECTOR_SELF_MATCH_READY")

    def test_d_mismatch_blocks_provider_and_rpc(self):
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(
            select_rows=[_row(model=_MODEL_MARKER)],
            embed=embed, rpc=rpc, model=_MODEL_MARKER_2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "EMBEDDING_MODEL_MISMATCH")
        self.assertEqual(embed.calls, [], "模型不一致绝不调用 provider")
        self.assertEqual(rpc.calls, [], "模型不一致绝不调用 RPC")
        self.assertEqual(result["execution"]["database_reads"], 1)
        self.assertEqual(result["execution"]["provider_calls"], 0)

    def test_d_mismatch_returns_no_model_names(self):
        result, log_line, db, e, r = _run_module(
            select_rows=[_row(model=_MODEL_MARKER)], model=_MODEL_MARKER_2)
        text = json.dumps(result, ensure_ascii=False) + log_line
        self.assertNotIn(_MODEL_MARKER, text, "不得返回库内模型名")
        self.assertNotIn(_MODEL_MARKER_2, text, "不得返回当前配置模型名")


# ==========================================
# E. provider 查询向量
# ==========================================

class TestProviderVector(unittest.TestCase):

    def test_e_provider_called_once_with_content(self):
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(embed=embed, rpc=rpc)
        self.assertEqual(e.calls, [_CONTENT_MARKER], "恰一次且输入为库内 content")
        self.assertEqual(result["execution"]["provider_calls"], 1)
        self.assertEqual(result["execution"]["database_reads"], 2)

    def test_e_valid_vector_reaches_rpc(self):
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(embed=embed, rpc=rpc)
        self.assertEqual(rpc.calls, [{
            "query_embedding": _vec(1024),
            "p_user_id": _USER_MARKER,
            "match_count": 5,
        }], "RPC 恰一次且参数形状固定")

    def _bad_vector_case(self, vec, expected_code):
        embed = _RecordingEmbed(result=vec)
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(embed=embed, rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], expected_code)
        self.assertEqual(rpc.calls, [], "向量非法绝不调用 RPC")
        self.assertEqual(e.calls, [_CONTENT_MARKER], "provider 恰一次不重试")
        self.assertEqual(result["execution"]["provider_calls"], 1)

    def test_e_empty_and_none(self):
        self._bad_vector_case([], "EMBEDDING_UNAVAILABLE")
        self._bad_vector_case(None, "EMBEDDING_UNAVAILABLE")

    def test_e_non_list(self):
        self._bad_vector_case({"a": 1}, "EMBEDDING_RESPONSE_INVALID")
        self._bad_vector_case("0.1,0.2", "EMBEDDING_RESPONSE_INVALID")
        self._bad_vector_case(1024, "EMBEDDING_RESPONSE_INVALID")

    def test_e_non_numeric_element(self):
        self._bad_vector_case(["x"] * 1024, "EMBEDDING_RESPONSE_INVALID")
        self._bad_vector_case([None] * 1024, "EMBEDDING_RESPONSE_INVALID")

    def test_e_non_finite(self):
        bad = _vec(1024)
        bad[10] = float("nan")
        self._bad_vector_case(bad, "EMBEDDING_NON_FINITE_VALUES")
        bad2 = _vec(1024)
        bad2[10] = float("inf")
        self._bad_vector_case(bad2, "EMBEDDING_NON_FINITE_VALUES")

    def test_e_dimension_mismatch(self):
        for dim in (768, 1536, 1023, 1025):
            with self.subTest(dim=dim):
                self._bad_vector_case(_vec(dim), "EMBEDDING_DIMENSION_MISMATCH")

    def test_e_zero_vector(self):
        self._bad_vector_case([0.0] * 1024, "EMBEDDING_ZERO_VECTOR")

    def test_e_provider_exception_no_retry(self):
        embed = _RecordingEmbed(exc=RuntimeError(f"{_PROVIDER_SECRET_MARKER} p"))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(embed=embed, rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INTERNAL_ERROR")
        self.assertEqual(e.calls, [_CONTENT_MARKER], "异常后绝不重试")
        self.assertEqual(rpc.calls, [])
        self.assertEqual(result["execution"]["provider_calls"], 1)


# ==========================================
# F. RPC 调用与 Top1 判定
# ==========================================

class TestRpcCallAndTop1(unittest.TestCase):

    def _run(self, rpc, embed=None):
        return _run_module(embed=embed, rpc=rpc)

    def test_f_rpc_name_and_service_role_source(self):
        self.assertEqual(mvs.RPC_NAME, "match_memory_items")
        handler_src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_vector_selftest)
        self.assertIn("supabase_service.rpc(_mvs.RPC_NAME", handler_src,
                      "handler 经 service_role 客户端构造 RPC callable")
        self.assertNotIn("match_memories'", handler_src)
        self.assertNotIn("match_active_memories", handler_src)
        module_src = inspect.getsource(mvs)
        self.assertNotIn("match_memories", module_src)
        self.assertNotIn("match_active_memories", module_src)

    def test_f_params_shape(self):
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(rpc=rpc)
        self.assertEqual(len(rpc.calls), 1, "每请求 RPC 恰一次")
        params = rpc.calls[0]
        self.assertEqual(set(params.keys()),
                         {"query_embedding", "p_user_id", "match_count"})
        self.assertEqual(params["match_count"], 5, "match_count 固定 5")
        self.assertEqual(params["p_user_id"], _USER_MARKER)
        self.assertEqual(len(params["query_embedding"]), 1024)
        self.assertTrue(all(isinstance(v, float) for v in
                            params["query_embedding"]))

    def test_f_ready(self):
        for sim in (1.0, 0.9999, 0.99):
            with self.subTest(sim=sim):
                rpc = _RecordingRpc(rows=[_rpc_row(_TOP1_ID_MARKER, sim)])
                result, log_line, db, e, r = _run_module(rpc=rpc)
                self.assertTrue(result["ok"])
                self.assertEqual(result["code"], "VECTOR_SELF_MATCH_READY")
                self.assertEqual(result["stats"], {
                    "selected": 1, "rpc_returned": 1, "top1_match": True,
                    "top1_similarity": sim, "dimension": 1024})
                self.assertEqual(result["retrieval"], {
                    "method": "pgvector_cosine_selftest_v1",
                    "active_only": True, "expired_excluded": True,
                    "user_scoped": True, "writes_executed": False})
                self.assertEqual(result["execution"], {
                    "provider_calls": 1, "database_reads": 2,
                    "database_writes": 0, "pinecone_touched": False,
                    "llm_touched": False})

    def test_f_empty_results(self):
        rpc = _RecordingRpc(rows=[])
        result, log_line, db, e, r = _run_module(rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VECTOR_SELF_MATCH_NO_RESULTS")
        self.assertEqual(result["stats"]["rpc_returned"], 0)

    def test_f_non_list_data(self):
        for data in (None, {"rows": []}, "rows"):
            with self.subTest(data=data):
                rpc = _RecordingRpc(data=data)
                result, log_line, db, e, r = _run_module(rpc=rpc)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "VECTOR_RPC_RESPONSE_INVALID")

    def test_f_row_count_over_match_count(self):
        rpc = _RecordingRpc(rows=[_rpc_row(_TOP1_ID_MARKER, 0.9999)] * 6)
        result, log_line, db, e, r = _run_module(rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VECTOR_RPC_RESPONSE_INVALID")

    def test_f_row_missing_id(self):
        rpc = _RecordingRpc(rows=[{"similarity": 0.9999}])
        result, log_line, db, e, r = _run_module(rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VECTOR_RPC_RESPONSE_INVALID")

    def test_f_bad_similarity(self):
        cases = {
            "non_numeric": [{"memory_item_id": _TOP1_ID_MARKER,
                             "similarity": "high"}],
            "missing": [{"memory_item_id": _TOP1_ID_MARKER}],
            "nan": [{"memory_item_id": _TOP1_ID_MARKER,
                     "similarity": float("nan")}],
            "inf": [{"memory_item_id": _TOP1_ID_MARKER,
                     "similarity": float("inf")}],
            "out_of_range": [{"memory_item_id": _TOP1_ID_MARKER,
                              "similarity": 1.5}],
        }
        for name, rows in cases.items():
            with self.subTest(case=name):
                rpc = _RecordingRpc(rows=rows)
                result, log_line, db, e, r = _run_module(rpc=rpc)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "VECTOR_RPC_RESPONSE_INVALID")

    def test_f_top1_mismatch(self):
        rpc = _RecordingRpc(rows=[_rpc_row(_OTHER_ID_MARKER, 0.9999),
                                  _rpc_row(_TOP1_ID_MARKER, 0.98)])
        result, log_line, db, e, r = _run_module(rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VECTOR_SELF_MATCH_TOP1_MISMATCH")
        self.assertEqual(result["stats"]["top1_match"], False)
        self.assertEqual(result["stats"]["rpc_returned"], 2)
        self.assertNotIn(_TOP1_ID_MARKER, json.dumps(result))
        self.assertNotIn(_OTHER_ID_MARKER, json.dumps(result))

    def test_f_low_similarity(self):
        rpc = _RecordingRpc(rows=[_rpc_row(_TOP1_ID_MARKER, 0.98)])
        result, log_line, db, e, r = _run_module(rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VECTOR_SELF_MATCH_LOW_SIMILARITY")
        self.assertEqual(result["stats"]["top1_match"], True)
        self.assertEqual(result["stats"]["top1_similarity"], 0.98)

    def test_f_rpc_exception(self):
        rpc = _RecordingRpc(exc=RuntimeError(f"{_RPC_SECRET_MARKER} rpc boom"))
        result, log_line, db, e, r = _run_module(rpc=rpc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VECTOR_RPC_FAILED")
        self.assertEqual(len(rpc.calls), 1, "RPC 异常后不重试")
        self.assertEqual(result["execution"]["database_reads"], 2)

    def test_f_rpc_called_once_per_request(self):
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, db, e, r = _run_module(rpc=rpc)
        self.assertEqual(len(rpc.calls), 1)
        self.assertEqual(e.calls, [_CONTENT_MARKER])


# ==========================================
# G. HTTP 脱敏
# ==========================================

class TestNoLeakage(unittest.TestCase):

    _MARKERS = (_CONTENT_MARKER, _MODEL_MARKER, _MODEL_MARKER_2,
                _USER_MARKER, str(_ITEM_ID_MARKER), _OTHER_ID_MARKER,
                str(_VEC_MARKER), _SUBJECT_KEY_MARKER,
                _PROVIDER_SECRET_MARKER, _RPC_SECRET_MARKER,
                _DB_SECRET_MARKER, "SHOULD-NOT-LEAK",
                "DOUBAO", "DOUBAO_EMBEDDING_EP", "siliconflow",
                "api.siliconflow", "Bearer", "api_key",
                "Authorization", "rest/v1", "memory_items",
                "content_hash", "traceback")

    def _leak_scan(self, send, logs):
        text = _resp_text(send) + "".join(logs)
        for marker in self._MARKERS:
            self.assertNotIn(marker, text, f"泄漏标记 {marker!r} 出现在响应/日志")
        self.assertNotIn("confirm", text.lower(), "请求体内容不得回显")
        return text

    def test_g_success_path_no_leakage(self):
        send, logs, db, e = _call_handler()
        self._leak_scan(send, logs)

    def test_g_failure_paths_no_leakage(self):
        scenarios = [
            ("no_candidates", _SelftestFakeService(select_rows=[])),
            ("select_error", _SelftestFakeService(
                select_exc=RuntimeError(f"{_DB_SECRET_MARKER} boom"))),
            ("filtered_row", _SelftestFakeService(
                select_rows=[_row(user="OTHER-USER")])),
            ("mismatch", _SelftestFakeService(
                select_rows=[_row(model=_MODEL_MARKER)])),
            ("empty_vector", _SelftestFakeService(select_rows=[_row()],
                                                  rpc_rows=_ok_rpc_rows())),
            ("rpc_error", _SelftestFakeService(
                select_rows=[_row()],
                rpc_exc=RuntimeError(f"{_RPC_SECRET_MARKER} rpc"))),
            ("top1_mismatch", _SelftestFakeService(
                select_rows=[_row()],
                rpc_rows=[_rpc_row(_OTHER_ID_MARKER, 0.9999)])),
            ("low_similarity", _SelftestFakeService(
                select_rows=[_row()],
                rpc_rows=[_rpc_row(_TOP1_ID_MARKER, 0.97)])),
        ]
        for name, db in scenarios:
            with self.subTest(scenario=name):
                embed = _RecordingEmbed(
                    result=[] if name == "empty_vector"
                    else _vec(1024, marker=_VEC_MARKER))
                send, logs, db, e = _call_handler(embed=embed, db=db,
                                                  model=_MODEL_MARKER_2
                                                  if name == "mismatch"
                                                  else _MODEL_MARKER)
                self._leak_scan(send, logs)


# ==========================================
# H. 零写入隔离
# ==========================================

class TestIsolation(unittest.TestCase):

    def test_h_module_source_has_no_forbidden_calls(self):
        src = inspect.getsource(mvs)
        # 注意：pinecone_touched / llm_touched 为响应字段名，不按裸词匹配；
        # RPC 经注入 callable 调用，模块源码不得出现客户端 .rpc( 链
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
                       "memory_recall", "tool_loop", "compose_member_view",
                       "schedule", "cron", "print(",
                       "match_memories", "match_active_memories"):
            self.assertNotIn(banned, src,
                             f"memory_vector_selftest 源码不得包含 {banned!r}")

    def test_h_module_never_reads_env_for_model(self):
        src = inspect.getsource(mvs)
        self.assertNotIn("DOUBAO_EMBEDDING_EP", src,
                         "模型标识由 gateway 读取后注入，模块不读环境变量")

    def test_h_handler_source_has_no_forbidden_calls(self):
        src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_vector_selftest)
        # 本阶段唯一例外：service_role 只读 RPC（.rpc( 允许）；
        # 旧词面召回 RPC / Pinecone / LLM / 调度仍然一律禁止
        for banned in ("PineconeMemoryClient", "from pinecone",
                       "import pinecone", "ask_role", "_ask_llm",
                       "create_task", "ensure_future", "Timer(",
                       "threading", "getenv",
                       "import memory_recall", "memory_recall",
                       "tool_loop", "compose_member_view",
                       "stable_system", "volatile_block",
                       "schedule", "cron",
                       "match_active_memories"):
            self.assertNotIn(banned, src,
                             f"selftest handler 源码不得包含 {banned!r}")

    def test_h_handler_reuses_server_dependencies(self):
        src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_vector_selftest)
        self.assertIn('os.environ.get("DOUBAO_EMBEDDING_EP"', src,
                      "模型标识只在 handler 内只读现有环境变量")
        self.assertIn("_srv_st._get_embedding", src,
                      "复用现有 _get_embedding，不新建客户端")
        self.assertIn("_srv_st.supabase_service", src,
                      "复用 service_role 客户端")
        self.assertIn("_resolve_pinecone_user_id", src,
                      "user_id 由服务端统一解析")
        self.assertIn("_mvs.RPC_NAME", src,
                      "只读 RPC 名来自模块常量")

    def test_h_behavior_zero_writes(self):
        db = _SelftestFakeService(select_rows=[_row()], rpc_rows=_ok_rpc_rows())
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        result, log_line, d, e, r = _run_module(db=db, embed=embed, rpc=rpc)
        self.assertEqual(result["code"], "VECTOR_SELF_MATCH_READY")
        self.assertEqual(db.forbidden, [], "无 insert/update/delete/upsert")
        self.assertEqual(result["execution"]["database_writes"], 0)
        self.assertFalse(result["retrieval"]["writes_executed"])
        self.assertFalse(result["execution"]["pinecone_touched"])
        self.assertFalse(result["execution"]["llm_touched"])

    def test_h_module_prints_nothing(self):
        buf = io.StringIO()
        db = _SelftestFakeService(select_rows=[_row()], rpc_rows=_ok_rpc_rows())
        embed = _RecordingEmbed(result=_vec(1024))
        rpc = _RecordingRpc(rows=_ok_rpc_rows())
        with redirect_stdout(buf):
            asyncio.run(mvs.run_selftest(db, _USER_MARKER, embed,
                                         _MODEL_MARKER, rpc))
            asyncio.run(mvs.run_selftest(
                _SelftestFakeService(select_rows=[]), _USER_MARKER,
                _RecordingEmbed(result=_vec(1024)),
                _MODEL_MARKER, _RecordingRpc(rows=_ok_rpc_rows())))
        self.assertEqual(buf.getvalue(), "", "模块不得直接打印任何内容")


if __name__ == "__main__":
    unittest.main()
