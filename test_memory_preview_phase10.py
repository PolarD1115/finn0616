# -*- coding: utf-8 -*-
"""第 10 阶段专项测试 —— 手动事实提取预览接口（只读、零写入）与 <final> 内部标记处理。

全部 unittest + mock + 合成数据；不连接真实 Supabase / LLM / Pinecone。

覆盖（任务 A-H）：
  A 鉴权与路由（POST-only、confirm、limit、非法请求零查询零调用）
  B 事件选择（成组、不拆散、单用户、非法后缀跳过、无 ID 泄露）
  C 敏感内容整批跳过
  D compression 单次调用与失败映射
  E <final> 清理（extractor 层 6 项）
  F 零写入（fake 只记录 select；write_guards 全 false）
  G 响应脱敏
  H 源码约束

运行：  python -m unittest test_memory_preview_phase10 -v
"""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import gateway
import memory_extractor as mx
import memory_preview as mp
from test_memory_phase1_fixes import FakeResult, FakeQuery, FakeSupabase


# ==========================================
# 合成事件（marker 值用于脱敏断言）
# ==========================================

def _pev(req, role, content="合成测试内容", occurred="2026-08-28T17:00:00+00:00"):
    return {"id": f"EVENTID_{req}_{role}", "user_id": "USERID_MARKER",
            "session_id": None, "channel": "web", "role": role, "content": content,
            "occurred_at": occurred, "created_at": occurred,
            "source_event_id": f"SRCID_{req}:{role}",
            "processing_status": "pending", "metadata": {"request_id": f"REQID_{req}"}}


# ==========================================
# ASGI fakes（gateway handler 测试）
# ==========================================

class FakeReceive:
    def __init__(self, body=b""):
        self._body = body
        self._sent = False

    async def __call__(self):
        if not self._sent:
            self._sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        return {"type": "http.disconnect"}


class FakeSend:
    def __init__(self):
        self.msgs = []

    async def __call__(self, msg):
        self.msgs.append(msg)

    @property
    def status(self):
        return self.msgs[0]["status"] if self.msgs else None

    @property
    def body_json(self):
        for m in self.msgs:
            if m.get("type") == "http.response.body":
                return json.loads(m.get("body", b"{}").decode("utf-8"))
        return {}


class RecordingFakeService(FakeSupabase):
    """记录全部方法调用的假 service_role 客户端（零写入断言用）。"""

    def __init__(self, rows):
        super().__init__(selector=lambda table, path: FakeResult(list(rows)))
        self.ops = []

    def table(self, name):
        self.ops.append(f"table:{name}")
        return _RecordingQuery(self, name)


class _RecordingQuery(FakeQuery):
    def _rec(self, method, *args, **kwargs):
        self._owner.ops.append(method)
        return self

    def select(self, *a, **k): return self._rec("select", *a, **k)
    def eq(self, *a, **k): return self._rec("eq", *a, **k)
    def in_(self, *a, **k): return self._rec("in_", *a, **k)
    def order(self, *a, **k): return self._rec("order", *a, **k)
    def limit(self, *a, **k): return self._rec("limit", *a, **k)

    def insert(self, *a, **k):
        self._owner.ops.append("INSERT!")
        return self

    def update(self, *a, **k):
        self._owner.ops.append("UPDATE!")
        return self

    def delete(self, *a, **k):
        self._owner.ops.append("DELETE!")
        return self

    def upsert(self, *a, **k):
        self._owner.ops.append("UPSERT!")
        return self

    def rpc(self, *a, **k):
        self._owner.ops.append("RPC!")
        return self


def _pair_rows(req, user_content="合成用户消息", asst_content="合成助手回复"):
    return [_pev(req, "user", user_content), _pev(req, "assistant", asst_content)]


def _rows_newest_first(*groups):
    """把若干请求组按新到旧交错排列（模拟 created_at DESC 查询结果）。"""
    rows = []
    for i, g in enumerate(groups):
        rows.append(_pev(g[0], "assistant", g[1],
                         occurred=f"2026-08-28T17:0{i}:10+00:00"))
        rows.append(_pev(g[0], "user", g[2],
                         occurred=f"2026-08-28T17:0{i}:00+00:00"))
    return rows


def _run_preview(rows, llm_fn):
    """运行 run_preview：返回 (result, fake_service, llm_arg_lengths, log_text)。"""
    fake_service = RecordingFakeService(rows)
    calls = []

    def _llm(prompt):
        calls.append(len(prompt))
        if isinstance(llm_fn, Exception):
            raise llm_fn
        if callable(llm_fn):
            return llm_fn(prompt)
        if isinstance(llm_fn, str):
            return llm_fn
        return json.dumps(llm_fn, ensure_ascii=False)

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = asyncio.run(mp.run_preview(fake_service, limit=10,
                                            ai_name="Finn", user_name="小满",
                                            llm_call=_llm))
    return result, fake_service, calls, buf.getvalue()


def _mk_llm(*_args):
    """占位 llm_call 工厂（用于不需要真实调用断言的场景）。"""
    def _call(prompt):
        return '{"memories":[]}'
    return _call


async def _call_handler(scope, receive, send):
    await gateway.HostFixMiddleware._handle_memory_extraction_preview(
        None, scope, receive, send)


# ==========================================
# E. <final> 清理（extractor 层）
# ==========================================

class TestFinalMarkupCleanup(unittest.TestCase):

    def _prompt_for(self, events):
        return mx.build_memory_extraction_prompt(events, ai_name="Finn", user_name="小满")

    def test_e1_assistant_full_wrapper_stripped(self):
        ev = _pev("g1", "assistant", "<final>\n\n合成助手回复正文\n\n</final>")
        prompt = self._prompt_for([_pev("g1", "user", "合成用户消息"), ev])
        self.assertNotIn("<final>", prompt)
        self.assertIn("合成助手回复正文", prompt)

    def test_e2_case_insensitive(self):
        ev = _pev("g1", "assistant", "<FINAL>正文一</FINAL>")
        prompt = self._prompt_for([ev])
        self.assertNotIn("<FINAL>", prompt)
        self.assertNotIn("<final>", prompt)
        self.assertIn("正文一", prompt)

    def test_e3_loose_tags_removed(self):
        ev = _pev("g1", "assistant", "前缀<final>中间</final>后缀")
        prompt = self._prompt_for([ev])
        self.assertNotIn("<final>", prompt)
        self.assertNotIn("</final>", prompt)
        self.assertIn("前缀中间后缀", prompt)

    def test_e4_user_content_not_rewritten(self):
        ev = _pev("g1", "user", "用户消息里提到 <final> 这个词")
        prompt = self._prompt_for([ev])
        self.assertIn("用户消息里提到 <final> 这个词", prompt, "user 事件内容不得被改写")

    def test_e5_candidate_with_residual_tag_rejected(self):
        events = [_pev("g1", "user", "合成用户消息")]
        cand = {"memory_type": "long_term", "content": "事实<final>残留</final>",
                "importance": 4, "confidence": 0.9, "source_event_indexes": [0]}
        item, reason = mx.validate_and_normalize_candidate(
            cand, events, "u", "b", None)
        self.assertIsNone(item)
        self.assertEqual(reason, "INTERNAL_MARKUP")

    def test_e6_input_events_not_mutated(self):
        ev = _pev("g1", "assistant", "<final>正文</final>")
        original = ev["content"]
        self._prompt_for([ev])
        mx._strip_internal_markup(ev["content"])
        self.assertEqual(ev["content"], original, "输入事件对象不得被修改")


# ==========================================
# B. 事件选择（纯函数）
# ==========================================

class TestSelectPreviewEvents(unittest.TestCase):

    def test_b_selects_complete_groups(self):
        rows = _rows_newest_first(("g1", "回复一", "用户消息一"),
                                  ("g2", "回复二", "用户消息二"))
        selected, stats = mp.select_preview_events(rows, 10)
        self.assertEqual(len(selected), 4)
        self.assertEqual(stats["groups_selected"], 2)
        self.assertEqual(stats["user_events"], 2)
        self.assertEqual(stats["assistant_events"], 2)

    def test_b_does_not_split_groups_over_limit(self):
        rows = _rows_newest_first(("g1", "回复一", "用户消息一"),
                                  ("g2", "回复二", "用户消息二"),
                                  ("g3", "回复三", "用户消息三"))
        selected, stats = mp.select_preview_events(rows, 4)
        self.assertEqual(len(selected), 4, "limit=4 时只取两个完整组，不拆散第三组")
        self.assertEqual(stats["incomplete_group_skipped"], 2)

    def test_b_skips_incomplete_groups(self):
        rows = [_pev("half", "assistant", "半轮回复")]
        rows += _rows_newest_first(("g1", "回复一", "用户消息一"))
        selected, stats = mp.select_preview_events(rows, 10)
        self.assertEqual(len(selected), 2)
        self.assertEqual(stats["incomplete_group_skipped"], 1)

    def test_b_skips_illegal_suffix_and_other_users(self):
        bad = dict(_pev("x", "user", "无后缀"), source_event_id="SRCID_x")
        other = dict(_pev("y", "user", "他人消息"), user_id="OTHER_USER")
        rows = [bad, other] + _rows_newest_first(("g1", "回复一", "用户消息一"))
        selected, stats = mp.select_preview_events(rows, 10)
        self.assertEqual(len(selected), 2)
        self.assertEqual(stats["illegal_suffix_skipped"], 1)
        self.assertEqual(stats["other_user_skipped"], 1)
        self.assertTrue(all(e["user_id"] == "USERID_MARKER" for e in selected))

    def test_b_no_complete_groups(self):
        rows = [_pev("half", "assistant", "半轮回复")]
        selected, stats = mp.select_preview_events(rows, 10)
        self.assertEqual(selected, [])


# ==========================================
# A. 鉴权与路由（gateway handler）
# ==========================================

class TestPreviewEndpoint(unittest.TestCase):

    def _run_handler(self, method="POST", body=b'{"confirm":"PREVIEW_ONLY","limit":10}',
                     service=None):
        scope = {"method": method, "path": "/api/memory-extraction-preview"}
        send = FakeSend()
        with patch.object(gateway, "_log", lambda m: None):
            asyncio.run(_call_handler(scope, FakeReceive(body), send))
        return send

    def test_a_non_post_returns_405_without_db_or_llm(self):
        called = []
        with patch.object(memory_preview_module(), "run_preview",
                          side_effect=lambda *a, **k: called.append(1)):
            send = self._run_handler(method="GET")
        self.assertEqual(send.status, 405)
        self.assertEqual(called, [], "非 POST 不得触发查询或模型调用")

    def test_a_confirm_missing_or_wrong_returns_400(self):
        for body in [b"{}", b'{"confirm":"YES"}']:
            with self.subTest(body=body):
                send = self._run_handler(body=body)
                self.assertEqual(send.status, 400)

    def test_a_limit_validation(self):
        for lim in ['"10"', "true", "1", "11", "0"]:
            with self.subTest(limit=lim):
                body = json.dumps({"confirm": "PREVIEW_ONLY", "limit": json.loads(lim)}).encode()
                send = self._run_handler(body=body)
                self.assertEqual(send.status, 400, msg=lim)

    def test_a_invalid_json_returns_400(self):
        send = self._run_handler(body=b"not json")
        self.assertEqual(send.status, 400)

    def test_a_valid_request_calls_preview_and_returns_200(self):
        fake_service = RecordingFakeService(_rows_newest_first(("g1", "回复一", "用户消息一")))
        import memory_preview
        with patch.object(memory_preview, "run_preview",
                          return_value={"ok": True, "code": "PREVIEW_READY",
                                        "candidates": [], "stats": {},
                                        "write_guards": mp._write_guards()}):
            send = self._run_handler(service=fake_service)
        self.assertEqual(send.status, 200)
        self.assertEqual(send.body_json.get("code"), "PREVIEW_READY")


def memory_preview_module():
    import memory_preview
    return memory_preview


# ==========================================
# B/C/D/F/G. run_preview 集成
# ==========================================

class TestPreviewFlow(unittest.TestCase):

    def test_b_no_pending_events(self):
        result, svc, calls, _ = _run_preview([], '{"memories":[]}')
        self.assertEqual(result["code"], "NO_PENDING_EVENTS")
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [], "无事件不得调用 LLM")

    def test_b_no_complete_groups(self):
        result, svc, calls, _ = _run_preview([_pev("half", "assistant", "半轮")],
                                             '{"memories":[]}')
        self.assertEqual(result["code"], "NO_COMPLETE_EVENT_GROUPS")
        self.assertEqual(calls, [])

    def test_b_response_has_no_raw_ids(self):
        rows = _rows_newest_first(("g1", "回复一", "用户消息一"))
        result, _, _, _ = _run_preview(rows, '{"memories":[]}')
        # 第 17 阶段起 PREVIEW_READY 附带随机 preview_token——随机串理论上可能
        # 撞上短 marker，脱敏断言先将其排除（token 自身的形态约束见第 17 阶段测试）
        dumped = json.dumps({k: v for k, v in result.items() if k != "preview_token"},
                            ensure_ascii=False)
        for marker in ("EVENTID_", "SRCID_", "USERID_MARKER", "REQID_", "g1"):
            self.assertNotIn(marker, dumped, msg=marker)

    def test_c_sensitive_batch_skipped(self):
        rows = _rows_newest_first(
            ("g1", "回复一", "我的密码是 abc123 请帮我记住"),
            ("g2", "回复二", "用户消息二"))
        calls = []

        def _llm(prompt):
            calls.append(1)
            return '{"memories":[]}'

        fake_service = RecordingFakeService(rows)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = asyncio.run(mp.run_preview(fake_service, limit=10,
                                                ai_name="Finn", user_name="小满",
                                                llm_call=_llm))
        self.assertEqual(result["code"], "SENSITIVE_BATCH_SKIPPED")
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [], "敏感批次不得调用 LLM")
        self.assertNotIn("abc123", json.dumps(result), "响应不得包含敏感正文")
        self.assertNotIn("abc123", buf.getvalue(), "日志不得包含敏感正文")

    def test_d_llm_error_direct(self):
        rows = _rows_newest_first(("g1", "回复一", "用户消息一"))
        result, _, _, log = _run_preview(rows, RuntimeError("mock llm down"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "LLM_ERROR")
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["write_guards"]["memory_items_written"])

    def test_d_empty_response_mapped(self):
        result, _, _, _ = _run_preview(
            _rows_newest_first(("g1", "回复一", "用户消息一")), "")
        self.assertEqual(result["code"], "EMPTY_RESPONSE")

    def test_d_json_parse_error_mapped(self):
        result, _, _, _ = _run_preview(
            _rows_newest_first(("g1", "回复一", "用户消息一")),
            "```json\n{}\n```")
        self.assertEqual(result["code"], "JSON_PARSE_ERROR")

    def test_d_all_rejected_mapped(self):
        rows = _rows_newest_first(("g1", "回复一", "用户消息一"))
        bad = {"memory_type": "raw_event", "content": "x", "source_event_indexes": [0]}
        result, _, _, _ = _run_preview(rows, {"memories": [bad]})
        self.assertEqual(result["code"], "ALL_CANDIDATES_REJECTED")
        self.assertEqual(result["status_plan"]["executed"], False)

    def test_d_success_preview_structure(self):
        rows = _rows_newest_first(("g1", "回复一", "用户最近开始学习 TypeScript。"))
        good = {"memory_type": "long_term", "content": "用户计划主要使用 TypeScript。",
                "subject_key": "primary_programming_language", "importance": 4,
                "confidence": 0.92, "valid_at": "2026-08-28T17:00:00+00:00",
                "invalid_at": None, "expires_at": None, "source_event_indexes": [1],
                "reason": "用户明确表达"}
        result, _, _, _ = _run_preview(rows, {"memories": [good]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "PREVIEW_READY")
        self.assertEqual(len(result["candidates"]), 1)
        cand = result["candidates"][0]
        self.assertEqual(cand["preview_index"], 1)
        self.assertEqual(cand["status"], "pending_review")
        self.assertEqual(cand["quality_hint"], "NEEDS_HUMAN_REVIEW")
        self.assertEqual(result["status_plan"]["simulated_status"], "processed")
        self.assertEqual(result["status_plan"]["executed"], False)
        self.assertEqual(result["stats"]["selected_events"], 2)
        wg = result["write_guards"]
        self.assertFalse(wg["memory_items_written"])
        self.assertFalse(wg["memory_events_updated"])
        self.assertFalse(wg["pinecone_touched"])


# ==========================================
# F. 零写入（fake 记录断言）
# ==========================================

class TestZeroWrite(unittest.TestCase):

    def test_f_only_select_operations(self):
        rows = _rows_newest_first(("g1", "回复一", "用户消息一"))
        fake_service = RecordingFakeService(rows)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            asyncio.run(mp.run_preview(fake_service, limit=10,
                                       ai_name="Finn", user_name="小满",
                                       llm_call=lambda p: '{"memories":[]}'))
        for op in fake_service.ops:
            self.assertNotIn("INSERT!", op)
            self.assertNotIn("UPDATE!", op)
            self.assertNotIn("DELETE!", op)
            self.assertNotIn("UPSERT!", op)
            self.assertNotIn("RPC!", op)
        self.assertIn("select", fake_service.ops)
        # 全部操作仅限 select 链
        allowed = {"select", "eq", "in_", "order", "limit", "execute"}
        for op in fake_service.ops:
            if op.startswith("table:"):
                continue
            self.assertIn(op, allowed, msg=op)

    def test_f_no_pinecone_import(self):
        with open(os.path.join(os.path.dirname(__file__), "memory_preview.py"),
                  encoding="utf-8") as f:
            src = f.read()
        # write_guards 的 pinecone_touched 是零写入声明字段（允许）；
        # 禁止的是实际导入或调用 Pinecone。第 17 阶段起 commit 执行器区段允许
        # memory_items 插入与 memory_events 条件更新（见下一条约束）；
        # 删除 / UPSERT / 存储过程 / 自动调度 / 环境变量仍全面禁止。
        for token in ("import pinecone", "pinecone_memory", "Pinecone(",
                      ".delete(", ".upsert(", ".rpc(",
                      "create_task", "Timer", "heartbeat", "os.environ"):
            self.assertNotIn(token, src, msg=f"memory_preview.py 不得包含: {token}")

    def test_f_write_ops_only_in_commit_section(self):
        """第 17 阶段约束：写入调用只允许出现在 commit 执行器区段；
        预览区段（run_preview 及 token 缓存）保持零写入。"""
        with open(os.path.join(os.path.dirname(__file__), "memory_preview.py"),
                  encoding="utf-8") as f:
            src = f.read()
        marker = "第 17 阶段：人工确认写入执行器"
        self.assertIn(marker, src, "缺少 commit 区段标记")
        preview_part, commit_part = src.split(marker, 1)
        for token in (".insert(", ".update("):
            self.assertNotIn(token, preview_part,
                             msg=f"预览区段不得包含 {token}（仅 commit 区段允许）")
        self.assertIn(".insert(", commit_part, "commit 区段应有 memory_items 插入")
        self.assertIn(".update(", commit_part, "commit 区段应有 memory_events 条件更新")


# ==========================================
# G. 响应脱敏
# ==========================================

class TestResponseSanitization(unittest.TestCase):

    def test_g_success_response_has_no_forbidden_fields(self):
        rows = _rows_newest_first(("g1", "回复一", "用户消息一"))
        good = {"memory_type": "long_term", "content": "事实化候选。",
                "subject_key": None, "importance": 4, "confidence": 0.9,
                "valid_at": None, "invalid_at": None, "expires_at": None,
                "source_event_indexes": [1], "reason": "r"}
        result, _, _, _ = _run_preview(rows, {"memories": [good]})
        # 排除随机 preview_token 后再做脱敏断言（避免随机串撞短 marker 的偶发误报）
        dumped = json.dumps({k: v for k, v in result.items() if k != "preview_token"},
                            ensure_ascii=False)
        for forbidden in ("EVENTID_", "SRCID_", "USERID_MARKER", "REQID_",
                          "content_hash", "batch_id", "metadata",
                          "source_event_id", "user_id", '"reason":'):
            self.assertNotIn(forbidden, dumped, msg=forbidden)

    def test_g_error_response_minimal(self):
        result, _, _, _ = _run_preview([], '{"memories":[]}')
        self.assertEqual(set(result.keys()) - {"ok", "code", "stats", "candidates",
                                               "rejected", "status_plan",
                                               "write_guards"}, set())


# ==========================================
# H. 源码约束（gateway 侧）
# ==========================================

class TestGatewaySourceConstraints(unittest.TestCase):

    def test_h_route_registered_under_api_scope(self):
        with open(os.path.join(os.path.dirname(__file__), "gateway.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn('/api/memory-extraction-preview', src)
        self.assertIn('_handle_memory_extraction_preview', src)

    def test_h_no_env_vars_no_commit_logic(self):
        with open(os.path.join(os.path.dirname(__file__), "memory_preview.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("os.environ", src)

    def test_h_extractor_still_not_referenced_by_gateway_autoruns(self):
        with open(os.path.join(os.path.dirname(__file__), "gateway.py"),
                  encoding="utf-8") as f:
            src = f.read()
        # gateway 仅在预览 handler 内引用 memory_preview，不得引用 memory_extractor
        self.assertNotIn("memory_extractor", src)


if __name__ == "__main__":
    unittest.main()
