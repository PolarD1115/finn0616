# -*- coding: utf-8 -*-
"""阶段 D3 专项测试 —— 行动日志/秘密日记 → 分层记忆桥接。

全 mock，不触真实 Supabase / LLM / Pinecone / 渠道。

覆盖：
  A. 活动日志成功 finalize → 异步提取 moment/long_term
  B. 秘密日记写入 → 强制 moment + source=private_diary
  C. 隐私过滤：search_memory 不返回秘密日记原文
  D. moment 可进 hybrid recall 白名单（无类型排除）
  E. 门控关闭零行为；秘密日记活动不走 activity 桥（防双写）
  F. 日志/响应不含秘密日记正文

运行：  python -m unittest test_memory_diary_bridge_phaseD3 -v
"""

import asyncio
import contextlib
import hashlib
import io
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import memory_diary_bridge as mdb


SECRET_MARKER = "SYNTHETIC_PRIVATE_DIARY_BODY_NEVER_LEAK_D3"
ACTIVITY_MARKER = "SYNTHETIC_ACTIVITY_RESULT_D3"


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeItemsService:
    def __init__(self):
        self.items = []
        self.calls = []

    def table(self, name):
        assert name == "memory_items"
        return _Q(self)

    def _dispatch(self, path):
        method = path[0][0]
        if method == "select":
            eqs = [(a[0], a[1]) for m, a, _ in path if m == "eq"]
            ins = [(a[0], list(a[1])) for m, a, _ in path if m == "in_"]
            rows = list(self.items)
            for col, val in eqs:
                rows = [r for r in rows if r.get(col) == val]
            for col, vals in ins:
                rows = [r for r in rows if r.get(col) in set(vals)]
            return FakeResult([dict(r) for r in rows])
        if method == "insert":
            row = dict(path[0][1][0])
            self.items.append(row)
            return FakeResult([dict(row)])
        raise AssertionError(method)


class _Q:
    def __init__(self, owner):
        self._owner = owner
        self._path = []

    def _rec(self, method, *a, **k):
        self._path.append((method, a, k))
        return self

    def select(self, *a, **k): return self._rec("select", *a, **k)
    def insert(self, *a, **k): return self._rec("insert", *a, **k)
    def eq(self, *a, **k): return self._rec("eq", *a, **k)
    def in_(self, *a, **k): return self._rec("in_", *a, **k)

    def execute(self):
        self._owner.calls.append(list(self._path))
        return self._owner._dispatch(self._path)


def _llm_moment(content, memory_type="moment"):
    payload = {
        "memories": [{
            "memory_type": memory_type,
            "content": content,
            "importance": 6,
            "confidence": 0.9,
            "valid_at": None,
            "invalid_at": None,
            "expires_at": None,
            "source_event_indexes": [0],
            "subject_key": None,
            "reason": "test",
        }]
    }
    raw = json.dumps(payload, ensure_ascii=False)

    def llm(_prompt):
        return raw
    return llm


def _run(coro):
    return asyncio.run(coro)


class TestDiaryBridgePhaseD3(unittest.TestCase):

    def setUp(self):
        os.environ["DIARY_MEMORY_BRIDGE_ENABLED"] = "true"

    def tearDown(self):
        os.environ.pop("DIARY_MEMORY_BRIDGE_ENABLED", None)

    def test_a_temp_event_role_and_adapt(self):
        ev = mdb.build_temp_event(content="hello", user_id="u1",
                                  channel=mdb.SOURCE_ACTIVITY)
        self.assertEqual(ev["role"], "event")
        adapted = mdb.to_extractable_events([ev])
        self.assertEqual(adapted[0]["role"], "user")
        self.assertEqual(adapted[0]["channel"], mdb.SOURCE_ACTIVITY)

    def test_b_activity_bridge_writes_moment(self):
        fake = FakeItemsService()
        content = f"用户喜欢在周末散步。含标记{ACTIVITY_MARKER}"
        r = _run(mdb.bridge_activity_log(
            activity_id="free:walk", activity_name="散步",
            status="succeeded", result_summary=f"今天去公园散步 {ACTIVITY_MARKER}",
            thought_summary="想出去走走",
            llm_call=_llm_moment(content, "moment"),
            supabase_service=fake, user_id="u-d3"))
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["inserted"], 1)
        self.assertEqual(fake.items[0]["source"], mdb.SOURCE_ACTIVITY)
        self.assertIn(fake.items[0]["memory_type"], ("moment", "long_term"))

    def test_c_private_diary_force_moment_and_source(self):
        fake = FakeItemsService()
        content = f"今天心情不错，想起一起看过的电影。{SECRET_MARKER}"
        r = _run(mdb.bridge_private_diary(
            title="夜记", content=SECRET_MARKER, mood="平静",
            action_key="diary_test_key",
            llm_call=_llm_moment(content, "long_term"),  # 强制覆盖为 moment
            supabase_service=fake, user_id="u-d3"))
        self.assertTrue(r["ok"])
        self.assertEqual(fake.items[0]["memory_type"], "moment")
        self.assertEqual(fake.items[0]["source"], mdb.SOURCE_PRIVATE_DIARY)
        self.assertTrue(fake.items[0]["metadata"].get("private"))

    def test_d_gate_off_zero_behavior(self):
        os.environ["DIARY_MEMORY_BRIDGE_ENABLED"] = "false"
        fake = FakeItemsService()
        called = {"n": 0}

        def llm(_p):
            called["n"] += 1
            return "{}"

        r = _run(mdb.bridge_activity_log(
            activity_name="x", status="succeeded", result_summary="y",
            llm_call=llm, supabase_service=fake, user_id="u"))
        self.assertEqual(r["error_code"], "DISABLED")
        self.assertEqual(called["n"], 0)
        self.assertEqual(len(fake.items), 0)

    def test_e_secret_diary_activity_skipped_on_activity_bridge(self):
        fake = FakeItemsService()
        r = _run(mdb.bridge_activity_log(
            activity_id="free:secret_diary", activity_name="写秘密日记",
            status="succeeded", result_summary=SECRET_MARKER,
            llm_call=_llm_moment(SECRET_MARKER),
            supabase_service=fake, user_id="u"))
        self.assertEqual(r["error_code"], "SECRET_DIARY_SKIPPED")
        self.assertEqual(len(fake.items), 0)

    def test_f_failed_status_skipped(self):
        r = _run(mdb.bridge_activity_log(
            activity_name="x", status="failed", result_summary="err",
            llm_call=_llm_moment("x"), supabase_service=FakeItemsService(),
            user_id="u"))
        self.assertEqual(r["error_code"], "STATUS_SKIPPED")

    def test_g_is_private_diary_helper(self):
        self.assertTrue(mdb.is_private_diary_memory_item(
            {"source": "private_diary", "content": SECRET_MARKER}))
        self.assertTrue(mdb.is_private_diary_memory_item(
            {"source": "x", "metadata": {"privacy": "private_diary"}}))
        self.assertFalse(mdb.is_private_diary_memory_item(
            {"source": "activity_log", "content": "ok"}))

    def test_h_search_memory_filters_private_diary(self):
        """search_memory B2 段应跳过 private_diary 来源。"""
        private_item = {
            "content": SECRET_MARKER,
            "source": mdb.SOURCE_PRIVATE_DIARY,
            "metadata": {"private": True},
            "memory_type": "moment",
        }
        public_item = {
            "content": "用户喜欢喝咖啡",
            "source": "web",
            "memory_type": "long_term",
        }

        async def fake_hybrid(*_a, **_k):
            return {"ok": True, "items": [private_item, public_item]}, "ok"

        import server

        with patch.object(server, "supabase_service", MagicMock()), \
             patch.object(server, "supabase", None), \
             patch.object(server, "pinecone_memory", MagicMock()), \
             patch("memory_hybrid_recall.run_hybrid_recall", new=fake_hybrid), \
             patch.object(server, "_resolve_pinecone_user_id", return_value="u"), \
             patch.object(server, "_get_embedding", new=AsyncMock(return_value=[0.1] * 8)):
            # pinecone search may throw — ok
            with patch.object(server.pinecone_memory, "search",
                              side_effect=Exception("skip")):
                out = _run(server.search_memory("咖啡"))
        self.assertNotIn(SECRET_MARKER, out)
        self.assertIn("咖啡", out)

    def test_i_hybrid_recall_has_no_type_whitelist_excluding_moment(self):
        """确认 hybrid recall 模块文档/RPC 不按类型排除 moment。"""
        import memory_hybrid_recall as mhr
        import inspect
        src = inspect.getsource(mhr)
        # 不应存在排除 moment 的硬编码白名单
        self.assertNotIn('memory_type != "moment"', src)
        self.assertNotIn("exclude.*moment", src)

    def test_j_bridge_log_no_secret_body(self):
        fake = FakeItemsService()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _run(mdb.bridge_private_diary(
                title="t", content=SECRET_MARKER, mood="平静",
                action_key="diary_abc",
                llm_call=_llm_moment(f"摘要不含标记词"),
                supabase_service=fake, user_id="u"))
        logged = buf.getvalue()
        self.assertNotIn(SECRET_MARKER, logged)

    def test_k_finalize_schedules_bridge_when_enabled(self):
        """activity_log.finalize 成功路径会调度桥接（mock schedule）。"""
        import home.activity_log as alog

        class _SB:
            def table(self, name):
                return self

            def update(self, *_a, **_k): return self
            def eq(self, *_a, **_k): return self
            def execute(self):
                return FakeResult([{"status": "succeeded"}])

        scheduled = {"n": 0}

        def fake_sched(coro):
            scheduled["n"] += 1
            try:
                coro.close()
            except Exception:
                pass

        with patch.object(alog, "_get_service_client", return_value=_SB()), \
             patch("memory_diary_bridge.schedule_bridge", new=fake_sched), \
             patch("memory_diary_bridge.diary_bridge_enabled", return_value=True):
            r = alog.finalize_activity_log(
                "act_key_1", activity_id="free:walk", activity_name="散步",
                status="succeeded", result_summary="ok")
        self.assertTrue(r.get("finalized"))
        self.assertEqual(scheduled["n"], 1)


if __name__ == "__main__":
    unittest.main()
