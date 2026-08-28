# -*- coding: utf-8 -*-
"""Phase 6 专项测试 —— 低成本共同经历与 AI 行为记忆。

全部使用 mock，禁止连接 Supabase / Pinecone / embedding / 上游 LLM / Zeabur / TG / QQ / WebDAV。
覆盖规范第十四节 A~I：
  A. 结构化输出解析
  B. 低价值过滤
  C. 有价值共同经历
  D. 批处理触发
  E. Supabase 写入 mock
  F. Pinecone 写入 mock
  G. 召回
  H. 人格隔离
  I. 成本统计
（J 相位回归在验证阶段单独运行其它 test_*.py）
"""

import asyncio
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import shared_experience as se


# ── 公共 fixtures ─────────────────────────────────────────────
GOOD_OUTPUT = (
    "最近我们聊了科一错题整理的事，用户在备考。\n"
    "<shared_experiences>\n"
    '{"shared_experiences":[{"summary":"用户和AI一起整理了科一错题并约定继续复盘",'
    '"user_events":["用户正在准备科一考试"],'
    '"ai_actions":["AI陪用户讨论了科一错题整理"],'
    '"commitments":["之后继续帮助复盘错题"],'
    '"open_threads":["科一错题复盘尚未完成"],'
    '"confidence":0.88,"style_sample":true}]}\n'
    "</shared_experiences>"
)

TWO_ITEM_OUTPUT = (
    "总结正文。\n"
    "<shared_experiences>\n"
    '{"shared_experiences":['
    '{"summary":"用户和AI一起整理了科一错题","ai_actions":["AI陪用户讨论错题"],"confidence":0.8},'
    '{"summary":"用户计划明天继续复盘","open_threads":["错题复盘未完成"],"confidence":0.7}'
    ']}\n'
    "</shared_experiences>"
)


def _make_dep():
    """构造一个 mock 的 server 依赖（_save_memory_to_db + pinecone_memory）。"""
    dep = MagicMock()
    dep._save_memory_to_db = MagicMock(return_value=True)
    dep.pinecone_memory = MagicMock()
    dep.pinecone_memory.add = MagicMock(return_value=True)
    dep.ask_role_sync = MagicMock(return_value=GOOD_OUTPUT)
    dep.supabase = MagicMock()
    return dep


# ════════════════════════════════════════════════════════════
# A. 结构化输出解析
# ════════════════════════════════════════════════════════════
class TestAParse(unittest.TestCase):
    def test_A1_valid_json(self):
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"用户和AI整理了错题","confidence":0.8}]}'
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["summary"], "用户和AI整理了错题")
        self.assertFalse(items[0]["style_sample"])

    def test_A2_empty_array(self):
        self.assertEqual(se.parse_shared_experiences('{"shared_experiences":[]}'), [])

        self.assertEqual(se.parse_shared_experiences('{"shared_experiences":[]}'), [])  # 重复确认空数组

    def test_A3_multiple_items(self):
        items = se.parse_shared_experiences(TWO_ITEM_OUTPUT.split("<shared_experiences>\n", 1)[1].split("</shared_experiences>")[0].strip())
        self.assertEqual(len(items), 2)

    def test_A4_missing_fields(self):
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"有摘要"},{"summary":""},{"no_summary":1}]}'
        )
        self.assertEqual(len(items), 1)  # 只有第一条有 summary
        self.assertEqual(items[0]["user_events"], [])
        self.assertEqual(items[0]["ai_actions"], [])

    def test_A5_invalid_json(self):
        self.assertEqual(se.parse_shared_experiences("not json at all"), [])
        self.assertEqual(se.parse_shared_experiences("{broken"), [])
        self.assertEqual(se.parse_shared_experiences(""), [])
        self.assertEqual(se.parse_shared_experiences(None), [])

    def test_A6_too_many_capped(self):
        arr = [{"summary": f"摘要{i}"} for i in range(5)]
        items = se.parse_shared_experiences(json.dumps({"shared_experiences": arr}))
        self.assertEqual(len(items), se.MAX_ITEMS)

    def test_A7_overlong_clamped(self):
        long_summary = "字" * 200
        long_item = "x" * 100
        items = se.parse_shared_experiences(json.dumps({"shared_experiences": [{
            "summary": long_summary,
            "user_events": [long_item],
        }]}))
        self.assertEqual(len(items), 1)
        self.assertLessEqual(len(items[0]["summary"]), se.MAX_SUMMARY_CHARS)
        self.assertLessEqual(len(items[0]["user_events"][0]), se.MAX_ARRAY_ITEM_CHARS)
        self.assertLessEqual(len(items[0]["user_events"]), se.MAX_ARRAY_ITEMS)

    def test_A8_confidence_invalid(self):
        # 非法/越界值一律 clamp 到 [0,1]
        self.assertEqual(se._clamp_confidence("abc"), 0.0)
        self.assertEqual(se._clamp_confidence(-1), 0.0)
        self.assertEqual(se._clamp_confidence(5.0), 1.0)
        self.assertEqual(se._clamp_confidence(0.5), 0.5)
        self.assertEqual(se._clamp_confidence(None), 0.0)
        self.assertEqual(se._clamp_confidence(float("nan")), 0.0)
        # 模型给的非法 confidence 经 sanitize 后归 0
        for bad in ["abc", -1, 5.0, None]:
            items = se.parse_shared_experiences(json.dumps({"shared_experiences": [
                {"summary": "x", "confidence": bad}]}))
            expected = 0.0 if bad in ("abc", -1, None) else 1.0
            self.assertEqual(items[0]["confidence"], expected)


    def test_A9_style_sample_forced_false(self):
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"x","style_sample":true}]}'
        )
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["style_sample"])  # 强制 false

    def test_A10_no_assistant_leakage(self):
        # summary 含旧 assistant 角色标记 → 整条丢弃
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"assistant: 我之前说过..."}]}'
        )
        self.assertEqual(items, [])
        # 数组项含 assistant 标记 → 该项被滤除
        items2 = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"正常摘要","ai_actions":["assistant: 你好"]}]}'
        )
        self.assertEqual(items2[0]["ai_actions"], [])


# ════════════════════════════════════════════════════════════
# B. 低价值过滤（代码层保证：提示词含排除规则 + 空/无标签不写入）
# ════════════════════════════════════════════════════════════
class TestBLowValue(unittest.TestCase):
    def test_B1_prompt_excludes_lowvalue(self):
        p = se.build_extraction_prompt_suffix("助手", "用户")
        for kw in ["哈哈", "天气", "宠物tick", "工具结果", "thinking", "口头禅", "撒娇"]:
            self.assertIn(kw, p, f"提示词应排除 {kw}")
        self.assertIn("不是风格样本", p)
        self.assertIn("style_sample", p)

    def test_B2_empty_result_no_write(self):
        dep = _make_dep()
        cnt = se.persist_shared_experiences(se.parse_shared_experiences('{"shared_experiences":[]}'), dep)
        self.assertEqual(cnt, {"supabase": 0, "pinecone": 0})
        dep._save_memory_to_db.assert_not_called()
        dep.pinecone_memory.add.assert_not_called()

    def test_B3_no_tag_no_extraction(self):
        core, raw = se.split_summary_and_shared("就是一段普通总结，没有结构化标签。")
        self.assertIsNone(raw)
        self.assertEqual(core, "就是一段普通总结，没有结构化标签。")
        self.assertEqual(se.parse_shared_experiences(raw), [])


# ════════════════════════════════════════════════════════════
# C. 有价值共同经历
# ════════════════════════════════════════════════════════════
class TestCValuable(unittest.TestCase):
    def test_C1_joint_task(self):
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"用户和AI一起整理了科一错题","ai_actions":["AI陪用户讨论错题整理"]}]}'
        )
        self.assertEqual(len(items), 1)
        self.assertIn("整理", items[0]["summary"])

    def test_C2_ai_helped(self):
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"AI帮用户查了快递单号","ai_actions":["AI查询了快递物流"]}]}'
        )
        self.assertEqual(len(items), 1)

    def test_C3_commitment(self):
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"约定明天继续复盘","commitments":["明天继续复盘错题"]}]}'
        )
        self.assertEqual(items[0]["commitments"], ["明天继续复盘错题"])

    def test_C4_open_thread(self):
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"错题还没整理完","open_threads":["错题复盘未完成"]}]}'
        )
        self.assertEqual(items[0]["open_threads"], ["错题复盘未完成"])

    def test_C5_persist_all_fields(self):
        dep = _make_dep()
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"用户和AI整理错题",'
            '"user_events":["用户备考科一"],"ai_actions":["AI陪讨论"],'
            '"commitments":["继续复盘"],"open_threads":["未完成"],"confidence":0.9}]}'
        )
        cnt = se.persist_shared_experiences(items, dep)
        self.assertEqual(cnt, {"supabase": 1, "pinecone": 1})
        self.assertTrue(dep._save_memory_to_db.called)
        self.assertTrue(dep.pinecone_memory.add.called)


# ════════════════════════════════════════════════════════════
# D. 批处理触发（不每轮调用，只在批量总结触发；每批最多一次）
# ════════════════════════════════════════════════════════════
class TestDBatchTrigger(unittest.IsolatedAsyncioTestCase):
    def _mock_dep_with_chats(self, n):
        dep = _make_dep()
        data = [{"id": i, "title": f"用户{i}", "content": f"聊天内容{i}", "tags": "Web_Chat"} for i in range(n)]
        result = MagicMock()
        result.data = data
        dep.supabase.table.return_value.select.return_value.in_.return_value.order.return_value.execute.return_value = result
        return dep

    async def test_D1_below_threshold_no_extraction(self):
        dep = self._mock_dep_with_chats(5)  # < 30
        with patch.dict(os.environ, {"SUMMARY_THRESHOLD": "30"}), patch("napcat._get_deps", return_value=dep), patch("napcat._naplog"):
            await __import__("napcat").check_and_summarize_all()
        dep.ask_role_sync.assert_not_called()
        dep._save_memory_to_db.assert_not_called()
        dep.pinecone_memory.add.assert_not_called()

    async def test_D2_at_threshold_one_call_and_extract(self):
        dep = self._mock_dep_with_chats(30)  # == threshold
        with patch.dict(os.environ, {"SUMMARY_THRESHOLD": "30"}), patch("napcat._get_deps", return_value=dep), patch("napcat._naplog"):
            await __import__("napcat").check_and_summarize_all()
        # 只调用一次 LLM（复用，0 额外调用）
        self.assertEqual(dep.ask_role_sync.call_count, 1)
        # 提示词含结构化提取后缀
        prompt_arg = dep.ask_role_sync.call_args[0][1]
        self.assertIn("<shared_experiences>", prompt_arg)
        # Core_Cognition 保存一次（用切分后的 core_text）
        self.assertTrue(dep._save_memory_to_db.called)
        # 共同经历写入：GOOD_OUTPUT 含 1 条 → pinecone add 1 次
        self.assertEqual(dep.pinecone_memory.add.call_count, 1)

    async def test_D3_no_tag_degrades_gracefully(self):
        dep = self._mock_dep_with_chats(30)
        dep.ask_role_sync = MagicMock(return_value="纯文本总结，无结构化块。")
        with patch.dict(os.environ, {"SUMMARY_THRESHOLD": "30"}), patch("napcat._get_deps", return_value=dep), patch("napcat._naplog"):
            await __import__("napcat").check_and_summarize_all()
        self.assertEqual(dep.ask_role_sync.call_count, 1)
        # Core_Cognition 仍保存（整段当作正文）
        self.assertTrue(dep._save_memory_to_db.called)
        # 无共同经历写入
        dep.pinecone_memory.add.assert_not_called()

    async def test_D4_failure_does_not_block_archive(self):
        dep = self._mock_dep_with_chats(30)
        dep.ask_role_sync = MagicMock(side_effect=Exception("LLM 全部端点失败"))
        with patch.dict(os.environ, {"SUMMARY_THRESHOLD": "30"}), patch("napcat._get_deps", return_value=dep), patch("napcat._naplog"):
            await __import__("napcat").check_and_summarize_all()  # 不应抛
        # 归档仍执行（update 链被调用）
        self.assertTrue(dep.supabase.table.return_value.update.called)


# ════════════════════════════════════════════════════════════
# E. Supabase 写入 mock
# ════════════════════════════════════════════════════════════
class TestESupabaseWrite(unittest.TestCase):
    def test_E1_uses_correct_fields(self):
        dep = _make_dep()
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"用户和AI整理错题","confidence":0.8}]}'
        )
        se.persist_shared_experiences(items, dep)
        args = dep._save_memory_to_db.call_args[0]
        self.assertEqual(args[0], "🤝 共同经历")          # title
        self.assertEqual(args[2], "事件")                 # category
        self.assertEqual(args[3], "平静")                 # mood
        self.assertEqual(args[4], "Shared_Experience")    # tags

    def test_E2_no_raw_chat_in_content(self):
        dep = _make_dep()
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"用户和AI整理错题"}]}'
        )
        se.persist_shared_experiences(items, dep)
        content_json = dep._save_memory_to_db.call_args[0][1]
        data = json.loads(content_json)
        self.assertNotIn("user:", content_json)
        self.assertNotIn("assistant:", content_json)
        self.assertNotIn("聊天内容", content_json)

    def test_E3_no_thinking_in_content(self):
        dep = _make_dep()
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"用户和AI整理错题"}]}'
        )
        se.persist_shared_experiences(items, dep)
        content_json = dep._save_memory_to_db.call_args[0][1]
        self.assertNotIn("thinking", content_json.lower())
        self.assertNotIn("reasoning", content_json.lower())

    def test_E4_failure_no_block(self):
        dep = _make_dep()
        dep._save_memory_to_db = MagicMock(side_effect=Exception("DB 挂了"))
        items = se.parse_shared_experiences('{"shared_experiences":[{"summary":"x"}]}')
        cnt = se.persist_shared_experiences(items, dep)
        self.assertEqual(cnt["supabase"], 0)
        # 不抛异常，pinecone 仍尝试
        self.assertEqual(cnt["pinecone"], 1)


# ════════════════════════════════════════════════════════════
# F. Pinecone 写入 mock
# ════════════════════════════════════════════════════════════
class TestFPineconeWrite(unittest.TestCase):
    def test_F1_short_summary_and_metadata(self):
        dep = _make_dep()
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"用户和AI整理错题"}]}'
        )
        se.persist_shared_experiences(items, dep)
        call = dep.pinecone_memory.add.call_args
        msgs = call[0][0]
        meta = call[1]["metadata"]
        self.assertEqual(msgs, [{"role": "memory", "content": "用户和AI整理错题"}])
        self.assertEqual(meta["schema_version"], "v2")
        self.assertEqual(meta["source_role"], "system")
        self.assertEqual(meta["memory_type"], "shared_experience")
        self.assertEqual(meta["tags"], "Shared_Experience")
        self.assertFalse(meta["style_sample"])

    def test_F2_no_assistant_raw(self):
        dep = _make_dep()
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"用户和AI整理错题"}]}'
        )
        se.persist_shared_experiences(items, dep)
        msgs = dep.pinecone_memory.add.call_args[0][0]
        text = msgs[0]["content"]
        self.assertNotIn("assistant:", text)
        self.assertNotIn("user:", text)

    def test_F3_empty_no_write(self):
        dep = _make_dep()
        se.persist_shared_experiences([], dep)
        dep.pinecone_memory.add.assert_not_called()

    def test_F4_failure_no_block(self):
        dep = _make_dep()
        dep.pinecone_memory.add = MagicMock(side_effect=Exception("Pinecone 挂了"))
        items = se.parse_shared_experiences('{"shared_experiences":[{"summary":"x"}]}')
        cnt = se.persist_shared_experiences(items, dep)
        self.assertEqual(cnt["pinecone"], 0)
        self.assertEqual(cnt["supabase"], 1)


# ════════════════════════════════════════════════════════════
# G. 召回（分区 + 短摘要注入，不改 top_k/filter/namespace）
# ════════════════════════════════════════════════════════════
class TestGRecall(unittest.TestCase):
    def test_G1_partition(self):
        results = [
            {"memory": "memory: 用户问过快递", "tags": ""},
            {"memory": "memory: 用户和AI整理错题", "tags": "Shared_Experience"},
            {"memory": "memory: 普通流水", "tags": ""},
        ]
        regular, shared = se.partition_recall(results)
        self.assertEqual(len(regular), 2)
        self.assertEqual(len(shared), 1)

    def test_G2_render_short_summary(self):
        shared = [{"memory": "memory: 用户和AI整理了科一错题"}]
        block = se.render_shared_context(shared)
        self.assertIn("【相关共同经历】", block)
        self.assertIn("用户和AI整理了科一错题", block)
        self.assertNotIn("memory_type", block)
        self.assertNotIn("evidence", block)
        self.assertNotIn("style_sample", block)

    def test_G3_no_json_raw(self):
        shared = [{"memory": "memory: 摘要文本"}]
        block = se.render_shared_context(shared)
        self.assertNotIn("{", block)
        self.assertNotIn("shared_experiences", block)

    def test_G4_assistant_format_filtered(self):
        shared = [{"memory": "memory: assistant: 我说过要陪你"}]
        block = se.render_shared_context(shared)
        self.assertEqual(block, "")  # 含旧 assistant 格式 → 不注入

    def test_G5_empty_returns_empty(self):
        self.assertEqual(se.render_shared_context([]), "")
        self.assertEqual(se.render_shared_context(None), "")

    def test_G6_no_query_logic_in_module(self):
        # 模块只做渲染分区，不发起 Pinecone 查询、不改 top_k/filter/namespace
        with open(se.__file__, encoding="utf-8") as f:
            src = f.read()
        for token in ["top_k=", "namespace=", ".query(", "upsert", "pine_filter", ".Index("]:
            self.assertNotIn(token, src, f"模块不应含查询逻辑 {token}")


# ════════════════════════════════════════════════════════════
# H. 人格隔离
# ════════════════════════════════════════════════════════════
class TestHPersonaIsolation(unittest.TestCase):
    def test_H1_no_raw_assistant_in_pinecone(self):
        dep = _make_dep()
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"AI帮用户查了快递"}]}'
        )
        se.persist_shared_experiences(items, dep)
        text = dep.pinecone_memory.add.call_args[0][0][0]["content"]
        self.assertNotIn("assistant:", text)
        self.assertEqual(text, "AI帮用户查了快递")  # 只有短事实

    def test_H2_style_sample_always_false(self):
        for val in [True, "true", 1, "yes"]:
            items = se.parse_shared_experiences(
                json.dumps({"shared_experiences": [{"summary": "x", "style_sample": val}]})
            )
            self.assertFalse(items[0]["style_sample"])

    def test_H3_prompt_disclaimer(self):
        p = se.build_extraction_prompt_suffix("助手", "用户")
        self.assertIn("不是风格样本", p)
        self.assertIn("不得复制", p)
        self.assertIn("人格定性", p)

    def test_H4_no_persona_modification(self):
        # 模块不触碰 AI_PERSONA / world_book / reply_rules
        with open(se.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("AI_PERSONA", src)
        self.assertNotIn("world_book", src)
        self.assertNotIn("reply_rules", src)


# ════════════════════════════════════════════════════════════
# I. 成本统计
# ════════════════════════════════════════════════════════════
class TestICost(unittest.TestCase):
    def test_I1_no_per_round_extraction(self):
        # 每轮不调用提取：split 无标签时 se_raw=None，parse 不调用任何外部服务
        core, raw = se.split_summary_and_shared("普通总结")
        self.assertIsNone(raw)
        self.assertEqual(se.parse_shared_experiences(raw), [])

    def test_I2_one_call_per_batch(self):
        dep = _make_dep()
        items = se.parse_shared_experiences(
            '{"shared_experiences":['
            '{"summary":"第一条"},{"summary":"第二条"},{"summary":"第三条"},'
            '{"summary":"第四条"}]}'  # 4 条，应截断为 3
        )
        cnt = se.persist_shared_experiences(items, dep)
        self.assertEqual(len(items), 3)  # 截断
        # 每条 1 次 pinecone add（embedding 在 add 内部，无额外模型调用）
        self.assertEqual(dep.pinecone_memory.add.call_count, 3)
        self.assertEqual(cnt["pinecone"], 3)

    def test_I3_no_new_embedding_model(self):
        # persist 只复用 dep.pinecone_memory.add，不引入独立 embedding/LLM 调用
        dep = _make_dep()
        items = se.parse_shared_experiences('{"shared_experiences":[{"summary":"x"}]}')
        se.persist_shared_experiences(items, dep)
        self.assertTrue(dep.pinecone_memory.add.called)      # 唯一外部写入路径
        self.assertFalse(dep._get_embedding.called)           # 不直接调 embedding
        self.assertFalse(dep.ask_role_sync.called)            # 不触发额外 LLM

    def test_I4_failure_no_retry(self):
        dep = _make_dep()
        dep.pinecone_memory.add = MagicMock(side_effect=Exception("fail"))
        items = se.parse_shared_experiences(
            '{"shared_experiences":[{"summary":"a"},{"summary":"b"}]}'
        )
        cnt = se.persist_shared_experiences(items, dep)
        # 每条只尝试一次，不重试
        self.assertEqual(dep.pinecone_memory.add.call_count, 2)
        self.assertEqual(cnt["pinecone"], 0)


# ════════════════════════════════════════════════════════════
# split_summary_and_shared 单测
# ════════════════════════════════════════════════════════════
class TestSplit(unittest.TestCase):
    def test_split_no_tag(self):
        core, raw = se.split_summary_and_shared("just a summary")
        self.assertEqual(core, "just a summary")
        self.assertIsNone(raw)

    def test_split_with_tag(self):
        core, raw = se.split_summary_and_shared(GOOD_OUTPUT)
        self.assertEqual(core, "最近我们聊了科一错题整理的事，用户在备考。")
        self.assertIn("shared_experiences", raw)

    def test_split_open_no_close(self):
        out = "正文\n<shared_experiences>\n{bad json"
        core, raw = se.split_summary_and_shared(out)
        self.assertEqual(core, "正文")
        self.assertIn("bad json", raw)
        self.assertEqual(se.parse_shared_experiences(raw), [])

    def test_split_code_fence(self):
        out = (
            "总结。\n<shared_experiences>\n"
            "```json\n" + '{"shared_experiences":[{"summary":"围栏测试"}]}' + "\n```\n"
            "</shared_experiences>"
        )
        core, raw = se.split_summary_and_shared(out)
        self.assertEqual(core, "总结。")
        items = se.parse_shared_experiences(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["summary"], "围栏测试")

    def test_split_empty(self):
        core, raw = se.split_summary_and_shared("")
        self.assertEqual(core, "")
        self.assertIsNone(raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
