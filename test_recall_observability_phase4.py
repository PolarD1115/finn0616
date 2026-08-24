"""
test_recall_observability_phase4.py — 第 4 阶段召回观测增强专项测试
================================================================
对齐项目惯例：unittest + mock，不触生产数据/外部服务。
覆盖 A-L: 基本统计、缺失/非法 score、不足两条、legacy/v2、
  assistant_format、source 白名单、调用点 source、后台来源、
  召回行为不变、日志脱敏、Phase 3 回归
"""

import unittest
import math
from unittest.mock import Mock, patch, MagicMock
import io
import sys


# ============================================================
# A-E: _log_pinecone_recall 统计函数测试
# ============================================================
class TestLogPineconeRecall(unittest.TestCase):

    def _call_and_capture(self, results, source="test"):
        """调用 _log_pinecone_recall 并捕获 stdout。"""
        import server
        old_stdout = sys.stdout
        sys.stdout = captured = io.StringIO()
        try:
            server._log_pinecone_recall(results, source)
        finally:
            sys.stdout = old_stdout
        return captured.getvalue()

    def test_a_basic_stats(self):
        """A: 5 个 match 的基本统计正确。"""
        results = [
            {"score": 0.71, "schema_version": "", "memory": "old format"},
            {"score": 0.70, "schema_version": "v2", "memory": "new"},
            {"score": 0.66, "schema_version": "", "memory": "user: x | assistant: y"},
            {"score": 0.61, "schema_version": "v2", "memory": "new2"},
            {"score": 0.48, "schema_version": "", "memory": "old2"},
        ]
        log = self._call_and_capture(results, "web_user")
        self.assertIn("source=web_user", log)
        self.assertIn("results=5", log)
        self.assertIn("scored=5", log)
        self.assertIn("top1=0.71", log)
        self.assertIn("top2=0.70", log)
        self.assertIn("gap=0.01", log)
        self.assertIn("ge70=2", log)  # 0.71, 0.70
        self.assertIn("ge65=3", log)  # 0.71, 0.70, 0.66
        self.assertIn("ge50=4", log)  # all except 0.48
        self.assertIn("legacy=3", log)  # 3 without v2
        self.assertIn("v2=2", log)
        self.assertIn("assistant_format=1", log)  # "user: x | assistant: y"
        self.assertIn("missing_score=0", log)

    def test_b_missing_and_invalid_scores(self):
        """B: None/字符串/NaN/Infinity 不崩溃，计入 missing_score。"""
        results = [
            {"score": None, "schema_version": "", "memory": "a"},
            {"score": "0.75", "schema_version": "v2", "memory": "b"},  # 字符串数字
            {"score": "invalid", "schema_version": "", "memory": "c"},
            {"score": float('nan'), "schema_version": "v2", "memory": "d"},
            {"score": float('inf'), "schema_version": "", "memory": "e"},
            {"score": 0.65, "schema_version": "v2", "memory": "f"},
        ]
        log = self._call_and_capture(results, "tg_user")
        self.assertIn("results=6", log)
        # "0.75" 字符串可转 float，应被计入有效
        # None, "invalid", NaN, Infinity → missing
        self.assertIn("missing_score=4", log)
        self.assertIn("ge65=", log)

    def test_c_fewer_than_two(self):
        """C: 0/1/2 条结果不崩溃，gap 安全处理。"""
        # 0 条
        log = self._call_and_capture([], "unknown")
        self.assertIn("results=0", log)
        self.assertIn("top1=na", log)
        self.assertIn("gap=na", log)

        # 1 条
        results = [{"score": 0.60, "schema_version": "v2", "memory": "only"}]
        log = self._call_and_capture(results, "unknown")
        self.assertIn("results=1", log)
        self.assertIn("top1=0.60", log)
        self.assertIn("top2=na", log)
        self.assertIn("gap=na", log)

        # 2 条
        results = [{"score": 0.80, "schema_version": "v2", "memory": "a"},
                   {"score": 0.70, "schema_version": "", "memory": "b"}]
        log = self._call_and_capture(results, "unknown")
        self.assertIn("top1=0.80", log)
        self.assertIn("top2=0.70", log)
        self.assertIn("gap=0.1", log)

    def test_d_legacy_v2_classification(self):
        """D: schema_version v2/缺失/v1/None metadata 分类正确。"""
        results = [
            {"score": 0.7, "schema_version": "v2", "memory": "a"},
            {"score": 0.6, "schema_version": "", "memory": "b"},  # 缺失 → legacy
            {"score": 0.5, "schema_version": "v1", "memory": "c"},  # v1 → legacy
        ]
        log = self._call_and_capture(results, "unknown")
        self.assertIn("legacy=2", log)
        self.assertIn("v2=1", log)

    def test_e_assistant_format_count(self):
        """E: 只有含 'assistant:' 的 memory 被计数，不输出正文。"""
        results = [
            {"score": 0.7, "schema_version": "", "memory": "user: hello | assistant: hi there"},
            {"score": 0.6, "schema_version": "v2", "memory": "memory: curated content"},
            {"score": 0.5, "schema_version": "", "memory": "just plain text"},
        ]
        log = self._call_and_capture(results, "unknown")
        self.assertIn("assistant_format=1", log)
        # 不输出正文
        self.assertNotIn("hi there", log)
        self.assertNotIn("curated content", log)
        self.assertNotIn("just plain text", log)

    def test_non_list_input(self):
        """非 list 输入不崩溃。"""
        log = self._call_and_capture(None, "unknown")
        self.assertIn("results=0", log)
        log = self._call_and_capture("string", "unknown")
        self.assertIn("results=0", log)


# ============================================================
# F: source 白名单
# ============================================================
class TestSourceWhitelist(unittest.TestCase):

    def _make_client(self):
        import server
        client = server.PineconeMemoryClient.__new__(server.PineconeMemoryClient)
        client.pc = Mock()
        client.index_name = "test"
        client.index = Mock()
        return client

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_f_valid_source_preserved(self, _mock_emb):
        """F: 合法 source 原样使用。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        with patch("builtins.print"):  # suppress log
            client.search(query="test", user_id="uid", source="web_user")
        call_kwargs = client.index.query.call_args[1]
        # source 不进入 filter
        self.assertNotIn("source", str(call_kwargs.get("filter", {})))

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_f_invalid_source_becomes_unknown(self, _mock_emb):
        """F: 非法 source 归为 unknown。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        old_stdout = sys.stdout
        sys.stdout = captured = io.StringIO()
        try:
            client.search(query="test", user_id="uid", source="malicious_source")
        finally:
            sys.stdout = old_stdout
        self.assertIn("source=unknown", captured.getvalue())

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_f_source_not_in_filter(self, _mock_emb):
        """F: source 不进入 Pinecone filter。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        with patch("builtins.print"):
            client.search(query="test", user_id="uid", source="tg_user")
        call_filter = client.index.query.call_args[1]["filter"]
        self.assertNotIn("source", call_filter)


# ============================================================
# G-J: 召回行为不变
# ============================================================
class TestRecallBehaviorUnchanged(unittest.TestCase):

    def _make_client(self):
        import server
        client = server.PineconeMemoryClient.__new__(server.PineconeMemoryClient)
        client.pc = Mock()
        client.index_name = "test"
        client.index = Mock()
        return client

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_j_top_k_unchanged(self, _mock_emb):
        """J: top_k 不变。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        with patch("builtins.print"):
            client.search(query="test", user_id="uid", limit=5, source="web_user")
        self.assertEqual(client.index.query.call_args[1]["top_k"], 5)

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_j_filter_unchanged(self, _mock_emb):
        """J: filter 仍按 user_id 隔离。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        with patch("builtins.print"):
            client.search(query="test", user_id="uid", limit=5, source="web_user")
        call_filter = client.index.query.call_args[1]["filter"]
        self.assertEqual(call_filter["user_id"], "uid")

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_j_results_order_unchanged(self, _mock_emb):
        """J: 结果顺序不变。"""
        client = self._make_client()
        m1 = Mock(id="id1", score=0.8, metadata={"text": "r1", "schema_version": "v2"})
        m2 = Mock(id="id2", score=0.6, metadata={"text": "r2", "schema_version": ""})
        client.index.query.return_value = Mock(matches=[m1, m2])
        with patch("builtins.print"):
            result = client.search(query="test", user_id="uid", limit=5, source="web_user")
        results = result["results"]
        self.assertEqual(results[0]["memory"], "r1")
        self.assertEqual(results[1]["memory"], "r2")
        # 新增字段存在
        self.assertIn("schema_version", results[0])
        self.assertIn("source_role", results[0])

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_j_no_extra_query(self, _mock_emb):
        """J: 不增加额外 Pinecone query。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        with patch("builtins.print"):
            client.search(query="test", user_id="uid", limit=5, source="web_user")
        self.assertEqual(client.index.query.call_count, 1)  # 只查询一次


# ============================================================
# K: 日志脱敏
# ============================================================
class TestLogPrivacy(unittest.TestCase):

    def _call_and_capture(self, results, source="test"):
        import server
        old_stdout = sys.stdout
        sys.stdout = captured = io.StringIO()
        try:
            server._log_pinecone_recall(results, source)
        finally:
            sys.stdout = old_stdout
        return captured.getvalue()

    def test_k_no_user_query_in_log(self):
        """K: 日志不包含用户查询正文。"""
        results = [{"score": 0.7, "memory": "user's secret message here"}]
        log = self._call_and_capture(results, "web_user")
        self.assertNotIn("user's secret message here", log)

    def test_k_no_memory_text_in_log(self):
        """K: 日志不包含记忆正文。"""
        results = [{"score": 0.7, "memory": "SYNTHETIC_MEMORY_CONTENT"}]
        log = self._call_and_capture(results, "web_user")
        self.assertNotIn("SYNTHETIC_MEMORY_CONTENT", log)

    def test_k_no_user_id_in_log(self):
        """K: 日志不包含 user_id。"""
        results = [{"score": 0.7, "memory": "text"}]
        log = self._call_and_capture(results, "web_user")
        self.assertNotIn("user_id=", log)
        self.assertNotIn("uid", log.lower())

    def test_k_no_vector_id_in_log(self):
        """K: 日志不包含 vector ID。"""
        results = [{"score": 0.7, "id": "vec_abc123", "memory": "text"}]
        log = self._call_and_capture(results, "web_user")
        self.assertNotIn("vec_abc123", log)

    def test_k_no_base64_in_log(self):
        """K: 日志不包含 Base64。"""
        results = [{"score": 0.7, "memory": "data:image/png;base64,AAAABBBB"}]
        log = self._call_and_capture(results, "web_user")
        self.assertNotIn("base64", log.lower())
        self.assertNotIn("AAAABBBB", log)

    def test_k_log_contains_safe_fields(self):
        """K: 日志包含安全的统计字段。"""
        results = [{"score": 0.7, "schema_version": "v2", "memory": "text"}]
        log = self._call_and_capture(results, "web_user")
        self.assertIn("source=web_user", log)
        self.assertIn("results=", log)
        self.assertIn("scored=", log)
        self.assertIn("ge", log)  # candidate score lines
        self.assertIn("legacy=", log)
        self.assertIn("v2=", log)


# ============================================================
# L: Phase 3 回归
# ============================================================
class TestPhase3Regression(unittest.TestCase):

    def test_l_memory_phase3_still_passes(self):
        """L: Phase 3 测试仍可通过（导入验证）。"""
        import test_memory_phase3
        suite = unittest.TestLoader().loadTestsFromModule(test_memory_phase3)
        runner = unittest.TextTestRunner(verbosity=0, stream=io.StringIO())
        result = runner.run(suite)
        self.assertTrue(result.wasSuccessful(), f"Phase 3 tests failed: {result.failures}")


if __name__ == "__main__":
    unittest.main()
