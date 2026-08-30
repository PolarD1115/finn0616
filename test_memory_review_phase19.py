# -*- coding: utf-8 -*-
"""第 19 阶段专项测试 —— pending_review 只读管理与人工审批接口。

两个受 API_SECRET 保护的接口：
  GET  /api/memory-review          → 只读列出 pending_review（最旧优先）+ review_session_token
  POST /api/memory-review/decision → 单条显式 approve / reject（乐观条件更新，不删除）

全部 unittest + mock + 合成数据；不连接真实 Supabase / LLM / Pinecone；
不真实调用生产 list / decision 接口；不真实 UPDATE、不删除任何数据。

覆盖（任务 A-J）：
  A 路由与鉴权（/api/* 鉴权覆盖两路由、GET 仅 GET / decision 仅 POST、OPTIONS CORS、
    非法方法不查库不更新）
  B 列表查询（只查 pending_review、最旧优先、limit 默认与边界、非法 limit、
    无数据响应、有数据生成 token、禁止字段不外泄、固定 privacy_hint）
  C review session（token 随机、TTL、容量、不入日志、单 index 消费、其余 index
    仍可用、全部处理完消费 token、失败不消费、已处理 index 不可重复操作）
  D 请求校验（confirm/token/index/decision 白名单与额外字段全拒绝）
  E approve（pending_review→active、UTC 时间戳、条件含 pending_review、仅更新
    允许字段、返回必须恰 1 行、0 行/多行 state changed、不改其他字段）
  F reject（pending_review→rejected、不删除、不动 last_confirmed_at/invalid_at、
    仅更新 status/updated_at）
  G 冲突（active subject_key 冲突 / active 精确 hash 重复、subject_key 空跳过、
    冲突不 UPDATE 不消费 index、不返回旧 item 内容或 ID）
  H 隐私控制（医疗候选不自动批准、必须显式 decision、无批量、无默认 decision）
  I 响应和日志脱敏（无 token/正文/ID/user_id/hash/来源/异常原文/SQL）
  J 隔离与零删除（源码无删除/DROP/memory_events/Pinecone/LLM/自动调度/Timer；
    行为无 delete/upsert/rpc）

运行：  python -m unittest test_memory_review_phase19 -v
说明：进程内 review session 在多 worker / 重启场景会失效（统一返回
REVIEW_SESSION_NOT_FOUND_OR_EXPIRED）——该限制只记录在文档与报告中，
本测试不模拟 Redis / 多进程。
"""

import asyncio
import datetime
import io
import json
import os
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import gateway
import memory_review as mr
import server as _srv
from test_memory_phase1_fixes import FakeResult
from test_memory_preview_phase10 import FakeReceive, FakeSend


# ==========================================
# 合成数据
# ==========================================

_ITEM_UUID = "aaaaaaaa-bbbb-cccc-dddd-000000000001"
_OTHER_UUID = "aaaaaaaa-bbbb-cccc-dddd-000000000002"
_USER_MARKER = "USERID_REVIEW_MARKER"
_HASH_MARKER = "HASH_REVIEW_MARKER"
_DB_SECRET_MARKER = "SECRET_DB_MARKER"   # 注入异常消息，断言不外泄


def _mk_row(i=1, user_id=_USER_MARKER, subject_key=None,
            content="用户在合成测试中提到每周三次慢跑。", content_hash=_HASH_MARKER,
            created_at="2026-08-20T08:00:00+00:00"):
    """合成 pending_review 行（字段对齐 memory_items 真实表结构）。"""
    return {
        "id": f"aaaaaaaa-bbbb-cccc-dddd-{i:012d}",
        "user_id": user_id,
        "status": "pending_review",
        "subject_key": subject_key,
        "content_hash": content_hash,
        "created_at": created_at,
        "memory_type": "long_term",
        "content": content,
        "importance": 3,
        "confidence": 0.7,
        "source": "web",
        "valid_at": "2026-08-20T08:00:00+00:00",
        "invalid_at": None,
        "expires_at": None,
    }


_ITEM_RESPONSE_KEYS = {
    "review_index", "memory_type", "content", "importance", "confidence",
    "subject_key", "valid_at", "invalid_at", "expires_at", "source",
    "created_at", "privacy_hint",
}


# ==========================================
# 假 service_role 客户端
# ==========================================

class ReviewFakeSupabase:
    """第 19 阶段假 service_role 客户端：按表+过滤器分流，可注入各阶段失败。

    - memory_items SELECT → 依 select 过滤器分流：
        * eq subject_key → 主题冲突检查 → subject_active_rows（或 conflict_error）
        * eq content_hash → 精确重复检查 → hash_active_rows（或 conflict_error）
        * 其余（status=pending_review 列表）→ list_rows（或 list_error）
    - memory_items UPDATE → 返回指定行数（update_counts 逐次优先，否则 update_count）
    全部操作与载荷/过滤器记录在 ops/select_filters/order_args/limit_args/
    update_payloads/update_filters 供断言；delete/upsert/rpc 记录进
    forbidden_ops（必须恒空）。
    """

    def __init__(self, list_rows=None, subject_active_rows=None, hash_active_rows=None,
                 list_error=None, conflict_error=None,
                 update_count=1, update_counts=None, update_error=None):
        self.list_rows = list(list_rows or [])
        self.subject_active_rows = list(subject_active_rows or [])
        self.hash_active_rows = list(hash_active_rows or [])
        self.list_error = list_error
        self.conflict_error = conflict_error
        self.update_count = update_count
        self.update_counts = update_counts
        self.update_error = update_error
        self.ops = []              # ("select"|"update", table)
        self.select_filters = []
        self.order_args = []       # (table, column, desc)
        self.limit_args = []       # (table, n)
        self.update_payloads = []
        self.update_filters = []
        self.forbidden_ops = []    # 任何 delete/upsert/rpc 尝试
        self._update_i = 0

    def table(self, name):
        return _ReviewFakeQuery(self, name)


class _ReviewFakeQuery:
    def __init__(self, owner, table):
        self._owner = owner
        self._table = table
        self._op = None
        self._payload = None
        self._filters = []

    def select(self, *a, **k):
        self._op = "select"
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self, *a, **k):
        self._owner.forbidden_ops.append(("delete", self._table, a, k))
        return self

    def upsert(self, *a, **k):
        self._owner.forbidden_ops.append(("upsert", self._table, a, k))
        return self

    def rpc(self, *a, **k):
        self._owner.forbidden_ops.append(("rpc", self._table, a, k))
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
            o.select_filters.append(list(self._filters))
            cols = [c for _, c, _ in self._filters]
            if "subject_key" in cols:
                if o.conflict_error is not None:
                    raise o.conflict_error
                return FakeResult(list(o.subject_active_rows))
            if "content_hash" in cols:
                if o.conflict_error is not None:
                    raise o.conflict_error
                return FakeResult(list(o.hash_active_rows))
            if o.list_error is not None:
                raise o.list_error
            return FakeResult(list(o.list_rows))
        if self._op == "update":
            o.ops.append(("update", self._table))
            o.update_payloads.append(dict(self._payload))
            o.update_filters.append(list(self._filters))
            if o.update_error is not None:
                raise o.update_error
            if o.update_counts is not None:
                n = o.update_counts[o._update_i] if o._update_i < len(o.update_counts) else 0
            else:
                n = o.update_count
            o._update_i += 1
            return FakeResult([{"id": f"row-{j}"} for j in range(n)])
        return FakeResult([])


# ==========================================
# 调用辅助
# ==========================================

def _run_list(fake, limit=20):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = asyncio.run(mr.run_list(fake, limit=limit))
    return result, buf.getvalue()


def _run_decision(fake, token, index, decision):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = asyncio.run(mr.run_decision(fake, token, index, decision))
    return result, buf.getvalue()


def _make_session(n=2, fake=None, subject_key=None):
    """跑一次真实 run_list 构造 session，返回 (fake, token, result)。"""
    fake = fake or ReviewFakeSupabase(
        list_rows=[_mk_row(i, subject_key=subject_key) for i in range(1, n + 1)])
    result, _ = _run_list(fake)
    assert result.get("ok"), result
    return fake, result["review_session_token"], result


def _get_review(query=None):
    """直调 GET handler（patch server.supabase_service 为 sentinel fake）。"""
    send = FakeSend()
    scope = {"method": "GET", "path": "/api/memory-review",
             "query_string": (query or "").encode("utf-8")}
    sentinel = ReviewFakeSupabase()
    with patch.object(_srv, "supabase_service", sentinel), \
         patch.object(gateway, "_log", lambda m: None):
        asyncio.run(gateway.HostFixMiddleware._handle_memory_review(
            None, scope, FakeReceive(b""), send))
    return send, sentinel


def _post_decision(body, fake=None):
    """直调 POST decision handler；可注入 fake 或拦截 run_decision。"""
    send = FakeSend()
    captured = []

    def _fake_run(sb, token, index, decision):
        captured.append({"sb": sb, "token": token, "index": index,
                         "decision": decision})
        return {"ok": True, "code": "MEMORY_APPROVED",
                "result": {"review_index": index, "new_status": "active"}}

    scope = {"method": "POST", "path": "/api/memory-review/decision"}
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    fake = fake or ReviewFakeSupabase()
    with patch.object(_srv, "supabase_service", fake), \
         patch.object(mr, "run_decision", side_effect=_fake_run), \
         patch.object(gateway, "_log", lambda m: None):
        asyncio.run(gateway.HostFixMiddleware._handle_memory_review_decision(
            None, scope, FakeReceive(raw), send))
    return send, captured


def _valid_body(**over):
    body = {"confirm": "DECIDE_MEMORY_REVIEW", "review_session_token": "tok-marker",
            "review_index": 1, "decision": "approve"}
    body.update(over)
    return body


class _CacheReset(unittest.TestCase):
    """每个用例前清空进程内 session 缓存（模块全局状态隔离）。"""

    def setUp(self):
        mr._review_sessions.clear()


# ==========================================
# A. 路由与鉴权
# ==========================================

class TestReviewRouteAuth(_CacheReset):

    def _mw_call(self, scope, body=b""):
        send = FakeSend()
        app = gateway.HostFixMiddleware(None)
        asyncio.run(app(scope, FakeReceive(body), send))
        return send

    def test_a_list_requires_api_secret(self):
        scope = {"type": "http", "path": "/api/memory-review",
                 "method": "GET", "headers": []}
        with patch.dict(os.environ, {"API_SECRET": "test-secret-marker"}):
            send = self._mw_call(scope)
        self.assertEqual(send.status, 401, "无鉴权头必须 401")

    def test_a_decision_requires_api_secret(self):
        scope = {"type": "http", "path": "/api/memory-review/decision",
                 "method": "POST", "headers": []}
        with patch.dict(os.environ, {"API_SECRET": "test-secret-marker"}):
            send = self._mw_call(scope)
        self.assertEqual(send.status, 401, "无鉴权头必须 401")

    def test_a_options_preflight_without_auth_both_routes(self):
        for path in ("/api/memory-review", "/api/memory-review/decision"):
            with self.subTest(path=path):
                scope = {"type": "http", "path": path,
                         "method": "OPTIONS", "headers": []}
                send = self._mw_call(scope)
                self.assertEqual(send.status, 204, "OPTIONS 沿用全局 CORS 预检")

    def test_a_list_get_only_and_no_db_on_other_methods(self):
        for method in ("POST", "DELETE", "PUT"):
            with self.subTest(method=method):
                called = []
                send = FakeSend()
                scope = {"method": method, "path": "/api/memory-review"}
                with patch.object(mr, "run_list",
                                  side_effect=lambda *a, **k: called.append(1)), \
                     patch.object(gateway, "_log", lambda m: None):
                    asyncio.run(gateway.HostFixMiddleware._handle_memory_review(
                        None, scope, FakeReceive(b""), send))
                self.assertEqual(send.status, 405)
                self.assertEqual(called, [], "非 GET 不得触发列表执行器")

    def test_a_decision_post_only_and_no_db_on_other_methods(self):
        for method in ("GET", "DELETE", "PUT"):
            with self.subTest(method=method):
                called = []
                send = FakeSend()
                scope = {"method": method, "path": "/api/memory-review/decision"}
                with patch.object(mr, "run_decision",
                                  side_effect=lambda *a, **k: called.append(1)), \
                     patch.object(gateway, "_log", lambda m: None):
                    asyncio.run(gateway.HostFixMiddleware._handle_memory_review_decision(
                        None, scope, FakeReceive(b""), send))
                self.assertEqual(send.status, 405)
                self.assertEqual(called, [], "非 POST 不得触发决策执行器")

    def test_a_middleware_valid_secret_reaches_list_handler(self):
        scope = {"type": "http", "path": "/api/memory-review",
                 "method": "GET",
                 "headers": [(b"authorization", b"Bearer test-secret-marker")]}
        sentinel = ReviewFakeSupabase()
        with patch.dict(os.environ, {"API_SECRET": "test-secret-marker"}), \
             patch.object(_srv, "supabase_service", sentinel), \
             patch.object(gateway, "_log", lambda m: None):
            send = self._mw_call(scope)
        self.assertEqual(send.status, 200, "鉴权通过后进入列表 handler")
        self.assertEqual(send.body_json.get("code"), "NO_PENDING_REVIEW_ITEMS")


# ==========================================
# B. 列表查询
# ==========================================

class TestReviewList(_CacheReset):

    def test_b_no_pending_items(self):
        fake = ReviewFakeSupabase()
        result, out = _run_list(fake)
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "NO_PENDING_REVIEW_ITEMS")
        self.assertEqual(result["stats"], {"count": 0})
        self.assertEqual(result["items"], [])
        self.assertNotIn("review_session_token", result, "无数据不得返回 token")

    def test_b_ready_response_shape(self):
        _, token, result = _make_session(n=2)
        self.assertEqual(result["code"], "REVIEW_ITEMS_READY")
        self.assertIsInstance(token, str) and self.assertTrue(token)
        self.assertEqual(result["expires_in_seconds"], mr.REVIEW_TOKEN_TTL_SECONDS)
        self.assertEqual(result["stats"], {"count": 2})
        self.assertEqual([it["review_index"] for it in result["items"]], [1, 2])
        for it in result["items"]:
            self.assertEqual(set(it.keys()), _ITEM_RESPONSE_KEYS,
                             "响应条目必须严格等于白名单字段")
            self.assertEqual(it["privacy_hint"], "REVIEW_REQUIRED")

    def test_b_query_only_pending_review_oldest_first(self):
        fake, _, _ = _make_session(n=1)
        self.assertEqual(fake.ops, [("select", "memory_items")],
                         "列表只发一次 SELECT 且仅 memory_items")
        filters = fake.select_filters[0]
        self.assertIn(("eq", "status", "pending_review"), filters)
        self.assertTrue(all(op != "in_" for op, _, _ in filters),
                        "不得用 in_ 同时查 active/rejected")
        self.assertIn(("memory_items", "created_at", False), fake.order_args,
                      "必须按 created_at 升序（最旧优先）")
        self.assertIn(("memory_items", 20), fake.limit_args, "默认只拉 20 条")

    def test_b_limit_default_and_bounds(self):
        for query, expect_ok in [("", True), ("limit=1", True), ("limit=20", True),
                                 ("limit=0", False), ("limit=21", False),
                                 ("limit=abc", False), ("limit=1.5", False),
                                 ("limit=True", False), ("limit=-3", False)]:
            with self.subTest(query=query):
                calls = []

                def _fake_run(sb, limit=None, calls=calls):
                    calls.append(limit)
                    return {"ok": True, "code": "NO_PENDING_REVIEW_ITEMS",
                            "stats": {"count": 0}, "items": []}

                send = FakeSend()
                scope = {"method": "GET", "path": "/api/memory-review",
                         "query_string": query.encode("utf-8")}
                with patch.object(mr, "run_list", side_effect=_fake_run), \
                     patch.object(gateway, "_log", lambda m: None):
                    asyncio.run(gateway.HostFixMiddleware._handle_memory_review(
                        None, scope, FakeReceive(b""), send))
                if expect_ok:
                    self.assertEqual(send.status, 200)
                    self.assertEqual(calls, [20 if query == "" else int(query.split("=")[1])],
                                     "默认 limit=20，显式值透传")
                else:
                    self.assertEqual(send.status, 400)
                    self.assertEqual(send.body_json.get("code"), "INVALID_REVIEW_REQUEST")
                    self.assertEqual(calls, [], "非法 limit 不得触发查询")

    def test_b_forbidden_fields_never_leak(self):
        _, _, result = _make_session(n=1)
        raw = json.dumps(result, ensure_ascii=False)
        for marker in (_USER_MARKER, _HASH_MARKER, _ITEM_UUID, _OTHER_UUID,
                       '"id"', '"user_id"', '"content_hash"', '"metadata"',
                       '"source_event_ids"', '"source_batch_id"',
                       '"superseded_by"', '"created_by"', '"last_confirmed_at"',
                       '"updated_at"', '"status"'):
            self.assertNotIn(marker, raw, f"列表响应不得包含 {marker}")

    def test_b_run_list_clamps_limit_defensively(self):
        fake = ReviewFakeSupabase()
        _run_list(fake, limit=99)
        self.assertIn(("memory_items", 20), fake.limit_args, "越界 limit 夹取到 20")
        fake2 = ReviewFakeSupabase()
        _run_list(fake2, limit=0)
        self.assertIn(("memory_items", 1), fake2.limit_args, "下限夹取到 1")

    def test_b_list_query_failure_sanitized(self):
        fake = ReviewFakeSupabase(list_error=RuntimeError(_DB_SECRET_MARKER))
        result, out = _run_list(fake)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "REVIEW_QUERY_FAILED")
        self.assertNotIn(_DB_SECRET_MARKER, json.dumps(result))
        self.assertNotIn(_DB_SECRET_MARKER, out)


# ==========================================
# C. review session
# ==========================================

class TestReviewSession(_CacheReset):

    def test_c_token_random_and_opaque(self):
        _, t1, _ = _make_session(n=1)
        _, t2, _ = _make_session(n=1)
        self.assertNotEqual(t1, t2, "两次列表 token 必须不同")
        self.assertGreaterEqual(len(t1), 32, "token_urlsafe(32) 长度下限")
        self.assertNotIn(_ITEM_UUID, t1)
        self.assertNotIn(_USER_MARKER, t1)

    def test_c_ttl_expiry(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase(update_count=1)
        # 未过期可用
        ok, _ = _run_decision(fake, token, 1, "reject")
        self.assertTrue(ok["ok"])
        # 手工把 session 改成已过期 → 视为失效
        _, token2, _ = _make_session(n=1)
        mr._review_sessions[token2]["expires_at"] = time.time() - 1
        expired, _ = _run_decision(fake, token2, 1, "reject")
        self.assertEqual(expired["code"], "REVIEW_SESSION_NOT_FOUND_OR_EXPIRED")

    def test_c_capacity_evicts_oldest(self):
        tokens = []
        for _ in range(mr.REVIEW_SESSION_MAX_ENTRIES + 2):
            _, token, _ = _make_session(n=1)
            tokens.append(token)
        self.assertNotIn(tokens[0], mr._review_sessions, "最旧 session 被移除")
        self.assertIn(tokens[-1], mr._review_sessions)

    def test_c_token_never_logged(self):
        _, token, result = _make_session(n=2)
        fake = ReviewFakeSupabase(update_count=1)
        _, out_ok = _run_decision(fake, token, 1, "approve")
        self.assertNotIn(token, out_ok, "日志不得包含 token")
        result_raw = json.dumps(result)
        self.assertIn(token, result_raw)  # 响应本身含 token（设计如此）

    def test_c_single_index_consumed_others_alive(self):
        _, token, _ = _make_session(n=2)
        fake = ReviewFakeSupabase(update_count=1)
        r1, _ = _run_decision(fake, token, 1, "reject")
        self.assertTrue(r1["ok"])
        again, _ = _run_decision(fake, token, 1, "approve")
        self.assertEqual(again["code"], "REVIEW_INDEX_ALREADY_DECIDED")
        self.assertEqual(fake.ops.count(("update", "memory_items")), 1,
                         "已处理 index 不得再次 UPDATE")
        r2, _ = _run_decision(fake, token, 2, "approve")
        self.assertTrue(r2["ok"], "其他未处理 index 仍可用")
        self.assertEqual(r2["result"]["new_status"], "active")

    def test_c_session_consumed_when_all_decided(self):
        _, token, _ = _make_session(n=2)
        fake = ReviewFakeSupabase(update_count=1)
        self.assertTrue(_run_decision(fake, token, 1, "reject")[0]["ok"])
        self.assertTrue(_run_decision(fake, token, 2, "reject")[0]["ok"])
        self.assertNotIn(token, mr._review_sessions,
                         "全部 index 处理完必须消费整个 token")
        after, _ = _run_decision(fake, token, 1, "approve")
        self.assertEqual(after["code"], "REVIEW_SESSION_NOT_FOUND_OR_EXPIRED")

    def test_c_failure_keeps_index_for_retry(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase(update_count=0)  # 0 行 → 状态已变化
        fail, _ = _run_decision(fake, token, 1, "approve")
        self.assertEqual(fail["code"], "REVIEW_ITEM_STATE_CHANGED")
        self.assertIn(token, mr._review_sessions, "失败不消费 index")
        # 恢复为 1 行后同 index 重试成功
        fake.update_count = 1
        retry, _ = _run_decision(fake, token, 1, "approve")
        self.assertTrue(retry["ok"], "失败后未处理 index 可安全重试")

    def test_c_index_out_of_range(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase()
        for idx in (0, 5, -1):
            with self.subTest(index=idx):
                res, _ = _run_decision(fake, token, idx, "approve")
                self.assertEqual(res["code"], "REVIEW_INDEX_NOT_FOUND")
        self.assertEqual(fake.ops, [], "越界 index 不产生任何数据库操作")

    def test_c_module_defends_against_bad_types(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase()
        for idx in (True, "1", 1.0, None):
            with self.subTest(index=idx):
                res, _ = _run_decision(fake, token, idx, "approve")
                self.assertEqual(res["code"], "INVALID_REVIEW_REQUEST",
                                 "模块层防御：index 必须 int 且非 bool")
        bad_tok, _ = _run_decision(fake, "", 1, "approve")
        self.assertEqual(bad_tok["code"], "REVIEW_SESSION_NOT_FOUND_OR_EXPIRED")
        bad_dec, _ = _run_decision(fake, token, 1, "APPROVE")
        self.assertEqual(bad_dec["code"], "INVALID_DECISION")


# ==========================================
# D. 请求校验（handler 层）
# ==========================================

class TestDecisionValidation(_CacheReset):

    def test_d_wrong_confirm(self):
        send, captured = _post_decision(_valid_body(confirm="WRONG"))
        self.assertEqual(send.status, 400)
        self.assertEqual(send.body_json.get("code"), "INVALID_CONFIRMATION")
        self.assertEqual(captured, [])

    def test_d_missing_or_empty_token(self):
        for tok in (None, "", 123):
            with self.subTest(token=tok):
                send, captured = _post_decision(_valid_body(review_session_token=tok))
                self.assertEqual(send.status, 400)
                self.assertEqual(captured, [])

    def test_d_bad_index_type(self):
        for idx in (True, False, "1", 1.5, None, [1]):
            with self.subTest(index=idx):
                send, captured = _post_decision(_valid_body(review_index=idx))
                self.assertEqual(send.status, 400)
                self.assertEqual(captured, [])

    def test_d_bad_decision(self):
        for dec in ("APPROVE", "Reject", "yes", "", 1, None):
            with self.subTest(decision=dec):
                send, captured = _post_decision(_valid_body(decision=dec))
                self.assertEqual(send.status, 400)
                self.assertEqual(send.body_json.get("code"), "INVALID_DECISION")
                self.assertEqual(captured, [])

    def test_d_extra_fields_rejected(self):
        extra = {
            "item_id": _ITEM_UUID, "content": "改写正文", "status": "active",
            "user_id": _USER_MARKER, "importance": 9, "confidence": 1.0,
            "subject_key": "hack", "memory_type": "core", "valid_at": "x",
            "expires_at": "x", "metadata": {}, "active": True, "reason": "r",
            "comment": "c", "reviewed_all": True,
        }
        for k, v in extra.items():
            with self.subTest(field=k):
                send, captured = _post_decision(_valid_body(**{k: v}))
                self.assertEqual(send.status, 400, f"额外字段 {k} 必须 400")
                self.assertEqual(send.body_json.get("code"), "INVALID_REVIEW_REQUEST")
                self.assertEqual(captured, [])

    def test_d_invalid_json_and_non_dict(self):
        for raw in (b"not json", b"[1,2]", b"null", b'"str"'):
            with self.subTest(raw=raw):
                send, captured = _post_decision(raw)
                self.assertEqual(send.status, 400)
                self.assertEqual(captured, [])


# ==========================================
# E. approve 语义
# ==========================================

class TestApproveSemantics(_CacheReset):

    def test_e_approve_success_payload_and_filters(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase(update_count=1)
        result, _ = _run_decision(fake, token, 1, "approve")
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "MEMORY_APPROVED")
        self.assertEqual(result["result"], {"review_index": 1, "new_status": "active"})

        self.assertEqual(len(fake.update_payloads), 1)
        payload = fake.update_payloads[0]
        self.assertEqual(set(payload.keys()),
                         {"status", "updated_at", "last_confirmed_at"},
                         "approve 只更新 status/updated_at/last_confirmed_at")
        self.assertEqual(payload["status"], "active")
        for f in ("updated_at", "last_confirmed_at"):
            self.assertIn("+00:00", payload[f], f"{f} 必须为 UTC ISO 时间")

        filters = fake.update_filters[0]
        self.assertIn(("eq", "id", _ITEM_UUID), filters)
        self.assertIn(("eq", "status", "pending_review"), filters,
                      "乐观条件必须包含 status=pending_review")

    def test_e_zero_rows_state_changed(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase(update_count=0)
        result, _ = _run_decision(fake, token, 1, "approve")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "REVIEW_ITEM_STATE_CHANGED")
        self.assertIn(token, mr._review_sessions, "0 行不消费 token")
        self.assertIn(1, mr._review_sessions[token]["items"], "0 行不消费 index")

    def test_e_multi_rows_also_state_changed(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase(update_count=2)  # 防御：行数必须恰为 1
        result, _ = _run_decision(fake, token, 1, "approve")
        self.assertEqual(result["code"], "REVIEW_ITEM_STATE_CHANGED")

    def test_e_update_error_sanitized(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase(update_error=RuntimeError(_DB_SECRET_MARKER))
        result, out = _run_decision(fake, token, 1, "approve")
        self.assertEqual(result["code"], "REVIEW_UPDATE_FAILED")
        self.assertNotIn(_DB_SECRET_MARKER, json.dumps(result))
        self.assertNotIn(_DB_SECRET_MARKER, out)
        self.assertNotIn("RuntimeError", json.dumps(result))
        self.assertIn("RuntimeError", out, "日志只允许异常类型名")

    def test_e_only_allowed_columns_touched(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase(update_count=1)
        result, _ = _run_decision(fake, token, 1, "approve")
        payload = fake.update_payloads[0]
        untouched = ("content", "memory_type", "content_hash", "importance",
                     "confidence", "source", "source_event_ids", "source_batch_id",
                     "subject_key", "valid_at", "invalid_at", "expires_at",
                     "metadata", "created_by", "superseded_by")
        for col in untouched:
            self.assertNotIn(col, payload, f"approve 不得修改 {col}")

    def test_e_service_unavailable(self):
        result, _ = _run_decision(None, "tok", 1, "approve")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "REVIEW_UPDATE_FAILED")


# ==========================================
# F. reject 语义
# ==========================================

class TestRejectSemantics(_CacheReset):

    def test_f_reject_success_minimal_payload(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase(update_count=1)
        result, _ = _run_decision(fake, token, 1, "reject")
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "MEMORY_REJECTED")
        self.assertEqual(result["result"], {"review_index": 1, "new_status": "rejected"})

        payload = fake.update_payloads[0]
        self.assertEqual(set(payload.keys()), {"status", "updated_at"},
                         "reject 只更新 status/updated_at")
        self.assertNotIn("last_confirmed_at", payload, "不动 last_confirmed_at")
        self.assertNotIn("invalid_at", payload, "不动 invalid_at")
        self.assertEqual(payload["status"], "rejected")

        filters = fake.update_filters[0]
        self.assertIn(("eq", "status", "pending_review"), filters)

    def test_f_reject_never_deletes(self):
        _, token, _ = _make_session(n=2)
        fake = ReviewFakeSupabase(update_count=1)
        self.assertTrue(_run_decision(fake, token, 1, "reject")[0]["ok"])
        self.assertTrue(_run_decision(fake, token, 2, "reject")[0]["ok"])
        self.assertEqual(fake.forbidden_ops, [], "不得出现 delete/upsert/rpc")
        self.assertEqual([op for op in fake.ops if op[0] != "select" and op[0] != "update"],
                         [], "只有 select/update 两种操作")

    def test_f_reject_zero_rows(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase(update_count=0)
        result, _ = _run_decision(fake, token, 1, "reject")
        self.assertEqual(result["code"], "REVIEW_ITEM_STATE_CHANGED")
        self.assertIn(1, mr._review_sessions[token]["items"])

    def test_f_reject_skips_conflict_checks(self):
        _, token, _ = _make_session(n=1, subject_key="mood")
        fake = ReviewFakeSupabase(update_count=1,
                                  subject_active_rows=[{"id": _OTHER_UUID}],
                                  hash_active_rows=[{"id": _OTHER_UUID}])
        result, _ = _run_decision(fake, token, 1, "reject")
        self.assertTrue(result["ok"], "reject 不做 approve 专属冲突检查")
        selects = [f for f in fake.select_filters
                   if any(c in ("subject_key", "content_hash") for _, c, _ in f)]
        self.assertEqual(selects, [], "reject 不得发起冲突查询")


# ==========================================
# G. 冲突保护（approve）
# ==========================================

class TestConflictProtection(_CacheReset):

    def test_g_active_subject_conflict(self):
        _, token, _ = _make_session(n=1, subject_key="exercise_habit")
        fake = ReviewFakeSupabase(update_count=1,
                                  subject_active_rows=[{"id": _OTHER_UUID}])
        result, _ = _run_decision(fake, token, 1, "approve")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ACTIVE_SUBJECT_CONFLICT")
        self.assertEqual([op for op in fake.ops if op[0] == "update"], [],
                         "冲突时不得 UPDATE")
        self.assertIn(1, mr._review_sessions[token]["items"],
                      "冲突不消费 index")
        self.assertEqual(fake.forbidden_ops, [])

    def test_g_subject_conflict_filters(self):
        _, token, _ = _make_session(n=1, subject_key="exercise_habit")
        fake = ReviewFakeSupabase(subject_active_rows=[])
        self.assertTrue(_run_decision(fake, token, 1, "approve")[0]["ok"])
        subject_selects = [f for f in fake.select_filters
                           if any(c == "subject_key" for _, c, _ in f)]
        self.assertEqual(len(subject_selects), 1)
        f = subject_selects[0]
        self.assertIn(("eq", "user_id", _USER_MARKER), f)
        self.assertIn(("eq", "subject_key", "exercise_habit"), f)
        self.assertIn(("eq", "status", "active"), f)
        self.assertIn(("neq", "id", _ITEM_UUID), f, "冲突检查必须排除自身")

    def test_g_active_exact_duplicate(self):
        _, token, _ = _make_session(n=1, subject_key=None)
        fake = ReviewFakeSupabase(update_count=1,
                                  hash_active_rows=[{"id": _OTHER_UUID}])
        result, _ = _run_decision(fake, token, 1, "approve")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ACTIVE_EXACT_DUPLICATE")
        self.assertEqual([op for op in fake.ops if op[0] == "update"], [])
        self.assertNotIn('"rejected"', json.dumps(result),
                         "不得自动把当前项标 rejected")

    def test_g_null_subject_skips_subject_check(self):
        _, token, _ = _make_session(n=1, subject_key=None)
        fake = ReviewFakeSupabase(update_count=1)
        result, _ = _run_decision(fake, token, 1, "approve")
        self.assertTrue(result["ok"], "subject_key 为空时仍可人工 approve")
        subject_selects = [f for f in fake.select_filters
                           if any(c == "subject_key" for _, c, _ in f)]
        self.assertEqual(subject_selects, [], "subject_key 为空不做主题冲突检查")

    def test_g_conflict_response_hides_old_item(self):
        _, token, _ = _make_session(n=1, subject_key="exercise_habit")
        fake = ReviewFakeSupabase(subject_active_rows=[{"id": _OTHER_UUID,
                                                        "content": "旧事实正文"}])
        result, out = _run_decision(fake, token, 1, "approve")
        raw = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(_OTHER_UUID, raw, "冲突响应不得返回旧 item ID")
        self.assertNotIn("旧事实正文", raw, "冲突响应不得返回旧 item 正文")
        self.assertNotIn("旧事实正文", out)

    def test_g_conflict_query_error(self):
        _, token, _ = _make_session(n=1, subject_key="exercise_habit")
        fake = ReviewFakeSupabase(conflict_error=RuntimeError(_DB_SECRET_MARKER))
        result, out = _run_decision(fake, token, 1, "approve")
        self.assertEqual(result["code"], "REVIEW_QUERY_FAILED")
        self.assertNotIn(_DB_SECRET_MARKER, json.dumps(result))
        self.assertNotIn(_DB_SECRET_MARKER, out)


# ==========================================
# H. 隐私控制
# ==========================================

class TestPrivacyControl(_CacheReset):

    def test_h_medical_candidate_listed_never_auto_decided(self):
        medical = _mk_row(1, content="用户在合成测试中提到服用某处方药物。")
        fake = ReviewFakeSupabase(list_rows=[medical])
        result, _ = _run_list(fake)
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "REVIEW_ITEMS_READY")
        self.assertEqual([op for op in fake.ops if op[0] == "update"], [],
                         "列表展示绝不触发自动批准/拒绝")
        self.assertEqual(fake.forbidden_ops, [])
        # 医疗候选也只得到固定隐私提示，不做自动分类
        self.assertEqual(result["items"][0]["privacy_hint"], "REVIEW_REQUIRED")

    def test_h_decision_must_be_explicit_no_default(self):
        send, captured = _post_decision(_valid_body(decision=None))
        self.assertEqual(send.status, 400, "缺少 decision 不得默认 approve/reject")
        self.assertEqual(captured, [])

    def test_h_single_item_per_call_no_batch(self):
        _, token, _ = _make_session(n=2)
        fake = ReviewFakeSupabase(update_count=1)
        # 每次调用只允许一个 review_index（int），不存在批量列表入参
        r1, _ = _run_decision(fake, token, 1, "approve")
        r2, _ = _run_decision(fake, token, 2, "approve")
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertEqual(len(fake.update_payloads), 2, "两次调用各更新一条")
        for p in fake.update_payloads:
            self.assertEqual(p["status"], "active")


# ==========================================
# I. 响应和日志脱敏
# ==========================================

class TestSanitization(_CacheReset):

    def test_i_decision_response_no_sensitive_data(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase(update_count=1)
        result, out = _run_decision(fake, token, 1, "approve")
        raw = json.dumps(result, ensure_ascii=False)
        for marker in (token, _ITEM_UUID, _USER_MARKER, _HASH_MARKER,
                       "合成测试", "慢跑", _DB_SECRET_MARKER,
                       '"content"', '"user_id"', '"content_hash"',
                       "review_session_token", "SQL"):
            self.assertNotIn(marker, raw, f"decision 响应不得包含 {marker}")
        self.assertNotIn(token, out, "日志不得包含 token")
        self.assertNotIn(_USER_MARKER, out)
        self.assertNotIn("慢跑", out, "日志不得包含候选正文")

    def test_i_error_response_shape(self):
        _, token, _ = _make_session(n=1)
        fake = ReviewFakeSupabase(update_error=RuntimeError(_DB_SECRET_MARKER))
        result, _ = _run_decision(fake, token, 1, "approve")
        self.assertEqual(set(result.keys()), {"ok", "code", "stats"},
                         "错误响应只含 ok/code/stats")
        self.assertEqual(result["stats"], {})

    def test_i_list_stdout_counts_only(self):
        _, _, result = _make_session(n=2)
        fake = ReviewFakeSupabase(update_count=1)
        _, out = _run_decision(fake, result["review_session_token"], 1, "reject")
        self.assertNotIn(_HASH_MARKER, out)
        self.assertNotIn(_ITEM_UUID, out)


# ==========================================
# J. 隔离与零删除（源码断言）
# ==========================================

class TestSourceIsolation(unittest.TestCase):

    def _src(self, name):
        with io.open(name, encoding="utf-8") as f:
            return f.read()

    def test_j_memory_review_no_forbidden_operations(self):
        src = self._src("memory_review.py").lower()
        for pat in (".delete(", "upsert(", ".rpc(", "truncate(",
                    "create_client", "create_task", "threading", "timer(",
                    "os.environ", "getenv", "openai", ".execute()  # noexecute"):
            self.assertNotIn(pat, src, f"memory_review.py 不得出现 {pat!r}")

    def test_j_memory_review_imports_minimal(self):
        import ast
        tree = ast.parse(self._src("memory_review.py"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"asyncio", "datetime", "secrets", "time"},
                         "模块只允许标准库四项 import——无 LLM / Pinecone / 事件 / 调度")

    def test_j_memory_review_select_update_only(self):
        src = self._src("memory_review.py")
        self.assertIn('.select(', src)
        self.assertIn('.update(', src)
        self.assertNotIn(".insert(", src)

    def test_j_gateway_routes_exact(self):
        src = self._src("gateway.py")
        self.assertEqual(src.count('"/api/memory-review"'), 1, "列表路由恰好一处")
        self.assertEqual(src.count('"/api/memory-review/decision"'), 1,
                         "决策路由恰好一处")

    def test_j_no_delete_in_tested_module_ast(self):
        import ast
        tree = ast.parse(self._src("memory_review.py"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(node.func.attr, "delete", "不得调用 delete")
                self.assertNotEqual(node.func.attr, "upsert", "不得调用 upsert")
                self.assertNotEqual(node.func.attr, "rpc", "不得调用 rpc")

    def test_j_context_injection_untouched(self):
        src = self._src("gateway.py")
        start = src.find("async def _inject_context")
        self.assertGreaterEqual(start, 0)
        # 只截取 _inject_context 函数体（到下一个同缩进函数定义为止），
        # 避免把文件后段的审批 handler 注释误算进上下文注入代码
        end = src.find("\n    async def ", start + 10)
        inj = src[start:end]
        self.assertNotIn("memory_items", inj, "_inject_context 不得读取 memory_items")
        self.assertNotIn("pending_review", inj, "_inject_context 不得读取 pending_review")
        self.assertNotIn("memory_review", inj, "_inject_context 不得调用审批模块")


if __name__ == "__main__":
    unittest.main()
