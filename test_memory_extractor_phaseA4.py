# -*- coding: utf-8 -*-
"""阶段 A4 专项测试 —— 提取器五层分层（core/long_term/current/moment/memo）。

全部 unittest + mock + 脱敏假数据（SYNTHETIC_*），不连接 Supabase / LLM。

覆盖：
  A. 5 类 memory_type 全部可产出（且白名单外的 fact 仍被拒绝）
  B. core 置信度门槛：confidence < 0.9 降级 long_term；显式记忆请求豁免
  C. current 过期时间：缺失时按 valid_at + 7 天补；缺 valid_at 用事件
     occurred_at 起算；两者都缺用 now_utc；模型给的值早于 valid_at 仍 clamp
  D. 防模仿 / 角色前缀 / verbatim-copy / 证据映射条款不回归
  E. 原有验证逻辑不回归（数值边界、来源索引、批内去重合并）

运行：  python -m unittest test_memory_extractor_phaseA4 -v
"""

import asyncio
import datetime
import json
import unittest

import memory_extractor as mx


TEST_USER = "test-user"
_TEST_NOW = datetime.datetime(2026, 9, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _ev(idx, role, content, occurred_at="2026-09-10T10:00:00+00:00"):
    return {"id": f"ev-A4-{idx}", "user_id": TEST_USER, "session_id": None,
            "channel": "web", "role": role, "content": content,
            "content_hash": f"h-{idx}", "occurred_at": occurred_at,
            "created_at": occurred_at, "processing_status": "pending",
            "attempt_count": 0, "last_error": None, "metadata": {}}


def _cand(**overrides):
    base = {"memory_type": "long_term", "content": "SYNTHETIC_A4_FACT_TEXT",
            "importance": 4, "confidence": 0.8, "valid_at": None,
            "invalid_at": None, "expires_at": None,
            "source_event_indexes": [0], "subject_key": None}
    base.update(overrides)
    return base


def _validate(cands, events, now=None):
    now = now or _TEST_NOW
    ok, rejected = [], []
    for c in cands:
        item, reason = mx.validate_and_normalize_candidate(
            c, events, TEST_USER, "batch-A4", now)
        if item is not None:
            ok.append(item)
        else:
            rejected.append(reason)
    return ok, rejected


def _run_extract(cands, events):
    payload = json.dumps({"memories": cands}, ensure_ascii=False)

    def _llm(prompt):
        return payload
    return asyncio.run(mx.extract_memory_candidates(events, _llm, user_id=TEST_USER))


# ==========================================
# A. 5 类全部可产出
# ==========================================

class TestFiveTypesAccepted(unittest.TestCase):

    def test_all_five_types_pass(self):
        events = [_ev(0, "user", "我叫小满，我喜欢喝无糖咖啡，最近在准备考试。")]
        cands = [
            _cand(memory_type="core", content="用户的名字是小满。",
                  confidence=0.95, importance=9),
            _cand(memory_type="long_term", content="用户喜欢喝无糖咖啡。",
                  confidence=0.85, importance=5),
            _cand(memory_type="current", content="用户最近在准备考试。",
                  confidence=0.8, importance=4),
            _cand(memory_type="moment", content="用户第一次和 AI 聊到咖啡偏好。",
                  confidence=0.8, importance=5),
            _cand(memory_type="memo", content="用户提到下周要继续聊考试安排。",
                  confidence=0.7, importance=3),
        ]
        ok, rejected = _validate(cands, events)
        self.assertEqual(rejected, [], f"不应有拒绝: {rejected}")
        self.assertEqual({r["memory_type"] for r in ok},
                         {"core", "long_term", "current", "moment", "memo"})

    def test_type_outside_five_still_rejected(self):
        events = [_ev(0, "user", "我喜欢喝无糖咖啡。")]
        ok, rejected = _validate([_cand(memory_type="fact")], events)
        self.assertEqual(ok, [])
        self.assertEqual(rejected, ["INVALID_MEMORY_TYPE"],
                         "5 类白名单之外的类型（fact/shared_experience）仍被拒绝")


# ==========================================
# B. core 置信度门槛
# ==========================================

class TestCoreConfidenceGate(unittest.TestCase):

    def test_core_low_confidence_downgraded_to_long_term(self):
        events = [_ev(0, "user", "我喜欢喝无糖咖啡。")]
        ok, rejected = _validate(
            [_cand(memory_type="core", content="用户喜欢喝无糖咖啡。",
                   confidence=0.85, importance=9)], events)
        self.assertEqual(rejected, [])
        self.assertEqual(ok[0]["memory_type"], "long_term",
                         "core 且 confidence < 0.9 → 降级 long_term（不拒绝）")

    def test_core_low_importance_also_downgraded(self):
        events = [_ev(0, "user", "我喜欢喝无糖咖啡。")]
        ok, rejected = _validate(
            [_cand(memory_type="core", content="用户喜欢喝无糖咖啡。",
                   confidence=0.95, importance=5)], events)
        self.assertEqual(rejected, [])
        self.assertEqual(ok[0]["memory_type"], "long_term",
                         "core 且 importance < 8 → 降级 long_term")

    def test_core_qualified_stays_core(self):
        events = [_ev(0, "user", "我叫小满，我喜欢喝无糖咖啡。")]
        ok, rejected = _validate(
            [_cand(memory_type="core", content="用户的名字是小满。",
                   confidence=0.95, importance=9)], events)
        self.assertEqual(rejected, [])
        self.assertEqual(ok[0]["memory_type"], "core")

    def test_explicit_memory_request_keeps_core_and_bumps_importance(self):
        events = [_ev(0, "user", "记住，我对芒果过敏，这个一定要记下来。")]
        ok, rejected = _validate(
            [_cand(memory_type="core", content="用户对芒果过敏。",
                   confidence=0.5, importance=3)], events)
        self.assertEqual(rejected, [])
        self.assertEqual(ok[0]["memory_type"], "core",
                         "被引用 user 事件含显式记忆请求词 → 豁免降级")
        self.assertGreaterEqual(ok[0]["importance"], 8, "importance 提升到 >= 8")

    def test_downgrade_works_in_full_extraction_flow(self):
        events = [_ev(0, "user", "我叫小满，我最喜欢的是无糖咖啡。")]
        result = _run_extract(
            [_cand(memory_type="core", content="用户最喜欢无糖咖啡。",
                   confidence=0.6, importance=4)], events)
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidates"][0]["memory_type"], "long_term")


# ==========================================
# C. current 过期时间
# ==========================================

class TestCurrentExpiresAt(unittest.TestCase):

    def test_constant_is_seven_days(self):
        self.assertEqual(mx.CURRENT_DEFAULT_EXPIRY_HOURS, 168,
                         "current 默认有效期为 7 天（阶段 A4）")

    def test_missing_expires_backfilled_from_valid_at(self):
        events = [_ev(0, "user", "我这几天都在准备考试。")]
        ok, rejected = _validate(
            [_cand(memory_type="current", content="用户最近在准备考试。",
                   confidence=0.8, importance=4,
                   valid_at="2026-09-01T08:00:00+00:00", expires_at=None)], events)
        self.assertEqual(rejected, [])
        base = datetime.datetime.fromisoformat("2026-09-01T08:00:00+00:00")
        expected = (base + datetime.timedelta(
            hours=mx.CURRENT_DEFAULT_EXPIRY_HOURS)).isoformat()
        self.assertEqual(ok[0]["expires_at"], expected,
                         "缺 expires_at → valid_at + 7 天")

    def test_missing_valid_at_backfilled_from_event_occurred_at(self):
        occurred = "2026-09-10T10:00:00+00:00"
        events = [_ev(0, "user", "我这几天都在准备考试。", occurred_at=occurred)]
        ok, rejected = _validate(
            [_cand(memory_type="current", content="用户最近在准备考试。",
                   confidence=0.8, importance=4,
                   valid_at=None, expires_at=None)], events)
        self.assertEqual(rejected, [])
        base = datetime.datetime.fromisoformat(occurred)
        expected = (base + datetime.timedelta(
            hours=mx.CURRENT_DEFAULT_EXPIRY_HOURS)).isoformat()
        self.assertEqual(ok[0]["expires_at"], expected,
                         "缺 valid_at → 来源事件 occurred_at + 7 天")

    def test_missing_both_backfilled_from_now_utc(self):
        events = [_ev(0, "user", "我这几天都在准备考试。", occurred_at="garbage")]
        ok, rejected = _validate(
            [_cand(memory_type="current", content="用户最近在准备考试。",
                   confidence=0.8, importance=4,
                   valid_at=None, expires_at=None)], events)
        self.assertEqual(rejected, [])
        expected = (_TEST_NOW + datetime.timedelta(
            hours=mx.CURRENT_DEFAULT_EXPIRY_HOURS)).isoformat()
        self.assertEqual(ok[0]["expires_at"], expected,
                         "时间来源全部缺失 → now_utc + 7 天")

    def test_model_expires_earlier_than_valid_at_still_clamped(self):
        events = [_ev(0, "user", "我这几天都在准备考试。")]
        ok, rejected = _validate(
            [_cand(memory_type="current", content="用户最近在准备考试。",
                   confidence=0.8, importance=4,
                   valid_at="2026-09-10T10:00:00+00:00",
                   expires_at="2026-09-09T10:00:00+00:00")], events)
        self.assertEqual(rejected, [])
        self.assertEqual(ok[0]["expires_at"], "2026-09-10T10:00:00+00:00",
                         "模型给的 expires_at 早于 valid_at → clamp（DB CHECK 不回归）")

    def test_model_valid_expires_kept(self):
        events = [_ev(0, "user", "我这几天都在准备考试。")]
        ok, rejected = _validate(
            [_cand(memory_type="current", content="用户最近在准备考试。",
                   confidence=0.8, importance=4,
                   valid_at="2026-09-10T10:00:00+00:00",
                   expires_at="2026-09-20T10:00:00+00:00")], events)
        self.assertEqual(rejected, [])
        self.assertEqual(ok[0]["expires_at"], "2026-09-20T10:00:00+00:00",
                         "模型给了合法 expires_at → 原样保留")

    def test_non_current_type_expires_stays_none(self):
        events = [_ev(0, "user", "我喜欢喝无糖咖啡。")]
        ok, rejected = _validate(
            [_cand(memory_type="long_term", content="用户喜欢喝无糖咖啡。",
                   confidence=0.85, importance=5, expires_at=None)], events)
        self.assertEqual(rejected, [])
        self.assertIsNone(ok[0]["expires_at"], "非 current 类型不补 expires_at")


# ==========================================
# D. 防模仿条款不回归
# ==========================================

class TestAntiMimicryUnchanged(unittest.TestCase):

    def _events(self):
        return [_ev(0, "user", "我昨天加班到十点才回家。"),
                _ev(1, "assistant", "辛苦啦，记得好好休息，明天会更好的呀。")]

    def test_verbatim_copy_still_rejected(self):
        ok, rejected = _validate(
            [_cand(content="记得好好休息，明天会更好的呀。")], self._events())
        self.assertEqual(ok, [])
        self.assertIn("VERBATIM_COPY", rejected)

    def test_role_prefix_still_rejected(self):
        ok, rejected = _validate(
            [_cand(content="assistant: 用户加班到十点。")], self._events())
        self.assertEqual(ok, [])
        self.assertIn("ROLE_PREFIX_CONTENT", rejected)

    def test_assistant_self_ref_still_rejected(self):
        ok, rejected = _validate(
            [_cand(content="我(Finn)：用户加班到十点。")], self._events())
        self.assertEqual(ok, [])
        self.assertIn("ASSISTANT_SELF_REF", rejected)

    def test_internal_markup_still_rejected(self):
        ok, rejected = _validate(
            [_cand(content="<final>用户加班到十点</final>")], self._events())
        self.assertEqual(ok, [])
        self.assertIn("INTERNAL_MARKUP", rejected)

    def test_over_inference_still_rejected(self):
        ok, rejected = _validate(
            [_cand(content="用户经常加班到十点。")], self._events())
        self.assertEqual(ok, [])
        self.assertIn("OVER_INFERENCE", rejected)

    def test_assistant_only_source_still_rejected(self):
        ok, rejected = _validate(
            [_cand(content="用户加班到十点。", source_event_indexes=[1])],
            self._events())
        self.assertEqual(ok, [])
        self.assertIn("ASSISTANT_ONLY_SOURCE", rejected)


# ==========================================
# E. 原有验证逻辑不回归
# ==========================================

class TestExistingValidationNoRegression(unittest.TestCase):

    def _events(self):
        return [_ev(0, "user", "我喜欢喝无糖咖啡。")]

    def test_importance_bounds(self):
        ok, rejected = _validate(
            [_cand(content="用户喜欢无糖咖啡。", importance=11)], self._events())
        self.assertEqual(rejected, ["INVALID_IMPORTANCE"])

    def test_confidence_bounds(self):
        ok, rejected = _validate(
            [_cand(content="用户喜欢无糖咖啡。", confidence=1.5)], self._events())
        self.assertEqual(rejected, ["INVALID_CONFIDENCE"])

    def test_empty_content_rejected(self):
        ok, rejected = _validate([_cand(content="   ")], self._events())
        self.assertEqual(rejected, ["EMPTY_CONTENT"])

    def test_source_index_out_of_range_rejected(self):
        ok, rejected = _validate(
            [_cand(content="用户喜欢无糖咖啡。", source_event_indexes=[5])],
            self._events())
        self.assertEqual(rejected, ["SOURCE_INDEX_OUT_OF_RANGE"])

    def test_in_batch_dedupe_merges_same_hash(self):
        events = [_ev(0, "user", "我喜欢喝无糖咖啡。")]
        result = _run_extract(
            [_cand(content="用户喜欢无糖咖啡。", confidence=0.6),
             _cand(content="用户喜欢无糖咖啡。", confidence=0.9)], events)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["candidates"]), 1, "批内同 content_hash 去重")
        self.assertEqual(result["candidates"][0]["confidence"], 0.9,
                         "保留 confidence 最高者")

    def test_metadata_keeps_explicit_request_flag(self):
        events = [_ev(0, "user", "请记住我喜欢无糖咖啡。")]
        ok, rejected = _validate(
            [_cand(content="用户喜欢无糖咖啡。")], events)
        self.assertEqual(rejected, [])
        self.assertTrue(ok[0]["metadata"]["explicit_memory_request"],
                        "显式记忆请求标记不回归")
