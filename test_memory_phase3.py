"""
test_memory_phase3.py — 第 3 阶段记忆系统改造专项测试
================================================================
对齐项目惯例：unittest + mock，不触生产数据/数据库/Pinecone/上游模型。
覆盖：
  A. incoming reasoning_content 被移除
  B. 工具调用字段不被破坏
  C. 多模态 content 不被破坏
  D. 网页自动 Pinecone 写入不含 assistant 回复
  E. TG 自动 Pinecone 写入不含 assistant 回复
  F. save_memory 仍能保存提炼记忆
  G. 统一用户 ID
  H. search 始终加入 user_id filter
  I. search 返回 score
  J. 历史记忆纪律提示存在
  K. 旧数据兼容
"""

import unittest
import os
import json
import datetime
from unittest.mock import Mock, patch, MagicMock

# ============================================================
# 测试 G: 统一用户 ID（纯函数，可独立测试）
# ============================================================
class TestResolveUserId(unittest.TestCase):
    """测试 _resolve_pinecone_user_id 的优先级和空白处理。"""

    def _call(self):
        """延迟导入避免 server.py 初始化副作用。"""
        import server
        return server._resolve_pinecone_user_id()

    def test_user_id_takes_priority(self):
        with patch.dict(os.environ, {"USER_ID": "uid_primary", "MEM0_USER_ID": "uid_legacy"}):
            self.assertEqual(self._call(), "uid_primary")

    def test_mem0_fallback(self):
        env = {"MEM0_USER_ID": "uid_legacy"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("USER_ID", None)
            self.assertEqual(self._call(), "uid_legacy")

    def test_default_when_neither(self):
        os.environ.pop("USER_ID", None)
        os.environ.pop("MEM0_USER_ID", None)
        self.assertEqual(self._call(), "default")

    def test_blank_string_treated_as_unset(self):
        with patch.dict(os.environ, {"USER_ID": "  ", "MEM0_USER_ID": "uid_legacy"}):
            self.assertEqual(self._call(), "uid_legacy")


# ============================================================
# 测试 A/B/C: reasoning_content 清洗（纯函数测试）
# ============================================================
class TestStripReasoningContent(unittest.TestCase):
    """测试 _strip_incoming_reasoning 的字段移除和保留行为。"""

    def _call(self, messages):
        import gateway
        return gateway._strip_incoming_reasoning(messages)

    def test_a_reasoning_removed(self):
        """测试 A: reasoning_content 被移除，content 保留。"""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "正常回复", "reasoning_content": "不应再次转发的历史推理"},
            {"role": "user", "content": "next"},
        ]
        result = self._call(msgs)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[2]["content"], "正常回复")
        self.assertNotIn("reasoning_content", result[2])
        # 其他消息不受影响
        self.assertEqual(result[0]["content"], "sys")
        self.assertEqual(result[1]["content"], "hello")

    def test_b_tool_fields_preserved(self):
        """测试 B: tool_calls/tool_call_id/name 保留。"""
        msgs = [
            {"role": "assistant", "content": "", "reasoning_content": "thinking",
             "tool_calls": [{"id": "call_1", "function": {"name": "weather", "arguments": "{}"}}]},
            {"role": "tool", "content": "result", "tool_call_id": "call_1", "name": "weather",
             "reasoning_content": "should_be_removed"},
        ]
        result = self._call(msgs)
        self.assertIn("tool_calls", result[0])
        self.assertEqual(result[0]["tool_calls"][0]["id"], "call_1")
        self.assertNotIn("reasoning_content", result[0])
        self.assertIn("tool_call_id", result[1])
        self.assertEqual(result[1]["tool_call_id"], "call_1")
        self.assertIn("name", result[1])
        self.assertNotIn("reasoning_content", result[1])

    def test_c_multimodal_content_preserved(self):
        """测试 C: 多模态 content (list) 不被破坏。"""
        multimodal = [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]
        msgs = [
            {"role": "user", "content": multimodal, "reasoning_content": "rm_me"},
        ]
        result = self._call(msgs)
        self.assertIsInstance(result[0]["content"], list)
        self.assertEqual(len(result[0]["content"]), 2)
        self.assertNotIn("reasoning_content", result[0])

    def test_non_list_messages_returns_as_is(self):
        """非 list 输入不崩溃。"""
        self.assertIsNone(self._call(None))
        self.assertEqual(self._call("string"), "string")

    def test_non_dict_items_skipped(self):
        """list 中非 dict 项不崩溃。"""
        msgs = [{"role": "user", "content": "ok"}, "string_item", 42, None]
        result = self._call(msgs)
        self.assertEqual(len(result), 4)


# ============================================================
# 测试 D/E/F: PineconeMemoryClient.add 行为（mock 测试）
# ============================================================
class TestPineconeAddBehavior(unittest.TestCase):
    """测试 add() 的 user-only 写入、v2 metadata、save_memory 分类。"""

    def _make_client(self):
        """创建一个 mock Pinecone 客户端，不连接真实 Pinecone。"""
        import server
        client = server.PineconeMemoryClient.__new__(server.PineconeMemoryClient)
        client.pc = Mock()
        client.index_name = "test"
        client.index = Mock()
        # mock _get_embedding 返回非空向量
        return client

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_d_web_write_user_only(self, _mock_emb):
        """测试 D: 网页自动写入只含 user，metadata 为 web/chat_user_raw/v2/user。"""
        client = self._make_client()
        client.add(
            [{"role": "user", "content": "SYNTHETIC_USER_MSG"}],
            user_id="test_uid",
            metadata={
                "schema_version": "v2",
                "source_role": "user",
                "memory_type": "chat_user_raw",
                "channel": "web",
                "created_at": "2026-08-22T12:00:00+00:00",
            }
        )
        # 验证 upsert 被调用
        client.index.upsert.assert_called_once()
        call_args = client.index.upsert.call_args
        vectors = call_args[1]["vectors"] if "vectors" in call_args[1] else call_args[0][0]
        vec = vectors[0]
        # metadata 检查
        meta = vec["metadata"]
        self.assertEqual(meta["source_role"], "user")
        self.assertEqual(meta["memory_type"], "chat_user_raw")
        self.assertEqual(meta["channel"], "web")
        self.assertEqual(meta["schema_version"], "v2")
        self.assertEqual(meta["user_id"], "test_uid")
        # text 只含 user，不含 assistant
        self.assertIn("user:", meta["text"])
        self.assertNotIn("assistant:", meta["text"])

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_e_tg_write_user_only(self, _mock_emb):
        """测试 E: TG 自动写入只含 user，channel=tg。"""
        client = self._make_client()
        client.add(
            [{"role": "user", "content": "SYNTHETIC_TG_MSG"}],
            user_id="test_uid",
            metadata={
                "schema_version": "v2",
                "source_role": "user",
                "memory_type": "chat_user_raw",
                "channel": "tg",
                "created_at": "2026-08-22T12:00:00+00:00",
            }
        )
        client.index.upsert.assert_called_once()
        vectors = client.index.upsert.call_args[1]["vectors"]
        meta = vectors[0]["metadata"]
        self.assertEqual(meta["channel"], "tg")
        self.assertNotIn("assistant:", meta["text"])

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_f_save_memory_curated(self, _mock_emb):
        """测试 F: save_memory 工具使用 curated_memory/agent/mcp，text 不含 assistant 前缀。"""
        client = self._make_client()
        client.add(
            [{"role": "memory", "content": "SYNTHETIC_TITLE: SYNTHETIC_CONTENT"}],
            metadata={
                "schema_version": "v2",
                "source_role": "agent",
                "memory_type": "curated_memory",
                "channel": "mcp",
                "created_at": "2026-08-22T12:00:00+00:00",
            }
        )
        client.index.upsert.assert_called_once()
        meta = client.index.upsert.call_args[1]["vectors"][0]["metadata"]
        self.assertEqual(meta["source_role"], "agent")
        self.assertEqual(meta["memory_type"], "curated_memory")
        self.assertEqual(meta["channel"], "mcp")
        self.assertNotEqual(meta["memory_type"], "chat_user_raw")

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_backward_compat_no_metadata(self, _mock_emb):
        """旧调用 add(messages) 无 metadata 仍可工作。"""
        client = self._make_client()
        client.add([{"role": "user", "content": "old style"}], user_id="uid")
        client.index.upsert.assert_called_once()
        meta = client.index.upsert.call_args[1]["vectors"][0]["metadata"]
        self.assertIn("text", meta)
        self.assertIn("user_id", meta)
        # 无 schema_version（旧格式）

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_metadata_cannot_override_text_or_userid(self, _mock_emb):
        """调用方 metadata 不得覆盖 text 或 user_id。"""
        client = self._make_client()
        client.add(
            [{"role": "user", "content": "real"}],
            user_id="real_uid",
            metadata={"text": "HACKED", "user_id": "HACKED"}
        )
        meta = client.index.upsert.call_args[1]["vectors"][0]["metadata"]
        self.assertNotEqual(meta["text"], "HACKED")
        self.assertNotEqual(meta["user_id"], "HACKED")


# ============================================================
# 测试 H/I: search 行为（mock 测试）
# ============================================================
class TestSearchBehavior(unittest.TestCase):
    """测试 search() 的 user_id 隔离和 score 返回。"""

    def _make_client(self):
        import server
        client = server.PineconeMemoryClient.__new__(server.PineconeMemoryClient)
        client.pc = Mock()
        client.index_name = "test"
        client.index = Mock()
        return client

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_h_always_has_user_id_filter(self, _mock_emb):
        """测试 H: 无 filters 时仍有 user_id filter。"""
        client = self._make_client()
        # mock query 返回空 matches
        client.index.query.return_value = Mock(matches=[])
        client.search(query="test", user_id="uid_123")
        call_kwargs = client.index.query.call_args[1]
        self.assertIn("filter", call_kwargs)
        self.assertEqual(call_kwargs["filter"]["user_id"], "uid_123")

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_h_filter_cannot_override_user_id(self, _mock_emb):
        """测试 H: 调用方 filter 不能覆盖统一 user_id。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        client.search(query="test", user_id="real_uid",
                      filters={"user_id": "malicious_uid", "tags": "event"})
        call_kwargs = client.index.query.call_args[1]
        self.assertEqual(call_kwargs["filter"]["user_id"], "real_uid")
        self.assertEqual(call_kwargs["filter"]["tags"], "event")

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_i_returns_score(self, _mock_emb):
        """测试 I: 结果包含真实 score，排序不变，不做阈值过滤。"""
        client = self._make_client()
        # mock 3 个 match 带 score
        match1 = Mock(id="id1", score=0.85, metadata={"text": "result1"})
        match2 = Mock(id="id2", score=0.60, metadata={"text": "result2"})
        match3 = Mock(id="id3", score=0.30, metadata={"text": "result3"})
        client.index.query.return_value = Mock(matches=[match1, match2, match3])
        result = client.search(query="test", user_id="uid", limit=5)
        self.assertIsInstance(result, dict)
        results = result["results"]
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["score"], 0.85)
        self.assertEqual(results[1]["score"], 0.60)
        self.assertEqual(results[2]["score"], 0.30)
        # score 不进入 memory 文本
        for r in results:
            self.assertNotIn("0.85", r["memory"])
            self.assertNotIn("0.60", r["memory"])

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_i_no_threshold_filtering(self, _mock_emb):
        """测试 I: 不按 score 过滤，低分结果也返回。"""
        client = self._make_client()
        match = Mock(id="id1", score=0.05, metadata={"text": "low_score_result"})
        client.index.query.return_value = Mock(matches=[match])
        result = client.search(query="test", user_id="uid", limit=5)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["score"], 0.05)

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_i_score_null_handled(self, _mock_emb):
        """score 无法解析时不报错。"""
        client = self._make_client()
        match = Mock(id="id1", score=None, metadata={"text": "no_score"})
        client.index.query.return_value = Mock(matches=[match])
        result = client.search(query="test", user_id="uid", limit=5)
        self.assertEqual(len(result["results"]), 1)
        self.assertIsNone(result["results"][0]["score"])


# ============================================================
# 测试 J: 历史记忆纪律提示存在
# ============================================================
class TestMemoryDisciplinePrompt(unittest.TestCase):
    """验证网页和 _build_channel_context 最终上下文中包含关键约束语义。"""

    REQUIRED_PHRASES = [
        "不是思考过程",
        "不是回复或语气范例",
        "不得模仿",
        "不得延续",
    ]

    def test_j_web_prompt_has_discipline(self):
        """网页 _inject_context 的 volatile_block 包含纪律约束。"""
        # 直接检查 gateway.py 源码中的提示文本
        import gateway
        import inspect
        source = inspect.getsource(gateway.HostFixMiddleware._inject_context)
        for phrase in self.REQUIRED_PHRASES:
            self.assertIn(phrase, source, f"网页 prompt 缺少关键约束: {phrase}")

    def test_j_channel_prompt_has_discipline(self):
        """_build_channel_context 的 volatile_parts 包含纪律约束。"""
        import server
        import inspect
        source = inspect.getsource(server._build_channel_context)
        for phrase in self.REQUIRED_PHRASES:
            self.assertIn(phrase, source, f"TG/QQ prompt 缺少关键约束: {phrase}")


# ============================================================
# 测试 K: 旧数据兼容
# ============================================================
class TestLegacyDataCompat(unittest.TestCase):
    """验证旧格式 Pinecone match 不导致崩溃，且注入规则禁止模仿 assistant。"""

    def _make_client(self):
        import server
        client = server.PineconeMemoryClient.__new__(server.PineconeMemoryClient)
        client.pc = Mock()
        client.index_name = "test"
        client.index = Mock()
        return client

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_k_old_mixed_vector_returned(self, _mock_emb):
        """旧格式向量（user+assistant 拼接）仍可被 search 返回，不崩溃。"""
        client = self._make_client()
        old_match = Mock(
            id="old_id",
            score=0.70,
            metadata={"text": "user: old question | assistant: old reply with tone"}
        )
        client.index.query.return_value = Mock(matches=[old_match])
        result = client.search(query="test", user_id="uid", limit=5)
        self.assertEqual(len(result["results"]), 1)
        # 旧向量 text 含 assistant 段
        self.assertIn("assistant:", result["results"][0]["memory"])
        # 不删除、不改写
        self.assertEqual(old_match.metadata["text"], "user: old question | assistant: old reply with tone")

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_k_old_vector_no_new_metadata(self, _mock_emb):
        """旧向量 metadata 无 schema_version 仍可返回。"""
        client = self._make_client()
        old_match = Mock(
            id="old_id",
            score=0.50,
            metadata={"text": "just text", "user_id": "uid"}
        )
        client.index.query.return_value = Mock(matches=[old_match])
        result = client.search(query="test", user_id="uid", limit=5)
        self.assertEqual(len(result["results"]), 1)

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_k_no_threshold_blocks_old(self, _mock_emb):
        """本阶段不按 score 过滤，旧向量不会被阈值拦截。"""
        client = self._make_client()
        old_match = Mock(
            id="old_id",
            score=0.01,  # 极低分
            metadata={"text": "old irrelevant"}
        )
        client.index.query.return_value = Mock(matches=[old_match])
        result = client.search(query="test", user_id="uid", limit=5)
        self.assertEqual(len(result["results"]), 1)


# ============================================================
# 测试 L: 补充边界测试
# ============================================================
class TestEdgeCases(unittest.TestCase):
    """补充边界测试：metadata None 排除、非 dict metadata、多 reasoning、score 异常值等。"""

    def _make_client(self):
        import server
        client = server.PineconeMemoryClient.__new__(server.PineconeMemoryClient)
        client.pc = Mock()
        client.index_name = "test"
        client.index = Mock()
        return client

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_metadata_none_values_excluded(self, _mock_emb):
        """metadata 中值为 None 的键不写入。"""
        client = self._make_client()
        client.add(
            [{"role": "user", "content": "msg"}],
            user_id="uid",
            metadata={"schema_version": "v2", "source_role": None, "channel": "web"}
        )
        meta = client.index.upsert.call_args[1]["vectors"][0]["metadata"]
        self.assertIn("schema_version", meta)
        self.assertNotIn("source_role", meta)  # None 被排除
        self.assertIn("channel", meta)

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_metadata_non_dict_ignored(self, _mock_emb):
        """metadata 非 dict 类型时不崩溃，正常写入基本 text+user_id。"""
        client = self._make_client()
        client.add([{"role": "user", "content": "msg"}], user_id="uid", metadata="invalid_string")
        meta = client.index.upsert.call_args[1]["vectors"][0]["metadata"]
        self.assertIn("text", meta)
        self.assertIn("user_id", meta)

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_search_metadata_none_on_match(self, _mock_emb):
        """match.metadata 为 None 时不崩溃。"""
        client = self._make_client()
        match = Mock(id="id1", score=0.5, metadata=None)
        client.index.query.return_value = Mock(matches=[match])
        # metadata 为 None 时 `if m.metadata` 过滤掉，返回空
        result = client.search(query="test", user_id="uid", limit=5)
        self.assertEqual(result, [])

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_search_score_non_numeric(self, _mock_emb):
        """score 为非数字值时不崩溃。"""
        client = self._make_client()
        match = Mock(id="id1", score="invalid", metadata={"text": "result"})
        client.index.query.return_value = Mock(matches=[match])
        result = client.search(query="test", user_id="uid", limit=5)
        # score="invalid" 通过 getattr 获取，不会 crash
        self.assertEqual(len(result["results"]), 1)

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_search_filters_preserved(self, _mock_emb):
        """search 的其他 filters（非 user_id）被保留。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        client.search(query="test", user_id="uid", filters={"tags": "event", "channel": "web"})
        call_filter = client.index.query.call_args[1]["filter"]
        self.assertEqual(call_filter["user_id"], "uid")
        self.assertEqual(call_filter["tags"], "event")
        self.assertEqual(call_filter["channel"], "web")

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_find_similar_user_id_isolation(self, _mock_emb):
        """find_similar 加入 user_id 隔离。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        client.find_similar("probe text", top_k=3, user_id="custom_uid")
        call_kwargs = client.index.query.call_args[1]
        self.assertIn("filter", call_kwargs)
        self.assertEqual(call_kwargs["filter"]["user_id"], "custom_uid")

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_save_memory_text_not_assistant_format(self, _mock_emb):
        """save_memory 的 text 不以 'assistant:' 开头，避免被误分类为旧 AI 回复。"""
        client = self._make_client()
        client.add(
            [{"role": "memory", "content": "Title: Content"}],
            metadata={"schema_version": "v2", "source_role": "agent",
                      "memory_type": "curated_memory", "channel": "mcp",
                      "created_at": "2026-08-22T12:00:00+00:00"}
        )
        meta = client.index.upsert.call_args[1]["vectors"][0]["metadata"]
        self.assertNotIn("assistant:", meta["text"])
        self.assertIn("memory:", meta["text"])

    def test_multiple_reasoning_in_same_request(self):
        """同一请求中多个 assistant 消息含 reasoning_content 全部清洗。"""
        import gateway
        msgs = [
            {"role": "assistant", "content": "r1", "reasoning_content": "rc1"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "r2", "reasoning_content": "rc2"},
            {"role": "assistant", "content": "r3", "reasoning_content": "rc3"},
        ]
        result = gateway._strip_incoming_reasoning(msgs)
        for m in result:
            self.assertNotIn("reasoning_content", m)
        self.assertEqual(len(result), 4)

    def test_reasoning_content_empty_string(self):
        """reasoning_content 值为空字符串时仍被移除。"""
        import gateway
        msgs = [{"role": "assistant", "content": "ok", "reasoning_content": ""}]
        result = gateway._strip_incoming_reasoning(msgs)
        self.assertNotIn("reasoning_content", result[0])
        self.assertEqual(result[0]["content"], "ok")


if __name__ == "__main__":
    unittest.main()
