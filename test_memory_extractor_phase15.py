# -*- coding: utf-8 -*-
"""第 15 阶段专项测试 —— 严格 JSON 围栏兼容与 Prompt 精简。

第 14 阶段生产预览返回 JSON_PARSE_ERROR（模型输出非严格 JSON，具体形态未知）。
本阶段为「整个响应被单一完整 json 围栏包裹」增加最小兼容，其余形态仍严格拒绝；
同时精简 Prompt 消除重复、保留全部安全边界。

全部 mock + 合成数据，不触网、不写库。

运行：  python -m unittest test_memory_extractor_phase15 -v
"""

import json
import unittest

import memory_extractor as mx
import memory_preview as mp


GOOD_JSON = ('{"memories":[{"memory_type":"long_term",'
             '"content":"用户计划主要使用 TypeScript。",'
             '"importance":4,"confidence":0.9,"source_event_indexes":[0]}]}')


def fence(body, lang="json"):
    """构造单一完整 Markdown 围栏。"""
    return f"```{lang}\n{body}\n```"


def _parse(text):
    return mx.parse_memory_extraction_response(text)


# ==========================================
# A-D. 兼容形态
# ==========================================

class TestAcceptedForms(unittest.TestCase):

    def test_a_bare_json_still_supported(self):
        rows, err = _parse(GOOD_JSON)
        self.assertIsNone(err)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["memory_type"], "long_term")

    def test_b_single_json_fence_supported(self):
        rows, err = _parse(fence(GOOD_JSON))
        self.assertIsNone(err)
        self.assertEqual(rows[0]["content"], "用户计划主要使用 TypeScript。")

    def test_c_language_marker_case_variants(self):
        for lang in ("json", "JSON", "Json"):
            with self.subTest(lang=lang):
                rows, err = _parse(fence(GOOD_JSON, lang))
                self.assertIsNone(err, msg=lang)
                self.assertEqual(len(rows), 1)

    def test_d_no_language_marker_supported(self):
        rows, err = _parse(fence(GOOD_JSON, ""))
        self.assertIsNone(err)
        self.assertEqual(len(rows), 1)

    def test_b_empty_memories_in_fence(self):
        rows, err = _parse(fence('{"memories":[]}'))
        self.assertIsNone(err)
        self.assertEqual(rows, [])


# ==========================================
# E-M. 必须拒绝的围栏形态
# ==========================================

class TestRejectedFenceForms(unittest.TestCase):

    def test_e_explanation_before_fence_rejected(self):
        rows, err = _parse("以下是结果：\n" + fence(GOOD_JSON))
        self.assertIsNone(rows)
        self.assertEqual(err, "JSON_PARSE_ERROR")

    def test_f_explanation_after_fence_rejected(self):
        rows, err = _parse(fence(GOOD_JSON) + "\n以上是提取结果。")
        self.assertIsNone(rows)
        self.assertEqual(err, "JSON_PARSE_ERROR")

    def test_g_multiple_fences_rejected(self):
        rows, err = _parse(fence(GOOD_JSON) + "\n\n" + fence('{"memories":[]}', ""))
        self.assertIsNone(rows)
        self.assertEqual(err, "JSON_PARSE_ERROR")

    def test_h_unsupported_language_rejected(self):
        for lang in ("python", "javascript", "yaml", "JSON5"):
            with self.subTest(lang=lang):
                rows, err = _parse(fence(GOOD_JSON, lang))
                self.assertIsNone(rows)
                self.assertEqual(err, "JSON_PARSE_ERROR", msg=lang)

    def test_i_non_json_inside_fence_rejected(self):
        rows, err = _parse(fence("我认为用户喜欢咖啡", "json"))
        self.assertIsNone(rows)
        self.assertEqual(err, "JSON_PARSE_ERROR")

    def test_j_truncated_json_rejected(self):
        rows, err = _parse(fence('{"memories":[', "json"))
        self.assertIsNone(rows)
        self.assertEqual(err, "JSON_PARSE_ERROR")

    def test_k_top_level_array_rejected(self):
        for text in ("[]", fence("[]", "json")):
            with self.subTest(text=text[:20]):
                rows, err = _parse(text)
                self.assertIsNone(rows)
                self.assertEqual(err, "JSON_PARSE_ERROR")

    def test_l_python_dict_rejected_without_eval(self):
        rows, err = _parse(fence("{'memories': []}", "python"))
        self.assertIsNone(rows)
        self.assertEqual(err, "JSON_PARSE_ERROR")

    def test_m_json_with_explanation_inside_fence_rejected(self):
        rows, err = _parse(fence(GOOD_JSON + "\n以上是提取结果。", "json"))
        self.assertIsNone(rows)
        self.assertEqual(err, "JSON_PARSE_ERROR")

    def test_m2_missing_close_fence_rejected(self):
        rows, err = _parse("```json\n" + GOOD_JSON)
        self.assertIsNone(rows)
        self.assertEqual(err, "JSON_PARSE_ERROR")


# ==========================================
# N. 不修复畸形 JSON
# ==========================================

class TestNoJsonRepair(unittest.TestCase):

    def test_n_malformed_variants_rejected(self):
        cases = [
            ("trailing_comma", '{"memories":[],}'),
            ("single_quotes", fence("{'memories': []}", "json")),
            ("missing_bracket", '{"memories":['),
            ("unescaped_newline_in_string", '{"memories":[{"content":"a\nb"}]}'),
        ]
        for name, text in cases:
            with self.subTest(case=name):
                rows, err = _parse(text)
                self.assertIsNone(rows, msg=name)
                self.assertEqual(err, "JSON_PARSE_ERROR", msg=name)


# ==========================================
# O. API 失败契约回归（run_preview）
# ==========================================

class TestPreviewFailureContract(unittest.TestCase):

    def test_o_unparseable_output_keeps_zero_write_contract(self):
        import test_memory_preview_phase10 as t10
        rows = t10._rows_newest_first(("g1", "回复一", "用户消息一"))
        fake_service = t10.RecordingFakeService(rows)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = __import__("asyncio").run(mp.run_preview(
                fake_service, limit=10, ai_name="Finn", user_name="小满",
                llm_call=lambda p: fence("{broken json", "json")))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "JSON_PARSE_ERROR")
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["write_guards"]["memory_items_written"])
        self.assertFalse(result["write_guards"]["memory_events_updated"])
        self.assertFalse(result["write_guards"]["pinecone_touched"])
        self.assertFalse(result["status_plan"]["executed"])
        # 不返回模型原文
        self.assertNotIn("broken", json.dumps(result, ensure_ascii=False))


# ==========================================
# P. 第 13 阶段规则保持
# ==========================================

class TestPhase13RulesPreserved(unittest.TestCase):

    def _events(self):
        return [self._pev("user", "我把项目部署完了。"),
                self._pev("assistant", "<final>你太厉害了，还超额完成了两个阶段。</final>")]

    def _pev(self, role, content):
        return {"id": f"synthetic-{role}-1", "user_id": "synthetic-user",
                "session_id": None, "channel": "web", "role": role,
                "content": content, "occurred_at": "2026-08-29T16:00:00+00:00",
                "created_at": "2026-08-29T16:00:00+00:00",
                "source_event_id": f"synthetic-req:{role}",
                "processing_status": "pending", "metadata": {}}

    def _validate(self, cands, events):
        ok, rejected = [], []
        for c in cands:
            item, reason = mx.validate_and_normalize_candidate(
                c, events, "synthetic-user", "synthetic-batch", None)
            (ok if item is not None else rejected).append(item or reason)
        return ok, rejected

    def test_p_high_confidence_unsupported_evaluation_still_rejected(self):
        ok, rejected = self._validate(
            [{"memory_type": "moment",
              "content": "用户超额完成了两个阶段的工作。",
              "importance": 6, "confidence": 0.95, "source_event_indexes": [0, 1]}],
            self._events())
        self.assertEqual(ok, [])
        self.assertIn("EVALUATION_UNSUPPORTED", rejected)

    def test_p_vague_reference_still_rejected(self):
        ok, rejected = self._validate(
            [{"memory_type": "current", "content": "用户要求相应延长结束时间。",
              "importance": 3, "confidence": 0.9,
              "valid_at": "2026-08-29T16:00:00+00:00",
              "expires_at": "2026-08-29T17:00:00+00:00",
              "source_event_indexes": [0]}],
            self._events())
        self.assertIn("VAGUE_REFERENCE", rejected)

    def test_p_assistant_only_source_still_rejected(self):
        ok, rejected = self._validate(
            [{"memory_type": "long_term", "content": "用户完成了部署工作。",
              "importance": 4, "confidence": 0.9, "source_event_indexes": [1]}],
            self._events())
        self.assertIn("ASSISTANT_ONLY_SOURCE", rejected)

    def test_p_current_expiry_still_added(self):
        # current 未给 expires_at 时按常量补默认 7 天（阶段 A4；提取器不解析自然语言时长）
        events = [self._pev("user", "我先去洗澡，大约一小时后回来。")]
        ok, rejected = self._validate(
            [{"memory_type": "current", "content": "用户暂时离开去洗澡。",
              "importance": 3, "confidence": 0.9,
              "valid_at": "2026-08-29T16:30:00+00:00", "expires_at": None,
              "source_event_indexes": [0]}],
            events)
        self.assertEqual(rejected, [])
        self.assertEqual(ok[0]["expires_at"], "2026-09-05T16:30:00+00:00",
                         "默认过期 = valid_at + 7 天")

    def test_p_final_cleanup_still_working(self):
        events = self._events()
        prompt = mx.build_memory_extraction_prompt(events, ai_name="Finn", user_name="小满")
        self.assertNotIn("<final>", prompt)
        self.assertIn("你太厉害了", prompt, "剥离包装后正文保留供理解对话")


# ==========================================
# Q. Prompt 精简规则完整性
# ==========================================

class TestPromptSlimCompleteness(unittest.TestCase):

    @classmethod
    def _prompt(cls):
        events = [{"id": "e1", "user_id": "u", "channel": "web", "role": "user",
                   "content": "合成用户消息。", "occurred_at": "t"}]
        return mx.build_memory_extraction_prompt(events, ai_name="Finn", user_name="小满")

    def test_q_all_required_rules_present(self):
        prompt = self._prompt()
        required = [
            "事实提取模块", "不是回复生成模块",                       # 1
            "用户明确表达",                                        # 2
            "当成用户事实",                                        # 3
            "禁止复制或改写",                                      # 4
            "可独立核验的事实", "拆成多个候选", "直接丢弃",            # 5-7
            "含糊指代", "转录",                                    # 8
            "评价（超额", "数量（两个阶段", "频率（经常",              # 9
            "条件（在某种情况下", "原因和因果", "时间跨度（长期",        # 9
            "自评分", "不能证明",                                  # 10
            "不要轻易输出 core",                                   # 11
            "必须给 expires_at",                                   # 12
            "core / current / long_term / moment / memo",          # 13
            "source_event_indexes 必须引用这些索引",                  # 14
            "memories 最多",                                       # 15
            "只返回一个 JSON 对象",                                 # 16
            "不要 Markdown 代码围栏",                               # 17
            '{"memories":[]}',                                     # 18
            "证据边界", "禁止把 AI 的猜测",                           # assistant 边界
        ]
        for phrase in required:
            self.assertIn(phrase, prompt, msg=f"Prompt 缺少规则: {phrase}")

    def test_q_slimmer_than_previous(self):
        # 第 15 阶段前 Prompt 为 2387 字符；精简后不得增长
        self.assertLessEqual(len(self._prompt()), 2387)


# ==========================================
# R. 零写入源码约束
# ==========================================

class TestZeroWrite(unittest.TestCase):

    def test_r_extractor_source_has_no_db_or_retry(self):
        with open("memory_extractor.py", encoding="utf-8") as f:
            src = f.read()
        for token in (".insert(", ".upsert(", ".update(", ".delete(", ".rpc(",
                      "create_client", "supabase", "pinecone", "Pinecone",
                      "eval(", "ast.literal_eval", "time.sleep",
                      "asyncio.sleep(1"):
            self.assertNotIn(token, src, msg=f"memory_extractor.py 不得包含: {token}")

    def test_r_no_second_llm_call(self):
        # 模块内 LLM 调用入口唯一（make_compression_llm_call），无第二次调用构造
        with open("memory_extractor.py", encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("make_compression_llm_call"), 2,
                         "工厂定义 + 文档/调用说明共 2 处，无额外调用")
        self.assertNotIn("ask_role_sync(\"compression\", prompt, temperature=0.7)\n        return",
                         src)


if __name__ == "__main__":
    unittest.main()
