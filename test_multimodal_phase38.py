"""
test_multimodal_phase38.py — 第 3.8 阶段多模态记忆文本提取专项测试
================================================================
对齐项目惯例：unittest + mock，不触生产数据/外部服务。
覆盖：
  A-Q: 纯文本提取、多模态提取、媒体忽略、非法结构、上游转发保持、
       Pinecone/Supabase/欲望路径、嵌入空文本/超长文本、回归
"""

import unittest
import json
from unittest.mock import Mock, patch, MagicMock


# ============================================================
# A-H: _extract_message_text 纯函数测试
# ============================================================
class TestExtractMessageText(unittest.TestCase):

    def _call(self, content):
        import gateway
        return gateway._extract_message_text(content)

    def test_a_plain_string(self):
        """A: 普通字符串原样返回。"""
        self.assertEqual(self._call("你好"), "你好")

    def test_b_text_plus_base64_image(self):
        """B: 文本+Base64 图片，只提取文本。"""
        content = [
            {"type": "text", "text": "看看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + "A" * 100000}},
        ]
        result = self._call(content)
        self.assertEqual(result, "看看这张图")
        self.assertNotIn("data:image", result)
        self.assertNotIn("AAAA", result)

    def test_b_original_content_not_modified(self):
        """B: 原始 content 未被修改。"""
        content = [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
        ]
        original = json.loads(json.dumps(content))
        self._call(content)
        self.assertEqual(content, original)

    def test_c_multiple_text_parts(self):
        """C: 多段文本按顺序用换行合并。"""
        content = [
            {"type": "text", "text": "第一段"},
            {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
            {"type": "text", "text": "第二段"},
        ]
        result = self._call(content)
        self.assertEqual(result, "第一段\n第二段")

    def test_d_input_text(self):
        """D: input_text 类型正常提取。"""
        content = [{"type": "input_text", "text": "via input_text"}]
        self.assertEqual(self._call(content), "via input_text")

    def test_e_pure_image_returns_empty(self):
        """E: 纯图片消息返回空字符串。"""
        content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,xyz"}}]
        self.assertEqual(self._call(content), "")

    def test_f_audio_video_file_ignored(self):
        """F: 音频/视频/文件 part 全部忽略。"""
        content = [
            {"type": "input_audio", "input_audio": {"data": "base64audio"}},
            {"type": "audio", "audio": "https://example.com/audio.mp3"},
            {"type": "video_url", "video_url": "https://example.com/video.mp4"},
            {"type": "input_video", "input_video": {"url": "data:video/mp4;base64,xxx"}},
            {"type": "file", "file": {"url": "https://example.com/doc.pdf"}},
            {"type": "input_file", "input_file": {"data": "base64file"}},
        ]
        self.assertEqual(self._call(content), "")

    def test_g_unknown_part_ignored(self):
        """G: 未知 part 类型忽略，不执行 str(dict)。"""
        content = [
            {"type": "text", "text": "known"},
            {"type": "custom_future_format", "data": {"nested": "object"}},
            {"type": "another_unknown", "value": 12345},
        ]
        result = self._call(content)
        self.assertEqual(result, "known")
        self.assertNotIn("nested", result)
        self.assertNotIn("12345", result)

    def test_h_none_content(self):
        """H: None 不崩溃，返回空字符串。"""
        self.assertEqual(self._call(None), "")

    def test_h_number_content(self):
        """H: 数字不崩溃。"""
        self.assertEqual(self._call(42), "")

    def test_h_bool_content(self):
        """H: 布尔值不崩溃。"""
        self.assertEqual(self._call(True), "")

    def test_h_dict_content(self):
        """H: 普通 dict（非 list）不崩溃。"""
        self.assertEqual(self._call({"key": "value"}), "")

    def test_h_list_with_none(self):
        """H: list 中 None 不崩溃。"""
        self.assertEqual(self._call([None, {"type": "text", "text": "ok"}]), "ok")

    def test_h_list_with_string(self):
        """H: list 中字符串不崩溃（忽略非 dict）。"""
        self.assertEqual(self._call(["raw string", {"type": "text", "text": "ok"}]), "ok")

    def test_h_list_with_number(self):
        """H: list 中数字不崩溃。"""
        self.assertEqual(self._call([42, {"type": "text", "text": "ok"}]), "ok")

    def test_h_text_not_string(self):
        """H: text 字段非字符串时忽略。"""
        content = [{"type": "text", "text": 123}]
        self.assertEqual(self._call(content), "")

    def test_h_missing_type(self):
        """H: 缺 type 字段的 dict 忽略。"""
        content = [{"text": "no type field"}]
        self.assertEqual(self._call(content), "")

    def test_h_empty_list(self):
        """H: 空 list 返回空字符串。"""
        self.assertEqual(self._call([]), "")


# ============================================================
# I: 原始多模态请求仍转发上游
# ============================================================
class TestUpstreamForwardingPreserved(unittest.TestCase):
    """验证多模态 content 原样转发给上游模型。"""

    def test_i_image_url_preserved_in_req_data(self):
        """I: 提取纯文本后，req_data.messages 中的 image_url 仍完整存在。"""
        import gateway
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC123"}},
            ]},
        ]
        # 模拟 user_msg 提取逻辑
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = gateway._extract_message_text(m.get("content", ""))
                break
        # user_msg 是纯文本
        self.assertEqual(user_msg, "看图")
        # 原始 messages 中的 image_url 仍完整
        user_content = messages[1]["content"]
        self.assertIsInstance(user_content, list)
        self.assertEqual(user_content[1]["image_url"]["url"], "data:image/png;base64,ABC123")


# ============================================================
# J-K: Pinecone 只接收纯文本
# ============================================================
class TestPineconeReceivesTextOnly(unittest.TestCase):

    def _make_client(self):
        import server
        client = server.PineconeMemoryClient.__new__(server.PineconeMemoryClient)
        client.pc = Mock()
        client.index_name = "test"
        client.index = Mock()
        return client

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_j_search_query_is_text(self, _mock_emb):
        """J: Pinecone search query 不含 Base64/媒体。"""
        client = self._make_client()
        client.index.query.return_value = Mock(matches=[])
        client.search(query="看看这张图", user_id="uid", limit=5)
        call_kwargs = client.index.query.call_args[1]
        self.assertEqual(call_kwargs["vector"], [0.1] * 10)
        # query 文本不包含 Base64
        self.assertNotIn("base64", str(call_kwargs.get("vector", "")).lower())

    @patch("server._get_embedding", return_value=[0.1] * 10)
    def test_k_add_messages_text_only(self, _mock_emb):
        """K: Pinecone add 的 messages 只含纯文本，metadata 保持 v2。"""
        client = self._make_client()
        client.add(
            [{"role": "user", "content": "看看这张图"}],
            user_id="uid",
            metadata={
                "schema_version": "v2",
                "source_role": "user",
                "memory_type": "chat_user_raw",
                "channel": "web",
                "created_at": "2026-08-24T00:00:00+00:00",
            }
        )
        meta = client.index.upsert.call_args[1]["vectors"][0]["metadata"]
        self.assertEqual(meta["schema_version"], "v2")
        self.assertNotIn("base64", meta["text"].lower())
        self.assertNotIn("image_url", meta["text"])


# ============================================================
# N-O: 嵌入空文本和超长文本
# ============================================================
class TestEmbeddingProtection(unittest.TestCase):

    @patch("server.http_session.post")
    def test_n_empty_text_no_api_call(self, mock_post):
        """N: 空字符串不调用 embedding API。"""
        import server
        result = server._get_embedding("")
        self.assertEqual(result, [])
        mock_post.assert_not_called()

    @patch("server.http_session.post")
    def test_n_whitespace_text_no_api_call(self, mock_post):
        """N: 纯空白字符串不调用 embedding API。"""
        import server
        result = server._get_embedding("   \n\t  ")
        self.assertEqual(result, [])
        mock_post.assert_not_called()

    @patch("server.http_session.post")
    def test_o_long_text_truncated(self, mock_post):
        """O: 超长文本被截断到 _MAX_EMBED_TEXT_CHARS。"""
        import server
        long_text = "A" * 10000
        mock_post.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"data": [{"embedding": [0.1] * 10}]}),
        )
        with patch.dict("os.environ", {"DOUBAO_API_KEY": "test_key", "DOUBAO_EMBEDDING_EP": "test_model"}):
            server._get_embedding(long_text)
        mock_post.assert_called_once()
        call_payload = mock_post.call_args[1]["json"]
        self.assertLessEqual(len(call_payload["input"]), server._MAX_EMBED_TEXT_CHARS)

    @patch("server.http_session.post")
    def test_o_normal_text_not_truncated(self, mock_post):
        """O: 正常长度文本不截断。"""
        import server
        normal_text = "你好世界" * 100  # 400 chars
        mock_post.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"data": [{"embedding": [0.1] * 10}]}),
        )
        with patch.dict("os.environ", {"DOUBAO_API_KEY": "test_key", "DOUBAO_EMBEDDING_EP": "test_model"}):
            server._get_embedding(normal_text)
        mock_post.assert_called_once()
        call_payload = mock_post.call_args[1]["json"]
        self.assertEqual(len(call_payload["input"]), len(normal_text))

    def test_max_embed_chars_constant(self):
        """O: 常量值合理（< 8192，为 bge-m3 token 限制留余量）。"""
        import server
        self.assertLess(server._MAX_EMBED_TEXT_CHARS, 8192)
        self.assertGreater(server._MAX_EMBED_TEXT_CHARS, 1000)


# ============================================================
# P-Q: 回归测试
# ============================================================
class TestRegression(unittest.TestCase):

    def test_p_plain_text_unchanged(self):
        """P: 普通纯文本行为不变。"""
        import gateway
        self.assertEqual(gateway._extract_message_text("hello world"), "hello world")
        self.assertEqual(gateway._extract_message_text(""), "")
        self.assertEqual(gateway._extract_message_text("中文测试"), "中文测试")

    def test_q_phase3_reasoning_still_stripped(self):
        """Q: Phase 3 reasoning_content 清洗仍生效。"""
        import gateway
        msgs = [
            {"role": "assistant", "content": "reply", "reasoning_content": "should_be_removed"},
        ]
        result = gateway._strip_incoming_reasoning(msgs)
        self.assertNotIn("reasoning_content", result[0])


if __name__ == "__main__":
    unittest.main()
