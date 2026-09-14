# -*- coding: utf-8 -*-
"""阶段 D2 专项测试 —— 画像反思周更（只增不减 + 长度保护）。

全 mock，不触真实服务。

覆盖：
  A. 只增不减：缺段落 → PARAGRAPH_REMOVED
  B. 长度保护：<90% → LENGTH_REGRESSION
  C. 最多 5 句新增
  D. key 不删；可新增 profile_* key
  E. 门控关闭
  F. 日志脱敏（无画像正文）

运行：  python -m unittest test_memory_profile_reflect_phaseD2 -v
"""

import asyncio
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

import memory_profile_reflect as mpr


PROFILE_SECRET = "SYNTHETIC_PROFILE_BODY_SHOULD_NOT_LOG_D2"


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeFacts:
    def __init__(self, rows=None):
        self.rows = {r["key"]: dict(r) for r in (rows or [])}
        self.upserts = []
        self.deletes = []

    def table(self, name):
        assert name == "user_facts"
        return self

    def select(self, *_a, **_k):
        return self

    def upsert(self, data, on_conflict=None):
        self._pending = ("upsert", data)
        return self

    def delete(self):
        self._pending = ("delete", None)
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def execute(self):
        if getattr(self, "_pending", None) and self._pending[0] == "upsert":
            data = self._pending[1]
            self.rows[data["key"]] = dict(data)
            self.upserts.append(dict(data))
            self._pending = None
            return FakeResult([dict(data)])
        if getattr(self, "_pending", None) and self._pending[0] == "delete":
            # 测试守卫：本模块不应调用 delete
            self.deletes.append(getattr(self, "_eq", None))
            self._pending = None
            return FakeResult([])
        return FakeResult([dict(v) for v in self.rows.values()])


class FakeMemService:
    def __init__(self, items=None):
        self.items = items or []

    def table(self, name):
        assert name == "memory_items"
        return _MQ(self)


class _MQ:
    def __init__(self, owner):
        self._owner = owner
        self._path = []

    def select(self, *a, **k):
        self._path.append(("select", a)); return self
    def eq(self, *a, **k):
        self._path.append(("eq", a)); return self
    def in_(self, *a, **k):
        self._path.append(("in_", a)); return self
    def gte(self, *a, **k):
        self._path.append(("gte", a)); return self
    def order(self, *a, **k):
        self._path.append(("order", a)); return self
    def limit(self, *a, **k):
        self._path.append(("limit", a)); return self

    def execute(self):
        return FakeResult([dict(r) for r in self._owner.items])


def _run(coro):
    return asyncio.run(coro)


class TestProfileReflectPhaseD2(unittest.TestCase):

    def setUp(self):
        os.environ["PROFILE_REFLECT_ENABLED"] = "true"

    def tearDown(self):
        os.environ.pop("PROFILE_REFLECT_ENABLED", None)

    def test_a_paragraph_removed_rejected(self):
        old = "喜欢喝美式咖啡。\n讨厌早起。"
        new = "喜欢喝美式咖啡。"  # 删了第二段
        r = mpr.validate_profile_growth(old, new)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "PARAGRAPH_REMOVED")

    def test_b_length_regression_rejected(self):
        # 旧段落原文仍在，但尾部填充被吃掉 → 总长跌破 90%
        old = "喜欢喝美式咖啡。" + (" " * 100)
        new = "喜欢喝美式咖啡。"
        r = mpr.validate_profile_growth(old, new)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "LENGTH_REGRESSION")

    def test_c_too_many_new_sentences(self):
        old = "喜欢猫。"
        new = (old + "一句一。一句二。一句三。一句四。一句五。一句六。")
        r = mpr.validate_profile_growth(old, new)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "TOO_MANY_NEW_SENTENCES")

    def test_d_valid_growth_accepted(self):
        old = "喜欢喝美式咖啡。\n讨厌早起。"
        new = old + "\n最近开始学吉他。"
        r = mpr.validate_profile_growth(old, new)
        self.assertTrue(r["ok"])

    def test_e_gate_off(self):
        os.environ["PROFILE_REFLECT_ENABLED"] = "false"
        r = _run(mpr.run_profile_reflect(
            supabase_client=FakeFacts(),
            supabase_service=FakeMemService(),
            user_id="u",
            llm_call=lambda p: "{}"))
        self.assertEqual(r["error_code"], "DISABLED")

    def test_f_upsert_keeps_keys_no_delete(self):
        old_val = f"基础画像。{PROFILE_SECRET}"
        facts = FakeFacts([{"key": "hobby", "value": old_val}])
        mems = FakeMemService([
            {"content": "用户开始学吉他", "memory_type": "long_term",
             "status": "active", "created_at": "2026-09-10T00:00:00+00:00"}
        ])
        new_val = old_val + "\n最近开始学吉他。"
        payload = json.dumps({
            "updates": [{"key": "hobby", "value": new_val}]
        }, ensure_ascii=False)

        r = _run(mpr.run_profile_reflect(
            supabase_client=facts, supabase_service=mems, user_id="u",
            llm_call=lambda p: payload))
        self.assertTrue(r["ok"])
        self.assertEqual(r["updates_written"], 1)
        self.assertEqual(facts.deletes, [])
        self.assertIn("hobby", facts.rows)
        self.assertIn("吉他", facts.rows["hobby"]["value"])
        self.assertIn(PROFILE_SECRET, facts.rows["hobby"]["value"])

    def test_g_length_fail_not_written(self):
        old_val = "第一段内容必须保留。" + ("详" * 40)
        facts = FakeFacts([{"key": "bio", "value": old_val}])
        mems = FakeMemService([])
        # 模型试图删减：旧全文不再是子串 → 拒绝且不写入
        payload = json.dumps({
            "updates": [{"key": "bio", "value": "第一段内容必须保留。"}]
        }, ensure_ascii=False)
        r = _run(mpr.run_profile_reflect(
            supabase_client=facts, supabase_service=mems, user_id="u",
            llm_call=lambda p: payload))
        self.assertEqual(r["rejected"], 1)
        self.assertEqual(r["updates_written"], 0)
        self.assertEqual(facts.rows["bio"]["value"], old_val)
        self.assertIn(r["length_checks"][0]["error_code"],
                      ("LENGTH_REGRESSION", "PARAGRAPH_REMOVED"))

    def test_h_log_redacts_profile_body(self):
        old_val = PROFILE_SECRET
        facts = FakeFacts([{"key": "bio", "value": old_val}])
        mems = FakeMemService([])
        new_val = old_val + "\n新一句。"
        payload = json.dumps({
            "updates": [{"key": "bio", "value": new_val}]
        }, ensure_ascii=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _run(mpr.run_profile_reflect(
                supabase_client=facts, supabase_service=mems, user_id="u",
                llm_call=lambda p: payload))
        logged = buf.getvalue()
        self.assertNotIn(PROFILE_SECRET, logged)
        self.assertIn("old_len=", logged)

    def test_i_excludes_sys_and_llm_keys(self):
        self.assertFalse(mpr._is_true_profile_key("sys_config"))
        self.assertFalse(mpr._is_true_profile_key("llm_models"))
        self.assertFalse(mpr._is_true_profile_key("llm_settings"))
        self.assertFalse(mpr._is_true_profile_key("desire_drive_state"))
        self.assertTrue(mpr._is_true_profile_key("hobby"))
        self.assertTrue(mpr._is_true_profile_key("desire_note_2026_09_01"))

    def test_j_new_profile_key_allowed(self):
        facts = FakeFacts([{"key": "hobby", "value": "喜欢猫。"}])
        mems = FakeMemService([])
        payload = json.dumps({
            "updates": [{
                "key": "profile_music_abc123",
                "value": "最近开始学钢琴。"
            }]
        }, ensure_ascii=False)
        r = _run(mpr.run_profile_reflect(
            supabase_client=facts, supabase_service=mems, user_id="u",
            llm_call=lambda p: payload))
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["updates_written"], 1)
        self.assertIn("hobby", facts.rows)  # 旧 key 仍在


if __name__ == "__main__":
    unittest.main()
