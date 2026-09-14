# -*- coding: utf-8 -*-
"""阶段 D2b 专项测试 —— AI 人格反思周更（只增不减 + 长度保护）。

全 mock，不触真实服务。

覆盖：
  1. fetch_current_persona：sys_ai_persona / fallback AI_PERSONA / 都缺 / 失败
  2. fetch_persona_memories：类型过滤、private_diary 排除、时间窗、limit、失败
  3. build_persona_reflect_prompt：含老人设/记忆/只增不减/最多 5 句
  4. run_persona_reflect：正常 / 空返回 / 校验失败 / 首次创建 / 门控关
  5. 日志脱敏
  6. 复用 validate_profile_growth

运行：  python -B -m unittest test_memory_persona_reflect_phaseD2b -v
"""

import asyncio
import contextlib
import datetime
import io
import os
import unittest
from unittest.mock import patch

import memory_persona_reflect as mper
import memory_profile_reflect as mpr


PERSONA_SECRET = "SYNTHETIC_PERSONA_BODY_SHOULD_NOT_LOG_D2B"
MEMORY_SECRET = "SYNTHETIC_PERSONA_MEMORY_SHOULD_NOT_LOG_D2B"


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeService:
    """同时服务 user_facts 与 memory_items。"""

    def __init__(self, facts=None, items=None, fail_facts=False, fail_items=False):
        self.facts = {r["key"]: dict(r) for r in (facts or [])}
        self.items = list(items or [])
        self.fail_facts = fail_facts
        self.fail_items = fail_items
        self.upserts = []
        self.calls = []
        self._pending = None
        self._eq = []
        self._table = None

    def table(self, name):
        self._table = name
        self._eq = []
        self._pending = None
        self._path = []
        return self

    def select(self, *_a, **_k):
        self._path.append(("select", _a))
        return self

    def upsert(self, data, on_conflict=None):
        self._pending = ("upsert", data, on_conflict)
        self._path.append(("upsert", (data,), {"on_conflict": on_conflict}))
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        self._path.append(("eq", (col, val)))
        return self

    def neq(self, col, val):
        self._path.append(("neq", (col, val)))
        return self

    def in_(self, col, vals):
        self._path.append(("in_", (col, list(vals))))
        return self

    def gte(self, col, val):
        self._path.append(("gte", (col, val)))
        return self

    def order(self, col, desc=False, **_k):
        self._path.append(("order", (col,), {"desc": desc}))
        return self

    def limit(self, n):
        self._path.append(("limit", (n,)))
        return self

    def execute(self):
        self.calls.append((self._table, list(self._path)))
        if self._table == "user_facts":
            if self.fail_facts:
                raise RuntimeError("mock facts failure")
            if self._pending and self._pending[0] == "upsert":
                data = dict(self._pending[1])
                self.facts[data["key"]] = data
                self.upserts.append(data)
                self._pending = None
                return FakeResult([dict(data)])
            rows = list(self.facts.values())
            for col, val in self._eq:
                rows = [r for r in rows if r.get(col) == val]
            return FakeResult([dict(r) for r in rows])

        if self._table == "memory_items":
            if self.fail_items:
                raise RuntimeError("mock items failure")
            rows = list(self.items)
            for m, a, *rest in self._path:
                kwargs = rest[0] if rest else {}
                if m == "eq" and len(a) >= 2:
                    rows = [r for r in rows if r.get(a[0]) == a[1]]
                elif m == "neq" and len(a) >= 2:
                    rows = [r for r in rows if r.get(a[0]) != a[1]]
                elif m == "in_" and len(a) >= 2:
                    allowed = set(a[1])
                    rows = [r for r in rows if r.get(a[0]) in allowed]
                elif m == "gte" and len(a) >= 2:
                    rows = [r for r in rows if str(r.get(a[0]) or "") >= str(a[1])]
                elif m == "limit" and a:
                    rows = rows[: int(a[0])]
            return FakeResult([dict(r) for r in rows])

        raise AssertionError(self._table)


def _run(coro):
    return asyncio.run(coro)


def _recent(days_ago=1, **extra):
    ts = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(days=days_ago)).isoformat()
    row = {
        "content": extra.pop("content", "记忆"),
        "memory_type": extra.pop("memory_type", "moment"),
        "status": "active",
        "source": extra.pop("source", "web"),
        "importance": extra.pop("importance", 5),
        "created_at": ts,
        "user_id": "u",
    }
    row.update(extra)
    return row


class TestPersonaReflectPhaseD2b(unittest.TestCase):

    def setUp(self):
        os.environ["PERSONA_REFLECT_ENABLED"] = "true"
        os.environ.pop("AI_PERSONA", None)

    def tearDown(self):
        os.environ.pop("PERSONA_REFLECT_ENABLED", None)
        os.environ.pop("AI_PERSONA", None)

    # ── 1. fetch_current_persona ──

    def test_fetch_persona_from_db(self):
        svc = FakeService(facts=[{"key": "sys_ai_persona", "value": "我是小爱。"}])
        os.environ["AI_PERSONA"] = "环境人设不应优先"
        self.assertEqual(_run(mper.fetch_current_persona(svc)), "我是小爱。")

    def test_fetch_persona_fallback_env(self):
        svc = FakeService(facts=[])
        os.environ["AI_PERSONA"] = "环境人设"
        self.assertEqual(_run(mper.fetch_current_persona(svc)), "环境人设")

    def test_fetch_persona_both_missing(self):
        svc = FakeService(facts=[])
        self.assertEqual(_run(mper.fetch_current_persona(svc)), "")

    def test_fetch_persona_failure_returns_empty_then_env(self):
        svc = FakeService(fail_facts=True)
        os.environ["AI_PERSONA"] = "失败后回退"
        self.assertEqual(_run(mper.fetch_current_persona(svc)), "失败后回退")

    def test_fetch_persona_failure_no_env(self):
        svc = FakeService(fail_facts=True)
        self.assertEqual(_run(mper.fetch_current_persona(svc)), "")

    def test_fetch_persona_none_service_uses_env(self):
        os.environ["AI_PERSONA"] = "无服务回退"
        self.assertEqual(_run(mper.fetch_current_persona(None)), "无服务回退")

    # ── 2. fetch_persona_memories ──

    def test_fetch_memories_type_filter(self):
        items = [
            _recent(content="瞬间A", memory_type="moment", importance=9),
            _recent(content="共同经历B", memory_type="shared_experience", importance=8),
            _recent(content="核心不应出现", memory_type="core", importance=10),
            _recent(content="长期不应出现", memory_type="long_term", importance=10),
            _recent(content="我感到成长", memory_type="current", importance=7),
            _recent(content="临时状态缺主语", memory_type="current", importance=7),
        ]
        svc = FakeService(items=items)
        out = _run(mper.fetch_persona_memories(svc, "u", days=7))
        self.assertIn("瞬间A", out)
        self.assertIn("共同经历B", out)
        self.assertIn("我感到成长", out)
        self.assertNotIn("核心不应出现", out)
        self.assertNotIn("长期不应出现", out)
        self.assertNotIn("临时状态缺主语", out)
        _, path = svc.calls[0]
        in_types = next(a[1] for m, a, *_ in path
                        if m == "in_" and a[0] == "memory_type")
        self.assertEqual(set(in_types),
                         {"moment", "shared_experience", "current"})
        neq = next(((a[0], a[1]) for m, a, *_ in path if m == "neq"), None)
        self.assertEqual(neq, ("source", "private_diary"))

    def test_fetch_memories_excludes_private_diary(self):
        items = [
            _recent(content="公开瞬间", memory_type="moment", source="web"),
            _recent(content="私密日记", memory_type="moment", source="private_diary"),
        ]
        svc = FakeService(items=items)
        out = _run(mper.fetch_persona_memories(svc, "u"))
        self.assertIn("公开瞬间", out)
        self.assertNotIn("私密日记", out)

    def test_fetch_memories_time_window(self):
        items = [
            _recent(days_ago=1, content="近窗", memory_type="moment"),
            _recent(days_ago=30, content="过旧", memory_type="moment"),
        ]
        svc = FakeService(items=items)
        out = _run(mper.fetch_persona_memories(svc, "u", days=7))
        self.assertIn("近窗", out)
        self.assertNotIn("过旧", out)
        _, path = svc.calls[0]
        gte = next((a for m, a, *_ in path if m == "gte"), None)
        self.assertIsNotNone(gte)
        self.assertEqual(gte[0], "created_at")

    def test_fetch_memories_limit_20(self):
        items = [
            _recent(content=f"m{i}", memory_type="moment", importance=10 - (i % 10))
            for i in range(30)
        ]
        svc = FakeService(items=items)
        out = _run(mper.fetch_persona_memories(svc, "u"))
        self.assertLessEqual(len(out), 20)

    def test_fetch_memories_failure_returns_empty(self):
        svc = FakeService(fail_items=True)
        self.assertEqual(_run(mper.fetch_persona_memories(svc, "u")), [])

    def test_fetch_memories_none_service(self):
        self.assertEqual(_run(mper.fetch_persona_memories(None, "u")), [])

    # ── 3. build_persona_reflect_prompt ──

    def test_build_prompt_contents(self):
        prompt = mper.build_persona_reflect_prompt(
            "老人设段落。", [MEMORY_SECRET], "小爱", "用户甲")
        self.assertIn("老人设段落。", prompt)
        self.assertIn(MEMORY_SECRET, prompt)
        self.assertIn("不得删除", prompt)
        self.assertIn("不得缩写", prompt)
        self.assertIn("不得合并", prompt)
        self.assertIn("最多新增 5 句", prompt)
        self.assertIn("小爱", prompt)
        self.assertIn("第一人称", prompt)

    # ── 4. run_persona_reflect ──

    def test_run_ok_upserts(self):
        old = f"温柔体贴。{PERSONA_SECRET}"
        svc = FakeService(
            facts=[{"key": "sys_ai_persona", "value": old}],
            items=[_recent(content=MEMORY_SECRET, memory_type="moment")],
        )
        new = old + "\n最近更懂倾听。"
        r = _run(mper.run_persona_reflect(
            svc, lambda p: new, "u", "小爱", "用户"))
        self.assertTrue(r["ok"])
        self.assertIsNone(r["error_code"])
        self.assertEqual(len(svc.upserts), 1)
        self.assertEqual(svc.upserts[0]["key"], "sys_ai_persona")
        self.assertEqual(svc.upserts[0]["confidence"], 1.0)
        self.assertIn("倾听", svc.facts["sys_ai_persona"]["value"])
        self.assertEqual(r["new_sentences"], 1)

    def test_run_empty_llm_no_write(self):
        old = "原始人设。"
        svc = FakeService(facts=[{"key": "sys_ai_persona", "value": old}])
        r = _run(mper.run_persona_reflect(
            svc, lambda p: "   ", "u", "小爱", "用户"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "EMPTY_RESPONSE")
        self.assertEqual(svc.upserts, [])
        self.assertEqual(svc.facts["sys_ai_persona"]["value"], old)

    def test_run_paragraph_removed_no_write(self):
        old = "第一段必须保留。\n第二段也要。"
        svc = FakeService(facts=[{"key": "sys_ai_persona", "value": old}])
        bad = "第一段必须保留。"
        r = _run(mper.run_persona_reflect(
            svc, lambda p: bad, "u", "小爱", "用户"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "PARAGRAPH_REMOVED")
        self.assertEqual(svc.upserts, [])

    def test_run_length_regression_no_write(self):
        # 通过 run 路径：删减导致旧段落不再是子串 → 拒绝写入
        # （纯 LENGTH_REGRESSION 依赖尾部空白，fetch 会 strip，故另用直接校验覆盖）
        old = "核心性格定义。" + ("详" * 40)
        svc = FakeService(facts=[{"key": "sys_ai_persona", "value": old}])
        bad = "核心性格定义。"
        r = _run(mper.run_persona_reflect(
            svc, lambda p: bad, "u", "小爱", "用户"))
        self.assertFalse(r["ok"])
        self.assertIn(r["error_code"],
                      ("LENGTH_REGRESSION", "PARAGRAPH_REMOVED"))
        self.assertEqual(svc.upserts, [])

    def test_validate_length_regression_direct(self):
        old = "核心性格定义。" + (" " * 100)
        new = "核心性格定义。"
        check = mper.validate_profile_growth(old, new)
        self.assertFalse(check["ok"])
        self.assertEqual(check["error_code"], "LENGTH_REGRESSION")

    def test_run_too_many_sentences_no_write(self):
        old = "核心性格。"
        svc = FakeService(facts=[{"key": "sys_ai_persona", "value": old}])
        bad = old + "一句一。一句二。一句三。一句四。一句五。一句六。"
        r = _run(mper.run_persona_reflect(
            svc, lambda p: bad, "u", "小爱", "用户"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "TOO_MANY_NEW_SENTENCES")
        self.assertEqual(svc.upserts, [])

    def test_run_first_time_creates_key(self):
        os.environ["AI_PERSONA"] = "初始环境人设。"
        svc = FakeService(facts=[])  # 库中尚无 sys_ai_persona
        new = "初始环境人设。\n多了一点自我认知。"
        r = _run(mper.run_persona_reflect(
            svc, lambda p: new, "u", "小爱", "用户"))
        self.assertTrue(r["ok"])
        self.assertIn("sys_ai_persona", svc.facts)
        self.assertEqual(svc.facts["sys_ai_persona"]["value"], new)
        # 环境变量未被改写
        self.assertEqual(os.environ["AI_PERSONA"], "初始环境人设。")

    def test_run_gate_off_zero_behavior(self):
        os.environ["PERSONA_REFLECT_ENABLED"] = "false"
        svc = FakeService(
            facts=[{"key": "sys_ai_persona", "value": "x"}],
            items=[_recent(content="y", memory_type="moment")],
        )
        called = []
        r = _run(mper.run_persona_reflect(
            svc, lambda p: called.append(p) or "x\n新。", "u", "小爱", "用户"))
        self.assertEqual(r["error_code"], "DISABLED")
        self.assertEqual(called, [])
        self.assertEqual(svc.calls, [])
        self.assertEqual(svc.upserts, [])

    # ── 5. 日志脱敏 ──

    def test_log_redacts_bodies(self):
        old = PERSONA_SECRET
        svc = FakeService(
            facts=[{"key": "sys_ai_persona", "value": old}],
            items=[_recent(content=MEMORY_SECRET, memory_type="moment")],
        )
        new = old + "\n新一句。"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _run(mper.run_persona_reflect(
                svc, lambda p: new, "u", "小爱", "用户"))
        logged = buf.getvalue()
        self.assertNotIn(PERSONA_SECRET, logged)
        self.assertNotIn(MEMORY_SECRET, logged)
        self.assertIn("old_len=", logged)

    # ── 6. 复用校验 ──

    def test_reuses_validate_profile_growth(self):
        old = "原人设。"
        svc = FakeService(facts=[{"key": "sys_ai_persona", "value": old}])
        new = old + "\n补充一句。"
        with patch.object(mper, "validate_profile_growth",
                          wraps=mpr.validate_profile_growth) as mocked:
            r = _run(mper.run_persona_reflect(
                svc, lambda p: new, "u", "小爱", "用户"))
            self.assertTrue(r["ok"])
            mocked.assert_called_once()
            args, _kwargs = mocked.call_args
            self.assertEqual(args[0], old)
            self.assertEqual(args[1], new)

    def test_exports_d2_validators(self):
        self.assertIs(mper.validate_profile_growth, mpr.validate_profile_growth)
        self.assertIs(mper.split_paragraphs, mpr.split_paragraphs)
        self.assertIs(mper.count_new_sentences, mpr.count_new_sentences)


if __name__ == "__main__":
    unittest.main()
