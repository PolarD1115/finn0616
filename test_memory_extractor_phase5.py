# -*- coding: utf-8 -*-
"""第 5 阶段专项测试 —— 长期事实提取器（离线 Mock 阶段）。

全部使用合成数据与注入的 mock LLM；不连接真实 Supabase、不调用真实 LLM、
不写任何数据库。合成标识：synthetic-user / synthetic-request。

覆盖（任务 A-L）：
  A 正常事实提取  B 普通闲聊过滤  C assistant 不得成为记忆  D current 自动过期
  E core 高门槛   F moment 无回复范例  G memo  H 非法 JSON/字段
  I 批内精确去重  J LLM 异常  K 失败状态计划  L 数据库零调用约束

运行：  python -m unittest test_memory_extractor_phase5 -v
"""

import asyncio
import hashlib
import io
import json
import os
import unittest
import uuid
from contextlib import redirect_stdout

import memory_extractor as mx


# ==========================================
# 合成事件工厂（全部虚构脱敏）
# ==========================================

def _ev(idx, role, content, occurred="2026-08-28T10:00:00+08:00", channel="web"):
    """构造合成 memory_events 行；id 为确定性合成 UUID（非真实数据）。"""
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"synthetic-event-{idx}")),
        "user_id": "synthetic-user",
        "session_id": "synthetic-session",
        "channel": channel,
        "role": role,
        "content": content,
        "occurred_at": occurred,
        "created_at": "2026-08-28T10:01:00+08:00",
        "source_event_id": f"synthetic-request:{role}",
        "processing_status": "pending",
        "metadata": {},
    }


USER_TS = "我最近开始学习 TypeScript，之后项目准备主要使用它。"
ASSISTANT_ACK = "好的，我会记住这件事。"


def _llm_returning(payload):
    """返回注入用的 mock llm_call：同步 callable(prompt)->str。"""
    def _call(prompt):
        return json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else payload
    return _call


def _run(events, llm_call):
    """执行提取并捕获 stdout 日志，返回 (result, log_text)。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = asyncio.run(mx.extract_memory_candidates(
            events, llm_call, user_id="synthetic-user",
            ai_name="Finn", user_name="小满"))
    return result, buf.getvalue()


def _cand(**overrides):
    """一条合法 long_term 候选模板。"""
    base = {
        "memory_type": "long_term",
        "content": "用户目前计划主要使用 TypeScript 开发项目。",
        "subject_key": "primary_programming_language",
        "importance": 4,
        "confidence": 0.92,
        "valid_at": "2026-08-28T10:00:00+08:00",
        "invalid_at": None,
        "expires_at": None,
        "source_event_indexes": [0],
        "reason": "用户明确表达了持续性的技术选择",
    }
    base.update(overrides)
    return base


# ==========================================
# A. 正常事实提取
# ==========================================

class TestNormalExtraction(unittest.TestCase):

    def test_a_normal_long_term_candidate(self):
        events = [_ev(0, "user", USER_TS), _ev(1, "assistant", ASSISTANT_ACK)]
        result, log = _run(events, _llm_returning({"memories": [_cand()]}))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error_code"])
        self.assertEqual(len(result["candidates"]), 1)
        item = result["candidates"][0]
        self.assertEqual(item["memory_type"], "long_term")
        self.assertEqual(item["content"], "用户目前计划主要使用 TypeScript 开发项目。")
        self.assertEqual(item["status"], "pending_review")
        self.assertEqual(item["created_by"], "memory_extractor")
        self.assertEqual(item["source"], "web")
        # source_event_ids 由代码从输入事件 id 生成，不来自模型
        self.assertEqual(item["source_event_ids"], [events[0]["id"]])
        # content_hash 与标准 SHA-256 一致
        self.assertEqual(item["content_hash"],
                         hashlib.sha256(item["content"].encode("utf-8")).hexdigest())
        self.assertEqual(item["source_batch_id"], result["batch_id"])
        self.assertIsNone(item["superseded_by"])
        self.assertIsNone(item["invalid_at"])
        # 不包含 assistant 回复内容
        self.assertNotIn("我会记住", item["content"])
        self.assertNotIn(ASSISTANT_ACK, item["content"])
        # 日志不含正文与 Prompt
        self.assertNotIn(USER_TS, log)
        self.assertNotIn("事实提取模块", log)

    def test_a_model_uuid_forgery_never_used(self):
        # 模型伪造 source_event_ids / 直接给 uuid → 一律忽略，代码从 indexes 生成
        events = [_ev(0, "user", USER_TS)]
        cand = _cand(source_event_ids=["00000000-0000-0000-0000-000000000000"],
                     source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidates"][0]["source_event_ids"], [events[0]["id"]])


# ==========================================
# B. 普通闲聊被过滤
# ==========================================

class TestSmallTalkFiltered(unittest.TestCase):

    def test_b_empty_memories_is_processed_ok(self):
        events = [_ev(0, "user", "哈哈，今天聊得挺开心。")]
        result, _ = _run(events, _llm_returning({"memories": []}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["status_plan"]["processing_status"], "processed")

    def test_b_low_value_greeting_rejected(self):
        events = [_ev(0, "user", "哈哈，今天聊得挺开心。")]
        cand = _cand(content="哈哈，今天聊得挺开心。", memory_type="moment",
                     source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ALL_CANDIDATES_REJECTED")
        self.assertIn("LOW_VALUE_GREETING", result["rejected"])
        self.assertEqual(result["status_plan"]["processing_status"], "failed")


# ==========================================
# C. assistant 回复不得成为记忆
# ==========================================

class TestAssistantIsolation(unittest.TestCase):

    def test_c_assistant_only_source_rejected(self):
        events = [_ev(0, "user", "我喜欢无糖咖啡"),
                  _ev(1, "assistant", "当然可以，我会永远记住你的偏好。")]
        cand = _cand(content="AI 会永远记住用户的偏好，并会温柔地陪伴用户。",
                     source_event_indexes=[1])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertFalse(result["ok"])
        self.assertIn("ASSISTANT_ONLY_SOURCE", result["rejected"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["status_plan"]["processing_status"], "failed")

    def test_c_verbatim_copy_of_assistant_rejected(self):
        # 候选引用了 user 事件，但内容是 assistant 原文的照抄 → verbatim-copy 拦截
        events = [_ev(0, "user", "我喜欢无糖咖啡"),
                  _ev(1, "assistant", "当然可以，我会一直温柔地陪着你，永远记住你的偏好。")]
        cand = _cand(content="当然可以，我会一直温柔地陪着你，永远记住你的偏好。",
                     source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertFalse(result["ok"])
        self.assertIn("VERBATIM_COPY", result["rejected"])

    def test_c_role_prefix_rejected(self):
        events = [_ev(0, "user", USER_TS)]
        cand = _cand(content="我(Finn)：用户喜欢 TypeScript。", source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertIn("ASSISTANT_SELF_REF", result["rejected"])
        cand2 = _cand(content="回复: 用户喜欢 TypeScript。", source_event_indexes=[0])
        result2, _ = _run(events, _llm_returning({"memories": [cand2]}))
        self.assertIn("ROLE_PREFIX_CONTENT", result2["rejected"])

    def test_c_user_promise_wording_allowed(self):
        # user 来源的「我会」是合法承诺事实，不因「我会」开头被误拒
        events = [_ev(0, "user", "我决定每天晚上十一点前睡觉，我会坚持这个计划。")]
        cand = _cand(content="用户决定每天晚上十一点前睡觉，并会坚持这个计划。",
                     source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertTrue(result["ok"], msg=f"rejected={result['rejected']}")

    def test_c_question_with_fact_signal_kept(self):
        # 「你能记住我喜欢喝美式咖啡吗？」—— 问句形态但含事实信号，不得误拒
        events = [_ev(0, "user", "你能记住我喜欢喝美式咖啡吗？")]
        cand = _cand(content="用户喜欢喝美式咖啡，并要求 AI 记住这一偏好。",
                     memory_type="long_term", source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertTrue(result["ok"], msg=f"rejected={result['rejected']}")


# ==========================================
# D. current 自动过期
# ==========================================

class TestCurrentExpiry(unittest.TestCase):

    def test_d_default_expiry_added(self):
        events = [_ev(0, "user", "我这几天都在准备考试，最近睡得也不太好。")]
        cand = _cand(content="用户最近在准备考试，睡眠质量不佳。", memory_type="current",
                     confidence=0.8, importance=4, valid_at=None, expires_at=None,
                     source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertTrue(result["ok"])
        item = result["candidates"][0]
        self.assertEqual(item["memory_type"], "current")
        self.assertIsNotNone(item["expires_at"], "current 未给 expires_at 时必须补默认有限期")
        # 默认过期 = occurred_at + 7 天（阶段 A4；事件 occurred_at 为 2026-08-28T10:00+08:00）
        expected = "2026-09-04T10:00:00+08:00"
        self.assertEqual(item["expires_at"], expected)
        self.assertNotEqual(item["memory_type"], "core")

    def test_d_model_expiry_kept_and_clamped(self):
        events = [_ev(0, "user", "我这几天都在准备考试。")]
        # 模型给了 expires_at → 保留
        cand = _cand(content="用户最近在准备考试。", memory_type="current",
                     valid_at="2026-08-28T10:00:00+08:00",
                     expires_at="2026-09-04T10:00:00+08:00", source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertEqual(result["candidates"][0]["expires_at"], "2026-09-04T10:00:00+08:00")
        # 模型给的 expires_at 早于 valid_at → clamp 到 valid_at（满足 DB CHECK）
        cand2 = _cand(content="用户最近在准备考试。", memory_type="current",
                      valid_at="2026-08-28T10:00:00+08:00",
                      expires_at="2026-08-27T10:00:00+08:00", source_event_indexes=[0])
        result2, _ = _run(events, _llm_returning({"memories": [cand2]}))
        self.assertEqual(result2["candidates"][0]["expires_at"], "2026-08-28T10:00:00+08:00")

    def test_d_invalid_time_rejected(self):
        events = [_ev(0, "user", "我这几天都在准备考试。")]
        cand = _cand(content="用户最近在准备考试。", memory_type="current",
                     expires_at="下周三", source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertIn("INVALID_TIME", result["rejected"])


# ==========================================
# E. core 高门槛
# ==========================================

class TestCoreGate(unittest.TestCase):

    def test_e1_ordinary_preference_downgraded(self):
        # 普通一次性偏好，模型却标 core 且置信度不足 → 降级 long_term
        events = [_ev(0, "user", "我最近喜欢上了喝美式咖啡。")]
        cand = _cand(content="用户最近喜欢喝美式咖啡。", memory_type="core",
                     confidence=0.85, importance=5, source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidates"][0]["memory_type"], "long_term")

    def test_e2_low_confidence_core_downgraded(self):
        events = [_ev(0, "user", USER_TS)]
        cand = _cand(memory_type="core", confidence=0.88, importance=9,
                     source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertEqual(result["candidates"][0]["memory_type"], "long_term")

    def test_e3_explicit_memory_request_keeps_core(self):
        # 用户明确要求记住（user 事件含「记住」）→ core 跳过降级并提升 importance
        events = [_ev(0, "user", "请一定记住我的生日是5月1日。")]
        cand = _cand(content="用户的生日是5月1日。", memory_type="core",
                     confidence=0.95, importance=6, source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertTrue(result["ok"])
        item = result["candidates"][0]
        self.assertEqual(item["memory_type"], "core")
        self.assertEqual(item["importance"], 8)
        self.assertTrue(item["metadata"]["explicit_memory_request"])

    def test_e4_assistant_only_core_rejected(self):
        events = [_ev(0, "user", "随便聊聊"), _ev(1, "assistant", ASSISTANT_ACK)]
        cand = _cand(content="用户的核心身份设定是某某。", memory_type="core",
                     confidence=0.99, importance=10, source_event_indexes=[1])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertIn("ASSISTANT_ONLY_SOURCE", result["rejected"])


# ==========================================
# F. moment 不得包含回复范例
# ==========================================

class TestMomentRules(unittest.TestCase):

    def test_f_valid_moment_accepted(self):
        events = [_ev(0, "user", "我们今天把科一的错题都整理完了，下次继续复盘。"),
                  _ev(1, "assistant", "好的，我会一直陪着你复习。")]
        cand = _cand(content="用户和 AI 一起整理过考试错题，并约定之后继续复盘。",
                     memory_type="moment", source_event_indexes=[0, 1])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertTrue(result["ok"], msg=f"rejected={result['rejected']}")
        item = result["candidates"][0]
        self.assertEqual(item["memory_type"], "moment")
        self.assertNotIn("我会一直陪着", item["content"], "不得包含 assistant 原文")
        self.assertNotIn("好的，", item["content"])

    def test_f_moment_with_assistant_quote_rejected(self):
        events = [_ev(0, "user", "我们今天把科一的错题都整理完了。"),
                  _ev(1, "assistant", "好的，我会一直温柔地陪着你复习，直到考试结束。")]
        cand = _cand(content='AI 当时温柔地说："我会一直温柔地陪着你复习，直到考试结束。"',
                     memory_type="moment", source_event_indexes=[0, 1])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertTrue(len(result["rejected"]) > 0, "包含 assistant 原文的 moment 必须被拒")


# ==========================================
# G. memo
# ==========================================

class TestMemoRules(unittest.TestCase):

    def test_g_valid_memo_accepted(self):
        events = [_ev(0, "user", "租房合同还有几个细节没谈完，下次接着说。")]
        cand = _cand(content="用户提到租房合同细节尚未谈完，下次对话需要接续讨论。",
                     memory_type="memo", source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidates"][0]["memory_type"], "memo")

    def test_g_full_transcript_rejected(self):
        events = [_ev(0, "user", "租房合同还有几个细节没谈完，下次接着说。")]
        cand = _cand(content="用户: 租房合同还有几个细节没谈完，下次接着说。\n回复: 好的。",
                     memory_type="memo", source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [cand]}))
        self.assertIn("ROLE_PREFIX_CONTENT", result["rejected"])


# ==========================================
# H. 非法 JSON / 字段
# ==========================================

class TestInvalidInput(unittest.TestCase):

    def _bad_cases(self):
        good = {"memories": [_cand()]}
        return [
            ("empty_string", "", "EMPTY_RESPONSE"),
            ("plain_text", "你好，我不是 JSON", "JSON_PARSE_ERROR"),
            # 🔒 第 15 阶段：单一完整围栏已兼容（见 test_h_single_fence_now_accepted），
            #    围栏外说明 / 多围栏仍拒绝：
            ("fence_with_explanation", "以下是结果：\n```json\n" + json.dumps(good) + "\n```", "JSON_PARSE_ERROR"),
            ("fence_then_explanation", "```json\n" + json.dumps(good) + "\n```\n以上是提取结果。", "JSON_PARSE_ERROR"),
            ("multiple_fences", "```json\n" + json.dumps(good) + "\n```\n```text\nx\n```", "JSON_PARSE_ERROR"),
            ("top_level_array", json.dumps([_cand()]), "JSON_PARSE_ERROR"),
            ("memories_missing", json.dumps({"foo": 1}), "JSON_PARSE_ERROR"),
            ("memories_not_list", json.dumps({"memories": "x"}), "JSON_PARSE_ERROR"),
            ("item_not_dict", json.dumps({"memories": ["x"]}), "JSON_PARSE_ERROR"),
        ]

    def test_h_parse_level_rejections(self):
        events = [_ev(0, "user", USER_TS), _ev(1, "assistant", ASSISTANT_ACK)]
        for name, text, expected_err in self._bad_cases():
            with self.subTest(case=name):
                result, _ = _run(events, _llm_returning(text))
                self.assertFalse(result["ok"], msg=name)
                self.assertEqual(result["error_code"], expected_err, msg=name)
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["status_plan"]["processing_status"], "failed")

    def test_h_single_fence_now_accepted(self):
        # 🔒 第 15 阶段：整个响应被单一完整 json 围栏包裹时兼容（剥离后严格解析）
        events = [_ev(0, "user", USER_TS), _ev(1, "assistant", ASSISTANT_ACK)]
        fenced = "```json\n" + json.dumps({"memories": [_cand()]}, ensure_ascii=False) + "\n```"
        result, _ = _run(events, _llm_returning(fenced))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["memory_type"], "long_term")

    def test_h_validation_level_rejections(self):
        events = [_ev(0, "user", USER_TS), _ev(1, "assistant", ASSISTANT_ACK)]
        cases = [
            ("missing_content", {"memory_type": "long_term", "source_event_indexes": [0]}, "EMPTY_CONTENT"),
            ("invalid_memory_type", _cand(memory_type="raw_event"), "INVALID_MEMORY_TYPE"),
            ("assistant_style_type", _cand(memory_type="reply_example"), "INVALID_MEMORY_TYPE"),
            ("invalid_time", _cand(valid_at="不是时间"), "INVALID_TIME"),
            ("index_out_of_range", _cand(source_event_indexes=[99]), "SOURCE_INDEX_OUT_OF_RANGE"),
            ("index_assistant_only", _cand(source_event_indexes=[1]), "ASSISTANT_ONLY_SOURCE"),
            ("too_long_content", _cand(content="长" * 501), "CONTENT_TOO_LONG"),
            ("missing_indexes", {"memory_type": "long_term", "content": "用户在学 TypeScript。"}, "MISSING_SOURCE_INDEXES"),
            ("invalid_importance", _cand(importance=99), "INVALID_IMPORTANCE"),
            ("invalid_confidence", _cand(confidence=1.5), "INVALID_CONFIDENCE"),
        ]
        for name, cand, expected_reason in cases:
            with self.subTest(case=name):
                result, _ = _run(events, _llm_returning({"memories": [cand]}))
                self.assertFalse(result["ok"], msg=name)
                self.assertIn(expected_reason, result["rejected"], msg=name)
                self.assertEqual(result["candidates"], [], msg=name)
                self.assertEqual(result["error_code"], "ALL_CANDIDATES_REJECTED", msg=name)
                self.assertEqual(result["status_plan"]["processing_status"], "failed", msg=name)

    def test_h_partial_pass_is_processed(self):
        # 一条合法 + 一条被拒 → processed（正常过滤），拒绝记录在 rejected
        events = [_ev(0, "user", USER_TS)]
        good = _cand()
        bad = _cand(content="x", memory_type="raw_event", source_event_indexes=[0])
        result, _ = _run(events, _llm_returning({"memories": [good, bad]}))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(result["status_plan"]["processing_status"], "processed")


# ==========================================
# I. 批内精确去重
# ==========================================

class TestBatchDedup(unittest.TestCase):

    def test_i_same_content_keeps_best(self):
        events = [_ev(0, "user", USER_TS), _ev(1, "user", "对，我之后项目就主要用 TypeScript 了。")]
        c1 = _cand(confidence=0.8, source_event_indexes=[0])
        c2 = _cand(confidence=0.95, source_event_indexes=[1])
        result, _ = _run(events, _llm_returning({"memories": [c1, c2]}))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["candidates"]), 1, "同 content_hash 只保留一条")
        item = result["candidates"][0]
        self.assertEqual(item["confidence"], 0.95, "保留置信度更高的一条")
        self.assertEqual(len(item["source_event_ids"]), 2, "合并来源（并集）")


# ==========================================
# J. LLM 异常
# ==========================================

class TestLlmFailure(unittest.TestCase):

    def test_j_llm_exception_returns_failure(self):
        events = [_ev(0, "user", USER_TS)]
        def _boom(prompt):
            raise RuntimeError("mock: 全部端点失败")
        result, log = _run(events, _boom)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "LLM_ERROR")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["status_plan"]["processing_status"], "failed")
        self.assertEqual(result["status_plan"]["last_error"], "LLM_ERROR")
        # 不向上抛（执行到此即通过）；日志不含正文与 Prompt
        self.assertNotIn(USER_TS, log)
        self.assertNotIn("事实提取模块", log)


# ==========================================
# K. 失败状态计划
# ==========================================

class TestStatusPlans(unittest.TestCase):

    def test_k_json_failure_plan(self):
        events = [_ev(0, "user", USER_TS)]
        result, _ = _run(events, _llm_returning("不是 JSON"))
        plan = result["status_plan"]
        self.assertEqual(plan["processing_status"], "failed")
        self.assertEqual(plan["last_error"], "JSON_PARSE_ERROR")
        self.assertIsNone(plan["processed_at"])
        self.assertEqual(plan["attempt_count_increment"], 1)
        self.assertEqual(len(plan["event_ids"]), 1)
        self.assertEqual(plan["batch_id"], result["batch_id"])

    def test_k_llm_failure_plan(self):
        events = [_ev(0, "user", USER_TS)]
        def _boom(prompt):
            raise RuntimeError("mock")
        result, _ = _run(events, _boom)
        self.assertEqual(result["status_plan"]["last_error"], "LLM_ERROR")

    def test_k_all_rejected_plan(self):
        events = [_ev(0, "user", USER_TS)]
        result, _ = _run(events, _llm_returning({"memories": [_cand(memory_type="raw_event")]}))
        self.assertEqual(result["status_plan"]["processing_status"], "failed")
        self.assertEqual(result["status_plan"]["last_error"], "ALL_CANDIDATES_REJECTED")

    def test_k_success_plan_only_after_validation(self):
        events = [_ev(0, "user", USER_TS), _ev(1, "assistant", ASSISTANT_ACK)]
        result, _ = _run(events, _llm_returning({"memories": [_cand()]}))
        plan = result["status_plan"]
        self.assertEqual(plan["processing_status"], "processed")
        self.assertIsNotNone(plan["processed_at"])
        self.assertIsNone(plan["last_error"])
        # 计划覆盖全部输入事件（含 assistant）
        self.assertEqual(len(plan["event_ids"]), 2)


# ==========================================
# L. 数据库零调用约束
# ==========================================

class TestNoDatabaseAccess(unittest.TestCase):

    def test_l_source_has_no_db_or_pinecone_calls(self):
        with open(os.path.join(os.path.dirname(__file__), "memory_extractor.py"),
                  encoding="utf-8") as f:
            src = f.read()
        forbidden = [".insert(", ".upsert(", ".update(", ".delete(",
                     "create_client", "supabase", "pinecone", "Pinecone",
                     "SUPABASE_KEY", "SUPABASE_SERVICE_KEY", "table("]
        for token in forbidden:
            self.assertNotIn(token, src, msg=f"提取器模块不得包含数据库/Pinecone 相关调用: {token}")

    def test_l_real_llm_factory_is_lazy(self):
        # 真实调用工厂存在但惰性 import server——import 本模块不触发任何网络/DB
        with open(os.path.join(os.path.dirname(__file__), "memory_extractor.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("def make_compression_llm_call", src)
        self.assertIn("import server", src)
        # import server 必须位于函数体内（惰性），模块级不得导入
        module_level = src.split("def make_compression_llm_call")[0]
        self.assertNotIn("import server", module_level)

    def test_l_no_env_reads(self):
        with open(os.path.join(os.path.dirname(__file__), "memory_extractor.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("os.environ", src, "提取器不得读取环境变量（含密钥）")

    def test_l_no_auto_scheduling(self):
        # 模块不含任何自动启动/后台调度代码
        with open(os.path.join(os.path.dirname(__file__), "memory_extractor.py"),
                  encoding="utf-8") as f:
            src = f.read()
        for token in ("asyncio.create_task", "threading.Thread", "while True",
                      "APScheduler", "__main__"):
            self.assertNotIn(token, src, msg=f"不得包含自动调度: {token}")


if __name__ == "__main__":
    unittest.main()
