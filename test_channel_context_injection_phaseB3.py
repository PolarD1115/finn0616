# -*- coding: utf-8 -*-
"""阶段 B3 专项测试 —— TG/QQ 渠道 _build_channel_context 接 active 自动注入。

全部 unittest + mock + 脱敏假数据（SYNTHETIC_*），不连接 Supabase / Pinecone / 嵌入 API。

覆盖：
  A. 门控关（默认）时不发生召回、不注入
  B. 门控开时注入块以独立段落追加到返回字符串末尾（在既有段落之后）
  C. 返回值是字符串、绝不伪装 user/assistant（源码约束 + 运行时校验）
  D. 既有输出结构不变（画像/总结/深层记忆/历史/实时状态全保留）
  E. 召回失败 / 服务不可用时优雅跳过，不影响主上下文
  F. 签名零改动（调用方 napcat/heartbeat 无需变更）；去重基底传入当轮已算好的文本
  G. 空 query 不触发召回

运行：  python -m unittest test_channel_context_injection_phaseB3 -v
"""

import asyncio
import contextlib
import inspect
import io
import os
import types
import unittest
from unittest.mock import patch

import gateway
import memory_context_injection as mci
import server


TEST_USER_ID = "srv-user"
_AM_KEY = "ACTIVE_MEMORY_INJECTION_ENABLED"
_BLOCK_TITLE = "【长期记忆 · 事实参考】"


# ==========================================
# 假件
# ==========================================

class FakeResult:
    def __init__(self, data):
        self.data = data


class _RecordingEmbed:
    def __init__(self):
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        return [0.05] * 1024


class _FakeServiceRpc:
    """service_role 客户端假件：只支持 .rpc(name, params).execute()。"""

    def __init__(self, rows=(), exc=None):
        self.rows = list(rows)
        self.exc = exc
        self.name = None
        self.params = None
        self.execute_count = 0

    def rpc(self, name, params):
        self.name = name
        self.params = params
        return self

    def execute(self):
        self.execute_count += 1
        if self.exc is not None:
            raise self.exc
        return FakeResult([dict(r) for r in self.rows])


class _FakeChannelQuery:
    def __init__(self, owner, table):
        self._owner = owner
        self._table = table
        self._path = []

    def _rec(self, *a, **k):
        self._path.append((a, k))
        return self

    def select(self, *a, **k): return self._rec(*a, **k)
    def eq(self, *a, **k): return self._rec(*a, **k)
    def neq(self, *a, **k): return self._rec(*a, **k)
    def in_(self, *a, **k): return self._rec(*a, **k)
    def order(self, *a, **k): return self._rec(*a, **k)
    def limit(self, *a, **k): return self._rec(*a, **k)

    def execute(self):
        if self._table == "user_facts":
            return FakeResult([dict(r) for r in self._owner.profile_rows])
        if self._table == "memories":
            is_core = any(a and a[0] == "tags" and a[1] == "Core_Cognition"
                          for a, k in self._path if a)
            rows = (self._owner.summary_rows if is_core
                    else self._owner.history_rows)
            return FakeResult([dict(r) for r in rows])
        return FakeResult([])


class _FakeChannelSb:
    def __init__(self, profile_rows=(), summary_rows=(), history_rows=()):
        self.profile_rows = list(profile_rows)
        self.summary_rows = list(summary_rows)
        self.history_rows = list(history_rows)

    def table(self, name):
        return _FakeChannelQuery(self, name)


def _rpc_row(content="SYNTHETIC_ACTIVE_MEMORY_用户喜欢无糖咖啡",
             similarity=0.82):
    return {"memory_item_id": "11111111-1111-1111-1111-111111111111",
            "content": content, "memory_type": "long_term", "importance": 5,
            "confidence": 0.9, "subject_key": None, "valid_at": None,
            "expires_at": None, "source": "web", "similarity": similarity}


def _std_fake_sb():
    return _FakeChannelSb(
        profile_rows=[{"key": "喜好", "value": "SYNTHETIC_PROFILE_喜欢无糖咖啡"}],
        summary_rows=[{"content": "SYNTHETIC_SUMMARY_阶段总结行"}],
        history_rows=[{"content": "用户: SYNTHETIC_HISTORY_我家猫又拆家了\n回复: 好呀",
                       "tags": "TG_MSG"}])


# ==========================================
# 执行辅助
# ==========================================

def _run_ctx(fake_sb, svc, *, query="无糖咖啡", gate=None, embed=None,
             channel_tag="TG_MSG", inject_device=None, source="tg_user",
             pinecone=None):
    """在 mock 环境下执行 _build_channel_context，返回 (输出, stdout, embed)。"""
    if embed is None:
        embed = _RecordingEmbed()
    old_gate = os.environ.pop(_AM_KEY, None)
    if gate is not None:
        os.environ[_AM_KEY] = gate
    buf = io.StringIO()
    try:
        with patch.dict(os.environ, {"USER_NAME": "小满",
                                     "CALENDAR_INJECT": "false",
                                     "DEVICE_CONTEXT_ENABLED": "false"}), \
             patch.object(server, "supabase", fake_sb), \
             patch.object(server, "supabase_service", svc), \
             patch.object(server, "pinecone_memory", pinecone), \
             patch.object(server, "_get_embedding", embed), \
             patch.object(server, "_resolve_pinecone_user_id",
                          lambda: TEST_USER_ID), \
             patch.object(server, "weather_tools",
                          types.SimpleNamespace(enabled=lambda: False)), \
             patch.object(gateway, "_home_context_enabled", lambda: False):
            with contextlib.redirect_stdout(buf):
                result = asyncio.run(server._build_channel_context(
                    query, channel_tag=channel_tag,
                    inject_device=inject_device, source=source))
    finally:
        os.environ.pop(_AM_KEY, None)
        if old_gate is not None:
            os.environ[_AM_KEY] = old_gate
    return result, buf.getvalue(), embed


# ==========================================
# A. 门控关
# ==========================================

class TestGateOff(unittest.TestCase):

    def test_gate_off_no_recall_no_injection(self):
        fake_sb = _std_fake_sb()
        svc = _FakeServiceRpc(rows=[_rpc_row()])
        embed = _RecordingEmbed()
        result, _, embed = _run_ctx(fake_sb, svc, gate=None, embed=embed)

        self.assertNotIn(_BLOCK_TITLE, result, "门控关时不注入")
        self.assertNotIn("SYNTHETIC_ACTIVE_MEMORY", result)
        self.assertEqual(svc.execute_count, 0, "门控关时零 RPC 调用")
        self.assertEqual(embed.calls, [], "门控关时零 embedding 调用")

    def test_gate_explicit_false_also_off(self):
        result, _, _ = _run_ctx(_std_fake_sb(), _FakeServiceRpc(rows=[_rpc_row()]),
                                gate="false")
        self.assertNotIn(_BLOCK_TITLE, result)


# ==========================================
# B. 门控开：追加注入段
# ==========================================

class TestGateOnInjection(unittest.TestCase):

    def test_injection_appended_after_existing_sections(self):
        result, _, _ = _run_ctx(_std_fake_sb(), _FakeServiceRpc(rows=[_rpc_row()]),
                                gate="true")

        self.assertIn(_BLOCK_TITLE, result, "注入块以独立段落出现")
        self.assertIn("SYNTHETIC_ACTIVE_MEMORY_用户喜欢无糖咖啡", result)
        self.assertGreater(result.index(_BLOCK_TITLE),
                           result.index("【深层关联记忆】"),
                           "注入段追加在既有段落之后")
        self.assertGreater(result.index(_BLOCK_TITLE),
                           result.index("[实时状态 · 回复前请先读这里]"),
                           "注入段在实时状态之后（volatile 末尾）")

    def test_recall_bound_to_server_embedding_and_service_rpc(self):
        fake_sb = _std_fake_sb()
        svc = _FakeServiceRpc(rows=[_rpc_row()])
        embed = _RecordingEmbed()
        _run_ctx(fake_sb, svc, gate="true", embed=embed, query="我喜欢喝什么")

        self.assertEqual(svc.execute_count, 1, "只读 RPC 恰调用一次")
        self.assertEqual(svc.name, "match_memory_items")
        self.assertEqual(svc.params["p_user_id"], TEST_USER_ID,
                         "user_id 服务端解析")
        self.assertEqual(svc.params["match_count"], 10)
        self.assertEqual(embed.calls, ["我喜欢喝什么"], "embedding 恰调用一次")

    def test_dedup_base_uses_round_context_texts(self):
        """跨来源去重基底 = 当轮已算好的画像/总结/Pinecone/历史用户侧文本。"""
        captured = {}

        async def fake_build(query, server_user_id, recall_fn,
                             existing_context_texts=None, max_items=3, now=None):
            captured["query"] = query
            captured["user_id"] = server_user_id
            captured["existing"] = list(existing_context_texts or [])
            return (None, "log")

        with patch.object(mci, "build_active_memory_injection", fake_build):
            _run_ctx(_std_fake_sb(), _FakeServiceRpc(rows=[]), gate="true")

        self.assertEqual(captured["query"], "无糖咖啡")
        self.assertEqual(captured["user_id"], TEST_USER_ID)
        existing = "\n".join(captured["existing"])
        self.assertIn("SYNTHETIC_PROFILE_喜欢无糖咖啡", existing, "画像进去重基底")
        self.assertIn("SYNTHETIC_SUMMARY_阶段总结行", existing, "总结进去重基底")
        self.assertIn("用户曾提到", existing, "历史用户侧文本进去重基底")

    def test_dedup_existing_hit_skips_duplicate_item(self):
        """召回内容与画像重复时被跨来源去重，不再注入。"""
        fake_sb = _FakeChannelSb(
            profile_rows=[{"key": "喜好", "value": "用户喜欢无糖咖啡"}],
            summary_rows=[], history_rows=[])
        result, _, _ = _run_ctx(fake_sb, _FakeServiceRpc(rows=[_rpc_row(
            content="用户喜欢无糖咖啡")]), gate="true")

        self.assertNotIn(_BLOCK_TITLE, result,
                         "与画像完全重复的召回项应被去重（宁缺勿重）")


# ==========================================
# C. 返回字符串，绝不伪装 user/assistant
# ==========================================

class TestStringContract(unittest.TestCase):

    def test_returns_string_with_system_block_only(self):
        result, _, _ = _run_ctx(_std_fake_sb(), _FakeServiceRpc(rows=[_rpc_row()]),
                                gate="true")
        self.assertIsInstance(result, str, "返回值仍是纯字符串")

    def test_source_constraints(self):
        """源码约束：只追加 system 文本块，绝不构造 user/assistant 消息。"""
        src = inspect.getsource(server._build_channel_context)
        self.assertIn("volatile_parts.append(_am_content)", src,
                      "注入块以文本段落追加")
        self.assertIn('_am_message.get("role") == "system"', src,
                      "只接受模块承诺的 system 消息")
        self.assertNotIn('{"role": "user"', src)
        self.assertNotIn('{"role": "assistant"', src)
        self.assertIn("build_active_memory_injection", src, "复用第 41 阶段构建体")
        self.assertIn("_active_memory_injection_enabled", src, "复用 Web 门控")

    def test_signature_unchanged(self):
        """签名零改动 → 调用方（napcat/heartbeat）零变更。"""
        sig = inspect.signature(server._build_channel_context)
        self.assertEqual(list(sig.parameters),
                         ["query", "channel_tag", "inject_device", "source"])


# ==========================================
# D. 既有输出结构不变
# ==========================================

class TestExistingStructurePreserved(unittest.TestCase):

    def test_all_existing_sections_still_present_with_gate_on(self):
        result, _, _ = _run_ctx(_std_fake_sb(), _FakeServiceRpc(rows=[_rpc_row()]),
                                gate="true")

        self.assertIn("关于小满：", result)
        self.assertIn("SYNTHETIC_PROFILE_喜欢无糖咖啡", result, "画像保留")
        self.assertIn("【近3次阶段总结】:", result)
        self.assertIn("SYNTHETIC_SUMMARY_阶段总结行", result, "阶段总结保留")
        self.assertIn("【深层关联记忆】:", result, "Pinecone 段保留")
        self.assertIn("用户曾提到", result, "历史用户侧保留")
        self.assertIn("[实时状态 · 回复前请先读这里]", result, "实时状态保留")
        self.assertIn("📡 当前聊天渠道：", result)

    def test_qq_channel_tag_still_rendered(self):
        result, _, _ = _run_ctx(_std_fake_sb(), _FakeServiceRpc(rows=[_rpc_row()]),
                                gate="true", channel_tag="QQ_MSG")
        self.assertIn("QQ", result, "渠道显示不回归")


# ==========================================
# E. 失败安全
# ==========================================

class TestFailureSafety(unittest.TestCase):

    def test_rpc_failure_skips_injection(self):
        result, _, _ = _run_ctx(_std_fake_sb(),
                                _FakeServiceRpc(exc=RuntimeError("mock rpc")),
                                gate="true")
        self.assertNotIn(_BLOCK_TITLE, result, "召回失败 → 无注入")
        self.assertIn("关于小满：", result, "主上下文不受影响")

    def test_service_client_missing_skips_injection(self):
        """supabase_service 不可用（None）→ 召回失败降级 → 无注入、无异常。"""
        result, _, _ = _run_ctx(_std_fake_sb(), None, gate="true")
        self.assertNotIn(_BLOCK_TITLE, result)
        self.assertIn("关于小满：", result)

    def test_empty_query_no_recall(self):
        fake_sb = _std_fake_sb()
        svc = _FakeServiceRpc(rows=[_rpc_row()])
        embed = _RecordingEmbed()
        result, _, embed = _run_ctx(fake_sb, svc, gate="true", query="",
                                    embed=embed)

        self.assertNotIn(_BLOCK_TITLE, result)
        self.assertEqual(svc.execute_count, 0, "空 query 不触发 RPC")
        self.assertEqual(embed.calls, [], "空 query 不触发 embedding")


if __name__ == "__main__":
    unittest.main()
