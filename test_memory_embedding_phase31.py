# -*- coding: utf-8 -*-
"""第 31 阶段专项测试 —— active 记忆向量手动回填执行器。

  POST /api/memory-embedding-backfill
    → 服务端强制选定最旧 active 且 embedding IS NULL 的一条（limit 1）；
    → 用其数据库 content 恰调用一次 server._get_embedding；
    → 严格校验（list/tuple / float / finite / 1024 维 / 非零）；
    → 单条条件 UPDATE 原子写入 embedding/embedding_model/embedded_at 恰三列；
    → 响应与日志不含正文/向量/模型名/user_id/item id/hash/异常原文/SQL；
    → 无 Pinecone / LLM / 自动调度；幂等依据 embedding IS NULL 条件。

全部 unittest + mock + 合成数据：不真实调用 provider、不真实调用新接口、
不连接真实 Supabase / Pinecone / LLM、不修改任何数据。

覆盖（任务书 A-K）：
  A 路由与鉴权（/api/* API_SECRET、仅 POST、OPTIONS、非法方法零 DB/provider）
  B 请求校验（confirm 缺失/错误、全部注入字段拒绝、非法请求零 DB/provider）
  C 候选选择（强制条件、created_at 升序、limit 1、无候选零 provider、内存二次过滤）
  D provider（恰 1 次且输入为库内 content、异常映射、零自动重试）
  E 向量校验（1024 成功；768/1536/1023/1025/[]/None/非list/非数值/NaN/Inf/零向量/空模型）
  F UPDATE（payload 恰 3 字段、条件齐备、1/0/多行/异常、triplet 同语句）
  G 幂等与并发（成功后再调无候选、竞争第二个 0 行不覆盖、零 DELETE/UPSERT/RPC）
  H 写入格式（list[float] JSON 数组官方格式；响应/日志不暴露捕获值）
  I 脱敏（响应+日志全量扫描敏感标记）
  J 隔离（源码无删除/UPSERT/RPC/Pinecone/LLM/环境变量读取/调度/正式上下文）
  （K 全量记忆回归由命令行单独运行）

运行： python -m unittest test_memory_embedding_phase31 -v
"""

import asyncio
import datetime
import inspect
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import gateway
import memory_embedding as me
import server as _srv
from test_memory_preview_phase10 import FakeReceive, FakeSend


# ==========================================
# 常量与脱敏标记
# ==========================================

_PATH = "/api/memory-embedding-backfill"
_SECRET = "test-secret-marker-phase31"
# 注入脱敏扫描的独特标记（断言响应/日志不外泄）
_MODEL_MARKER = "MODEL-NAME-SECRET-123"
_USER_MARKER = "user-scope-777"
_CONTENT_MARKER = "记忆内容隐私标记：小满喜欢无糖咖啡"
_ITEM_ID_MARKER = 424242
_VEC_MARKER = 0.777123
_PROVIDER_SECRET_MARKER = "PROVIDER_RAW_ERROR_SECRET_MARKER"
_DB_SECRET_MARKER = "DB_RAW_ERROR_SECRET_MARKER"


def _vec(dim, marker=None):
    """合成向量：全 finite、非零；marker 可供泄漏扫描。"""
    if marker is None:
        return [0.001 * (i % 97) for i in range(dim)]
    return [marker + 0.001 * i for i in range(dim)]


def _row(content=_CONTENT_MARKER, user=_USER_MARKER, status="active",
         embedding=None, rid=_ITEM_ID_MARKER):
    """memory_items 候选行的合成最小形状（仅含模块 SELECT 的 5 列）。"""
    return {"id": rid, "content": content, "user_id": user,
            "status": status, "embedding": embedding}


# ==========================================
# 假 service_role 客户端（记录全部调用路径；绝不触网）
# ==========================================

class _FakeResult:
    """模拟 supabase-py execute() 返回（带 .data 属性）。"""

    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """链式记录器：记录每个方法（含 is_）；execute 时回调 owner 分流。"""

    def __init__(self, owner, table):
        self._owner = owner
        self._table = table
        self._path = []

    def _rec(self, method, *args, **kwargs):
        self._path.append((method, args, kwargs))
        return self

    def select(self, *a, **k): return self._rec("select", *a, **k)
    def eq(self, *a, **k): return self._rec("eq", *a, **k)
    def neq(self, *a, **k): return self._rec("neq", *a, **k)
    def is_(self, *a, **k): return self._rec("is_", *a, **k)
    def order(self, *a, **k): return self._rec("order", *a, **k)
    def limit(self, *a, **k): return self._rec("limit", *a, **k)
    def update(self, *a, **k): return self._rec("update", *a, **k)

    # 被禁操作：只记录、照常走 execute 分流（测试末统一断言 forbidden 为空）
    def insert(self, *a, **k): return self._rec("FORBIDDEN:insert", *a, **k)
    def delete(self, *a, **k): return self._rec("FORBIDDEN:delete", *a, **k)
    def upsert(self, *a, **k): return self._rec("FORBIDDEN:upsert", *a, **k)
    def rpc(self, *a, **k): return self._rec("FORBIDDEN:rpc", *a, **k)

    def execute(self, *a, **k):
        self._path.append(("execute", (), {}))
        self._owner.calls.append((self._table, list(self._path)))
        return self._owner._respond(self._table, list(self._path))


class _BackfillFakeService:
    """按 select/update 分流的假 service_role 客户端。"""

    def __init__(self, select_rows=(), select_exc=None,
                 update_rows=(), update_exc=None, stateful=False):
        self.calls = []            # (table, path) 每次执行
        self.table_calls = []      # 每次 table() 访问
        self.forbidden = []        # 捕获到的被禁操作
        self.select_rows = list(select_rows)
        self.select_exc = select_exc
        self.update_rows = list(update_rows)
        self.update_exc = update_exc
        self.stateful = stateful   # 模拟 embedding IS NULL 条件的数据状态
        self.embedded = False      # stateful 模式：是否已被回填
        self.update_payloads = []  # 每次 UPDATE 的 payload 值
        self.update_paths = []     # 每次 UPDATE 的条件路径

    def table(self, name):
        self.table_calls.append(name)
        return _FakeQuery(self, name)

    def _respond(self, table, path):
        kinds = [p[0] for p in path]
        for kind in ("insert", "delete", "upsert", "rpc"):
            if f"FORBIDDEN:{kind}" in kinds:
                self.forbidden.append(kind)
        if kinds and kinds[0] == "select":
            if self.select_exc is not None:
                raise self.select_exc
            return _FakeResult(list(self.select_rows))
        if kinds and kinds[0] == "update":
            payload = path[0][1][0] if path[0][1] else None
            self.update_payloads.append(payload)
            self.update_paths.append(list(path))
            if self.update_exc is not None:
                raise self.update_exc
            if self.stateful:
                # 并发语义：embedding 已非空时条件 UPDATE 命中 0 行，不覆盖
                if self.embedded:
                    return _FakeResult([])
                self.embedded = True
                return _FakeResult([{"id": 1, "embedding": "written"}])
            return _FakeResult(list(self.update_rows))
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


# ==========================================
# 调用辅助
# ==========================================

def _call_handler(body=None, raw=None, method="POST", embed=None, db=None,
                  model=_MODEL_MARKER, user_id=_USER_MARKER):
    """直调 handler；返回 (send, logs, db, embed)。"""
    send = FakeSend()
    logs = []
    if raw is None:
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
    scope = {"method": method, "path": _PATH}
    if db is None:
        db = _BackfillFakeService(select_rows=[])
    if embed is None:
        embed = _RecordingEmbed(result=_vec(1024))
    env = {"API_SECRET": _SECRET, "DOUBAO_EMBEDDING_EP": model or ""}
    with patch.dict(os.environ, env), \
         patch.object(_srv, "_get_embedding", embed), \
         patch.object(_srv, "supabase_service", db), \
         patch.object(_srv, "_resolve_pinecone_user_id", lambda: user_id), \
         patch.object(gateway, "_log", lambda m: logs.append(m)):
        asyncio.run(gateway.HostFixMiddleware._handle_memory_embedding_backfill(
            None, scope, FakeReceive(raw), send))
    return send, logs, db, embed


def _mw_call(scope, body=b""):
    """完整中间件分发（测鉴权/CORS；不 patch 时请求不会触达 handler 之前分支）。"""
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
    """第 index 次执行的方法序列 [(method, args, kwargs)]。"""
    return db.calls[index][1]


def _invalid_body(field):
    return {"confirm": me.CONFIRM_TOKEN, field: "CLIENT_INJECTED_MARKER"}


# ==========================================
# A. 路由与鉴权
# ==========================================

class TestRouteAuth(unittest.TestCase):

    def test_a_requires_api_secret(self):
        with patch.dict(os.environ, {"API_SECRET": _SECRET}):
            send = _mw_call(_auth_scope(with_auth=False),
                            json.dumps({"confirm": me.CONFIRM_TOKEN}).encode())
        self.assertEqual(send.status, 401, "无鉴权头必须 401")
        self.assertEqual(send.body_json.get("error"),
                         "Unauthorized: Missing or invalid API key")

    def test_a_wrong_api_secret_rejected(self):
        headers = [(b"authorization", b"Bearer wrong-secret")]
        scope = {"type": "http", "path": _PATH, "method": "POST",
                 "headers": headers}
        with patch.dict(os.environ, {"API_SECRET": _SECRET}):
            send = _mw_call(scope,
                            json.dumps({"confirm": me.CONFIRM_TOKEN}).encode())
        self.assertEqual(send.status, 401)

    def test_a_empty_api_secret_rejected(self):
        with patch.dict(os.environ, {"API_SECRET": ""}):
            send = _mw_call(_auth_scope(with_auth=True),
                            json.dumps({"confirm": me.CONFIRM_TOKEN}).encode())
        self.assertEqual(send.status, 503, "API_SECRET 为空必须拒绝而非放行")

    def test_a_options_preflight_without_auth(self):
        scope = {"type": "http", "path": _PATH, "method": "OPTIONS", "headers": []}
        with patch.dict(os.environ, {"API_SECRET": _SECRET}):
            send = _mw_call(scope)
        self.assertEqual(send.status, 204, "OPTIONS 沿用全局 CORS 预检（免鉴权）")

    def test_a_post_only_zero_db_and_provider(self):
        for method in ("GET", "PUT", "DELETE", "HEAD"):
            with self.subTest(method=method):
                embed = _RecordingEmbed(result=_vec(1024))
                db = _BackfillFakeService(select_rows=[_row()])
                send, logs, db, e = _call_handler(method=method, embed=embed,
                                                  db=db)
                self.assertEqual(send.status, 405)
                self.assertEqual(send.body_json.get("code"), "METHOD_NOT_ALLOWED")
                self.assertEqual(e.calls, [], "非 POST 绝不触碰 provider")
                self.assertEqual(db.table_calls, [], "非 POST 绝不触碰数据库")
                self.assertEqual(send.body_json["execution"]["provider_calls"], 0)
                self.assertEqual(send.body_json["execution"]["database_reads"], 0)
                self.assertEqual(send.body_json["execution"]["database_writes"], 0)

    def test_a_valid_secret_reaches_handler(self):
        embed = _RecordingEmbed(result=_vec(1024))
        db = _BackfillFakeService(select_rows=[_row()],
                                  update_rows=[{"id": _ITEM_ID_MARKER}])
        body = json.dumps({"confirm": me.CONFIRM_TOKEN}).encode("utf-8")
        env = {"API_SECRET": _SECRET, "DOUBAO_EMBEDDING_EP": _MODEL_MARKER}
        with patch.dict(os.environ, env), \
             patch.object(_srv, "_get_embedding", embed), \
             patch.object(_srv, "supabase_service", db), \
             patch.object(_srv, "_resolve_pinecone_user_id",
                          lambda: _USER_MARKER), \
             patch.object(gateway, "_log", lambda m: None):
            send = _mw_call(_auth_scope(), body)
        self.assertEqual(send.status, 200, "鉴权通过后进入回填 handler")
        self.assertEqual(send.body_json.get("code"), "MEMORY_EMBEDDING_BACKFILLED")
        self.assertEqual(embed.calls, [_CONTENT_MARKER], "输入恒为库内 content")

    def test_a_route_registered_in_dispatch(self):
        src = inspect.getsource(gateway.HostFixMiddleware.__call__)
        self.assertIn("/api/memory-embedding-backfill", src)

    def test_a_confirm_token_consistency(self):
        self.assertEqual(me.CONFIRM_TOKEN, "BACKFILL_ONE_ACTIVE_MEMORY")
        handler_src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_embedding_backfill)
        self.assertIn('"BACKFILL_ONE_ACTIVE_MEMORY"', handler_src)


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

    def test_b_confirm_wrong(self):
        for confirm in ("backfill_one_active_memory", "BACKFILL", "", 123,
                        None, True, ["BACKFILL_ONE_ACTIVE_MEMORY"]):
            with self.subTest(confirm=confirm):
                embed = _RecordingEmbed(result=_vec(1024))
                send, logs, db, e = _call_handler(body={"confirm": confirm},
                                                  embed=embed)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_CONFIRMATION")
                self.assertEqual(e.calls, [])
                self.assertEqual(db.table_calls, [])

    def test_b_injection_fields_all_rejected(self):
        # 客户端试图注入 item_id/正文/向量/模型/provider/维度/范围/批量等
        # 任何额外字段 → 400，绝不查库、绝不调 provider、绝不 UPDATE
        for field in ("item_id", "user_id", "content", "text", "vector",
                      "embedding", "model", "provider", "dimensions",
                      "limit", "status", "force", "overwrite", "write_back",
                      "batch", "query", "backfill", "id"):
            with self.subTest(field=field):
                embed = _RecordingEmbed(result=_vec(1024))
                db = _BackfillFakeService(select_rows=[_row()],
                                          update_rows=[{"id": 1}])
                send, logs, db2, e = _call_handler(body=_invalid_body(field),
                                                   embed=embed, db=db)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_BACKFILL_REQUEST")
                self.assertEqual(e.calls, [])
                self.assertEqual(db.table_calls, [])
                self.assertEqual(db.update_payloads, [])

    def test_b_invalid_json_and_non_dict(self):
        for raw in (b"{not json", b"[1,2,3]", b'"str"', b"null", b"123"):
            with self.subTest(raw=raw):
                embed = _RecordingEmbed(result=_vec(1024))
                send, logs, db, e = _call_handler(raw=raw, embed=embed)
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"),
                                 "INVALID_BACKFILL_REQUEST")
                self.assertEqual(e.calls, [])
                self.assertEqual(db.table_calls, [])

    def test_b_empty_body(self):
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(raw=b"", embed=embed)
        self.assertEqual(send.status, 400)
        self.assertEqual(send.body_json.get("code"), "INVALID_CONFIRMATION")
        self.assertEqual(e.calls, [])
        self.assertEqual(db.table_calls, [])

    def test_b_invalid_request_zero_side_effects(self):
        embed = _RecordingEmbed(result=_vec(1024))
        db = _BackfillFakeService(select_rows=[_row()])
        send, logs, db, e = _call_handler(
            body={"confirm": "WRONG", "model": "x", "item_id": 1},
            embed=embed, db=db)
        self.assertEqual(send.body_json["execution"]["provider_calls"], 0)
        self.assertEqual(send.body_json["execution"]["database_reads"], 0)
        self.assertEqual(send.body_json["execution"]["database_writes"], 0)
        self.assertIs(send.body_json["execution"]["pinecone_touched"], False)
        self.assertIs(send.body_json["execution"]["llm_touched"], False)
        self.assertEqual(e.calls, [])
        self.assertEqual(db.table_calls, [])


# ==========================================
# C. 候选选择
# ==========================================

class TestCandidateSelection(unittest.TestCase):

    def test_c_select_conditions(self):
        db = _BackfillFakeService(select_rows=[_row()],
                                  update_rows=[{"id": _ITEM_ID_MARKER}])
        _call_handler(body={"confirm": me.CONFIRM_TOKEN}, db=db)
        self.assertEqual(len(db.calls), 2, "恰好一次 SELECT + 一次 UPDATE")
        self.assertEqual(db.table_calls, ["memory_items", "memory_items"])
        sel = _ops(db, 0)
        self.assertEqual(sel[0][0], "select")
        self.assertEqual(sel[0][1][0], me._SELECT_COLUMNS
                         if hasattr(me, "_SELECT_COLUMNS")
                         else "id,content,user_id,status,embedding")
        names = [p[0] for p in sel]
        self.assertEqual(names[0], "select")
        self.assertIn(("eq", ("user_id", _USER_MARKER), {}), sel,
                      "必须强制当前服务端用户条件")
        self.assertIn(("eq", ("status", "active"), {}), sel,
                      "必须强制 active 条件")
        self.assertIn(("is_", ("embedding", None), {}), sel,
                      "必须强制 embedding IS NULL 条件")
        self.assertIn(("order", ("created_at",), {"desc": False}), sel,
                      "created_at 升序（最旧优先）")
        self.assertIn(("limit", (1,), {}), sel, "limit 恒为 1")

    def test_c_takes_first_row_only(self):
        # 即使（假）查询层失效返回多行，模块也只处理第一条、不批量
        rows = [_row(content="第一条内容标记A", rid=11),
                _row(content="第二条内容标记B", rid=22),
                _row(content="第三条内容标记C", rid=33)]
        db = _BackfillFakeService(select_rows=rows,
                                  update_rows=[{"id": 11}])
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(send.body_json.get("code"), "MEMORY_EMBEDDING_BACKFILLED")
        self.assertEqual(e.calls, ["第一条内容标记A"], "只处理第一条")
        self.assertEqual(len(db.update_payloads), 1, "不批量 UPDATE")

    def test_c_no_candidates_zero_provider(self):
        db = _BackfillFakeService(select_rows=[])
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(send.status, 200)
        body = send.body_json
        self.assertTrue(body["ok"])
        self.assertEqual(body["code"], "NO_ACTIVE_MEMORIES_NEED_EMBEDDING")
        self.assertEqual(set(body.keys()),
                         {"ok", "code", "stats", "execution"})
        self.assertEqual(body["stats"], {"selected": 0, "updated": 0})
        self.assertEqual(body["execution"],
                         {"provider_calls": 0, "database_reads": 1,
                          "database_writes": 0, "pinecone_touched": False,
                          "llm_touched": False})
        self.assertEqual(e.calls, [], "无候选绝不调用 provider")
        self.assertEqual(db.update_payloads, [])
        self.assertEqual(logs, ["🧬 active记忆向量回填：selected=0 updated=0 "
                                "dimension=none"])

    def test_c_select_failure_mapped_and_sanitized(self):
        db = _BackfillFakeService(
            select_exc=RuntimeError(f"{_DB_SECRET_MARKER} connection refused"))
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(send.status, 500)
        self.assertEqual(send.body_json.get("code"),
                         "MEMORY_EMBEDDING_QUERY_FAILED")
        self.assertEqual(e.calls, [], "SELECT 失败不得调用 provider")
        self.assertEqual(db.update_payloads, [])
        resp_and_logs = _resp_text(send) + "".join(logs)
        self.assertNotIn(_DB_SECRET_MARKER, resp_and_logs)
        self.assertIn("exception_type=RuntimeError", "".join(logs))

    def test_c_in_memory_refilter_rejects(self):
        # 内存二次确认：status / user / content / id 异常 → 拒绝且零 provider
        cases = [
            ("status_not_active", _row(status="pending_review")),
            ("user_mismatch", _row(user="someone-else")),
            ("content_empty", _row(content="")),
            ("content_blank", _row(content="   ")),
            ("content_not_str", _row(content=12345)),
            ("id_missing", _row(rid=None)),
        ]
        for name, row in cases:
            with self.subTest(case=name):
                db = _BackfillFakeService(select_rows=[row])
                embed = _RecordingEmbed(result=_vec(1024))
                send, logs, db, e = _call_handler(
                    body={"confirm": me.CONFIRM_TOKEN}, embed=embed, db=db)
                self.assertEqual(send.status, 500)
                self.assertEqual(send.body_json.get("code"),
                                 "MEMORY_EMBEDDING_CANDIDATE_INVALID")
                self.assertEqual(e.calls, [], "二次确认失败不触 provider")
                self.assertEqual(db.update_payloads, [], "不执行 UPDATE")
                self.assertEqual(send.body_json["stats"]["selected"], 1)
                self.assertEqual(send.body_json["stats"]["updated"], 0)

    def test_c_already_embedded_row_is_state_changed(self):
        # 行内 embedding 已非空（并发已回填）→ STATE_CHANGED，不覆盖
        db = _BackfillFakeService(
            select_rows=[_row(embedding=[0.1] * 1024)])
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(send.status, 409)
        self.assertEqual(send.body_json.get("code"),
                         "MEMORY_EMBEDDING_STATE_CHANGED")
        self.assertEqual(e.calls, [], "已有向量时不调用 provider")
        self.assertEqual(db.update_payloads, [], "不覆盖已有向量")

    def test_c_model_not_configured_zero_db_zero_provider(self):
        db = _BackfillFakeService(select_rows=[_row()])
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db, model="")
        self.assertEqual(send.status, 503)
        self.assertEqual(send.body_json.get("code"),
                         "EMBEDDING_MODEL_NOT_CONFIGURED")
        self.assertEqual(db.table_calls, [], "模型未配置不得查询数据库")
        self.assertEqual(e.calls, [], "模型未配置不得调用 provider")
        self.assertEqual(send.body_json["execution"]["database_reads"], 0)
        self.assertEqual(send.body_json["execution"]["provider_calls"], 0)


# ==========================================
# D. provider 调用
# ==========================================

class TestProviderCall(unittest.TestCase):

    def _success_db(self):
        return _BackfillFakeService(select_rows=[_row()],
                                    update_rows=[{"id": _ITEM_ID_MARKER}])

    def test_d_provider_called_exactly_once_with_db_content(self):
        db = self._success_db()
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(send.body_json.get("code"), "MEMORY_EMBEDDING_BACKFILLED")
        self.assertEqual(len(e.calls), 1, "provider 恰调用 1 次")
        self.assertEqual(e.calls, [_CONTENT_MARKER],
                         "输入恒为数据库 content（客户端无提交入口）")

    def test_d_provider_exception_is_internal_error(self):
        db = self._success_db()
        embed = _RecordingEmbed(
            exc=RuntimeError(f"{_PROVIDER_SECRET_MARKER} real reason"))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(send.status, 500)
        self.assertEqual(send.body_json.get("code"), "INTERNAL_ERROR")
        self.assertEqual(send.body_json["execution"]["provider_calls"], 1)
        self.assertEqual(db.update_payloads, [], "provider 异常不执行 UPDATE")
        resp_and_logs = _resp_text(send) + "".join(logs)
        self.assertNotIn(_PROVIDER_SECRET_MARKER, resp_and_logs)
        self.assertNotIn("real reason", resp_and_logs)
        self.assertIn("exception_type=RuntimeError", "".join(logs))

    def test_d_no_auto_retry_on_provider_failure(self):
        # provider 返回空（多种失败统一 []）→ 恰 1 次调用，无自动重试
        db = self._success_db()
        embed = _RecordingEmbed(result=[])
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(send.status, 503)
        self.assertEqual(send.body_json.get("code"), "EMBEDDING_UNAVAILABLE")
        self.assertEqual(e.calls, [_CONTENT_MARKER], "失败也恰调用 1 次")

        db2 = self._success_db()
        embed2 = _RecordingEmbed(result=_vec(768))
        send2, logs2, db2, e2 = _call_handler(
            body={"confirm": me.CONFIRM_TOKEN}, embed=embed2, db=db2)
        self.assertEqual(send2.status, 503)
        self.assertEqual(send2.body_json.get("code"),
                         "EMBEDDING_DIMENSION_MISMATCH")
        self.assertEqual(e2.calls, [_CONTENT_MARKER], "维度不符也不重试")
        self.assertEqual(db2.update_payloads, [])


# ==========================================
# E. 向量校验
# ==========================================

class TestVectorValidation(unittest.TestCase):

    def test_e_module_validate_success(self):
        values, failure = me.validate_vector(_vec(1024))
        self.assertIsNone(failure)
        self.assertEqual(len(values), 1024)
        values2, failure2 = me.validate_vector(tuple(_vec(1024)))
        self.assertIsNone(failure2, "tuple 同样放行")

    def test_e_dimension_mismatch(self):
        for dim in (768, 1536, 1023, 1025, 1):
            with self.subTest(dim=dim):
                code, actual = me.validate_vector(_vec(dim))[1]
                self.assertEqual(code, me.CODE_DIMENSION_MISMATCH)
                self.assertEqual(actual, dim)

    def test_e_empty_results(self):
        for empty in ([], None, ()):
            with self.subTest(empty=empty):
                code, actual = me.validate_vector(empty)[1]
                self.assertEqual(code, me.CODE_UNAVAILABLE)

    def test_e_wrong_container(self):
        for bad in ("1024 floats", {"embedding": [1.0]}, 42, 3.14,
                    {1.0, 2.0}, True):
            with self.subTest(bad=type(bad).__name__):
                code, _ = me.validate_vector(bad)[1]
                self.assertEqual(code, me.CODE_RESPONSE_INVALID)

    def test_e_non_floatable_elements(self):
        for bad in (["abc"], [None], [1.0, "x", 2.0], [1.0, {"a": 1}],
                    [1.0, [2.0]], [[1.0]]):
            with self.subTest(bad=bad):
                code, _ = me.validate_vector(bad)[1]
                self.assertEqual(code, me.CODE_RESPONSE_INVALID)

    def test_e_non_finite_values(self):
        for bad in ([float("nan")], [float("inf")], [float("-inf")],
                    [1.0, float("nan")], [float("-inf"), 2.0, 3.0]):
            with self.subTest(bad=bad):
                code, _ = me.validate_vector(bad)[1]
                self.assertEqual(code, me.CODE_NON_FINITE)

    def test_e_type_validity_precedes_finite(self):
        code, _ = me.validate_vector([float("nan"), "bad"])[1]
        self.assertEqual(code, me.CODE_RESPONSE_INVALID)

    def test_e_zero_vector_rejected(self):
        code, actual = me.validate_vector([0.0] * 1024)[1]
        self.assertEqual(code, me.CODE_ZERO_VECTOR)
        self.assertEqual(actual, 1024)

    def test_e_handler_maps_validation_errors_to_503(self):
        for embed_result, expected_code in (
                (_vec(768), me.CODE_DIMENSION_MISMATCH),
                (_vec(1536), me.CODE_DIMENSION_MISMATCH),
                (_vec(1023), me.CODE_DIMENSION_MISMATCH),
                (_vec(1025), me.CODE_DIMENSION_MISMATCH),
                ([], me.CODE_UNAVAILABLE),
                (None, me.CODE_UNAVAILABLE),
                ("not-a-list", me.CODE_RESPONSE_INVALID),
                ({"x": 1}, me.CODE_RESPONSE_INVALID),
                (["abc"], me.CODE_RESPONSE_INVALID),
                ([float("nan")], me.CODE_NON_FINITE),
                ([0.0] * 1024, me.CODE_ZERO_VECTOR)):
            with self.subTest(code=expected_code,
                              result=type(embed_result).__name__):
                db = _BackfillFakeService(select_rows=[_row()],
                                          update_rows=[{"id": 1}])
                embed = _RecordingEmbed(result=embed_result)
                send, logs, db, e = _call_handler(
                    body={"confirm": me.CONFIRM_TOKEN}, embed=embed, db=db)
                self.assertEqual(send.status, 503)
                self.assertEqual(send.body_json.get("code"), expected_code)
                self.assertEqual(e.calls, [_CONTENT_MARKER])
                self.assertEqual(db.update_payloads, [], "校验失败不执行 UPDATE")

    def test_e_dimension_mismatch_log_has_expected_actual(self):
        db = _BackfillFakeService(select_rows=[_row()])
        embed = _RecordingEmbed(result=_vec(768))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(logs, ["⚠️ active记忆向量回填失败：stage=embedding "
                                "error=EMBEDDING_DIMENSION_MISMATCH "
                                "expected=1024 actual=768"])


# ==========================================
# F. UPDATE 语义
# ==========================================

class TestUpdateSemantics(unittest.TestCase):

    def _run_success(self):
        db = _BackfillFakeService(select_rows=[_row()],
                                  update_rows=[{"id": _ITEM_ID_MARKER}])
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        return send, logs, db, embed

    def test_f_success_response_shape(self):
        send, logs, db, embed = self._run_success()
        self.assertEqual(send.status, 200)
        body = send.body_json
        self.assertEqual(set(body.keys()),
                         {"ok", "code", "stats", "write_result", "execution"})
        self.assertTrue(body["ok"])
        self.assertEqual(body["code"], "MEMORY_EMBEDDING_BACKFILLED")
        self.assertEqual(body["stats"],
                         {"selected": 1, "updated": 1, "dimension": 1024})
        self.assertEqual(
            body["write_result"],
            {"embedding_written": True, "embedding_model_written": True,
             "embedded_at_written": True, "memory_status_changed": False,
             "memory_content_changed": False})
        self.assertEqual(
            body["execution"],
            {"provider_calls": 1, "database_reads": 1, "database_writes": 1,
             "pinecone_touched": False, "llm_touched": False})
        self.assertEqual(logs, ["🧬 active记忆向量回填：selected=1 updated=1 "
                                "dimension=1024"])

    def test_f_payload_exactly_three_fields(self):
        send, logs, db, embed = self._run_success()
        self.assertEqual(len(db.update_payloads), 1)
        payload = db.update_payloads[0]
        self.assertEqual(set(payload.keys()),
                         {"embedding", "embedding_model", "embedded_at"},
                         "payload 恰三列")
        self.assertNotIn("updated_at", payload)
        self.assertNotIn("content", payload)
        self.assertNotIn("status", payload)

    def test_f_payload_values(self):
        send, logs, db, embed = self._run_success()
        payload = db.update_payloads[0]
        vec = payload["embedding"]
        self.assertIsInstance(vec, list, "写入格式为 list[float]")
        self.assertEqual(len(vec), 1024)
        self.assertTrue(all(isinstance(v, float) for v in vec))
        self.assertEqual(vec, embed.result, "与校验后向量逐值一致")
        self.assertTrue(any(v != 0.0 for v in vec), "非零向量")
        self.assertEqual(payload["embedding_model"], _MODEL_MARKER)
        parsed = datetime.datetime.fromisoformat(payload["embedded_at"])
        self.assertIsNotNone(parsed.tzinfo, "embedded_at 为 UTC aware ISO")

    def test_f_update_conditions(self):
        send, logs, db, embed = self._run_success()
        path = db.update_paths[0]
        self.assertEqual(path[0][0], "update")
        self.assertIn(("eq", ("id", _ITEM_ID_MARKER), {}), path,
                      "条件含内部 item id")
        self.assertIn(("eq", ("user_id", _USER_MARKER), {}), path,
                      "条件含服务端 user_id")
        self.assertIn(("eq", ("status", "active"), {}), path, "条件含 active")
        self.assertIn(("is_", ("embedding", None), {}), path,
                      "条件含 embedding IS NULL")

    def test_f_zero_rows_state_changed(self):
        db = _BackfillFakeService(select_rows=[_row()], update_rows=[])
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(send.status, 409)
        body = send.body_json
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "MEMORY_EMBEDDING_STATE_CHANGED")
        self.assertEqual(body["stats"], {"selected": 1, "updated": 0})
        self.assertEqual(body["execution"]["provider_calls"], 1)
        self.assertEqual(body["execution"]["database_writes"], 1,
                         "UPDATE 语句已发出（0 行命中）")
        self.assertIn("rows=0", "".join(logs))

    def test_f_multi_rows_update_invalid(self):
        db = _BackfillFakeService(select_rows=[_row()],
                                  update_rows=[{"id": 1}, {"id": 2}])
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(send.status, 500)
        self.assertFalse(send.body_json["ok"])
        self.assertEqual(send.body_json.get("code"),
                         "MEMORY_EMBEDDING_UPDATE_INVALID")
        self.assertEqual(send.body_json["stats"]["updated"], 0,
                         "多行时绝不声称成功")

    def test_f_update_exception_failed(self):
        db = _BackfillFakeService(
            select_rows=[_row()],
            update_exc=RuntimeError(f"{_DB_SECRET_MARKER} deadlock"))
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(send.status, 500)
        self.assertEqual(send.body_json.get("code"),
                         "MEMORY_EMBEDDING_UPDATE_FAILED")
        resp_and_logs = _resp_text(send) + "".join(logs)
        self.assertNotIn(_DB_SECRET_MARKER, resp_and_logs,
                         "数据库异常原文绝不外泄")
        self.assertIn("exception_type=RuntimeError", "".join(logs))

    def test_f_triplet_single_statement(self):
        # 三列在同一个 UPDATE payload 中同写（triplet CHECK 的代码侧保证）
        send, logs, db, embed = self._run_success()
        self.assertEqual(len(db.update_payloads), 1,
                         "恰一次 UPDATE 语句（三列同句）")
        payload = db.update_payloads[0]
        self.assertEqual(set(payload.keys()),
                         {"embedding", "embedding_model", "embedded_at"})


# ==========================================
# G. 幂等与并发
# ==========================================

class TestIdempotencyConcurrency(unittest.TestCase):

    def test_g_second_call_no_candidates_after_success(self):
        db = _BackfillFakeService(select_rows=[_row()],
                                  update_rows=[{"id": _ITEM_ID_MARKER}])
        embed = _RecordingEmbed(result=_vec(1024))
        send1, _, _, _ = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                       embed=embed, db=db)
        self.assertEqual(send1.body_json.get("code"),
                         "MEMORY_EMBEDDING_BACKFILLED")
        # 成功后再次调用：模拟 SELECT 已选不到 embedding IS NULL 的行
        db.select_rows = []
        send2, logs2, db2, e2 = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                              embed=embed, db=db)
        self.assertEqual(send2.status, 200)
        self.assertEqual(send2.body_json.get("code"),
                         "NO_ACTIVE_MEMORIES_NEED_EMBEDDING")
        self.assertEqual(send2.body_json["execution"]["provider_calls"], 0)
        self.assertEqual(send2.body_json["execution"]["database_writes"], 0)
        self.assertEqual(len(e2.calls), 1, "provider 总调用数仍为 1（第二次为 0）")
        self.assertEqual(len(db.update_payloads), 1, "第二次零 UPDATE")

    def test_g_concurrent_second_request_zero_rows(self):
        # 两个请求竞争同一条：stateful 假库模拟 embedding IS NULL 条件
        db = _BackfillFakeService(select_rows=[_row()], stateful=True)
        embed = _RecordingEmbed(result=_vec(1024))
        send1, _, _, _ = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                       embed=embed, db=db)
        self.assertEqual(send1.status, 200)
        self.assertEqual(send1.body_json.get("code"),
                         "MEMORY_EMBEDDING_BACKFILLED")
        # 第二个并发请求：SELECT 仍取到旧候选（读取在先），但 UPDATE 条件命中 0 行
        send2, logs2, db2, e2 = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                              embed=embed, db=db)
        self.assertEqual(send2.status, 409)
        self.assertEqual(send2.body_json.get("code"),
                         "MEMORY_EMBEDDING_STATE_CHANGED")
        self.assertEqual(send2.body_json["stats"]["updated"], 0)
        self.assertEqual(db.embedded, True, "已有向量未被覆盖")
        self.assertNotIn("delete", db.forbidden)

    def test_g_no_delete_insert_upsert_rpc_ever(self):
        db = _BackfillFakeService(select_rows=[_row()],
                                  update_rows=[{"id": _ITEM_ID_MARKER}])
        embed = _RecordingEmbed(result=_vec(1024))
        _call_handler(body={"confirm": me.CONFIRM_TOKEN}, embed=embed, db=db)
        # 失败路径同样扫描
        db2 = _BackfillFakeService(select_rows=[_row()], update_rows=[])
        _call_handler(body={"confirm": me.CONFIRM_TOKEN}, db=db2)
        db3 = _BackfillFakeService(select_exc=RuntimeError("x"))
        _call_handler(body={"confirm": me.CONFIRM_TOKEN}, db=db3)
        for d in (db, db2, db3):
            self.assertEqual(d.forbidden, [],
                             "任何路径都不得出现 insert/delete/upsert/rpc")


# ==========================================
# H. 写入格式
# ==========================================

class TestWriteFormat(unittest.TestCase):

    def test_h_embedding_written_as_float_list(self):
        # Supabase 官方确认格式：vector 列以 JSON 数组（list[float]）表示
        db = _BackfillFakeService(select_rows=[_row()],
                                  update_rows=[{"id": _ITEM_ID_MARKER}])
        embed = _RecordingEmbed(result=_vec(1024))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        self.assertEqual(send.status, 200)
        payload = db.update_payloads[0]
        self.assertIsInstance(payload["embedding"], list)
        self.assertTrue(all(isinstance(v, float)
                            for v in payload["embedding"]))

    def test_h_captured_vector_never_in_response_or_logs(self):
        db = _BackfillFakeService(select_rows=[_row()],
                                  update_rows=[{"id": _ITEM_ID_MARKER}])
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        text = _resp_text(send) + "".join(logs)
        self.assertNotIn(str(_VEC_MARKER), text,
                         "假客户端捕获的向量值绝不进入响应/日志")


# ==========================================
# I. 脱敏
# ==========================================

class TestNoLeakage(unittest.TestCase):

    def _leak_scan(self, send, logs):
        text = _resp_text(send) + "".join(logs)
        for marker in (_CONTENT_MARKER, _MODEL_MARKER, _USER_MARKER,
                       str(_ITEM_ID_MARKER), str(_VEC_MARKER),
                       _PROVIDER_SECRET_MARKER, _DB_SECRET_MARKER,
                       "DOUBAO", "DOUBAO_EMBEDDING_EP", "siliconflow",
                       "api.siliconflow", "Bearer", "api_key",
                       "Authorization", "rest/v1", "memory_items",
                       "traceback"):
            self.assertNotIn(marker, text, f"泄漏标记 {marker!r} 出现在响应/日志")
        return text

    def test_i_success_path_no_leakage(self):
        db = _BackfillFakeService(select_rows=[_row()],
                                  update_rows=[{"id": _ITEM_ID_MARKER,
                                                "embedding": "[0.1,0.2]"}])
        embed = _RecordingEmbed(result=_vec(1024, marker=_VEC_MARKER))
        send, logs, db, e = _call_handler(body={"confirm": me.CONFIRM_TOKEN},
                                          embed=embed, db=db)
        text = self._leak_scan(send, logs)
        self.assertNotIn("confirm", text.lower(), "请求体内容不得回显")

    def test_i_failure_paths_no_leakage(self):
        scenarios = [
            ("no_candidates", _BackfillFakeService(select_rows=[])),
            ("select_error", _BackfillFakeService(
                select_exc=RuntimeError(f"{_DB_SECRET_MARKER} boom"))),
            ("provider_empty", _BackfillFakeService(select_rows=[_row()])),
            ("provider_error", _BackfillFakeService(
                select_rows=[_row()],
                update_exc=RuntimeError(f"{_DB_SECRET_MARKER} err"))),
        ]
        for name, db in scenarios:
            with self.subTest(scenario=name):
                embed = _RecordingEmbed(
                    result=[] if name == "provider_empty" else _vec(1024),
                    exc=RuntimeError(f"{_PROVIDER_SECRET_MARKER} p") if
                    name == "provider_error" else None)
                send, logs, db, e = _call_handler(
                    body={"confirm": me.CONFIRM_TOKEN}, embed=embed, db=db)
                self._leak_scan(send, logs)


# ==========================================
# J. 隔离（源码静态检查）
# ==========================================

class TestIsolation(unittest.TestCase):

    def test_j_module_source_has_no_forbidden_calls(self):
        src = inspect.getsource(me)
        # 注意：pinecone_touched / llm_touched 为响应字段名，不按裸词匹配
        for banned in ("import pinecone", "from pinecone",
                       "PineconeMemoryClient", "import supabase",
                       "from supabase", "create_client",
                       ".delete(", ".upsert(", ".insert(", ".rpc(",
                       "DELETE FROM", "TRUNCATE", "DROP TABLE",
                       "create_task", "ensure_future", "Timer(",
                       "threading", "time.sleep", "subprocess",
                       "ask_role", "_ask_llm",
                       "import server", "from server",
                       "import gateway", "from gateway",
                       "import os", "getenv", "os.environ",
                       "requests.", "httpx", "urlopen", "urllib",
                       "stable_system", "volatile_block",
                       "schedule", "cron", "print("):
            self.assertNotIn(banned, src,
                             f"memory_embedding 源码不得包含 {banned!r}")

    def test_j_module_never_reads_env_for_model(self):
        src = inspect.getsource(me)
        self.assertNotIn("DOUBAO_EMBEDDING_EP", src,
                         "模型标识由 gateway 读取后注入，模块不读环境变量")

    def test_j_handler_source_has_no_forbidden_calls(self):
        src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_embedding_backfill)
        for banned in ("PineconeMemoryClient", "from pinecone",
                       "import pinecone", "ask_role", "_ask_llm",
                       "create_task", "ensure_future", "Timer(",
                       "threading", "getenv",
                       "import memory_recall", "stable_system",
                       "volatile_block", "schedule", "cron"):
            self.assertNotIn(banned, src,
                             f"backfill handler 源码不得包含 {banned!r}")

    def test_j_handler_reads_model_env_and_reuses_server(self):
        src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_embedding_backfill)
        self.assertIn('os.environ.get("DOUBAO_EMBEDDING_EP"', src,
                      "模型标识只在 handler 内只读现有环境变量")
        self.assertIn("_srv_bf._get_embedding", src,
                      "复用现有 _get_embedding，不新建客户端")
        self.assertIn("_srv_bf.supabase_service", src,
                      "复用 service_role 客户端")
        self.assertIn("_resolve_pinecone_user_id", src,
                      "user_id 由服务端统一解析")

    def test_j_no_formal_context_or_recall_touch(self):
        mod_src = inspect.getsource(me)
        handler_src = inspect.getsource(
            gateway.HostFixMiddleware._handle_memory_embedding_backfill)
        for src, name in ((mod_src, "module"), (handler_src, "handler")):
            self.assertNotIn("memory_recall", src,
                             f"{name} 不得引用 lexical 召回模块")
            self.assertNotIn("tool_loop", src, f"{name} 不得触碰 tool_loop")
            self.assertNotIn("compose_member_view", src,
                             f"{name} 不得触碰正式上下文")

    def test_j_module_prints_nothing(self):
        buf = io.StringIO()
        db = _BackfillFakeService(select_rows=[_row()],
                                  update_rows=[{"id": _ITEM_ID_MARKER}])
        embed = _RecordingEmbed(result=_vec(1024))
        with redirect_stdout(buf):
            asyncio.run(me.run_backfill(db, _USER_MARKER, embed,
                                        _MODEL_MARKER))
            asyncio.run(me.run_backfill(db, _USER_MARKER,
                                        _RecordingEmbed(result=[]),
                                        _MODEL_MARKER))
            asyncio.run(me.run_backfill(None, _USER_MARKER, embed,
                                        _MODEL_MARKER))
        self.assertEqual(buf.getvalue(), "",
                         "模块自身绝不打印（日志行由 gateway 负责）")

    def test_j_module_constants(self):
        self.assertEqual(me.EXPECTED_EMBEDDING_DIMENSION, 1024)
        self.assertEqual(me.CONFIRM_TOKEN, "BACKFILL_ONE_ACTIVE_MEMORY")
        for code, status in (
                (me.CODE_BACKFILLED, 200),
                (me.CODE_NO_CANDIDATES, 200),
                (me.CODE_STATE_CHANGED, 409),
                (me.CODE_MODEL_NOT_CONFIGURED, 503),
                (me.CODE_UNAVAILABLE, 503),
                (me.CODE_RESPONSE_INVALID, 503),
                (me.CODE_NON_FINITE, 503),
                (me.CODE_DIMENSION_MISMATCH, 503),
                (me.CODE_ZERO_VECTOR, 503),
                (me.CODE_QUERY_FAILED, 500),
                (me.CODE_CANDIDATE_INVALID, 500),
                (me.CODE_UPDATE_INVALID, 500),
                (me.CODE_UPDATE_FAILED, 500),
                (me.CODE_SERVICE_UNAVAILABLE, 503),
                (me.CODE_INTERNAL, 500)):
            self.assertEqual(me.HTTP_STATUS_BY_CODE[code], status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
