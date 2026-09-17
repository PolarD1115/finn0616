# -*- coding: utf-8 -*-
"""阶段 D1 专项测试 —— 换窗备忘 memo。

全 mock，不触真实服务。

覆盖：
  A. 生成时机（沉默阈值 / session_changed / 门控）
  B. memo 注入 volatile 格式
  C. expires_at 默认 7 天
  D. 同 subject_key 去重 → superseded
  E. 门控关闭零行为
  F. 只操作 memory_type=memo
  I. 沉默时长查 memory_events（排除本轮 / background）

运行：  python -m unittest test_memory_memo_phaseD1 -v
"""

import asyncio
import datetime
import os
import unittest
from unittest.mock import patch

import memory_memo as mm


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeService:
    def __init__(self):
        self.items = []
        self.updates = []

    def table(self, name):
        assert name == "memory_items"
        return _Q(self)

    def _dispatch(self, path):
        method = path[0][0]
        if method == "select":
            eqs = [(a[0], a[1]) for m, a, _ in path if m == "eq"]
            rows = list(self.items)
            for col, val in eqs:
                rows = [r for r in rows if r.get(col) == val]
            # order/limit 简化
            lim = next((a[0] for m, a, _ in path if m == "limit"), None)
            if any(m == "order" for m, a, _ in path):
                rows = sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)
            if lim is not None:
                rows = rows[:lim]
            return FakeResult([dict(r) for r in rows])
        if method == "insert":
            row = dict(path[0][1][0])
            row.setdefault("created_at", datetime.datetime.now(
                datetime.timezone.utc).isoformat())
            self.items.append(row)
            return FakeResult([dict(row)])
        if method == "update":
            payload = path[0][1][0]
            eqs = [(a[0], a[1]) for m, a, _ in path if m == "eq"]
            updated = []
            for r in self.items:
                ok = all(r.get(c) == v for c, v in eqs)
                if ok:
                    r.update(payload)
                    updated.append(dict(r))
                    self.updates.append(dict(r))
            return FakeResult(updated)
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
    def update(self, *a, **k): return self._rec("update", *a, **k)
    def eq(self, *a, **k): return self._rec("eq", *a, **k)
    def order(self, *a, **k): return self._rec("order", *a, **k)
    def limit(self, *a, **k): return self._rec("limit", *a, **k)

    def execute(self):
        return self._owner._dispatch(self._path)


def _run(coro):
    return asyncio.run(coro)


class TestMemoPhaseD1(unittest.TestCase):

    def setUp(self):
        os.environ["MEMORY_MEMO_ENABLED"] = "true"
        os.environ["MEMORY_MEMO_SILENCE_HOURS"] = "6"

    def tearDown(self):
        os.environ.pop("MEMORY_MEMO_ENABLED", None)
        os.environ.pop("MEMORY_MEMO_SILENCE_HOURS", None)

    def test_a_should_generate_on_silence(self):
        self.assertTrue(mm.should_generate_memo(silence_hours=7))
        self.assertFalse(mm.should_generate_memo(silence_hours=1))
        self.assertTrue(mm.should_generate_memo(session_changed=True, silence_hours=0))
        # Web session_id=None  alone 不触发
        self.assertFalse(mm.should_generate_memo(session_id=None, silence_hours=0))

    def test_b_gate_off(self):
        os.environ["MEMORY_MEMO_ENABLED"] = "false"
        self.assertFalse(mm.should_generate_memo(silence_hours=99))
        fake = FakeService()
        r = _run(mm.generate_and_store_memo(
            user_msg="hi", ai_msg="hello", silence_hours=99,
            llm_call=lambda p: "上次聊到：测试\n未完成：无\n对方状态：平静\n建议：问候",
            supabase_service=fake, user_id="u"))
        self.assertEqual(r["error_code"], "SKIPPED")
        self.assertEqual(len(fake.items), 0)

    def test_c_generate_sets_expires_and_type(self):
        fake = FakeService()
        r = _run(mm.generate_and_store_memo(
            user_msg="明天考试", ai_msg="加油，记得早睡",
            silence_hours=8,
            llm_call=lambda p: "上次聊到：明天考试\n未完成：早睡\n对方状态：紧张\n建议：先问考得怎样",
            supabase_service=fake, user_id="u1"))
        self.assertTrue(r["ok"])
        self.assertTrue(r["inserted"])
        item = fake.items[0]
        self.assertEqual(item["memory_type"], "memo")
        self.assertEqual(item["status"], "active")
        self.assertEqual(item["source"], mm.SOURCE)
        self.assertEqual(item["subject_key"], mm.MEMO_SUBJECT_KEY)
        self.assertIsNotNone(item["expires_at"])
        exp = datetime.datetime.fromisoformat(item["expires_at"])
        now = datetime.datetime.now(datetime.timezone.utc)
        delta = exp - now
        self.assertGreater(delta.total_seconds(), 6 * 86400)
        self.assertLess(delta.total_seconds(), 8 * 86400)

    def test_d_supersede_old_active_same_subject(self):
        fake = FakeService()
        old_id = "11111111-1111-1111-1111-111111111111"
        fake.items.append({
            "id": old_id, "user_id": "u1", "memory_type": "memo",
            "status": "active", "subject_key": mm.MEMO_SUBJECT_KEY,
            "source_event_ids": [], "content": "旧备忘",
            "created_at": "2026-01-01T00:00:00+00:00",
        })
        r = _run(mm.generate_and_store_memo(
            user_msg="新话题", ai_msg="好的",
            silence_hours=10,
            llm_call=lambda p: "上次聊到：新话题\n未完成：无\n对方状态：平静\n建议：接上",
            supabase_service=fake, user_id="u1"))
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["superseded"], 1)
        old = next(i for i in fake.items if i["id"] == old_id)
        self.assertEqual(old["status"], "superseded")
        # 新条仍是 active memo
        actives = [i for i in fake.items if i["status"] == "active"]
        self.assertEqual(len(actives), 1)
        self.assertEqual(actives[0]["memory_type"], "memo")

    def test_e_format_and_inject(self):
        block = mm.format_memo_block({
            "content": "上次聊到：考试\n未完成：复习\n对方状态：紧张\n建议：先问结果"
        })
        self.assertIn("【上次交接备忘】", block)
        self.assertIn("上次聊到", block)

        fake = FakeService()
        fake.items.append({
            "id": "a", "user_id": "u1", "memory_type": "memo", "status": "active",
            "content": "上次聊到：咖啡\n未完成：无\n对方状态：开心\n建议：聊近况",
            "created_at": "2026-09-14T00:00:00+00:00",
            "expires_at": (datetime.datetime.now(datetime.timezone.utc)
                           + datetime.timedelta(days=3)).isoformat(),
        })
        txt = _run(mm.inject_memo_text(fake, "u1"))
        self.assertIn("【上次交接备忘】", txt)
        self.assertIn("咖啡", txt)

    def test_f_inject_gate_off(self):
        os.environ["MEMORY_MEMO_ENABLED"] = "false"
        fake = FakeService()
        fake.items.append({
            "id": "a", "user_id": "u1", "memory_type": "memo", "status": "active",
            "content": "x", "created_at": "2026-09-14T00:00:00+00:00",
        })
        self.assertEqual(_run(mm.inject_memo_text(fake, "u1")), "")

    def test_g_expired_memo_not_injected(self):
        fake = FakeService()
        fake.items.append({
            "id": "a", "user_id": "u1", "memory_type": "memo", "status": "active",
            "content": "过期备忘", "created_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2026-01-02T00:00:00+00:00",
        })
        self.assertEqual(_run(mm.inject_memo_text(fake, "u1")), "")

    def test_h_empty_llm_no_insert(self):
        fake = FakeService()
        r = _run(mm.generate_and_store_memo(
            user_msg="hi", ai_msg="yo", silence_hours=9,
            llm_call=lambda p: "   ",
            supabase_service=fake, user_id="u"))
        self.assertEqual(r["error_code"], "EMPTY_MEMO")
        self.assertEqual(len(fake.items), 0)

    def test_i_hours_from_memory_events(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        before = now.isoformat()
        fake = EventsFake([
            {"user_id": "u1", "role": "user", "channel": "web",
             "occurred_at": (now - datetime.timedelta(hours=8, minutes=6)).isoformat()},
            {"user_id": "u1", "role": "assistant", "channel": "background",
             "occurred_at": (now - datetime.timedelta(hours=1)).isoformat()},
            {"user_id": "u1", "role": "user", "channel": "web",
             "occurred_at": before},  # 本轮，应被 before_iso 排除
            {"user_id": "other", "role": "user", "channel": "qq",
             "occurred_at": (now - datetime.timedelta(hours=20)).isoformat()},
        ])
        hours = _run(mm.hours_since_last_chat_event(fake, "u1", before_iso=before))
        self.assertGreaterEqual(hours, 8.0)
        self.assertLess(hours, 8.3)

    def test_j_hours_no_events_is_zero(self):
        self.assertEqual(_run(mm.hours_since_last_chat_event(None, "u1")), 0.0)
        fake = EventsFake([])
        self.assertEqual(
            _run(mm.hours_since_last_chat_event(fake, "u1", before_iso="2026-09-17T12:00:00+00:00")),
            0.0)


class EventsFake:
    """只读 fake：支撑 hours_since_last_chat_event 的 select/eq/in_/lt/order/limit。"""

    def __init__(self, rows):
        self.rows = list(rows)

    def table(self, name):
        assert name == "memory_events"
        return _EventsQ(self)


class _EventsQ:
    def __init__(self, owner):
        self._owner = owner
        self._path = []

    def _rec(self, method, *a, **k):
        self._path.append((method, a, k))
        return self

    def select(self, *a, **k): return self._rec("select", *a, **k)
    def eq(self, *a, **k): return self._rec("eq", *a, **k)
    def in_(self, *a, **k): return self._rec("in_", *a, **k)
    def lt(self, *a, **k): return self._rec("lt", *a, **k)
    def order(self, *a, **k): return self._rec("order", *a, **k)
    def limit(self, *a, **k): return self._rec("limit", *a, **k)

    def execute(self):
        rows = list(self._owner.rows)
        for method, a, _ in self._path:
            if method == "eq":
                col, val = a[0], a[1]
                rows = [r for r in rows if r.get(col) == val]
            elif method == "in_":
                col, vals = a[0], list(a[1])
                rows = [r for r in rows if r.get(col) in vals]
            elif method == "lt":
                col, val = a[0], a[1]
                rows = [r for r in rows if str(r.get(col) or "") < str(val)]
        if any(m == "order" for m, _, _ in self._path):
            rows = sorted(rows, key=lambda r: r.get("occurred_at") or "", reverse=True)
        lim = next((a[0] for m, a, _ in self._path if m == "limit"), None)
        if lim is not None:
            rows = rows[:lim]
        return FakeResult([dict(r) for r in rows])


if __name__ == "__main__":
    unittest.main()
