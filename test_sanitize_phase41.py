"""
test_sanitize_phase41.py — 第 4.1 阶段日志脱敏与空消息清洗专项测试
================================================================
unittest + mock，不触生产数据/外部服务。
覆盖 A-N: 日志脱敏、空消息清洗、tool_calls保留、多模态、不原地修改、回归
"""

import unittest
import copy
from unittest.mock import Mock, patch


# ============================================================
# A: search_memory 日志脱敏
# ============================================================
class TestSearchMemoryLogRedaction(unittest.TestCase):

    def _call(self, name, ok, text):
        import tool_loop
        return tool_loop._safe_tool_log_text(name, ok, text)

    def test_a_search_memory_ok_no_body(self):
        """A: search_memory 成功时只返回数量或'正文已隐藏'，不输出正文。"""
        text = "🧠 【语义相似记忆】:\n- user: SYNTHETIC_USER_SECRET | assistant: SYNTHETIC_ASSISTANT_SECRET\n- memory: SYNTHETIC_MEMORY"
        result = self._call("search_memory", True, text)
        self.assertNotIn("SYNTHETIC_USER_SECRET", result)
        self.assertNotIn("SYNTHETIC_ASSISTANT_SECRET", result)
        self.assertNotIn("SYNTHETIC_MEMORY", result)
        self.assertIn("OK", result)
        self.assertIn("已隐藏", result)

    def test_a_search_memory_count(self):
        """A: search_memory 返回 2 条记忆时日志含数量。"""
        text = "🧠 【语义相似记忆】:\n- item1\n- item2"
        result = self._call("search_memory", True, text)
        self.assertIn("2", result)
        self.assertIn("已隐藏", result)

    def test_a_search_memory_fail(self):
        """A: search_memory 失败时不输出错误正文。"""
        result = self._call("search_memory", False, "Error: SYNTHETIC_ERROR_DETAIL")
        self.assertNotIn("SYNTHETIC_ERROR_DETAIL", result)
        self.assertIn("FAIL", result)

    def test_a_other_tools_not_redacted(self):
        """A: 其他工具保留原有截断行为。"""
        result = self._call("wallet_check", True, "余额 100")
        self.assertIn("OK", result)
        self.assertIn("余额 100", result)

    def test_a_no_base64_in_log(self):
        """A: 日志不含 Base64。"""
        text = "- data:image/png;base64,SYNTHETIC_BASE64_DATA"
        result = self._call("search_memory", True, text)
        self.assertNotIn("SYNTHETIC_BASE64_DATA", result)
        self.assertNotIn("base64", result.lower())


# ============================================================
# B-L: 空消息清洗函数
# ============================================================
class TestSanitizeOutgoingMessages(unittest.TestCase):

    def _call(self, messages):
        import gateway
        return gateway._sanitize_outgoing_messages(messages)

    def test_b_empty_user_deleted(self):
        """B: 空 user 消息被删除。"""
        msgs = [
            {"role": "user", "content": ""},
            {"role": "user", "content": "   "},
            {"role": "user", "content": None},
        ]
        result = self._call(msgs)
        self.assertEqual(len(result), 0)

    def test_c_empty_assistant_deleted(self):
        """C: 空 assistant 消息被删除。"""
        msgs = [
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "valid reply"},
        ]
        result = self._call(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "valid reply")

    def test_d_assistant_tool_calls_preserved(self):
        """D: 带 tool_calls 的 assistant 消息保留，即使 content 为空。"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "function": {"name": "weather"}}]},
        ]
        result = self._call(msgs)
        self.assertEqual(len(result), 1)
        self.assertIn("tool_calls", result[0])
        self.assertEqual(result[0]["tool_calls"][0]["id"], "call_1")

    def test_e_tool_call_id_preserved(self):
        """E: 带 tool_call_id 的 tool 消息保留，即使 content 为空。"""
        msgs = [
            {"role": "tool", "content": "", "tool_call_id": "call_1", "name": "weather"},
        ]
        result = self._call(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tool_call_id"], "call_1")

    def test_f_multimodal_preserved(self):
        """F: 有效多模态内容保留。"""
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "synthetic://image"}},
                {"type": "file", "file": {"url": "synthetic://file"}},
            ]},
        ]
        result = self._call(msgs)
        self.assertEqual(len(result), 1)
        parts = result[0]["content"]
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[1]["image_url"]["url"], "synthetic://image")

    def test_g_empty_text_part_deleted(self):
        """G: 多模态数组中的空 text part 被删除。"""
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "   "},
                {"type": "text", "text": "有效内容"},
                {"type": "image_url", "image_url": {"url": "synthetic://image"}},
            ]},
        ]
        result = self._call(msgs)
        parts = result[0]["content"]
        self.assertEqual(len(parts), 2)  # empty text removed
        self.assertEqual(parts[0]["text"], "有效内容")
        self.assertEqual(parts[1]["type"], "image_url")

    def test_h_array_all_empty_deleted(self):
        """H: 只有空白 text part 的数组消息被删除。"""
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "   "},
                {"type": "text", "text": ""},
            ]},
        ]
        result = self._call(msgs)
        self.assertEqual(len(result), 0)

    def test_i_system_preserved(self):
        """I: 空 system 消息不被删除。"""
        msgs = [
            {"role": "system", "content": ""},
        ]
        result = self._call(msgs)
        self.assertEqual(len(result), 1)

    def test_j_order_preserved(self):
        """J: 清洗后剩余消息相对顺序不变。"""
        msgs = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": ""},  # will be deleted
            {"role": "user", "content": "msg2"},
            {"role": "assistant", "content": "reply2"},
            {"role": "user", "content": ""},  # will be deleted
            {"role": "user", "content": "msg3"},
        ]
        result = self._call(msgs)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0]["content"], "msg1")
        self.assertEqual(result[1]["content"], "msg2")
        self.assertEqual(result[2]["content"], "reply2")
        self.assertEqual(result[3]["content"], "msg3")

    def test_k_not_modified_in_place(self):
        """K: 原始输入不被修改。"""
        msgs = [
            {"role": "user", "content": "keep"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": [
                {"type": "text", "text": "  "},
                {"type": "image_url", "image_url": {"url": "synthetic://img"}},
            ]},
        ]
        original = copy.deepcopy(msgs)
        self._call(msgs)
        self.assertEqual(msgs, original)

    def test_l_normal_messages_unchanged(self):
        """L: 普通消息完全保持不变。"""
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = self._call(msgs)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["content"], "hello")
        self.assertEqual(result[1]["content"], "hi")

    def test_non_list_input(self):
        """非 list 输入原样返回。"""
        self.assertIsNone(self._call(None))
        self.assertEqual(self._call("string"), "string")

    def test_empty_list(self):
        """空 list 返回空 list。"""
        result = self._call([])
        self.assertEqual(result, [])


# ============================================================
# N: 回归测试
# ============================================================
class TestRegression(unittest.TestCase):

    def test_n_phase3_reasoning_still_stripped(self):
        """N: Phase 3 reasoning 清洗仍生效。"""
        import gateway
        msgs = [{"role": "assistant", "content": "reply", "reasoning_content": "secret"}]
        result = gateway._strip_incoming_reasoning(msgs)
        self.assertNotIn("reasoning_content", result[0])

    def test_n_phase38_extract_text_still_works(self):
        """N: Phase 3.8 多模态文本提取仍生效。"""
        import gateway
        content = [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]
        self.assertEqual(gateway._extract_message_text(content), "hello")


if __name__ == "__main__":
    unittest.main()
