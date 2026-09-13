# -*- coding: utf-8 -*-
"""阶段 C2 专项测试 —— memory_events 原始事件安全清理（两步人工确认）。

全部 unittest + mock + 合成数据（SYNTHETIC_*），不连接 Supabase。

覆盖：
  A. preview 只读零删除：COUNT + status/channel 分组 + 最旧/最新时间 + token 签发
  B. 参数越界拒绝（older_than_days 范围 1~90）；零结果不签发 token
  C. commit 白名单删除：只删 processed/failed 且早于阈值；pending/processing
     不删；除 memory_events 外不触碰任何表
  D. token 一次性消费（防重放）；未知 token 404 语义
  E. COUNT 一致性核对：漂移 >20% 中止不删且 token 失效
  F. gateway handler：字段白名单（额外 user_id/status/all → 400）、confirm
     字面量、405、路由注册、环境变量默认阈值
运行：  python -m unittest test_memory_cleanup_phaseC2 -v
"""

import asyncio
import datetime
import inspect
import json
import os
import unittest
from unittest.mock import patch

import gateway
import memory_cleanup
import server


# ==========================================
# 有状态假 Supabase 客户端（memory_events 专用）
# ==========================================

class FakeResult:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Q:
    def __init__(self, owner, table):
        self._owner = owner
        self._table = table
        self._path = []

    def _rec(self, method, *a, **k):
        self._path.append((method, a, k))
        return self

    def select(self, *a, **k): return self._rec("select", *a, **k)
    def in_(self, *a, **k): return self._rec("in_", *a, **k)
    def lt(self, *a, **k): return self._rec("lt", *a, **k)
    def order(self, *a, **k): return self._rec("order", *a, **k)
    def limit(self, *a, **k): return self._rec("limit", *a, **k)
    def delete(self, *a, **k): return self._rec("delete", *a, **k)

    def execute(self):
        self._owner.calls.append((self._table, list(self._path)))
        return self._owner._dispatch(self._table, self._path)


class _FakeCleanupService:
    """memory_events 有状态假客户端：in_/lt 过滤真实生效，delete 真删。"""

    def __init__(self, events=()):
        self.events = list(events)
        self.calls = []
        self.tables_used = set()
        self.delete_fail = False
        self.count_override = None   # 模拟 preview→commit 之间的计数漂移

    def table(self, name):
        self.tables_used.add(name)
        return _Q(self, name)

    def _matching(self, path):
        rows = list(self.events)
        for m, a, k in path:
            if m == "in_":
                rows = [r for r in rows if r.get(a[0]) in set(a[1])]
            elif m == "lt":
                rows = [r for r in rows if str(r.get(a[0])) < str(a[1])]
        return rows

    def _dispatch(self, table, path):
        method = path[0][0] if path else ""
        if method == "select":
            want_count = any(k.get("count") == "exact" for m, a, k in path)
            rows = self._matching(path)
            total = self.count_override if self.count_override is not None else len(rows)
            lim = next((a[0] for m, a, k in path if m == "limit"), None)
            if lim is not None:
                rows = rows[:lim]
            if any(m == "order" for m, a, k in path):
                desc = next((k.get("desc", False) for m, a, k in path
                             if m == "order"), False)
                rows = sorted(rows, key=lambda r: str(r.get("created_at")),
                              reverse=desc)
            return FakeResult([dict(r) for r in rows], count=total)
        if method == "delete":
            if self.delete_fail:
                raise RuntimeError("mock delete failure")
            rows = self._matching(path)
            deleted_ids = {r["id"] for r in rows}
            self.events = [r for r in self.events if r["id"] not in deleted_ids]
            return FakeResult([dict(r) for r in rows], count=len(rows))
        raise AssertionError(f"未预期的操作: {method}")


def _ev(i, status, days_ago, channel="web"):
    ts = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(days=days_ago)).isoformat()
    return {"id": f"ev-{i:03d}", "processing_status": status,
            "channel": channel, "created_at": ts}


def _std_events():
    """6 processed 旧 + 2 failed 旧 + 2 pending 旧 + 1 processing 旧 + 1 processed 新。"""
    events = []
    n = 0
    for _ in range(6):
        events.append(_ev(n, "processed", 10, "web")); n += 1
    for _ in range(2):
        events.append(_ev(n, "failed", 12, "qq")); n += 1
    for _ in range(2):
        events.append(_ev(n, "pending", 15, "tg")); n += 1
    events.append(_ev(n, "processing", 9, "web")); n += 1
    events.append(_ev(n, "processed", 3, "web")); n += 1
    return events


def setUpModule():
    memory_cleanup._token_cache.clear()
    memory_cleanup._used_tokens.clear()


# ==========================================
# A+B. preview
# ==========================================

class TestPreview(unittest.TestCase):

    def setUp(self):
        memory_cleanup._token_cache.clear()
        memory_cleanup._used_tokens.clear()

    def test_preview_counts_and_readonly(self):
        svc = _FakeCleanupService(_std_events())
        result = asyncio.run(memory_cleanup.run_preview(svc, older_than_days=7))

        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "CLEANUP_PREVIEW_READY")
        stats = result["stats"]
        self.assertEqual(stats["total"], 8, "只统计 processed/failed 且早于阈值")
        self.assertEqual(stats["by_status"], {"processed": 6, "failed": 2})
        self.assertEqual(stats["by_channel"], {"web": 6, "qq": 2})
        self.assertTrue(stats["threshold_iso"])
        self.assertFalse(stats["writes_executed"])
        self.assertTrue(result.get("cleanup_token"))
        self.assertEqual(result["expires_in_seconds"], 900)
        # 零删除、零写入：全程只有 select，且只碰 memory_events
        self.assertEqual(svc.tables_used, {"memory_events"})
        for table, path in svc.calls:
            self.assertEqual(path[0][0], "select", "preview 不得发生 delete")

    def test_preview_invalid_days_rejected(self):
        svc = _FakeCleanupService(_std_events())
        for bad in (0, 91, -1, True, "7", 2.5, None):
            result = asyncio.run(memory_cleanup.run_preview(svc, older_than_days=bad))
            self.assertEqual(result["code"], "INVALID_CLEANUP_REQUEST", msg=repr(bad))
        self.assertEqual(svc.calls, [], "参数非法时零查询")

    def test_preview_zero_rows_no_token(self):
        svc = _FakeCleanupService([])
        result = asyncio.run(memory_cleanup.run_preview(svc, older_than_days=7))
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "CLEANUP_NOTHING_TO_DELETE")
        self.assertNotIn("cleanup_token", result, "无内容可清不签发 token")

    def test_preview_service_missing(self):
        result = asyncio.run(memory_cleanup.run_preview(None, older_than_days=7))
        self.assertEqual(result["code"], "SERVICE_UNAVAILABLE")


# ==========================================
# C+D+E. commit
# ==========================================

class TestCommit(unittest.TestCase):

    def setUp(self):
        memory_cleanup._token_cache.clear()
        memory_cleanup._used_tokens.clear()

    def _preview(self, svc):
        return asyncio.run(memory_cleanup.run_preview(svc, older_than_days=7))

    def test_commit_deletes_whitelist_only(self):
        svc = _FakeCleanupService(_std_events())
        preview = self._preview(svc)
        result = asyncio.run(memory_cleanup.run_commit(svc, preview["cleanup_token"]))

        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "CLEANUP_COMPLETED")
        self.assertEqual(result["stats"]["deleted"], 8)
        remain_status = {r["processing_status"] for r in svc.events}
        self.assertEqual(remain_status, {"pending", "processing", "processed"},
                         "只删阈值外的 processed/failed；阈值内的 processed 与"
                         " pending/processing 全部保留")
        self.assertTrue(all(r["created_at"] >= result["stats"]["threshold_iso"]
                            or r["processing_status"] in ("pending", "processing")
                            for r in svc.events), "阈值之内的事件不被删")
        self.assertEqual(svc.tables_used, {"memory_events"},
                         "除 memory_events 外不触碰任何表")

    def test_commit_token_one_shot(self):
        svc = _FakeCleanupService(_std_events())
        preview = self._preview(svc)
        first = asyncio.run(memory_cleanup.run_commit(svc, preview["cleanup_token"]))
        self.assertTrue(first["ok"])
        second = asyncio.run(memory_cleanup.run_commit(svc, preview["cleanup_token"]))
        self.assertEqual(second["code"], "CLEANUP_TOKEN_ALREADY_USED", "token 防重放")

    def test_commit_unknown_token(self):
        svc = _FakeCleanupService(_std_events())
        result = asyncio.run(memory_cleanup.run_commit(svc, "not-a-token"))
        self.assertEqual(result["code"], "CLEANUP_TOKEN_NOT_FOUND_OR_EXPIRED")
        self.assertEqual(svc.calls, [])

    def test_commit_count_drift_aborts(self):
        svc = _FakeCleanupService(_std_events())
        preview = self._preview(svc)
        svc.count_override = 3  # 与 preview 的 8 相比漂移 >20%
        result = asyncio.run(memory_cleanup.run_commit(svc, preview["cleanup_token"]))

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CLEANUP_COUNT_DRIFT")
        self.assertEqual(result["stats"]["preview_count"], 8)
        self.assertEqual(result["stats"]["current_count"], 3)
        self.assertEqual(len(svc.events), 12, "漂移时一条都不删")
        retry = asyncio.run(memory_cleanup.run_commit(svc, preview["cleanup_token"]))
        self.assertEqual(retry["code"], "CLEANUP_TOKEN_ALREADY_USED",
                         "漂移中止后 token 已失效，必须重新 preview")

    def test_commit_zero_current_is_drift(self):
        """preview→commit 之间被清空（current=0）属最大漂移：中止并要求重新 preview。"""
        svc = _FakeCleanupService(_std_events())
        preview = self._preview(svc)
        svc.count_override = 0
        result = asyncio.run(memory_cleanup.run_commit(svc, preview["cleanup_token"]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CLEANUP_COUNT_DRIFT")
        self.assertEqual(len(svc.events), 12, "漂移时一条都不删")

    def test_delete_failure_no_partial_state(self):
        svc = _FakeCleanupService(_std_events())
        svc.delete_fail = True
        preview = self._preview(svc)
        result = asyncio.run(memory_cleanup.run_commit(svc, preview["cleanup_token"]))
        self.assertEqual(result["code"], "CLEANUP_DELETE_FAILED")
        self.assertEqual(len(svc.events), 12, "删除失败不产生部分状态")
        retry = asyncio.run(memory_cleanup.run_commit(svc, preview["cleanup_token"]))
        self.assertEqual(retry["code"], "CLEANUP_TOKEN_ALREADY_USED")


# ==========================================
# F. gateway handler
# ==========================================

def _call_handler(name, payload=None, method="POST"):
    sent = []
    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else b""
    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}
    scope = {"type": "http", "method": method, "path": "/api/x",
             "headers": [], "query_string": b""}
    async def send(msg):
        sent.append(msg)
    asyncio.run(getattr(gateway.HostFixMiddleware, name)(None, scope, receive, send))
    status = sent[0]["status"] if sent else None
    body = json.loads(sent[1]["body"].decode("utf-8")) if len(sent) > 1 else None
    return status, body


class TestGatewayHandlers(unittest.TestCase):

    def setUp(self):
        memory_cleanup._token_cache.clear()
        memory_cleanup._used_tokens.clear()

    def test_preview_extra_field_400(self):
        status, body = _call_handler(
            "_handle_memory_events_cleanup_preview",
            {"confirm": "CLEANUP_PREVIEW_ONLY", "older_than_days": 7,
             "user_id": "attacker"})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "INVALID_CLEANUP_REQUEST")

    def test_preview_wrong_confirm_400(self):
        status, body = _call_handler(
            "_handle_memory_events_cleanup_preview",
            {"confirm": "PREVIEW_ONLY"})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "INVALID_CONFIRMATION")

    def test_commit_extra_field_400(self):
        """user_id/status/all 等额外字段一律 400，删除范围不可被请求放大。"""
        for extra in ({"status": "all"}, {"user_id": "x"}, {"all": True}):
            payload = {"confirm": "CLEANUP_EXECUTE", "cleanup_token": "t"}
            payload.update(extra)
            status, body = _call_handler(
                "_handle_memory_events_cleanup_commit", payload)
            self.assertEqual(status, 400, msg=str(extra))
            self.assertEqual(body["code"], "INVALID_CLEANUP_REQUEST")

    def test_get_method_405(self):
        status, _ = _call_handler("_handle_memory_events_cleanup_preview",
                                  None, method="GET")
        self.assertEqual(status, 405)

    def test_preview_happy_path_via_handler(self):
        svc = _FakeCleanupService(_std_events())
        with patch.object(server, "supabase_service", svc):
            status, body = _call_handler(
                "_handle_memory_events_cleanup_preview",
                {"confirm": "CLEANUP_PREVIEW_ONLY", "older_than_days": 7})
        self.assertEqual(status, 200)
        self.assertEqual(body["code"], "CLEANUP_PREVIEW_READY")
        self.assertEqual(body["stats"]["total"], 8)
        self.assertTrue(body.get("cleanup_token"))

    def test_env_default_days(self):
        svc = _FakeCleanupService(_std_events())
        env = {k: v for k, v in os.environ.items()
               if k != "MEMORY_CLEANUP_OLDER_THAN_DAYS"}
        env["MEMORY_CLEANUP_OLDER_THAN_DAYS"] = "3"
        with patch.dict(os.environ, env, clear=True), \
             patch.object(server, "supabase_service", svc):
            status, body = _call_handler(
                "_handle_memory_events_cleanup_preview",
                {"confirm": "CLEANUP_PREVIEW_ONLY"})
        self.assertEqual(status, 200)
        self.assertEqual(body["stats"]["older_than_days"], 3,
                         "接口未传参数时读环境变量默认阈值")

    def test_routes_registered(self):
        """两个清理端点已注册进 /api/* 分发（受统一鉴权覆盖）。"""
        src = inspect.getsource(gateway.HostFixMiddleware.__call__)
        self.assertIn("/api/memory-events-cleanup-preview", src)
        self.assertIn("/api/memory-events-cleanup-commit", src)


if __name__ == "__main__":
    unittest.main()
