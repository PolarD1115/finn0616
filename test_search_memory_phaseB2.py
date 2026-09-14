# -*- coding: utf-8 -*-
"""阶段 B2 专项测试 —— search_memory 工具优先查 memory_items 混合召回。

全部 unittest + mock + 脱敏假数据（SYNTHETIC_*），不连接 Supabase / Pinecone / 嵌入 API。

覆盖：
  A. 新第一段（memory_items 混合召回）优先出现且格式为「🧠 【长期记忆】:」分块
  B. 查不到 / RPC 失败时回退旧两段（Pinecone 语义 + memories 关键词）
  C. 每段最多 5 条；三段拼接顺序：长期记忆 → 语义相似 → 关键词匹配
  D. 私密过滤（Secret_Diary）对旧段原样保留
  E. user_id 服务端解析（_resolve_pinecone_user_id）、RPC 参数正确
  F. 日志/输出不含查询原文、记忆正文、user_id

运行：  python -m unittest test_search_memory_phaseB2 -v
"""

import asyncio
import contextlib
import io
import unittest
from unittest.mock import patch

import server


TEST_USER_ID = "srv-user"


# ==========================================
# 假件
# ==========================================

class FakeResult:
    def __init__(self, data):
        self.data = data


class _RecordingEmbed:
    """记录调用并返回合法 1024 维向量的 embedding callable。"""

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


class _FakeAnonQuery:
    """anon 客户端链式查询假件：memories 关键词段专用，返回预置行。"""

    def __init__(self, owner, table):
        self._owner = owner
        self._table = table

    def _rec(self, *a, **k):
        return self

    def select(self, *a, **k): return self._rec()
    def or_(self, *a, **k): return self._rec()
    def order(self, *a, **k): return self._rec()
    def limit(self, *a, **k): return self._rec()

    def execute(self):
        return FakeResult([dict(r) for r in self._owner.keyword_rows])


class _FakeAnon:
    def __init__(self, keyword_rows=()):
        self.keyword_rows = list(keyword_rows)

    def table(self, name):
        return _FakeAnonQuery(self, name)


def _rpc_row(rid, content, similarity=0.82, memory_type="long_term"):
    return {"memory_item_id": rid, "content": content,
            "memory_type": memory_type, "importance": 5, "confidence": 0.9,
            "subject_key": None, "valid_at": None, "expires_at": None,
            "source": "web", "similarity": similarity}


def _keyword_row(rid="m1", content="SYNTHETIC_KEYWORD_喜欢无糖咖啡", tags="TG_MSG"):
    return {"id": rid, "title": "咖啡偏好", "content": content,
            "importance": 4, "tags": tags}


def _run_search(svc, anon, query="无糖咖啡", *, pinecone=None, embed=None):
    """在 mock 环境下执行 search_memory，返回 (输出文本, stdout, embed)。"""
    if embed is None:
        embed = _RecordingEmbed()
    buf = io.StringIO()
    with patch.object(server, "supabase_service", svc), \
         patch.object(server, "supabase", anon), \
         patch.object(server, "pinecone_memory", pinecone), \
         patch.object(server, "_get_embedding", embed), \
         patch.object(server, "_resolve_pinecone_user_id", lambda: TEST_USER_ID):
        with contextlib.redirect_stdout(buf):
            out = asyncio.run(server.search_memory(query))
    return out, buf.getvalue(), embed


# ==========================================
# A. 新第一段优先 + 三段拼接
# ==========================================

class TestItemsSectionPriority(unittest.TestCase):

    def test_items_section_first_and_formatted(self):
        svc = _FakeServiceRpc(rows=[_rpc_row("11111111-1111-1111-1111-111111111111",
                                             "用户喜欢无糖咖啡。"),
                                    _rpc_row("22222222-2222-2222-2222-222222222222",
                                             "用户最近在准备考试。",
                                             memory_type="current")])
        anon = _FakeAnon(keyword_rows=[_keyword_row()])
        out, _, _ = _run_search(svc, anon)

        lines = out.splitlines()
        self.assertEqual(lines[0], "🧠 【长期记忆】:", "新第一段在最前")
        self.assertIn("- 用户喜欢无糖咖啡。", lines)
        self.assertIn("- 用户最近在准备考试。", lines)
        # 旧关键词段仍在，且排在新段之后
        self.assertIn("🔍 【关键词匹配记忆】:", out)
        self.assertLess(out.index("🧠 【长期记忆】"),
                        out.index("🔍 【关键词匹配记忆】"),
                        "三段顺序：长期记忆 → 语义相似 → 关键词匹配")
        self.assertNotIn("【语义相似记忆】", out, "pinecone 未配置时该段跳过")

    def test_items_miss_falls_back_to_old_sections(self):
        svc = _FakeServiceRpc(rows=[])
        anon = _FakeAnon(keyword_rows=[_keyword_row()])
        out, _, _ = _run_search(svc, anon)

        self.assertNotIn("【长期记忆】", out, "查不到 memory_items 时不出现新段")
        self.assertIn("🔍 【关键词匹配记忆】:", out, "回退到旧关键词段")

    def test_rpc_failure_falls_back(self):
        svc = _FakeServiceRpc(exc=RuntimeError("mock rpc failure"))
        anon = _FakeAnon(keyword_rows=[_keyword_row()])
        out, _, _ = _run_search(svc, anon)

        self.assertNotIn("【长期记忆】", out, "RPC 失败时新段跳过")
        self.assertIn("🔍 【关键词匹配记忆】:", out, "旧段兜底不受影响")

    def test_all_empty_returns_placeholder(self):
        svc = _FakeServiceRpc(rows=[])
        anon = _FakeAnon(keyword_rows=[])
        out, _, _ = _run_search(svc, anon)
        self.assertEqual(out, "🧠 暂未搜到相关记忆。")

    def test_items_capped_at_five(self):
        rows = [_rpc_row(f"aaaaaaaa-0000-0000-0000-{i:012d}",
                         f"SYNTHETIC_ITEM_{i}") for i in range(7)]
        svc = _FakeServiceRpc(rows=rows)
        anon = _FakeAnon(keyword_rows=[])
        out, _, _ = _run_search(svc, anon)

        block_lines = [l for l in out.splitlines() if l.startswith("- ")]
        self.assertEqual(len(block_lines), 5, "长期记忆段最多 5 条")
        self.assertNotIn("SYNTHETIC_ITEM_5", out)
        self.assertNotIn("SYNTHETIC_ITEM_6", out)


# ==========================================
# D. 私密过滤不回归
# ==========================================

class TestPrivateFilter(unittest.TestCase):

    def test_secret_diary_keyword_row_filtered(self):
        svc = _FakeServiceRpc(rows=[])
        anon = _FakeAnon(keyword_rows=[
            _keyword_row(rid="m-sec", content="SYNTHETIC_SECRET_DIARY_BODY",
                         tags="Secret_Diary"),
            _keyword_row(rid="m-ok", content="SYNTHETIC_KEYWORD_普通记忆"),
        ])
        out, _, _ = _run_search(svc, anon)

        self.assertNotIn("SYNTHETIC_SECRET_DIARY_BODY", out,
                         "私密标签记忆不得通过搜索暴露（既有防御不回归）")
        self.assertIn("SYNTHETIC_KEYWORD_普通记忆", out)

    def test_priv_filter_import_fail_skips_b2_section(self):
        """D3 隐私 fail-closed：memory_diary_bridge import 失败时整段 B2 跳过，旧段仍可用。"""
        import builtins
        real_import = builtins.__import__

        def _block_diary_bridge(name, *args, **kwargs):
            if name == "memory_diary_bridge" or (
                    isinstance(name, str) and name.startswith("memory_diary_bridge.")):
                raise ImportError("simulated diary bridge unavailable")
            return real_import(name, *args, **kwargs)

        svc = _FakeServiceRpc(rows=[
            _rpc_row("11111111-1111-1111-1111-111111111111",
                     "SYNTHETIC_B2_SHOULD_NOT_APPEAR"),
        ])
        anon = _FakeAnon(keyword_rows=[
            _keyword_row(content="SYNTHETIC_KEYWORD_旧段仍可见"),
        ])
        with patch("builtins.__import__", side_effect=_block_diary_bridge):
            out, _, _ = _run_search(svc, anon)

        self.assertNotIn("【长期记忆】", out,
                         "import 失败时 B2 新记忆段整段跳过")
        self.assertNotIn("SYNTHETIC_B2_SHOULD_NOT_APPEAR", out)
        self.assertIn("🔍 【关键词匹配记忆】:", out, "旧关键词段不受影响")
        self.assertIn("SYNTHETIC_KEYWORD_旧段仍可见", out)
        self.assertEqual(svc.execute_count, 0,
                         "fail-closed 时不应发起 hybrid recall RPC")


# ==========================================
# E+F. user_id / RPC 参数 / 日志脱敏
# ==========================================

class TestUserIdAndTelemetry(unittest.TestCase):

    def test_user_id_resolved_server_side(self):
        svc = _FakeServiceRpc(rows=[_rpc_row("11111111-1111-1111-1111-111111111111",
                                             "用户喜欢无糖咖啡。")])
        anon = _FakeAnon(keyword_rows=[])
        embed = _RecordingEmbed()
        _run_search(svc, anon, query="我喜欢喝什么", embed=embed)

        self.assertEqual(svc.name, "match_memory_items")
        self.assertEqual(svc.params["p_user_id"], TEST_USER_ID,
                         "user_id 由服务端统一解析，无客户端提交入口")
        self.assertEqual(svc.params["match_count"], 10,
                         "match_count 由召回模块固定")
        self.assertEqual(embed.calls, ["我喜欢喝什么"],
                         "embedding 恰调用一次，输入为查询文本")

    def test_stdout_no_content_leak(self):
        svc = _FakeServiceRpc(rows=[_rpc_row("11111111-1111-1111-1111-111111111111",
                                             "SYNTHETIC_ITEM_SECRET_CONTENT")])
        anon = _FakeAnon(keyword_rows=[_keyword_row(
            content="SYNTHETIC_KEYWORD_SECRET_CONTENT")])
        out, stdout, _ = _run_search(svc, anon, query="SYNTHETIC_QUERY_TEXT")

        self.assertIn("SYNTHETIC_ITEM_SECRET_CONTENT", out, "正文进返回值供模型读取")
        self.assertNotIn("SYNTHETIC_ITEM_SECRET_CONTENT", stdout,
                         "stdout 不得出现记忆正文")
        self.assertNotIn("SYNTHETIC_KEYWORD_SECRET_CONTENT", stdout)
        self.assertNotIn("SYNTHETIC_QUERY_TEXT", stdout, "stdout 不得出现查询原文")
        self.assertNotIn(TEST_USER_ID, stdout, "stdout 不得出现 user_id")


if __name__ == "__main__":
    unittest.main()
