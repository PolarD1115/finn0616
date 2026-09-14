# -*- coding: utf-8 -*-
"""阶段 C1 专项测试 —— 四级总结接分层记忆（memory_items）。

全部 unittest + mock + 脱敏假数据（SYNTHETIC_*），不连接 Supabase / LLM。

覆盖：
  A. _fetch_layered_memories：active/类型/时间范围/排序/limit 查询条件、
     min_importance、失败返回 []、None 服务返回 []、只读（绝不 delete）、
     排除 source=private_diary（D3 隐私）
  B. _layered_summary_enabled 门控语义（默认开）
  C. 周总结 prompt 叠加【本周分层记忆】+ 综合提炼指引；门控关时不叠加、零查询
  D. 月/年总结同理
  E. 日总结可选附加昨日 moment/memo（importance>=6 前 10 条）
  F. 阅后即焚不回归：只删旧 memories 的 Core_Cognition 系列，memory_items 零删除

运行：  python -m unittest test_layered_summary_phaseC1 -v
"""

import asyncio
import datetime
import os
import unittest
from unittest.mock import patch

import heartbeat
import server


TEST_USER_ID = "test-user"


# ==========================================
# 假件（路径记录为 (method, args, kwargs) 三元组）
# ==========================================

class FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeItemsQuery:
    def __init__(self, owner, table):
        self._owner = owner
        self._table = table
        self._path = []

    def _rec(self, method, *a, **k):
        self._path.append((method, a, k))
        return self

    def select(self, *a, **k): return self._rec("select", *a, **k)
    def eq(self, *a, **k): return self._rec("eq", *a, **k)
    def neq(self, *a, **k): return self._rec("neq", *a, **k)
    def in_(self, *a, **k): return self._rec("in_", *a, **k)
    def gte(self, *a, **k): return self._rec("gte", *a, **k)
    def order(self, *a, **k): return self._rec("order", *a, **k)
    def limit(self, *a, **k): return self._rec("limit", *a, **k)
    def delete(self, *a, **k): return self._rec("delete", *a, **k)

    def execute(self):
        self._owner.calls.append((self._table, list(self._path)))
        return self._owner._dispatch(self._table, self._path)


class _FakeItemsService:
    """memory_items 假服务：记录查询路径；绝不允许 delete。"""

    def __init__(self, rows=(), fail=False):
        self.rows = list(rows)
        self.fail = fail
        self.calls = []
        self.tables_used = set()

    def table(self, name):
        self.tables_used.add(name)
        return _FakeItemsQuery(self, name)

    def _dispatch(self, table, path):
        if self.fail:
            raise RuntimeError("mock memory_items failure")
        # 尊重 .neq("source", ...)，便于验证 D3 隐私排除
        neq_source = next(
            (a[1] for m, a, k in path
             if m == "neq" and a and a[0] == "source" and len(a) >= 2),
            None)
        out = []
        for r in self.rows:
            if neq_source is not None and r.get("source") == neq_source:
                continue
            out.append(dict(r))
        return FakeResult(out)

    def delete_ops(self):
        return [(t, p) for t, p in self.calls
                if any(m == "delete" for m, a, k in p)]

    def methods(self):
        return {m for t, p in self.calls for m, a, k in p}


class _FakeAnonQuery:
    def __init__(self, owner, table):
        self._owner = owner
        self._table = table
        self._path = []

    def _rec(self, method, *a, **k):
        self._path.append((method, a, k))
        return self

    def select(self, *a, **k): return self._rec("select", *a, **k)
    def eq(self, *a, **k): return self._rec("eq", *a, **k)
    def gt(self, *a, **k): return self._rec("gt", *a, **k)
    def lt(self, *a, **k): return self._rec("lt", *a, **k)
    def in_(self, *a, **k): return self._rec("in_", *a, **k)
    def order(self, *a, **k): return self._rec("order", *a, **k)
    def limit(self, *a, **k): return self._rec("limit", *a, **k)
    def delete(self, *a, **k): return self._rec("delete", *a, **k)

    def execute(self):
        self._owner.calls.append((self._table, list(self._path)))
        return self._owner._dispatch(self._table, self._path)


class _FakeAnonSb:
    """memories 假客户端：按查询特征分流（日/周/月/年）并记录删除。"""

    def __init__(self):
        self.daily_rows = [{"title": "💬 TG 互动",
                            "created_at": "2026-09-05T12:00:00+00:00",
                            "category": "流水",
                            "content": "用户: SYNTHETIC_DAILY_流水内容\n回复: 好呀",
                            "mood": "平静"}]
        self.core_rows = [{"id": "w1", "content": "SYNTHETIC_CORE_日记甲"},
                          {"id": "w2", "content": "SYNTHETIC_CORE_日记乙"},
                          {"id": "w3", "content": "SYNTHETIC_CORE_日记丙"}]
        self.weekly_rows = [{"id": "wk1", "content": "SYNTHETIC_WEEKLY_总结一"},
                            {"id": "wk2", "content": "SYNTHETIC_WEEKLY_总结二"}]
        self.monthly_rows = [{"id": "mo1", "content": "SYNTHETIC_MONTHLY_总结一"}]
        self.deleted_ids = []
        self.calls = []

    def table(self, name):
        assert name == "memories", f"anon 客户端不应访问其他表: {name}"
        return _FakeAnonQuery(self, name)

    def _dispatch(self, table, path):
        method = path[0][0] if path else ""
        if method == "delete":
            ids = next((a[1] for m, a, k in path
                        if m == "in_" and a and a[0] == "id"), [])
            self.deleted_ids.extend(list(ids))
            return FakeResult([{"id": i} for i in ids])
        vals = [a[1] for m, a, k in path if m == "eq" and len(a) >= 2]
        if "Core_Cognition_Weekly" in vals:
            return FakeResult([dict(r) for r in self.weekly_rows])
        if "Core_Cognition_Monthly" in vals:
            return FakeResult([dict(r) for r in self.monthly_rows])
        if "Core_Cognition" in vals:
            return FakeResult([dict(r) for r in self.core_rows])
        return FakeResult([dict(r) for r in self.daily_rows])


# ==========================================
# 执行辅助
# ==========================================

def _next_weekday(base, weekday):
    return base + datetime.timedelta(days=(weekday - base.weekday()) % 7)


SUNDAY = _next_weekday(datetime.datetime(2026, 9, 10, 3, 15), 6)   # 某个周日 03:15
MONTH_END = datetime.datetime(2026, 11, 30, 3, 15)                 # 明天是 12-01
YEARLY = datetime.datetime(2026, 12, 31, 3, 15)                    # 12-31

_LAYERED_ROWS = [{"content": "SYNTHETIC_LAYERED_长期事实", "memory_type": "long_term",
                  "importance": 7},
                 {"content": "SYNTHETIC_LAYERED_瞬间", "memory_type": "moment",
                  "importance": 8}]


def _run_deep_dreaming(fake_sb, fake_svc, *, now, gate="true",
                       items_rows=_LAYERED_ROWS, llm_output="SYNTHETIC_SUMMARY_TEXT"):
    """mock 环境下执行 _perform_deep_dreaming，返回 (prompts, saved, fake_sb, fake_svc)。"""
    prompts, saved, emails = [], [], []

    async def _ask(role, prompt, **kw):
        prompts.append((role, str(prompt)))
        return llm_output

    def _save(title, content, category="流水", mood="平静", tags=""):
        saved.append({"title": title, "tags": tags})
        return True

    def _email(subject, content):
        emails.append(subject)

    def _now():
        return now

    key = "MEMORY_LAYERED_SUMMARY_ENABLED"
    old = os.environ.pop(key, None)
    if gate is not None:
        os.environ[key] = gate
    # 阶段 D2 画像反思挂在周日同一入口；本 C1 用例隔离分层总结门控，须关掉反思以免
    # 额外 memory_items 查询干扰「门控关 → 零查询」断言。
    _prf_key = "PROFILE_REFLECT_ENABLED"
    _prf_old = os.environ.get(_prf_key)
    os.environ[_prf_key] = "false"
    try:
        with patch.object(server, "ask_role", _ask), \
             patch.object(server, "_save_memory_to_db", _save), \
             patch.object(server, "_send_email_helper", _email), \
             patch.object(server, "_get_now_bj", _now), \
             patch.object(server, "supabase", fake_sb), \
             patch.object(server, "supabase_service", fake_svc), \
             patch.object(server, "_resolve_pinecone_user_id", lambda: TEST_USER_ID):
            asyncio.run(heartbeat._perform_deep_dreaming())
    finally:
        os.environ.pop(key, None)
        if old is not None:
            os.environ[key] = old
        if _prf_old is None:
            os.environ.pop(_prf_key, None)
        else:
            os.environ[_prf_key] = _prf_old
    return prompts, saved, fake_sb, fake_svc


def _prompt_with(prompts, marker):
    for _, text in prompts:
        if marker in text:
            return text
    return None


def _conds_by_column(path):
    """把查询路径压成 {列名: 值}（eq/neq/in_/gte/gt/lt/limit 的首参数为列名或值）。"""
    conds = {}
    for m, a, k in path:
        if a and len(a) >= 2 and m in ("eq", "neq", "in_", "gte", "gt", "lt"):
            # neq 用特殊键，避免与 eq 同列互相覆盖
            key = a[0] if m != "neq" else f"neq:{a[0]}"
            conds[key] = a[1]
        elif a and m == "limit":
            conds["__limit__"] = a[0]
    return conds


# ==========================================
# A. 读取函数单元测试
# ==========================================

class TestFetchLayeredMemories(unittest.TestCase):

    def test_query_conditions(self):
        svc = _FakeItemsService(rows=_LAYERED_ROWS)
        contents = heartbeat._fetch_layered_memories(
            svc, TEST_USER_ID, "2026-09-01T00:00:00+08:00",
            ("long_term", "moment", "memo"), 30)
        self.assertEqual(contents, ["SYNTHETIC_LAYERED_长期事实", "SYNTHETIC_LAYERED_瞬间"])
        self.assertEqual(svc.tables_used, {"memory_items"}, "只读 memory_items 一张表")
        table, path = svc.calls[0]
        self.assertEqual(path[0][0], "select")
        conds = _conds_by_column(path)
        self.assertEqual(conds.get("user_id"), TEST_USER_ID)
        self.assertEqual(conds.get("status"), "active")
        self.assertEqual(conds.get("memory_type"), ["long_term", "moment", "memo"])
        self.assertEqual(conds.get("created_at"), "2026-09-01T00:00:00+08:00")
        self.assertEqual(conds.get("neq:source"), "private_diary",
                         "D3：查询必须排除 private_diary")
        order_cols = [a[0] for m, a, k in path if m == "order"]
        self.assertEqual(order_cols, ["importance", "valid_at"],
                         "importance DESC, valid_at DESC")
        self.assertEqual(conds.get("__limit__"), 30)

    def test_min_importance_filter(self):
        svc = _FakeItemsService(rows=[])
        heartbeat._fetch_layered_memories(svc, TEST_USER_ID,
                                          "2026-09-01T00:00:00+08:00",
                                          ("moment", "memo"), 10, 6)
        _, path = svc.calls[0]
        conds = _conds_by_column(path)
        self.assertEqual(conds.get("importance"), 6, "日总结的 importance>=6 下限")
        self.assertEqual(conds.get("memory_type"), ["moment", "memo"])
        self.assertEqual(conds.get("__limit__"), 10)

    def test_failure_returns_empty_list(self):
        svc = _FakeItemsService(fail=True)
        self.assertEqual(
            heartbeat._fetch_layered_memories(svc, TEST_USER_ID,
                                              "2026-09-01T00:00:00+08:00"), [])

    def test_none_service_returns_empty_list(self):
        self.assertEqual(heartbeat._fetch_layered_memories(None, TEST_USER_ID,
                                                           "2026-09-01T00:00:00+08:00"), [])

    def test_empty_types_returns_empty_list(self):
        svc = _FakeItemsService(rows=[])
        self.assertEqual(
            heartbeat._fetch_layered_memories(svc, TEST_USER_ID,
                                              "2026-09-01T00:00:00+08:00", []), [])

    def test_excludes_private_diary_source(self):
        """D3 隐私：source=private_diary 的 moment 不进四级总结；普通 moment 正常返回。"""
        rows = [
            {"content": "SYNTHETIC_PRIVATE_DIARY_MOMENT", "memory_type": "moment",
             "importance": 9, "source": "private_diary"},
            {"content": "SYNTHETIC_PUBLIC_MOMENT", "memory_type": "moment",
             "importance": 8, "source": "web"},
            {"content": "SYNTHETIC_LONG_TERM_OK", "memory_type": "long_term",
             "importance": 7, "source": "activity_log"},
        ]
        svc = _FakeItemsService(rows=rows)
        contents = heartbeat._fetch_layered_memories(
            svc, TEST_USER_ID, "2026-09-01T00:00:00+08:00",
            ("long_term", "moment", "memo"), 30)
        self.assertNotIn("SYNTHETIC_PRIVATE_DIARY_MOMENT", contents)
        self.assertIn("SYNTHETIC_PUBLIC_MOMENT", contents)
        self.assertIn("SYNTHETIC_LONG_TERM_OK", contents)
        _, path = svc.calls[0]
        conds = _conds_by_column(path)
        self.assertEqual(conds.get("neq:source"), "private_diary",
                         "查询必须带 .neq('source', 'private_diary')")


# ==========================================
# B. 门控
# ==========================================

class TestGate(unittest.TestCase):

    def test_default_enabled(self):
        key = "MEMORY_LAYERED_SUMMARY_ENABLED"
        old = os.environ.pop(key, None)
        try:
            self.assertTrue(heartbeat._layered_summary_enabled(), "默认开")
        finally:
            if old is not None:
                os.environ[key] = old

    def test_gate_semantics(self):
        # 默认开语义（与 FREE_ACTIVITY_ENABLED 等既有开关一致）：
        # 仅 0/false/no 关闭，未设置与其他值一律开启
        for val in ("true", "1", "yes", "TRUE", "garbage"):
            with patch.dict(os.environ, {"MEMORY_LAYERED_SUMMARY_ENABLED": val}):
                self.assertTrue(heartbeat._layered_summary_enabled(), msg=repr(val))
        for val in ("false", "0", "no"):
            with patch.dict(os.environ, {"MEMORY_LAYERED_SUMMARY_ENABLED": val}):
                self.assertFalse(heartbeat._layered_summary_enabled(), msg=repr(val))


# ==========================================
# C/D. 周/月/年 prompt 叠加
# ==========================================

class TestWeeklyMonthlyYearly(unittest.TestCase):

    def test_weekly_prompt_includes_layered_block(self):
        prompts, saved, _, _ = _run_deep_dreaming(
            _FakeAnonSb(), _FakeItemsService(rows=_LAYERED_ROWS), now=SUNDAY,
            gate="true")
        week_prompt = _prompt_with(prompts, "【本周每日日记】")
        self.assertIsNotNone(week_prompt, "周日触发周总结")
        self.assertIn("【本周分层记忆】:", week_prompt)
        self.assertIn("SYNTHETIC_LAYERED_长期事实", week_prompt)
        self.assertIn("SYNTHETIC_LAYERED_瞬间", week_prompt)
        self.assertIn("请综合两者提炼", week_prompt, "综合提炼指引")
        self.assertIn("请将这周的日记提炼成一篇深度的周度长期记忆总结", week_prompt,
                      "原有指令保留")
        self.assertTrue(any(s["tags"] == "Core_Cognition_Weekly" for s in saved),
                        "周总结写回不回归")

    def test_weekly_gate_off_no_layered_no_query(self):
        fake_svc = _FakeItemsService(rows=_LAYERED_ROWS)
        prompts, _, _, svc = _run_deep_dreaming(_FakeAnonSb(), fake_svc,
                                                now=SUNDAY, gate="false")
        week_prompt = _prompt_with(prompts, "【本周每日日记】")
        self.assertIsNotNone(week_prompt)
        self.assertNotIn("【本周分层记忆】", week_prompt, "门控关不叠加")
        self.assertNotIn("请综合两者提炼", week_prompt)
        self.assertEqual(len(svc.calls), 0, "门控关时完全不读 memory_items")

    def test_monthly_prompt_includes_layered_block(self):
        prompts, saved, _, _ = _run_deep_dreaming(
            _FakeAnonSb(), _FakeItemsService(rows=_LAYERED_ROWS), now=MONTH_END,
            gate="true")
        month_prompt = _prompt_with(prompts, "【本月周度记忆】")
        self.assertIsNotNone(month_prompt, "月末触发月总结")
        self.assertIn("【本月分层记忆】:", month_prompt)
        self.assertIn("SYNTHETIC_LAYERED_长期事实", month_prompt)
        self.assertIn("请综合两者提炼", month_prompt)
        self.assertTrue(any(s["tags"] == "Core_Cognition_Monthly" for s in saved),
                        "月总结写回不回归")

    def test_yearly_prompt_includes_layered_block(self):
        prompts, saved, _, _ = _run_deep_dreaming(
            _FakeAnonSb(), _FakeItemsService(rows=_LAYERED_ROWS), now=YEARLY,
            gate="true")
        year_prompt = _prompt_with(prompts, "【本年度月度记忆】")
        self.assertIsNotNone(year_prompt, "12-31 触发年总结")
        self.assertIn("【本年度分层记忆】:", year_prompt)
        self.assertIn("SYNTHETIC_LAYERED_瞬间", year_prompt)
        self.assertTrue(any(s["tags"] == "Core_Cognition_Yearly" for s in saved),
                        "年总结写回不回归")


# ==========================================
# E. 日总结附加 moment/memo
# ==========================================

class TestDailyLayered(unittest.TestCase):

    def test_daily_prompt_appends_layered_with_importance_floor(self):
        svc = _FakeItemsService(rows=[{"content": "SYNTHETIC_LAYERED_MOMENT",
                                       "memory_type": "moment", "importance": 8}])
        prompts, saved, _, _ = _run_deep_dreaming(_FakeAnonSb(), svc,
                                                  now=SUNDAY, gate="true")
        daily_prompt = _prompt_with(prompts, "【昨日剧情")
        self.assertIsNotNone(daily_prompt)
        self.assertIn("【昨日分层记忆 · 情感坐标/备忘】:", daily_prompt)
        self.assertIn("SYNTHETIC_LAYERED_MOMENT", daily_prompt)
        # 第一次 memory_items 查询即日总结的读取：仅 moment/memo、importance>=6
        self.assertTrue(svc.calls, "日总结应触发分层记忆读取")
        _, path = svc.calls[0]
        conds = _conds_by_column(path)
        self.assertEqual(conds.get("memory_type"), ["moment", "memo"],
                         "日总结只取 moment/memo（long_term 不进日总结）")
        self.assertEqual(conds.get("importance"), 6)
        self.assertEqual(conds.get("__limit__"), 10)
        self.assertTrue(any(s["tags"] == "Core_Cognition" for s in saved),
                        "日总结写回不回归")

    def test_daily_gate_off_keeps_original_context(self):
        svc = _FakeItemsService(rows=_LAYERED_ROWS)
        prompts, _, _, svc2 = _run_deep_dreaming(_FakeAnonSb(), svc,
                                                 now=SUNDAY, gate="false")
        daily_prompt = _prompt_with(prompts, "【昨日剧情")
        self.assertIsNotNone(daily_prompt)
        self.assertNotIn("【昨日分层记忆", daily_prompt, "门控关不附加")
        self.assertEqual(len(svc2.calls), 0, "门控关零 memory_items 查询")


# ==========================================
# F. 阅后即焚不回归 + memory_items 永不删
# ==========================================

class TestBurnAfterReadUnchanged(unittest.TestCase):

    def test_monthly_deletes_only_memories_weekly_rows(self):
        fake_sb = _FakeAnonSb()
        svc = _FakeItemsService(rows=_LAYERED_ROWS)
        _run_deep_dreaming(fake_sb, svc, now=MONTH_END, gate="true")

        self.assertEqual(sorted(fake_sb.deleted_ids), ["wk1", "wk2"],
                         "月总结后仍只清理旧 memories 的周总结（阅后即焚不回归）")
        self.assertEqual(svc.delete_ops(), [], "memory_items 绝不发生 delete")
        self.assertEqual(svc.tables_used, {"memory_items"})

    def test_yearly_deletes_only_memories_monthly_rows(self):
        fake_sb = _FakeAnonSb()
        svc = _FakeItemsService(rows=_LAYERED_ROWS)
        _run_deep_dreaming(fake_sb, svc, now=YEARLY, gate="true")

        # 12-31 同时满足既有的月末（tomorrow.day==1）与年末触发条件：
        # 月末清理周总结 wk1/wk2 + 年末清理月总结 mo1（既有逻辑叠加，与本阶段无关）
        self.assertEqual(set(fake_sb.deleted_ids), {"wk1", "wk2", "mo1"},
                         "年总结后仍只清理旧 memories 的月总结（含同日月末清理）")
        self.assertEqual(svc.delete_ops(), [], "memory_items 绝不发生 delete")

    def test_layered_fetch_never_writes(self):
        svc = _FakeItemsService(rows=_LAYERED_ROWS)
        heartbeat._fetch_layered_memories(svc, TEST_USER_ID,
                                          "2026-09-01T00:00:00+08:00")
        # 每条查询路径的首操作必须是 select（后续为链式过滤/排序方法）
        ops = {p[0][0] for t, p in svc.calls if p}
        self.assertEqual(ops, {"select"}, "读取函数只允许 select")


if __name__ == "__main__":
    unittest.main()
