"""
test_legacy_isolation_phase5.py — 第 5 阶段旧 Pinecone assistant 混合向量隔离专项测试
================================================================
unittest + mock，不触生产数据/外部服务。
覆盖 A-P: v2保留、assistant_format过滤、legacy user-only保留、curated保留、
  metadata缺失、空memory、混合5条、顺序、网页/TG/search_memory路径、
  日志脱敏、召回行为保护、回归
"""

import unittest
import copy
from unittest.mock import Mock, patch
import io
import sys


# ============================================================
# A-I: _filter_recalled_memories + _is_assistant_format 纯函数测试
# ============================================================
class TestFilterRecalledMemories(unittest.TestCase):

    def _filter(self, results, source="test"):
        import server
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            return server._filter_recalled_memories(results, source)
        finally:
            sys.stdout = old

    def _is_af(self, text):
        import server
        return server._is_assistant_format(text)

    def test_a_v2_user_preserved(self):
        """A: v2 user 结果保留。"""
        results = [{"memory": "user: 用户事实", "schema_version": "v2", "score": 0.7}]
        kept, stats = self._filter(results, "web_user")
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats["kept_count"], 1)
        self.assertEqual(stats["v2_count"], 1)

    def test_b_v2_with_assistant_filtered(self):
        """B: 即使 schema_version=v2，含 assistant: 格式仍过滤。"""
        results = [{"memory": "user: xxx | assistant: yyy", "schema_version": "v2", "score": 0.9}]
        kept, stats = self._filter(results, "web_user")
        self.assertEqual(len(kept), 0)
        self.assertEqual(stats["filtered_legacy_assistant_count"], 1)

    def test_c_legacy_mixed_filtered(self):
        """C: legacy 混合 user+assistant 结果过滤。"""
        results = [{"memory": "user: hello | assistant: hi there", "schema_version": "", "score": 0.8}]
        kept, stats = self._filter(results, "web_user")
        self.assertEqual(len(kept), 0)
        self.assertEqual(stats["filtered_legacy_assistant_count"], 1)

    def test_d_legacy_user_only_preserved(self):
        """D: legacy user-only 结果保留。"""
        results = [{"memory": "user: 用户明确事实", "schema_version": "", "score": 0.6}]
        kept, stats = self._filter(results, "web_user")
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats["legacy_count"], 1)

    def test_e_curated_memory_preserved(self):
        """E: curated memory 保留。"""
        results = [{"memory": "memory: 用户长期偏好", "schema_version": "v2", "score": 0.7}]
        kept, stats = self._filter(results, "web_user")
        self.assertEqual(len(kept), 1)

    def test_f_assistant_only_filtered(self):
        """F: 纯 assistant 结果过滤。"""
        results = [{"memory": "assistant: 旧回复", "schema_version": "", "score": 0.5}]
        kept, stats = self._filter(results, "web_user")
        self.assertEqual(len(kept), 0)
        self.assertEqual(stats["filtered_legacy_assistant_count"], 1)

    def test_g_metadata_none(self):
        """G: 结果不含 schema_version 不崩溃，按 legacy 分类。"""
        results = [{"memory": "user: fact", "score": 0.6}]  # no schema_version key
        kept, stats = self._filter(results, "web_user")
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats["legacy_count"], 1)

    def test_g_assistant_format_with_none_metadata(self):
        """G: 无 schema_version 但有 assistant 格式仍可判断。"""
        results = [{"memory": "user: x | assistant: y", "score": 0.7}]
        kept, stats = self._filter(results, "web_user")
        self.assertEqual(len(kept), 0)
        self.assertEqual(stats["filtered_legacy_assistant_count"], 1)

    def test_h_empty_memory(self):
        """H: 空 memory 被安全处理。"""
        results = [{"memory": "", "schema_version": "", "score": 0.5}]
        kept, stats = self._filter(results, "web_user")
        self.assertEqual(len(kept), 1)  # empty memory 不含 assistant:，保留

    def test_h_none_memory(self):
        """H: None memory 不崩溃。"""
        results = [{"memory": None, "score": 0.5}]
        kept, stats = self._filter(results, "web_user")
        self.assertEqual(len(kept), 1)

    def test_i_mixed_five_results(self):
        """I: 混合 5 条结果，验证过滤计数。"""
        results = [
            {"memory": "user: v2 fact", "schema_version": "v2", "score": 0.9},  # keep
            {"memory": "user: old | assistant: reply", "schema_version": "", "score": 0.8},  # filter
            {"memory": "user: legacy user only", "schema_version": "", "score": 0.7},  # keep
            {"memory": "memory: curated", "schema_version": "v2", "score": 0.6},  # keep
            {"memory": "assistant: only old", "schema_version": "", "score": 0.5},  # filter
        ]
        kept, stats = self._filter(results, "web_user")
        self.assertEqual(stats["input_count"], 5)
        self.assertEqual(stats["kept_count"], 3)
        self.assertEqual(stats["filtered_legacy_assistant_count"], 2)

    def test_j_order_preserved(self):
        """J: 保留结果相对顺序不变。"""
        results = [
            {"memory": "first", "schema_version": "v2", "score": 0.9},
            {"memory": "user: x | assistant: y", "score": 0.8},  # filtered
            {"memory": "third", "schema_version": "v2", "score": 0.7},
        ]
        kept, _ = self._filter(results, "web_user")
        self.assertEqual(kept[0]["memory"], "first")
        self.assertEqual(kept[1]["memory"], "third")

    def test_no_assistant_word_false_positive(self):
        """普通包含 'assistant' 单词的文本不被误判。"""
        self.assertFalse(self._is_af("memory: assistant 是一个英文词"))
        self.assertFalse(self._is_af("用户说 assistant 这个词是什么意思"))
        self.assertFalse(self._is_af("普通文本不包含角色分隔格式"))

    def test_assistant_format_detected(self):
        """明确的角色分隔格式被检测。"""
        self.assertTrue(self._is_af("user: hello | assistant: hi"))
        self.assertTrue(self._is_af("assistant: 旧回复"))
        self.assertTrue(self._is_af("user: x\nassistant: y"))

    def test_non_list_input(self):
        """非 list 输入不崩溃。"""
        kept, stats = self._filter(None, "test")
        self.assertEqual(len(kept), 0)
        self.assertEqual(stats["input_count"], 0)
        kept, stats = self._filter("string", "test")
        self.assertEqual(len(kept), 0)


# ============================================================
# K-M: 调用路径测试
# ============================================================
class TestCallPaths(unittest.TestCase):

    def _make_client(self):
        import server
        client = server.PineconeMemoryClient.__new__(server.PineconeMemoryClient)
        client.pc = Mock()
        client.index_name = "test"
        client.index = Mock()
        return client

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_k_web_path_filtered(self, _mock_emb):
        """K: 网页路径 search 返回值不含 assistant_format。"""
        client = self._make_client()
        m1 = Mock(id="id1", score=0.9, metadata={"text": "user: secret | assistant: reply", "schema_version": ""})
        m2 = Mock(id="id2", score=0.7, metadata={"text": "user: safe fact", "schema_version": "v2"})
        client.index.query.return_value = Mock(matches=[m1, m2])
        with patch("builtins.print"):
            result = client.search(query="test", user_id="uid", limit=5, source="web_user")
        results = result["results"]
        self.assertEqual(len(results), 1)
        self.assertNotIn("assistant:", results[0]["memory"])

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_l_tg_path_filtered(self, _mock_emb):
        """L: TG 路径同样过滤。"""
        client = self._make_client()
        m = Mock(id="id1", score=0.8, metadata={"text": "assistant: old reply", "schema_version": ""})
        client.index.query.return_value = Mock(matches=[m])
        with patch("builtins.print"):
            result = client.search(query="test", user_id="uid", limit=5, source="tg_user")
        # 全部被过滤，返回空
        self.assertEqual(result, [])

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_m_search_memory_filtered(self, _mock_emb):
        """M: search_memory MCP 工具路径同样过滤。"""
        client = self._make_client()
        m1 = Mock(id="id1", score=0.9, metadata={"text": "user: fact", "schema_version": "v2"})
        m2 = Mock(id="id2", score=0.8, metadata={"text": "user: x | assistant: y", "schema_version": ""})
        client.index.query.return_value = Mock(matches=[m1, m2])
        with patch("builtins.print"):
            result = client.search(query="test", user_id="uid", limit=5, source="mcp")
        results = result["results"]
        self.assertEqual(len(results), 1)
        self.assertIn("fact", results[0]["memory"])

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_empty_after_filter(self, _mock_emb):
        """过滤后无结果返回空。"""
        client = self._make_client()
        m = Mock(id="id1", score=0.9, metadata={"text": "assistant: only old", "schema_version": ""})
        client.index.query.return_value = Mock(matches=[m])
        with patch("builtins.print"):
            result = client.search(query="test", user_id="uid", limit=5, source="web_user")
        self.assertEqual(result, [])


# ============================================================
# N: 日志脱敏
# ============================================================
class TestLogPrivacy(unittest.TestCase):

    def _filter_captured(self, results, source="test"):
        import server
        old = sys.stdout
        sys.stdout = captured = io.StringIO()
        try:
            server._filter_recalled_memories(results, source)
        finally:
            sys.stdout = old
        return captured.getvalue()

    def test_n_no_body_in_log(self):
        """N: 日志不包含记忆正文。"""
        results = [
            {"memory": "SYNTHETIC_USER_SECRET | assistant: SYNTHETIC_ASSISTANT_SECRET", "schema_version": ""},
            {"memory": "SYNTHETIC_SAFE_MEMORY", "schema_version": "v2"},
        ]
        log = self._filter_captured(results, "web_user")
        self.assertNotIn("SYNTHETIC_USER_SECRET", log)
        self.assertNotIn("SYNTHETIC_ASSISTANT_SECRET", log)
        self.assertNotIn("SYNTHETIC_SAFE_MEMORY", log)
        self.assertIn("input=2", log)
        self.assertIn("kept=1", log)
        self.assertIn("filtered_legacy_assistant=1", log)


# ============================================================
# O: 召回行为保护
# ============================================================
class TestRecallBehaviorProtected(unittest.TestCase):

    def _make_client(self):
        import server
        client = server.PineconeMemoryClient.__new__(server.PineconeMemoryClient)
        client.pc = Mock()
        client.index_name = "test"
        client.index = Mock()
        return client

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_o_query_called_once(self, _mock_emb):
        """O: Pinecone query 只调用一次。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        with patch("builtins.print"):
            client.search(query="test", user_id="uid", limit=5, source="web_user")
        self.assertEqual(client.index.query.call_count, 1)

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_o_top_k_unchanged(self, _mock_emb):
        """O: top_k 不变。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        with patch("builtins.print"):
            client.search(query="test", user_id="uid", limit=5, source="web_user")
        self.assertEqual(client.index.query.call_args[1]["top_k"], 5)

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_o_filter_unchanged(self, _mock_emb):
        """O: Pinecone filter 不变。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        with patch("builtins.print"):
            client.search(query="test", user_id="uid", limit=5, source="web_user")
        call_filter = client.index.query.call_args[1]["filter"]
        self.assertEqual(call_filter["user_id"], "uid")


# ============================================================
# P: 回归测试
# ============================================================
class TestRegression(unittest.TestCase):

    def test_p_phase3_still_passes(self):
        """P: Phase 3 测试仍通过。"""
        import test_memory_phase3
        suite = unittest.TestLoader().loadTestsFromModule(test_memory_phase3)
        runner = unittest.TextTestRunner(verbosity=0, stream=io.StringIO())
        result = runner.run(suite)
        self.assertTrue(result.wasSuccessful(), f"Phase 3 failed: {result.failures}")

    def test_p_phase4_still_passes(self):
        """P: Phase 4 观测测试仍通过。"""
        import test_recall_observability_phase4
        suite = unittest.TestLoader().loadTestsFromModule(test_recall_observability_phase4)
        runner = unittest.TextTestRunner(verbosity=0, stream=io.StringIO())
        result = runner.run(suite)
        self.assertTrue(result.wasSuccessful(), f"Phase 4 failed: {result.failures}")


if __name__ == "__main__":
    unittest.main()
