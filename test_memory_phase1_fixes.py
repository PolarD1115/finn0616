# -*- coding: utf-8 -*-
"""第 1 阶段修复专项测试 —— 记忆安全读取与失败保护。

全部 unittest + mock，不连接 Supabase / Pinecone / 上游 LLM，所有数据均为虚构脱敏样例。

覆盖：
  A. napcat.check_and_summarize_all 总结失败不归档（LLM 异常 / 总结为空 / 写入失败），
     成功路径只归档本次真正进入总结 prompt 的记录。
  B. gateway._extract_user_side_from_history 纯函数 + gateway._inject_context
     数据库历史不重放旧 assistant 回复，客户端真实 assistant 保留。
  C. server._build_channel_context 只注入用户侧历史文本。
  D. heartbeat._clean_old_memories 不执行任何删除（含源码约束）。

运行：  python -m unittest test_memory_phase1_fixes -v
"""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import napcat
import gateway
import server
import heartbeat


# ==========================================
# 通用假件（记录型，绝不触网）
# ==========================================

class FakeResult:
    """模拟 supabase-py execute() 返回的 APIResponse（带 .data 属性）。"""

    def __init__(self, data):
        self.data = data


class FakeQuery:
    """链式查询记录器：记录每个方法调用，execute 时回调 selector 返回预设结果。"""

    def __init__(self, owner, table):
        self._owner = owner
        self._table = table
        self._path = []

    def _rec(self, method, *args, **kwargs):
        self._path.append((method, args))
        return self

    def select(self, *a, **k): return self._rec("select", *a, **k)
    def insert(self, *a, **k): return self._rec("insert", *a, **k)
    def update(self, *a, **k): return self._rec("update", *a, **k)
    def delete(self, *a, **k): return self._rec("delete", *a, **k)
    def eq(self, *a, **k): return self._rec("eq", *a, **k)
    def neq(self, *a, **k): return self._rec("neq", *a, **k)
    def in_(self, *a, **k): return self._rec("in_", *a, **k)
    def lt(self, *a, **k): return self._rec("lt", *a, **k)
    def gt(self, *a, **k): return self._rec("gt", *a, **k)
    def order(self, *a, **k): return self._rec("order", *a, **k)
    def limit(self, *a, **k): return self._rec("limit", *a, **k)
    def maybe_single(self, *a, **k): return self._rec("maybe_single", *a, **k)

    def execute(self, *a, **k):
        self._path.append(("execute", ()))
        self._owner.calls.append((self._table, tuple(self._path)))
        return self._owner._selector(self._table, self._path)


class FakeSupabase:
    """按 (table, 查询路径) 分流返回预设结果的假客户端。"""

    def __init__(self, selector=None):
        self.calls = []
        self._selector = selector or (lambda table, path: FakeResult([]))

    def table(self, name):
        return FakeQuery(self, name)

    def updates_on(self, table):
        """返回对某表发起过 update 的查询路径列表。"""
        out = []
        for tbl, path in self.calls:
            if tbl == table and path and path[0][0] == "update":
                out.append(path)
        return out


def _row(rid, content, tags, ts):
    return {"id": rid, "title": f"测试记录{rid}", "content": content, "tags": tags,
            "created_at": ts, "category": "流水", "mood": "平静", "importance": 1}


# 三种写入格式的虚构历史样例（脱敏）
_WEB_USER = _row(1, "小满：我喜欢无糖咖啡", "Web_Chat", "2026-08-28T01:00:00+00:00")
_WEB_AI = _row(2, "我(Finn)：当然可以，我会一直记住你的偏好。", "Web_Chat", "2026-08-28T01:01:00+00:00")
_TG_MIXED = _row(3, "用户: 我最近在准备考试\n回复: 你一定可以的，加油。", "TG_MSG", "2026-08-28T02:00:00+00:00")
_QQ_MIXED = _row(4, "测试昵称: 我家猫又拆家了\n回复: 哈哈小猫咪嘛。", "QQ_MSG", "2026-08-28T03:00:00+00:00")

HISTORY_ROWS = [_WEB_USER, _WEB_AI, _TG_MIXED, _QQ_MIXED]

# Web 历史查询 / TG summaries 查询的分流 selector
def _memories_selector_factory(history_rows):
    def selector(table, path):
        if table == "user_facts":
            return FakeResult([])
        if table == "memories":
            for method, args in path:
                if method == "eq" and args and args[0] == "tags" and args[1] == "Core_Cognition":
                    return FakeResult([])
                if method == "in_" and args and args[0] == "tags":
                    return FakeResult(list(history_rows))
            return FakeResult(list(history_rows))
        return FakeResult([])
    return selector


def _history_query_selector(table, path):
    """只服务 in_ 历史查询；Core_Cognition 等其他查询一律返回空。"""
    if table == "memories":
        for method, args in path:
            if method == "in_" and args and args[0] == "tags":
                return FakeResult(list(HISTORY_ROWS))
    return FakeResult([])


# ==========================================
# A. 总结失败不归档
# ==========================================

class TestSummarizeFailureNoArchive(unittest.TestCase):
    """check_and_summarize_all：LLM 异常 / 总结为空 / 写入失败 一律不归档。"""

    def _make_dep(self, supabase, ask_fn, save_fn):
        class _Dep:
            pass
        dep = _Dep()
        dep.supabase = supabase
        dep.ask_role_sync = ask_fn
        dep._save_memory_to_db = save_fn
        return dep

    def _fake_supabase(self):
        # 8 条积压流水（id 1-8，created_at 升序），阈值 5 → 总结最新 5 条（id 4-8）
        rows = [
            _row(i, f"测试流水内容{i}号", ["Web_Chat", "QQ_MSG", "TG_MSG"][i % 3],
                 f"2026-08-27T0{i}:00:00+00:00")
            for i in range(1, 9)
        ]
        def selector(table, path):
            if table == "memories":
                for method, args in path:
                    if method == "in_" and args and args[0] == "tags":
                        return FakeResult(list(rows))
            return FakeResult([])
        return FakeSupabase(selector)

    def _run(self, dep):
        with patch.object(napcat, "_get_deps", lambda: dep), \
             patch.dict(os.environ, {"SUMMARY_THRESHOLD": "5", "CHAT_TAG": "Web_Chat",
                                     "AI_NAME": "Finn", "USER_NAME": "小满"}):
            asyncio.run(napcat.check_and_summarize_all())

    def test_llm_exception_does_not_archive(self):
        sb = self._fake_supabase()
        def ask_fail(role, prompt, **kw):
            raise RuntimeError("mock: compression 端点全部失败")
        dep = self._make_dep(sb, ask_fail, lambda *a, **k: True)
        self._run(dep)  # 不应向上抛异常
        self.assertEqual(sb.updates_on("memories"), [], "LLM 异常时禁止归档 UPDATE")

    def test_llm_empty_does_not_archive_and_no_empty_summary(self):
        sb = self._fake_supabase()
        saved = []
        dep = self._make_dep(sb, lambda role, prompt, **kw: "", lambda *a, **k: saved.append(a))
        self._run(dep)
        self.assertEqual(sb.updates_on("memories"), [], "总结为空时禁止归档 UPDATE")
        self.assertEqual(saved, [], "总结为空时不得生成空 Core_Cognition")

    def test_core_save_failed_does_not_archive(self):
        sb = self._fake_supabase()
        dep = self._make_dep(sb, lambda role, prompt, **kw: "这是一段成功的总结正文。",
                             lambda *a, **k: False)
        self._run(dep)
        self.assertEqual(sb.updates_on("memories"), [], "Core_Cognition 写入失败时禁止归档 UPDATE")

    def test_core_save_exception_does_not_archive(self):
        sb = self._fake_supabase()
        def save_fail(*a, **k):
            raise RuntimeError("mock: 写入异常")
        dep = self._make_dep(sb, lambda role, prompt, **kw: "这是一段成功的总结正文。", save_fail)
        self._run(dep)  # 不应向上抛异常
        self.assertEqual(sb.updates_on("memories"), [], "写入异常时禁止归档 UPDATE")

    def test_success_archives_only_summarized_items(self):
        sb = self._fake_supabase()
        dep = self._make_dep(sb, lambda role, prompt, **kw: "这是最新 5 条流水的总结正文。",
                             lambda *a, **k: True)
        self._run(dep)
        updates = sb.updates_on("memories")
        self.assertEqual(len(updates), 1, "成功路径应恰好归档一次")
        update_path = updates[0]
        in_ids = None
        for method, args in update_path:
            if method == "in_" and args and args[0] == "id":
                in_ids = args[1]
        self.assertEqual(in_ids, [4, 5, 6, 7, 8],
                         "只归档本次真正进入总结 prompt 的最新 threshold 条，而不是全部积压")


# ==========================================
# B. Web 历史不重放 assistant
# ==========================================

class TestExtractUserSide(unittest.TestCase):
    """gateway._extract_user_side_from_history 各写入格式分支。"""

    def test_web_user_entry(self):
        out = gateway._extract_user_side_from_history("小满：我喜欢无糖咖啡", "小满")
        self.assertEqual(out, "我喜欢无糖咖啡")

    def test_web_ai_entry_excluded(self):
        self.assertIsNone(gateway._extract_user_side_from_history(
            "我(Finn)：当然可以，我会一直记住你的偏好。", "小满"))

    def test_tg_mixed_user_side_only(self):
        out = gateway._extract_user_side_from_history(
            "用户: 我最近在准备考试\n回复: 你一定可以的，加油。", "小满")
        self.assertEqual(out, "我最近在准备考试")
        self.assertNotIn("回复", out or "")

    def test_qq_mixed_user_side_only(self):
        out = gateway._extract_user_side_from_history(
            "测试昵称: 我家猫又拆家了\n回复: 哈哈小猫咪嘛。", "小满")
        self.assertEqual(out, "我家猫又拆家了")
        self.assertNotIn("哈哈小猫咪嘛", out or "")

    def test_mixed_without_reply_marker_skipped(self):
        # TG 兜底格式（AI 未配置）无法确认结构 → 跳过
        self.assertIsNone(gateway._extract_user_side_from_history(
            "用户: 你好\n[未回复：AI 服务未配置]", "小满"))

    def test_unparseable_content_skipped(self):
        self.assertIsNone(gateway._extract_user_side_from_history(
            "一段无法判断角色的自由活动或总结正文。", "小满"))
        self.assertIsNone(gateway._extract_user_side_from_history("", "小满"))
        self.assertIsNone(gateway._extract_user_side_from_history(None, "小满"))

    def test_user_prefix_without_colon_skipped(self):
        # 以用户名开头但没有中文冒号分隔 → 无法安全切分，跳过
        self.assertIsNone(gateway._extract_user_side_from_history("小满今天很开心", "小满"))


class TestInjectContextNoAssistantReplay(unittest.TestCase):
    """Web /v1/chat/completions：数据库兜底历史不再生成 assistant 消息。"""

    def test_db_history_never_becomes_assistant_message(self):
        fake_sb = FakeSupabase(_memories_selector_factory(HISTORY_ROWS))
        req_data = {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "客户端系统提示"},
                {"role": "user", "content": "当前第一条用户消息"},
                {"role": "assistant", "content": "客户端真实历史回复"},
                {"role": "user", "content": "当前第二条用户消息"},
            ],
        }
        env = {
            "AI_NAME": "Finn", "USER_NAME": "小满", "USER_ID": "test-user",
            "AI_PERSONA": "测试人设", "CHAT_TAG": "Web_Chat",
            "INJECT_DB_HISTORY": "always", "INJECT_CORE_SUMMARIES": "never",
            "CALENDAR_INJECT": "false", "WEATHER_KEYWORD_INJECT": "false",
        }
        with patch.dict(os.environ, env), \
             patch.object(gateway, "_get_pinecone_memory", lambda: None), \
             patch.object(gateway, "_get_runtime_config",
                          lambda: {"device_context_enabled": False, "home_context_enabled": False}), \
             patch.object(gateway, "_stable_cached", lambda key, ttl: None), \
             patch.object(gateway, "_stable_set", lambda key, val: None):
            asyncio.run(gateway.HostFixMiddleware._inject_context(
                None, req_data, fake_sb, "测试查询"))

        dumped = json.dumps(req_data["messages"], ensure_ascii=False)
        # 1. 旧 AI 回复（两种写入格式）不得出现
        self.assertNotIn("当然可以，我会一直记住你的偏好", dumped, "Web 格式旧回复不得进入 messages")
        self.assertNotIn("你一定可以的，加油。", dumped, "TG 混合格式旧回复不得进入 messages")
        self.assertNotIn("哈哈小猫咪嘛。", dumped, "QQ 混合格式旧回复不得进入 messages")
        # 2. 数据库历史不得生成 assistant 角色
        db_assistants = [m for m in req_data["messages"]
                         if m.get("role") == "assistant" and m.get("content") != "客户端真实历史回复"]
        self.assertEqual(db_assistants, [], "数据库历史不得生成任何 assistant 消息")
        # 3. 用户侧内容保留
        self.assertIn("我喜欢无糖咖啡", dumped)
        self.assertIn("我最近在准备考试", dumped)
        self.assertIn("我家猫又拆家了", dumped)
        # 4. 客户端真实 assistant 保留
        self.assertIn("客户端真实历史回复", dumped)


# ==========================================
# C. TG/QQ 历史只注入用户侧
# ==========================================

class TestChannelContextUserSideOnly(unittest.TestCase):
    """server._build_channel_context：只注入用户侧历史文本。"""

    def test_channel_context_excludes_ai_reply(self):
        fake_sb = FakeSupabase(_memories_selector_factory(HISTORY_ROWS))
        env = {"USER_NAME": "小满", "AI_NAME": "Finn", "CALENDAR_INJECT": "false",
               "DEVICE_CONTEXT_ENABLED": "false"}
        with patch.dict(os.environ, env), \
             patch.object(server, "supabase", fake_sb), \
             patch.object(server, "pinecone_memory", None), \
             patch.object(server, "weather_tools", None), \
             patch.object(server, "_get_current_persona", lambda: "测试人设"), \
             patch.object(gateway, "_home_context_enabled", lambda: False):
            out = asyncio.run(server._build_channel_context(
                "测试查询", channel_tag="TG_MSG", source="tg_user"))

        self.assertIn("【近期用户表达", out, "历史块应使用用户表达标题")
        self.assertIn("用户曾提到：我最近在准备考试", out, "用户侧内容应保留")
        self.assertIn("用户曾提到：我家猫又拆家了", out, "QQ 用户侧内容应保留")
        self.assertNotIn("你一定可以的，加油。", out, "旧 AI 回复不得注入")
        self.assertNotIn("哈哈小猫咪嘛。", out, "旧 AI 回复不得注入")
        self.assertNotIn("当然可以，我会一直记住你的偏好", out, "Web 格式旧回复不得注入")
        self.assertNotIn("我(Finn)：", out, "不得把旧 AI 回复改造成历史角色")


# ==========================================
# D. 自动清理保护
# ==========================================

class TestCleanOldNoDelete(unittest.TestCase):
    """_clean_old_memories 不访问数据库、不执行删除。"""

    def test_clean_old_does_not_touch_database(self):
        class RecordingFake:
            def __init__(self):
                self.table_calls = []
                self.deleted_ops = []
            def table(self, name):
                self.table_calls.append(name)
                chain = self
                class _Chain:
                    def __getattr__(self, item):
                        def _m(*a, **k):
                            if item == "delete":
                                chain.deleted_ops.append(a)
                            return _m
                        return _m
                return _Chain()
        fake = RecordingFake()
        heartbeat._clean_old_memories(fake)
        self.assertEqual(fake.table_calls, [], "不得访问任何表")
        self.assertEqual(fake.deleted_ops, [], "不得执行 delete")

    def test_perform_deep_dreaming_has_no_inline_delete(self):
        # 源码约束：原「delete().lt("importance"」批量删除模式不得回归
        with open(os.path.join(os.path.dirname(__file__), "heartbeat.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('delete().lt("importance"', src)
        self.assertIn("_clean_old_memories", src)
        self.assertIn("_clean_old_memories, supabase", src)


# ==========================================
# 源码约束（防止同类逻辑回归）
# ==========================================

class TestSourceConstraints(unittest.TestCase):
    def test_napcat_archive_update_only_in_success_path(self):
        with open(os.path.join(os.path.dirname(__file__), "napcat.py"),
                  encoding="utf-8") as f:
            src = f.read()
        # 原实现有 2 处 Archived_Chat 归档 UPDATE（成功 + 失败兜底），修复后只允许成功路径 1 处
        self.assertEqual(src.count('"tags": "Archived_Chat"'), 1)
        self.assertNotIn("虽然总结失败", src, "失败归档的旧日志语不得残留")

    def test_gateway_no_db_assistant_replay(self):
        with open(os.path.join(os.path.dirname(__file__), "gateway.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('history_msgs.append({"role": "assistant"',
                         src, "数据库历史不得再生成 assistant 消息")

    def test_server_uses_user_side_history(self):
        with open(os.path.join(os.path.dirname(__file__), "server.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_extract_user_side_from_history", src)
        self.assertNotIn("【近期对话回顾】", src, "旧的历史原文注入标题不得残留")


if __name__ == "__main__":
    unittest.main()
