# -*- coding: utf-8 -*-
"""第 21 阶段专项测试 —— active 长期记忆只读召回预览。

受 API_SECRET 保护的接口：
  POST /api/memory-recall-preview → 用户手动提交一条查询，服务端只读查询
  同用户 status=active 的 memory_items，内存排除已过期条目，确定性词面
  相关性（deterministic_lexical_v1）排序，返回脱敏召回预览。

全部 unittest + mock + 合成数据；不连接真实 Supabase / LLM / Pinecone；
不真实调用生产召回预览接口；零写入、零状态修改。

覆盖（任务 A-K）：
  A 路由与鉴权（/api/* 鉴权覆盖、仅 POST、OPTIONS 现有行为、非法请求零查询）
  B 请求校验（confirm/query/top_k 白名单与边界、注入字段全拒绝、默认 top_k）
  C 状态隔离（查询只发 status=active 条件 + 内存二次过滤五类状态）
  D 用户隔离（查询含服务端 user_id、客户端无法提交 user_id、响应不含 user_id）
  E 过期（未过期返回/已过期过滤/空值保留/无效时间保守跳过/UTC aware/不 UPDATE）
  F 中文匹配（完整子串、bigram 重合、无重合不返回、短查询、标点空白 NFKC）
  G 英文数字（token 重合、大小写不敏感、项目名/日期/版本、停用过短 token）
  H subject_key（下划线空格规范化、命中、不入日志、无命中不返回）
  I 排序（文本优先、importance 仅 tie-break、updated_at 收尾、top_k 截断、
    score 0~1、稳定排序）
  J 响应脱敏（无 ID/user_id/hash/来源/batch/metadata/superseded_by/created_by）
  K 零写入与隔离（源码仅 select、无 Pinecone/LLM/调度、上下文注入未改动）

运行：  python -m unittest test_memory_recall_phase21 -v
"""

import asyncio
import datetime
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import gateway
import memory_recall as mrc
import server as _srv
from test_memory_phase1_fixes import FakeResult
from test_memory_preview_phase10 import FakeReceive, FakeSend


# ==========================================
# 合成数据
# ==========================================

_USER_MARKER = "USERID_RECALL_MARKER"
_SERVER_UID = "SERVER_UID_RECALL_MARKER"
_SUBJECT_MARKER = "SUBJECTKEY_RECALL_MARKER"
_CONTENT_MARKER = "合成召回测试慢跑内容"
_DB_SECRET_MARKER = "SECRET_DB_MARKER"   # 注入异常消息，断言不外泄


def _mk_row(i=1, status="active", content=_CONTENT_MARKER, subject_key=None,
            expires_at=None, importance=3, source="web",
            updated_at="2026-08-20T08:00:00+00:00"):
    """合成 active 行（字段对齐本阶段 SELECT 列；无 id/user_id 等内部列）。"""
    return {
        "memory_type": "long_term",
        "content": content,
        "importance": importance,
        "confidence": 0.7,
        "subject_key": subject_key,
        "valid_at": "2026-08-20T08:00:00+00:00",
        "expires_at": expires_at,
        "source": source,
        "updated_at": updated_at,
        "status": status,
    }


_ITEM_RESPONSE_KEYS = {
    "recall_index", "memory_type", "content", "importance", "confidence",
    "subject_key", "valid_at", "expires_at", "source", "score",
    "match_reasons",
}


# ==========================================
# 假 service_role 客户端
# ==========================================

class RecallFakeSupabase:
    """第 21 阶段假 service_role 客户端：记录 select 过滤器与列，可注入失败。

    memory_items SELECT → 返回注入行（或抛出 error）。insert/update/delete/
    upsert/rpc 一律记录进 forbidden_ops（必须恒空）——零写入断言的行为面。
    """

    def __init__(self, rows=None, error=None):
        self.rows = list(rows or [])
        self.error = error
        self.ops = []             # ("select", table)
        self.select_columns = []
        self.select_filters = []  # [("eq"|"neq"|"in_", col, val), ...]
        self.order_args = []      # (table, column, desc)
        self.limit_args = []      # (table, n)
        self.forbidden_ops = []

    def table(self, name):
        return _RecallFakeQuery(self, name)


class _RecallFakeQuery:
    def __init__(self, owner, table):
        self._owner = owner
        self._table = table
        self._op = None
        self._columns = None
        self._filters = []

    def select(self, *a, **k):
        self._op = "select"
        self._columns = a[0] if a else ""
        return self

    def insert(self, *a, **k):
        self._owner.forbidden_ops.append(("insert", self._table))
        return self

    def update(self, *a, **k):
        self._owner.forbidden_ops.append(("update", self._table))
        return self

    def delete(self, *a, **k):
        self._owner.forbidden_ops.append(("delete", self._table))
        return self

    def upsert(self, *a, **k):
        self._owner.forbidden_ops.append(("upsert", self._table))
        return self

    def rpc(self, *a, **k):
        self._owner.forbidden_ops.append(("rpc", self._table))
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self._filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in_", col, vals))
        return self

    def order(self, col, desc=False):
        self._owner.order_args.append((self._table, col, desc))
        return self

    def limit(self, n):
        self._owner.limit_args.append((self._table, n))
        return self

    def execute(self):
        o = self._owner
        if self._op == "select":
            o.ops.append(("select", self._table))
            o.select_columns.append(self._columns)
            o.select_filters.append(list(self._filters))
            if o.error is not None:
                raise o.error
            return FakeResult(list(o.rows))
        return FakeResult([])


# ==========================================
# 调用辅助
# ==========================================

def _run_recall(fake, query, top_k=5, uid=_SERVER_UID):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = asyncio.run(mrc.run_recall(fake, uid, query, top_k=top_k))
    return result, buf.getvalue()


def _run_recall_raw(supabase_service, server_user_id, query, top_k):
    """按 run_recall 原始签名调用（模块层防御测试用）。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = asyncio.run(
            mrc.run_recall(supabase_service, server_user_id, query, top_k))
    return result, buf.getvalue()


def _post_recall(body, fake=None):
    """直调 POST handler；默认拦截 run_recall 以捕获调用参数。"""
    send = FakeSend()
    captured = []

    def _fake_run(sb, uid, query, top_k=5):
        captured.append({"sb": sb, "uid": uid, "query": query,
                         "top_k": top_k})
        return {"ok": True, "code": "RECALL_PREVIEW_READY", "stats": {},
                "retrieval": {}, "items": []}

    scope = {"method": "POST", "path": "/api/memory-recall-preview"}
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    fake = fake or RecallFakeSupabase()
    with patch.object(_srv, "supabase_service", fake), \
         patch.object(_srv, "_resolve_pinecone_user_id",
                      return_value=_SERVER_UID), \
         patch.object(mrc, "run_recall", side_effect=_fake_run), \
         patch.object(gateway, "_log", lambda m: None):
        asyncio.run(gateway.HostFixMiddleware._handle_memory_recall_preview(
            None, scope, FakeReceive(raw), send))
    return send, captured


def _valid_body(**over):
    body = {"confirm": "RECALL_PREVIEW_ONLY",
            "query": "我之前在做什么项目？", "top_k": 5}
    body.update(over)
    return body


# ==========================================
# A. 路由与鉴权
# ==========================================

class TestRecallRouteAuth(unittest.TestCase):

    def _mw_call(self, scope, body=b""):
        send = FakeSend()
        app = gateway.HostFixMiddleware(None)
        asyncio.run(app(scope, FakeReceive(body), send))
        return send

    def test_a_recall_requires_api_secret(self):
        scope = {"type": "http", "path": "/api/memory-recall-preview",
                 "method": "POST", "headers": []}
        with patch.dict(os.environ, {"API_SECRET": "test-secret-marker"}):
            send = self._mw_call(scope)
        self.assertEqual(send.status, 401, "无鉴权头必须 401")

    def test_a_options_preflight_without_auth(self):
        scope = {"type": "http", "path": "/api/memory-recall-preview",
                 "method": "OPTIONS", "headers": []}
        send = self._mw_call(scope)
        self.assertEqual(send.status, 204, "OPTIONS 沿用全局 CORS 预检")

    def test_a_post_only_and_no_db_on_other_methods(self):
        for method in ("GET", "DELETE", "PUT", "PATCH"):
            with self.subTest(method=method):
                called = []
                send = FakeSend()
                scope = {"method": method, "path": "/api/memory-recall-preview"}
                with patch.object(mrc, "run_recall",
                                  side_effect=lambda *a, **k: called.append(1)), \
                     patch.object(gateway, "_log", lambda m: None):
                    asyncio.run(
                        gateway.HostFixMiddleware._handle_memory_recall_preview(
                            None, scope, FakeReceive(b""), send))
                self.assertEqual(send.status, 405)
                self.assertEqual(called, [], "非 POST 不得触发召回执行器")

    def test_a_middleware_valid_secret_reaches_handler(self):
        scope = {"type": "http", "path": "/api/memory-recall-preview",
                 "method": "POST",
                 "headers": [(b"authorization", b"Bearer test-secret-marker")]}
        sentinel = RecallFakeSupabase()
        with patch.dict(os.environ, {"API_SECRET": "test-secret-marker",
                                     "USER_ID": _SERVER_UID}), \
             patch.object(_srv, "supabase_service", sentinel), \
             patch.object(gateway, "_log", lambda m: None):
            send = self._mw_call(scope, json.dumps(_valid_body()).encode("utf-8"))
        self.assertEqual(send.status, 200, "鉴权通过后进入召回 handler")
        self.assertEqual(send.body_json.get("code"), "NO_RELEVANT_ACTIVE_MEMORIES")
        self.assertTrue(sentinel.ops, "合法请求应到达数据库只读查询")

    def test_a_invalid_json_zero_queries(self):
        fake = RecallFakeSupabase()
        send, _ = _post_recall(b"not-json{{{", fake=fake)
        self.assertEqual(send.status, 400)
        self.assertEqual(fake.ops, [], "非法 JSON 不得查询数据库")


# ==========================================
# B. 请求校验
# ==========================================

class TestRecallRequestValidation(unittest.TestCase):

    def _assert_rejected(self, body, expect_code=None):
        fake = RecallFakeSupabase()
        send, captured = _post_recall(body, fake=fake)
        self.assertEqual(send.status, 400, f"body={body!r} 必须 400")
        self.assertEqual(captured, [], "非法请求不得调用召回执行器")
        self.assertEqual(fake.ops, [], "非法请求不得查询数据库")
        if expect_code:
            self.assertEqual(send.body_json.get("code"), expect_code)

    def test_b_wrong_confirm(self):
        self._assert_rejected(_valid_body(confirm="RECALL_PREVIEW_ONLY2"))
        self._assert_rejected(_valid_body(confirm="WRITE_PENDING_REVIEW"))
        self._assert_rejected(_valid_body(confirm=None))

    def test_b_query_missing_empty_nonstring_toolong(self):
        self._assert_rejected({"confirm": "RECALL_PREVIEW_ONLY"})
        self._assert_rejected(_valid_body(query=""))
        self._assert_rejected(_valid_body(query="    "))
        self._assert_rejected(_valid_body(query=12345))
        self._assert_rejected(_valid_body(query=None))
        self._assert_rejected(_valid_body(query="查" * 501))

    def test_b_query_boundary_500_accepted(self):
        body = _valid_body(query="测" * 500)
        fake = RecallFakeSupabase()
        send, captured = _post_recall(body, fake=fake)
        self.assertEqual(send.status, 200)
        self.assertEqual(captured[0]["query"], "测" * 500)

    def test_b_top_k_default(self):
        body = {"confirm": "RECALL_PREVIEW_ONLY", "query": "项目进展"}
        _, captured = _post_recall(body)
        self.assertEqual(captured[0]["top_k"], 5, "top_k 缺省必须为 5")

    def test_b_top_k_bad_types_and_bounds(self):
        for bad in (True, False, "5", 5.0, None, 0, -1, 11, [5]):
            with self.subTest(top_k=bad):
                self._assert_rejected(_valid_body(top_k=bad))

    def test_b_top_k_boundary_values(self):
        for ok_val in (1, 10):
            body = _valid_body(top_k=ok_val)
            _, captured = _post_recall(body)
            self.assertEqual(captured[0]["top_k"], ok_val)

    def test_b_injection_fields_rejected(self):
        for extra in ("user_id", "status", "memory_type", "item_id", "id",
                      "namespace", "threshold", "provider", "model",
                      "include_pending", "include_rejected", "include_expired",
                      "write_back", "update_recall_count", "last_recalled_at"):
            with self.subTest(field=extra):
                self._assert_rejected(_valid_body(**{extra: "anything"}))

    def test_b_multiple_extra_fields_rejected(self):
        self._assert_rejected(_valid_body(user_id="u", status="active",
                                          include_pending=True))


# ==========================================
# C. 状态隔离
# ==========================================

class TestRecallStatusIsolation(unittest.TestCase):

    def test_c_query_only_active_condition(self):
        fake = RecallFakeSupabase(rows=[_mk_row(1)])
        _run_recall(fake, "慢跑内容")
        self.assertEqual(fake.ops, [("select", "memory_items")],
                         "只允许一次 memory_items SELECT")
        filters = fake.select_filters[0]
        self.assertIn(("eq", "user_id", _SERVER_UID), filters,
                      "必须带服务端 user_id 条件")
        self.assertIn(("eq", "status", "active"), filters,
                      "必须带 status=active 条件")
        self.assertEqual([c for c in fake.select_columns],
                         [mrc._SELECT_COLUMNS])
        self.assertIn(("memory_items", "importance", True), fake.order_args)
        self.assertIn(("memory_items", "updated_at", True), fake.order_args)
        self.assertEqual(fake.limit_args, [("memory_items", 200)])
        self.assertEqual(fake.forbidden_ops, [], "不得有任何写操作")

    def test_c_in_memory_second_filter_all_statuses(self):
        """即使查询层失效、fake 错误返回全部状态，内存也只保留 active。"""
        rows = [
            _mk_row(1, status="active", content="项目进展正常"),
            _mk_row(2, status="pending_review", content="项目进展候选"),
            _mk_row(3, status="rejected", content="项目进展已拒绝"),
            _mk_row(4, status="superseded", content="项目进展被替代"),
            _mk_row(5, status="expired", content="项目进展已过期"),
        ]
        fake = RecallFakeSupabase(rows=rows)
        result, _ = _run_recall(fake, "项目进展")
        self.assertEqual(result["code"], "RECALL_PREVIEW_READY")
        self.assertEqual(result["stats"]["active_fetched"], 5)
        self.assertEqual(result["stats"]["status_filtered"], 4,
                         "非 active 必须在内存二次过滤")
        self.assertEqual(result["stats"]["matched"], 1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["content"], "项目进展正常")
        self.assertEqual(result["items"][0]["recall_index"], 1)

    def test_c_no_match_response_shape(self):
        fake = RecallFakeSupabase(rows=[_mk_row(1, content="完全无关的内容")])
        result, _ = _run_recall(fake, "量子物理实验")
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "NO_RELEVANT_ACTIVE_MEMORIES")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["retrieval"]["method"],
                         "deterministic_lexical_v1")
        self.assertFalse(result["retrieval"]["semantic_search"])
        self.assertFalse(result["retrieval"]["writes_executed"])

    def test_c_stats_keys_present(self):
        fake = RecallFakeSupabase(rows=[_mk_row(1)])
        result, _ = _run_recall(fake, "慢跑内容")
        for key in ("active_fetched", "status_filtered", "expired_filtered",
                    "invalid_time_filtered", "matched", "returned"):
            self.assertIn(key, result["stats"])


# ==========================================
# D. 用户隔离
# ==========================================

class TestRecallUserIsolation(unittest.TestCase):

    def test_d_query_carries_server_uid(self):
        fake = RecallFakeSupabase(rows=[_mk_row(1)])
        _run_recall(fake, "慢跑内容", uid=_SERVER_UID)
        self.assertIn(("eq", "user_id", _SERVER_UID),
                      fake.select_filters[0])

    def test_d_handler_resolves_uid_server_side(self):
        body = _valid_body()
        _, captured = _post_recall(body)
        self.assertEqual(captured[0]["uid"], _SERVER_UID,
                         "user_id 必须来自服务端统一解析")

    def test_d_client_cannot_submit_user_id(self):
        fake = RecallFakeSupabase()
        send, _ = _post_recall(_valid_body(user_id="EVIL_USER"), fake=fake)
        self.assertEqual(send.status, 400)
        self.assertEqual(fake.ops, [], "注入 user_id 不得触达数据库")

    def test_d_response_never_contains_uid(self):
        fake = RecallFakeSupabase(rows=[_mk_row(1)])
        result, out = _run_recall(fake, "慢跑内容", uid=_USER_MARKER)
        raw = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(_USER_MARKER, raw, "响应不得包含 user_id")
        self.assertNotIn('"user_id"', raw)
        self.assertNotIn(_USER_MARKER, out, "日志不得包含 user_id")

    def test_d_rows_from_other_users_never_fetched(self):
        """模块不提交任何客户端 user_id；隔离完全由服务端条件保证。"""
        fake = RecallFakeSupabase(rows=[_mk_row(1, content="别人的项目记忆")])
        result, _ = _run_recall(fake, "项目记忆", uid=_SERVER_UID)
        # fake 只按过滤器分流——本假客户端只在带正确 uid 条件时返回行
        self.assertIn(("eq", "user_id", _SERVER_UID), fake.select_filters[0])
        # 响应条目不得携带 user_id 字段
        for item in result["items"]:
            self.assertNotIn("user_id", item)


# ==========================================
# E. 过期
# ==========================================

class TestRecallExpiryFilter(unittest.TestCase):

    def test_e_active_unexpired_returned(self):
        future = (datetime.datetime.now(datetime.timezone.utc)
                  + datetime.timedelta(hours=1)).isoformat()
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="每周慢跑计划执行中", expires_at=future)])
        result, _ = _run_recall(fake, "每周慢跑计划")
        self.assertEqual(result["code"], "RECALL_PREVIEW_READY")
        self.assertEqual(result["stats"]["expired_filtered"], 0)
        self.assertEqual(result["stats"]["returned"], 1)

    def test_e_active_expired_filtered(self):
        past = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=1)).isoformat()
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="每周慢跑计划执行中", expires_at=past)])
        result, _ = _run_recall(fake, "每周慢跑计划")
        self.assertEqual(result["code"], "NO_RELEVANT_ACTIVE_MEMORIES")
        self.assertEqual(result["stats"]["expired_filtered"], 1)
        self.assertEqual(result["stats"]["matched"], 0)
        self.assertEqual(result["items"], [])

    def test_e_expires_at_null_kept(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="每周慢跑计划执行中", expires_at=None)])
        result, _ = _run_recall(fake, "每周慢跑计划")
        self.assertEqual(result["stats"]["returned"], 1)

    def test_e_invalid_time_conservatively_skipped(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="每周慢跑计划执行中",
                          expires_at="not-a-timestamp")])
        result, _ = _run_recall(fake, "每周慢跑计划")
        self.assertEqual(result["code"], "NO_RELEVANT_ACTIVE_MEMORIES")
        self.assertEqual(result["stats"]["invalid_time_filtered"], 1)
        self.assertEqual(result["items"], [])

    def test_e_utc_aware_z_suffix_and_offset(self):
        past_z = "2026-01-01T00:00:00Z"
        future_offset = "2099-01-01T08:00:00+08:00"
        fake = RecallFakeSupabase(rows=[
            _mk_row(1, content="已过期记录甲", expires_at=past_z),
            _mk_row(2, content="未来过期记录乙", expires_at=future_offset),
        ])
        result, _ = _run_recall(fake, "记录")
        self.assertEqual(result["stats"]["expired_filtered"], 1)
        self.assertEqual([i["content"] for i in result["items"]],
                         ["未来过期记录乙"])

    def test_e_mixed_expiry_stats(self):
        past = "2026-01-01T00:00:00+00:00"
        future = "2099-01-01T00:00:00+00:00"
        fake = RecallFakeSupabase(rows=[
            _mk_row(1, content="项目甲进行中", expires_at=None),
            _mk_row(2, content="项目乙进行中", expires_at=past),
            _mk_row(3, content="项目丙进行中", expires_at=future),
            _mk_row(4, content="项目丁进行中", expires_at="garbage"),
        ])
        result, _ = _run_recall(fake, "项目")
        self.assertEqual(result["stats"]["active_fetched"], 4)
        self.assertEqual(result["stats"]["expired_filtered"], 1)
        self.assertEqual(result["stats"]["invalid_time_filtered"], 1)
        self.assertEqual(result["stats"]["matched"], 2)
        self.assertEqual(result["stats"]["returned"], 2)

    def test_e_never_updates_status(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="每周慢跑计划执行中",
                          expires_at="2020-01-01T00:00:00+00:00")])
        _run_recall(fake, "每周慢跑计划")
        self.assertEqual(fake.forbidden_ops, [],
                         "过期过滤绝不触发 UPDATE/INSERT/DELETE/UPSERT/RPC")
        self.assertEqual(fake.ops, [("select", "memory_items")])


# ==========================================
# F. 中文匹配
# ==========================================

class TestRecallChineseMatching(unittest.TestCase):

    def test_f_exact_query_in_content(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="用户在推进网关重构项目，进展顺利")])
        result, _ = _run_recall(fake, "网关重构项目")
        self.assertEqual(result["stats"]["matched"], 1)
        reasons = result["items"][0]["match_reasons"]
        self.assertIn("EXACT_QUERY_IN_CONTENT", reasons)

    def test_f_bigram_overlap(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="用户的项目每周汇报一次进展")])
        result, _ = _run_recall(fake, "项目进展")
        self.assertEqual(result["stats"]["matched"], 1)
        self.assertIn("CHINESE_BIGRAM_OVERLAP",
                      result["items"][0]["match_reasons"])

    def test_f_no_overlap_not_returned(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="用户在合成测试中提到每周三次慢跑",
                          importance=5)])
        result, _ = _run_recall(fake, "今天天气怎么样")
        self.assertEqual(result["code"], "NO_RELEVANT_ACTIVE_MEMORIES",
                         "完全无词面重合不得因 importance 高而返回")

    def test_f_single_char_query_conservative(self):
        fake = RecallFakeSupabase(rows=[_mk_row(1, content="项目进展顺利")])
        result, _ = _run_recall(fake, "好")
        self.assertEqual(result["code"], "NO_RELEVANT_ACTIVE_MEMORIES",
                         "单字符查询保守不召回（宁拒不放）")

    def test_f_punctuation_whitespace_nfkc(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="项目进展顺利，一切正常")])
        # 标点/空白不应阻断匹配
        result, _ = _run_recall(fake, " 项目，进展！ ")
        self.assertEqual(result["stats"]["matched"], 1)
        self.assertIn("EXACT_QUERY_IN_CONTENT",
                      result["items"][0]["match_reasons"])

    def test_f_nfkc_fullwidth_normalized(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="用户在写 Ｐｙｔｈｏｎ 脚本")])
        result, _ = _run_recall(fake, "python 脚本")
        self.assertEqual(result["stats"]["matched"], 1)


# ==========================================
# G. 英文和数字
# ==========================================

class TestRecallTokenMatching(unittest.TestCase):

    def test_g_token_overlap_case_insensitive(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="rikkahub 网关开发中")])
        result, _ = _run_recall(fake, "RIKKAHUB 网关")
        self.assertEqual(result["stats"]["matched"], 1)
        self.assertIn("TOKEN_OVERLAP", result["items"][0]["match_reasons"])

    def test_g_project_name_and_version(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="计划把网关升级到 v2.5 版本")])
        result, _ = _run_recall(fake, "v2.5 版本升级")
        self.assertEqual(result["stats"]["matched"], 1)

    def test_g_date_token_match(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="计划在 2026 年完成迁移")])
        result, _ = _run_recall(fake, "2026")
        self.assertEqual(result["stats"]["matched"], 1)
        self.assertIn("TOKEN_OVERLAP", result["items"][0]["match_reasons"])

    def test_g_short_tokens_disabled(self):
        # 单字母 token 停用：纯单字母重合不得产生召回
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="变量 a b c d 相关配置")])
        result, _ = _run_recall(fake, "a b")
        self.assertEqual(result["code"], "NO_RELEVANT_ACTIVE_MEMORIES",
                         "过短 token 必须停用，避免单字母泛滥")

    def test_g_token_query_no_false_chinese_match(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="用户喜欢在深夜写代码")])
        result, _ = _run_recall(fake, "gpt-5")
        self.assertEqual(result["code"], "NO_RELEVANT_ACTIVE_MEMORIES")


# ==========================================
# H. subject_key
# ==========================================

class TestRecallSubjectKey(unittest.TestCase):

    def test_h_underscore_space_normalized(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="今天吃了三顿饭",
                          subject_key="memory_system_重构")])
        result, _ = _run_recall(fake, "memory system 重构 进展")
        self.assertEqual(result["stats"]["matched"], 1)
        self.assertIn("SUBJECT_KEY_MATCH",
                      result["items"][0]["match_reasons"])

    def test_h_subject_only_hit(self):
        """正文完全无关、subject_key 命中 → 仍返回且标记 SUBJECT_KEY_MATCH。"""
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="今天吃了三顿饭",
                          subject_key="fin_项目计划")])
        result, _ = _run_recall(fake, "fin项目计划怎么样")
        self.assertEqual(result["stats"]["matched"], 1)
        self.assertIn("SUBJECT_KEY_MATCH",
                      result["items"][0]["match_reasons"])

    def test_h_subject_key_not_in_logs(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="今天吃了三顿饭",
                          subject_key=_SUBJECT_MARKER)])
        _, out = _run_recall(fake, "三顿饭")
        self.assertNotIn(_SUBJECT_MARKER, out,
                         "subject_key 原文不得单独泄露到日志")

    def test_h_no_text_no_subject_hit_not_returned(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="今天吃了三顿饭",
                          subject_key="fin_项目计划", importance=5)])
        result, _ = _run_recall(fake, "量子物理实验记录")
        self.assertEqual(result["code"], "NO_RELEVANT_ACTIVE_MEMORIES")


# ==========================================
# I. 排序
# ==========================================

class TestRecallOrdering(unittest.TestCase):

    def test_i_text_relevance_beats_importance(self):
        fake = RecallFakeSupabase(rows=[
            _mk_row(1, content="记忆系统重构完成了", importance=1),
            _mk_row(2, content="今天聊到进展", importance=5),
        ])
        result, _ = _run_recall(fake, "记忆系统重构进展")
        self.assertEqual(result["stats"]["matched"], 2)
        self.assertEqual(result["items"][0]["content"], "记忆系统重构完成了",
                         "文本相关性必须优先于 importance")
        self.assertGreater(result["items"][0]["score"],
                           result["items"][1]["score"])

    def test_i_importance_only_tie_break(self):
        fake = RecallFakeSupabase(rows=[
            _mk_row(1, content="用户坚持每周慢跑计划三个月", importance=2),
            _mk_row(2, content="每周慢跑计划已坚持三个月", importance=5),
        ])
        result, _ = _run_recall(fake, "每周慢跑计划")
        self.assertEqual(result["items"][0]["importance"], 5,
                         "同分时 importance 高者在前（仅 tie-break）")
        self.assertEqual(result["items"][0]["score"],
                         result["items"][1]["score"])

    def test_i_updated_at_final_tie_break(self):
        fake = RecallFakeSupabase(rows=[
            _mk_row(1, content="每周慢跑计划进行时", importance=3,
                    updated_at="2026-08-20T10:00:00+00:00"),
            _mk_row(2, content="每周慢跑计划推进中", importance=3,
                    updated_at="2026-08-25T10:00:00+00:00"),
        ])
        result, _ = _run_recall(fake, "每周慢跑计划")
        self.assertEqual(result["stats"]["matched"], 2)
        self.assertEqual(result["items"][0]["score"],
                         result["items"][1]["score"],
                         "两行必须同分（否则不是 updated_at tie-break 场景）")
        self.assertEqual(result["items"][0]["content"], "每周慢跑计划推进中",
                         "同分同 importance → updated_at 新者在前")

    def test_i_top_k_truncation(self):
        rows = [_mk_row(i, content=f"项目{i}进展正常", importance=3)
                for i in range(1, 9)]
        fake = RecallFakeSupabase(rows=rows)
        result, _ = _run_recall(fake, "项目", top_k=3)
        self.assertEqual(result["stats"]["matched"], 8)
        self.assertEqual(result["stats"]["returned"], 3)
        self.assertEqual([i["recall_index"] for i in result["items"]],
                         [1, 2, 3])
        self.assertEqual([i["content"] for i in result["items"]],
                         ["项目1进展正常", "项目2进展正常", "项目3进展正常"],
                         "同分同重要性同时间必须保持取数稳定序")

    def test_i_score_bounds_and_determinism(self):
        fake = RecallFakeSupabase(rows=[
            _mk_row(1, content="网关重构项目进展顺利", importance=4),
            _mk_row(2, content="网关重构", importance=2),
            _mk_row(3, content="网关网关网关", importance=5),
        ])
        r1, _ = _run_recall(fake, "网关重构项目")
        r2, _ = _run_recall(fake, "网关重构项目")
        for item in r1["items"]:
            self.assertTrue(0.0 <= item["score"] <= 1.0)
        self.assertEqual(r1, r2, "同输入结果必须完全一致（确定性）")

    def test_i_updated_at_parse_failure_sinks_last(self):
        fake = RecallFakeSupabase(rows=[
            _mk_row(1, content="每周慢跑计划执行中", importance=3,
                    updated_at="2026-08-25T10:00:00+00:00"),
            _mk_row(2, content="每周慢跑计划执行中", importance=3,
                    updated_at="garbage-time"),
        ])
        result, _ = _run_recall(fake, "每周慢跑计划")
        self.assertEqual(result["stats"]["matched"], 2,
                         "updated_at 解析失败只影响排序，不影响命中")


# ==========================================
# J. 响应脱敏
# ==========================================

class TestRecallSanitization(unittest.TestCase):

    def test_j_item_keys_exact_whitelist(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="项目进展正常", subject_key="proj_x")])
        result, _ = _run_recall(fake, "项目进展")
        for item in result["items"]:
            self.assertEqual(set(item.keys()), _ITEM_RESPONSE_KEYS)

    def test_j_no_sensitive_fields_anywhere(self):
        fake = RecallFakeSupabase(
            rows=[_mk_row(1, content="项目进展正常", subject_key="proj_x")])
        result, out = _run_recall(fake, "项目进展")
        raw = json.dumps(result, ensure_ascii=False)
        for marker in ('"user_id"', '"content_hash"', '"metadata"',
                       '"superseded_by"', '"created_by"', '"item_id"', '"id"',
                       '"source_event_ids"', '"source_batch_id"',
                       '"status"', '"updated_at"', '"last_recalled_at"',
                       _USER_MARKER, _DB_SECRET_MARKER):
            self.assertNotIn(marker, raw, f"响应不得包含 {marker}")
        self.assertNotIn(_USER_MARKER, out)

    def test_j_logs_counts_only(self):
        query_text = "我的慢跑计划怎么样了"
        fake = RecallFakeSupabase(rows=[_mk_row(1, content=_CONTENT_MARKER,
                                                subject_key=_SUBJECT_MARKER)])
        _, out = _run_recall(fake, query_text)
        self.assertNotIn(query_text, out, "日志不得包含 query 原文")
        self.assertNotIn(_CONTENT_MARKER, out, "日志不得包含记忆正文")
        self.assertNotIn(_SUBJECT_MARKER, out)
        self.assertNotIn(_USER_MARKER, out)
        self.assertIn("active记忆召回预览", out)
        self.assertIn("fetched=1", out)
        self.assertIn("matched=1", out)
        self.assertIn("returned=1", out)

    def test_j_query_error_sanitized(self):
        fake = RecallFakeSupabase(error=RuntimeError(_DB_SECRET_MARKER))
        result, out = _run_recall(fake, "任何查询")
        self.assertFalse(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "code", "stats"},
                         "错误响应只含 ok/code/stats")
        self.assertEqual(result["stats"]["active_fetched"], 0)
        self.assertNotIn(_DB_SECRET_MARKER, out, "异常原文不得入日志")
        self.assertNotIn(_DB_SECRET_MARKER, json.dumps(result))
        self.assertIn("stage=active_query", out)
        self.assertIn("error=RuntimeError", out)

    def test_j_no_match_error_free_shape(self):
        fake = RecallFakeSupabase(rows=[])
        result, _ = _run_recall(fake, "随便什么查询")
        self.assertEqual(set(result.keys()),
                         {"ok", "code", "stats", "retrieval", "items"})


# ==========================================
# K. 零写入与隔离（源码断言）
# ==========================================

class TestRecallSourceIsolation(unittest.TestCase):

    def _src(self, name):
        with io.open(name, encoding="utf-8") as f:
            return f.read()

    def test_k_memory_recall_no_forbidden_operations(self):
        src = self._src("memory_recall.py").lower()
        for pat in (".insert(", ".update(", ".delete(", "upsert(", ".rpc(",
                    "truncate(", "create_client", "create_task", "threading",
                    "timer(", "os.environ", "getenv", "import openai",
                    "from openai", "import pinecone", "from pinecone",
                    "pinecone(", "anthropic", "import requests",
                    "embeddings", "last_recalled_at"):
            self.assertNotIn(pat, src, f"memory_recall.py 不得出现 {pat!r}")

    def test_k_memory_recall_select_only(self):
        src = self._src("memory_recall.py")
        self.assertIn(".select(", src)
        self.assertNotIn(".insert(", src)

    def test_k_memory_recall_imports_minimal(self):
        import ast
        tree = ast.parse(self._src("memory_recall.py"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"asyncio", "datetime", "re",
                                    "unicodedata"},
                         "模块只允许标准库四项 import——无 LLM / Pinecone / 调度")

    def test_k_no_write_calls_in_ast(self):
        import ast
        tree = ast.parse(self._src("memory_recall.py"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 ("insert", "update", "delete", "upsert",
                                  "rpc"),
                                 "不得调用任何写方法")

    def test_k_only_memory_items_table(self):
        src = self._src("memory_recall.py")
        self.assertEqual(src.count('table("memory_items")'), 1,
                         "只允许一次 memory_items 表引用")
        self.assertNotIn("memory_events", src)

    def test_k_gateway_routes_exact(self):
        src = self._src("gateway.py")
        self.assertEqual(src.count('"/api/memory-recall-preview"'), 1,
                         "召回预览路由恰好一处")

    def test_k_context_injection_untouched(self):
        gw = self._src("gateway.py")
        start = gw.find("async def _inject_context")
        self.assertGreaterEqual(start, 0)
        end = gw.find("\n    async def ", start + 10)
        inj = gw[start:end]
        self.assertNotIn("memory_items", inj,
                         "_inject_context 不得读取 memory_items")
        self.assertNotIn("memory_recall", inj,
                         "_inject_context 不得调用召回模块")

        srv = self._src("server.py")
        start = srv.find("async def _build_channel_context")
        self.assertGreaterEqual(start, 0)
        end = srv.find("\nasync def ", start + 10)
        if end < 0:
            end = srv.find("\nclass ", start + 10)
        chan = srv[start:end]
        self.assertNotIn("memory_items", chan,
                         "_build_channel_context 不得读取 memory_items")
        self.assertNotIn("memory_recall", chan,
                         "_build_channel_context 不得调用召回模块")

    def test_k_no_env_or_dependencies_changed(self):
        import ast
        tree = ast.parse(self._src("memory_recall.py"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(node.func.attr, "setdefault")
        src = self._src("memory_recall.py")
        self.assertNotIn("environ[", src)
        self.assertNotIn("putenv", src)


# ==========================================
# 模块层防御校验（非法输入零查询）
# ==========================================

class TestRecallModuleDefense(unittest.TestCase):

    def test_m_invalid_inputs_never_query(self):
        fake = RecallFakeSupabase(rows=[_mk_row(1)])
        cases = [
            ("empty query", (fake, _SERVER_UID, "   ", 5)),
            ("long query", (fake, _SERVER_UID, "查" * 501, 5)),
            ("non-string query", (fake, _SERVER_UID, 42, 5)),
            ("bool top_k", (fake, _SERVER_UID, "ok", True)),
        ]
        for name, args in cases:
            with self.subTest(case=name):
                result, _ = _run_recall_raw(*args)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "INVALID_RECALL_REQUEST")
        self.assertEqual(fake.ops, [], "全部非法输入都不得查询数据库")

    def test_m_service_unavailable_no_query(self):
        result, _ = _run_recall(None, "查询", uid=_SERVER_UID)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "RECALL_SERVICE_UNAVAILABLE")

    def test_m_invalid_uid_no_query(self):
        fake = RecallFakeSupabase()
        result, _ = _run_recall(fake, "查询", uid="   ")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "RECALL_SERVICE_UNAVAILABLE")
        self.assertEqual(fake.ops, [])

    def test_m_top_k_clamped_defensively(self):
        """越界 top_k 在模块层被夹取（gateway 层才负责 400 拒绝）。"""
        fake = RecallFakeSupabase(
            rows=[_mk_row(i, content=f"项目{i}进展", importance=3)
                  for i in range(1, 5)])
        result, _ = _run_recall_raw(fake, _SERVER_UID, "项目", 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stats"]["returned"], 1,
                         "top_k=0 夹取为 MIN_TOP_K=1")
        result, _ = _run_recall_raw(fake, _SERVER_UID, "项目", 11)
        self.assertEqual(result["stats"]["returned"], 4,
                         "top_k=11 夹取为 MAX_TOP_K=10，且不超过命中数")


if __name__ == "__main__":
    unittest.main()
