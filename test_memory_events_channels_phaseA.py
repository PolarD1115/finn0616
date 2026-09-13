# -*- coding: utf-8 -*-
"""第 A 阶段专项测试 —— QQ / Telegram / 后台自主活动三渠道 memory_events 双写接入。

全部 unittest + mock + 脱敏假数据（SYNTHETIC_MEMORY_PHASEA_*），
不连接 Supabase / Pinecone / 上游 LLM。
对齐基准：gateway._save_conversation 第 3 阶段 Web 双写（见 test_memory_phase3_events.py）。

覆盖：
  A1. QQ（napcat._handle_merged 主成功路径）：
      字段约定 / user+assistant 成对一次批量 insert / service 不可用跳过 /
      insert 失败不影响主流程 / 门控关闭不写入 / 兜底回复路径不写账本 / 日志脱敏
  A2. TG（heartbeat async_telegram_polling._handle_merged 主成功路径）：同 A1 五项
  A3. 后台自主活动（async_autonomous_life 主动问候 + async_free_activity 自由活动）：
      单条 event 事件字段约定 / 不受 chat_history_write_enabled 门控 /
      秘密日记不落账本（与 memories 隐私语义一致）/ service 不可用与 insert
      失败均不影响后台循环

运行：  python -m unittest test_memory_events_channels_phaseA -v
"""

import asyncio
import contextlib
import datetime
import hashlib
import json
import os
import time
import types
import unittest
import uuid as uuid_mod
from unittest.mock import patch

import aggregator
import desire_bridge
import gateway
import heartbeat
import napcat
import server
import tool_loop
import home.activity_log as activity_log_mod
from test_memory_phase1_fixes import FakeSupabase, FakeQuery, FakeResult


# ==========================================
# 脱敏合成数据（任务指定标识）
# ==========================================

SYNTHETIC_QQ_TEXT = "SYNTHETIC_MEMORY_PHASEA_QQ_USER_TEXT"
SYNTHETIC_QQ_REPLY = "SYNTHETIC_MEMORY_PHASEA_QQ_ASSISTANT_REPLY"
SYNTHETIC_TG_TEXT = "SYNTHETIC_MEMORY_PHASEA_TG_USER_TEXT"
SYNTHETIC_TG_REPLY = "SYNTHETIC_MEMORY_PHASEA_TG_ASSISTANT_REPLY"
SYNTHETIC_GREETING = "SYNTHETIC_MEMORY_PHASEA_GREETING_MSG"
SYNTHETIC_FREE_LOG = "SYNTHETIC_MEMORY_PHASEA_FREE_LOG"
TEST_USER_ID = "test-user"


# ==========================================
# 通用假件（记录型，绝不触网）
# ==========================================

class _FailingQuery(FakeQuery):
    """insert 后在 execute 时抛出注入异常（记录 insert 调用再失败，贴近真实路径）。"""

    def __init__(self, owner, table, exc):
        super().__init__(owner, table)
        self._exc = exc

    def execute(self, *a, **k):
        self._path.append(("execute", ()))
        self._owner.calls.append((self._table, tuple(self._path)))
        raise self._exc


class RecordingService(FakeSupabase):
    """模拟 server.supabase_service（service_role 客户端），可注入 insert 异常。"""

    def __init__(self, insert_exc=None):
        super().__init__()
        self._insert_exc = insert_exc

    def table(self, name):
        if self._insert_exc is not None and name == "memory_events":
            return _FailingQuery(self, name, self._insert_exc)
        return FakeQuery(self, name)


class FakePinecone:
    """模拟 pinecone_memory：只记录 add 调用。"""

    def __init__(self):
        self.index = object()  # 非空即视为"已配置"
        self.add_calls = []

    def add(self, messages, user_id=None, metadata=None):
        self.add_calls.append({"messages": messages, "user_id": user_id,
                               "metadata": metadata})
        return True


class FakeResponse:
    """模拟 requests 的 Response（仅 .json / raise_for_status）。"""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


# ==========================================
# 后台循环测试辅助：快速休眠 + 轮询等待
# ==========================================

_POLL_INTERVAL = 0.02
_REAL_SLEEP = asyncio.sleep


async def _fast_sleep(delay=None, *args, **kwargs):
    """把任意时长 sleep 压到轮询间隔，让 while True 后台循环在测试里快速空转。"""
    try:
        d = float(delay)
    except (TypeError, ValueError):
        d = 0.0
    await _REAL_SLEEP(min(max(d, 0.0), _POLL_INTERVAL))


@contextlib.contextmanager
def _patched_fast_sleep():
    with patch.object(asyncio, "sleep", _fast_sleep):
        yield


async def _run_until(pred, timeout=10.0):
    """轮询等待条件成立；超时返回最后一次判定结果。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(_POLL_INTERVAL)
    return bool(pred())


async def _cancel_quietly(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ==========================================
# 断言辅助
# ==========================================

def _insert_payloads_on(fake, table):
    """返回对某表的全部 insert 调用 payload（每个元素是一次 insert 的入参）。"""
    out = []
    for tbl, path in fake.calls:
        if tbl == table and path and path[0][0] == "insert":
            out.append(path[0][1][0])
    return out


def _event_rows(fake_service):
    """扁平化 memory_events 全部写入行。"""
    rows = []
    for payload in _insert_payloads_on(fake_service, "memory_events"):
        if isinstance(payload, list):
            rows.extend(payload)
        else:
            rows.append(payload)
    return rows


def _by_role(rows, role):
    return [r for r in rows if r.get("role") == role]


def _assert_common_fields(test, row, *, channel, created_by):
    """校验三渠道共有字段约定（对齐 Web 第 3 阶段基准）。"""
    test.assertEqual(row["channel"], channel)
    test.assertEqual(row["created_by"], created_by)
    test.assertEqual(row["processing_status"], "pending")
    test.assertEqual(row["attempt_count"], 0)
    test.assertIsNone(row["session_id"], "无可靠会话标识，诚实写空")
    test.assertEqual(row["user_id"], TEST_USER_ID)
    test.assertEqual(set(row["metadata"].keys()), {"request_id"},
                     "metadata 只含 request_id，不含正文/密钥")
    dt = datetime.datetime.fromisoformat(row["occurred_at"])
    test.assertIsNotNone(dt.utcoffset(), "occurred_at 必须是带时区的 ISO 字符串")


def _assert_pair_fields(test, fake_service, *, channel, created_by,
                        user_text, asst_text):
    """校验 user + assistant 成对事件（内容/哈希/请求关联/批量 insert）。"""
    rows = _event_rows(fake_service)
    test.assertEqual(len(rows), 2, "user + assistant 各一条事件")
    user_rows = _by_role(rows, "user")
    asst_rows = _by_role(rows, "assistant")
    test.assertEqual(len(user_rows), 1)
    test.assertEqual(len(asst_rows), 1)
    u, a = user_rows[0], asst_rows[0]
    for r in (u, a):
        _assert_common_fields(test, r, channel=channel, created_by=created_by)
    test.assertEqual(u["content"], user_text)
    test.assertEqual(a["content"], asst_text)
    test.assertEqual(u["content_hash"],
                     hashlib.sha256(user_text.encode("utf-8")).hexdigest())
    test.assertEqual(a["content_hash"],
                     hashlib.sha256(asst_text.encode("utf-8")).hexdigest())
    uid_part = u["source_event_id"].rpartition(":")[0]
    aid_part = a["source_event_id"].rpartition(":")[0]
    test.assertTrue(uid_part and uid_part == aid_part,
                    "同一请求的 user/assistant 事件 source_event_id 前缀一致")
    test.assertTrue(u["source_event_id"].endswith(":user"))
    test.assertTrue(a["source_event_id"].endswith(":assistant"))
    uuid_mod.UUID(uid_part)  # request_id 是合法 UUID（服务端生成）
    test.assertEqual(u["metadata"]["request_id"], uid_part)
    test.assertEqual(a["metadata"]["request_id"], aid_part)
    test.assertEqual(len(_insert_payloads_on(fake_service, "memory_events")), 1,
                     "user + assistant 两条事件一次批量 insert（无半轮事件）")
    return u, a


def _assert_event_fields(test, row, *, content):
    """校验后台单条 event 事件字段约定。"""
    _assert_common_fields(test, row, channel="background", created_by="heartbeat")
    test.assertEqual(row["role"], "event")
    test.assertEqual(row["content"], content)
    test.assertEqual(row["content_hash"],
                     hashlib.sha256(content.encode("utf-8")).hexdigest())
    rid = row["source_event_id"].rpartition(":")[0]
    test.assertTrue(row["source_event_id"].endswith(":event"))
    uuid_mod.UUID(rid)
    test.assertEqual(row["metadata"]["request_id"], rid)


def _assert_single_event_rows(fake_service, content):
    """后台 while True 循环在测试里会连续跑多轮；断言"每轮一次单行 insert +
    行字段正确"这一不变量（对轮数不敏感，无竞态）。"""
    payloads = _insert_payloads_on(fake_service, "memory_events")
    if not payloads:
        raise AssertionError("后台活动循环未产生 memory_events 写入")
    for p in payloads:
        if len(p) != 1:
            raise AssertionError(
                f"后台活动每次只写一条 event 事件（非成对），实际 {len(p)} 条")
    rows = _event_rows(fake_service)
    _assert_event_fields(unittest.TestCase(), rows[0], content=content)
    return rows


# ==========================================
# A1. QQ 渠道
# ==========================================

class TestQQChannelEvents(unittest.TestCase):
    """napcat._handle_merged 主成功路径的 memory_events 双写。"""

    def _run_qq(self, fake_service, *, write_enabled=True, ask_reply=SYNTHETIC_QQ_REPLY):
        """在 mock 环境下执行 QQ _handle_merged，返回 (mem_saves, sent, summaries, napcat_logs)。"""
        mem_saves, sent, summaries = [], [], []
        log_start = len(napcat._napcat_logs)

        async def _fake_send(target_id, message, is_group=False):
            sent.append({"target": target_id, "message": message, "is_group": is_group})

        async def _fake_summarize():
            summaries.append(1)

        async def _fake_ctx(text, **kwargs):
            return ""

        async def _fake_ask(client, prompt, system_prompt="", temperature=0.7):
            return ask_reply

        def _fake_save(title, content, category="流水", mood="平静", tags=""):
            mem_saves.append({"title": title, "content": content, "tags": tags})
            return True

        dep = types.SimpleNamespace(
            supabase_service=fake_service,
            _get_llm_client=lambda role: object(),
            _build_channel_context=_fake_ctx,
            _ask_llm_async=_fake_ask,
            _save_memory_to_db=_fake_save,
            _resolve_pinecone_user_id=lambda: TEST_USER_ID,
        )

        async def main():
            captured = {}

            def _fake_get_aggregator(name, handler):
                captured["handler"] = handler
                return object()

            with patch.object(napcat, "_get_deps", lambda: dep), \
                 patch.object(napcat, "send_qq_message", _fake_send), \
                 patch.object(napcat, "check_and_summarize_all", _fake_summarize), \
                 patch.object(gateway, "_chat_write_enabled", lambda: write_enabled), \
                 patch.object(gateway, "_emotion_enabled", lambda: False), \
                 patch.object(gateway, "_device_context_enabled", lambda: False), \
                 patch.object(napcat, "_qq_aggregator", None), \
                 patch.object(aggregator, "get_aggregator", _fake_get_aggregator):
                napcat._get_qq_aggregator(None)  # 仅构建并捕获 _handle_merged
                handler = captured.get("handler")
                self.assertTrue(callable(handler), "未能捕获 QQ _handle_merged")
                items = [("__k", {"message_type": "private", "target_id": 10001,
                                  "sender_nick": "SYNTHETIC_NICK"})]
                await handler("sess_10001", SYNTHETIC_QQ_TEXT, items)
                # 给 create_task(check_and_summarize_all) 一个执行机会
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        asyncio.run(main())
        return mem_saves, sent, summaries, napcat._napcat_logs[log_start:]

    def test_user_and_assistant_events_written(self):
        fake_service = RecordingService()
        mem_saves, sent, summaries, _ = self._run_qq(fake_service)

        _assert_pair_fields(self, fake_service, channel="qq", created_by="napcat",
                            user_text=SYNTHETIC_QQ_TEXT, asst_text=SYNTHETIC_QQ_REPLY)
        # 旧逻辑回归：memories 流水与总结触发不受影响
        self.assertEqual(len(mem_saves), 1)
        self.assertEqual(mem_saves[0]["tags"], "QQ_MSG")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["message"], SYNTHETIC_QQ_REPLY)
        self.assertEqual(len(summaries), 1, "总结触发不受影响")

    def test_service_missing_skips_without_error(self):
        mem_saves, sent, summaries, logs = self._run_qq(None)

        self.assertEqual(len(sent), 1, "正常回复不受影响")
        self.assertEqual(len(mem_saves), 1, "memories 写入不受影响")
        self.assertEqual(len(summaries), 1, "总结触发不受影响")
        self.assertTrue(any("service_role 客户端不可用" in m for m in logs),
                        "service 缺失应有降级日志")

    def test_insert_failure_does_not_break_main_flow(self):
        fake_service = RecordingService(insert_exc=RuntimeError("mock insert failure"))
        mem_saves, sent, summaries, _ = self._run_qq(fake_service)

        self.assertEqual(len(sent), 1, "回复已正常发送")
        self.assertEqual(len(mem_saves), 1, "memories 写入不受影响")
        self.assertEqual(len(summaries), 1, "总结触发不受影响")
        ops = {path[0][0] for tbl, path in fake_service.calls if tbl == "memory_events"}
        self.assertTrue(ops.issubset({"insert"}), f"memory_events 只允许 insert，实际: {ops}")

    def test_gate_disabled_skips_events(self):
        fake_service = RecordingService()
        mem_saves, sent, _, _ = self._run_qq(fake_service, write_enabled=False)

        self.assertEqual(_event_rows(fake_service), [],
                         "chat_history_write_enabled=false 时事件不落库")
        self.assertEqual(mem_saves, [], "门控关闭同样跳过 memories 流水（既有行为）")
        self.assertEqual(len(sent), 1, "回复仍正常发送")

    def test_fallback_reply_not_written(self):
        """LLM 空回复兜底路径不写事件账本（兜底文案不是真实对话）。"""
        fake_service = RecordingService()
        mem_saves, sent, _, _ = self._run_qq(fake_service, ask_reply="")

        self.assertEqual(_event_rows(fake_service), [], "兜底路径不得写事件账本")
        self.assertEqual(mem_saves[0]["title"], "⚠️ QQ 未回复", "兜底 memories 写入保持原样")
        self.assertTrue(any("信号不太好" in s["message"] for s in sent),
                        "兜底文案仍发送")

    def test_logs_do_not_leak_content(self):
        fake_service = RecordingService(insert_exc=RuntimeError("mock insert failure"))
        _, _, _, logs = self._run_qq(fake_service)
        joined = "\n".join(logs)
        self.assertNotIn(SYNTHETIC_QQ_TEXT, joined, "日志不得包含用户正文")
        self.assertNotIn(SYNTHETIC_QQ_REPLY, joined, "日志不得包含回复正文")
        self.assertNotIn(TEST_USER_ID, joined, "日志不得包含 user_id")


# ==========================================
# A2. Telegram 渠道
# ==========================================

class TestTelegramChannelEvents(unittest.TestCase):
    """heartbeat async_telegram_polling._handle_merged 主成功路径的 memory_events 双写。"""

    async def _capture_tg_handler(self):
        """启动 async_telegram_polling，经 aggregator 捕获内层 _handle_merged 后取消任务。"""
        captured = {}

        def _fake_get_aggregator(name, handler):
            captured["handler"] = handler
            return object()

        with patch.object(aggregator, "get_aggregator", _fake_get_aggregator):
            task = asyncio.create_task(heartbeat.async_telegram_polling())
            ok = await _run_until(lambda: "handler" in captured)
            await _cancel_quietly(task)
        return captured.get("handler") if ok else None

    def _run_tg(self, fake_service, *, write_enabled=True, pinecone=None,
                ask_reply=SYNTHETIC_TG_REPLY):
        """在 mock 环境下执行 TG _handle_merged，返回 (mem_saves, gw_logs)。"""
        mem_saves = []
        gw_logs = []

        def _fake_save(title, content, category="流水", mood="平静", tags=""):
            mem_saves.append({"title": title, "content": content, "tags": tags})
            return True

        async def _fake_ctx(text, **kwargs):
            return ""

        async def _fake_ask(client, prompt, system_prompt="", temperature=0.7):
            return ask_reply

        def _gateway_log(msg):
            gw_logs.append(str(msg))

        async def main():
            handler = await self._capture_tg_handler()
            self.assertTrue(callable(handler), "未能捕获 TG _handle_merged")
            await handler(424242, SYNTHETIC_TG_TEXT, [("__k", {})])

        with patch.dict(os.environ, {"TG_BOT_TOKEN": "FAKE_TOKEN_PHASEA"}), \
             patch("requests.get", lambda *a, **k: FakeResponse({"ok": False})), \
             patch("requests.post", lambda *a, **k: FakeResponse({"ok": True, "result": []})), \
             patch.object(server, "_get_llm_client", lambda role: object()), \
             patch.object(server, "_ask_llm_async", _fake_ask), \
             patch.object(server, "_build_channel_context", _fake_ctx), \
             patch.object(server, "_save_memory_to_db", _fake_save), \
             patch.object(server, "_resolve_pinecone_user_id", lambda: TEST_USER_ID), \
             patch.object(server, "pinecone_memory", pinecone), \
             patch.object(server, "supabase_service", fake_service), \
             patch.object(gateway, "_chat_write_enabled", lambda: write_enabled), \
             patch.object(gateway, "_emotion_enabled", lambda: False), \
             patch.object(gateway, "_device_context_enabled", lambda: False), \
             patch.object(gateway, "_log", _gateway_log):
            asyncio.run(main())
        return mem_saves, gw_logs

    def test_user_and_assistant_events_written(self):
        fake_service = RecordingService()
        mc = FakePinecone()
        mem_saves, _ = self._run_tg(fake_service, pinecone=mc)

        _assert_pair_fields(self, fake_service, channel="tg", created_by="heartbeat",
                            user_text=SYNTHETIC_TG_TEXT, asst_text=SYNTHETIC_TG_REPLY)
        # 旧逻辑回归：memories 流水与 Pinecone（仅 user 消息）不受影响
        self.assertEqual(len(mem_saves), 1)
        self.assertEqual(mem_saves[0]["tags"], "TG_MSG")
        self.assertEqual(len(mc.add_calls), 1)
        self.assertEqual([m["content"] for m in mc.add_calls[0]["messages"]],
                         [SYNTHETIC_TG_TEXT], "Pinecone 仍只写 user 消息")

    def test_service_missing_skips_without_error(self):
        mc = FakePinecone()
        mem_saves, gw_logs = self._run_tg(None, pinecone=mc)

        self.assertEqual(len(mem_saves), 1, "memories 写入不受影响")
        self.assertEqual(len(mc.add_calls), 1, "Pinecone 写入不受影响")
        self.assertTrue(any("service_role 客户端不可用" in m for m in gw_logs),
                        "service 缺失应有降级日志")

    def test_insert_failure_does_not_break_main_flow(self):
        fake_service = RecordingService(insert_exc=RuntimeError("mock insert failure"))
        mc = FakePinecone()
        mem_saves, _ = self._run_tg(fake_service, pinecone=mc)

        self.assertEqual(len(mem_saves), 1, "memories 写入不受影响")
        self.assertEqual(len(mc.add_calls), 1, "Pinecone 写入不受影响")
        ops = {path[0][0] for tbl, path in fake_service.calls if tbl == "memory_events"}
        self.assertTrue(ops.issubset({"insert"}), f"memory_events 只允许 insert，实际: {ops}")

    def test_gate_disabled_skips_events(self):
        fake_service = RecordingService()
        mc = FakePinecone()
        mem_saves, _ = self._run_tg(fake_service, write_enabled=False, pinecone=mc)

        self.assertEqual(_event_rows(fake_service), [],
                         "chat_history_write_enabled=false 时事件不落库")
        self.assertEqual(mem_saves, [], "门控关闭同样跳过 memories 流水（既有行为）")
        self.assertEqual(mc.add_calls, [], "门控关闭同样跳过 Pinecone（既有行为）")

    def test_fallback_reply_not_written(self):
        """LLM 空回复兜底路径不写事件账本（兜底文案不是真实对话）。"""
        fake_service = RecordingService()
        mem_saves, _ = self._run_tg(fake_service, ask_reply="")

        self.assertEqual(_event_rows(fake_service), [], "兜底路径不得写事件账本")
        self.assertEqual(mem_saves[0]["title"], "⚠️ TG 未回复", "兜底 memories 写入保持原样")

    def test_logs_do_not_leak_content(self):
        fake_service = RecordingService(insert_exc=RuntimeError("mock insert failure"))
        _, gw_logs = self._run_tg(fake_service)
        joined = "\n".join(gw_logs)
        self.assertNotIn(SYNTHETIC_TG_TEXT, joined, "日志不得包含用户正文")
        self.assertNotIn(SYNTHETIC_TG_REPLY, joined, "日志不得包含回复正文")
        self.assertNotIn(TEST_USER_ID, joined, "日志不得包含 user_id")


# ==========================================
# A3-1. 后台主动问候（async_autonomous_life）
# ==========================================

class TestBackgroundGreetingEvents(unittest.TestCase):

    def _run_greeting(self, fake_service, *, write_enabled=True,
                      wait_for_events=True, min_saves=3):
        """运行自主生命循环；wait_for_events=True 等到事件写入，False 等到至少
        min_saves 次 memories 写入后取消（用于 service 缺失/insert 失败场景）。
        返回 mem_saves。"""
        mem_saves, pushes = [], []

        def _fake_save(title, content, category="流水", mood="平静", tags=""):
            mem_saves.append({"title": title, "content": content, "tags": tags})
            return True

        def _fake_push(text, title="通知", plain=False):
            pushes.append({"text": text, "title": title})

        def _fake_now():
            # 固定正午，避开深夜免打扰分支
            return datetime.datetime(2026, 9, 12, 12, 0, 0)

        async def _fake_ctx(query="", **kwargs):
            return ""

        async def _fake_ask_role(role, prompt, system_prompt="", temperature=0.7):
            return json.dumps({"send": True, "reason": "SYNTHETIC",
                               "message": SYNTHETIC_GREETING})

        async def main():
            task = asyncio.create_task(heartbeat.async_autonomous_life())
            if wait_for_events:
                ok = await _run_until(lambda: bool(_event_rows(fake_service)))
                self.assertTrue(ok, "自主生命循环未产生 memory_events 写入")
            else:
                ok = await _run_until(lambda: len(mem_saves) >= min_saves)
                self.assertTrue(ok, "自主生命循环未执行到 memories 写入")
            await _cancel_quietly(task)

        with patch.dict(os.environ, {"HEARTBEAT_INTERVAL": "0"}), \
             _patched_fast_sleep(), \
             patch.object(server, "_get_now_bj", _fake_now), \
             patch.object(server, "supabase", None), \
             patch.object(server, "_build_channel_context", _fake_ctx), \
             patch.object(server, "ask_role", _fake_ask_role), \
             patch.object(server, "_push_wechat", _fake_push), \
             patch.object(server, "_save_memory_to_db", _fake_save), \
             patch.object(server, "supabase_service", fake_service), \
             patch.object(server, "_resolve_pinecone_user_id", lambda: TEST_USER_ID), \
             patch.object(gateway, "_chat_write_enabled", lambda: write_enabled):
            asyncio.run(main())
        return mem_saves

    def test_greeting_event_written(self):
        fake_service = RecordingService()
        mem_saves = self._run_greeting(fake_service)

        _assert_single_event_rows(fake_service, f"主动问候: {SYNTHETIC_GREETING}")
        # 旧逻辑回归：主动问候 memories 流水保持原样
        self.assertTrue(any(m["title"] == "🤖 主动问候" for m in mem_saves))

    def test_greeting_not_gated_by_chat_history_write_enabled(self):
        """后台自主活动不受 chat_history_write_enabled 门控（开关管聊天记录，不管行为日志）。"""
        fake_service = RecordingService()
        self._run_greeting(fake_service, write_enabled=False)

        _assert_single_event_rows(fake_service, f"主动问候: {SYNTHETIC_GREETING}")

    def test_greeting_service_missing_skips(self):
        mem_saves = self._run_greeting(None, wait_for_events=False, min_saves=3)
        self.assertGreaterEqual(len(mem_saves), 3, "service 缺失时 memories 写入不受影响")

    def test_greeting_insert_failure_does_not_break_loop(self):
        fake_service = RecordingService(insert_exc=RuntimeError("mock insert failure"))
        mem_saves = self._run_greeting(fake_service, wait_for_events=False, min_saves=3)
        self.assertGreaterEqual(len(mem_saves), 3, "insert 失败时 memories 写入不受影响")
        ops = {path[0][0] for tbl, path in fake_service.calls if tbl == "memory_events"}
        self.assertTrue(ops.issubset({"insert", "execute"}),
                        f"memory_events 只允许 insert/execute，实际: {ops}")


# ==========================================
# A3-2. 后台自由活动（async_free_activity）
# ==========================================

class TestBackgroundFreeActivityEvents(unittest.TestCase):

    def _run_free_activity(self, fake_service, *, activity="网上冲浪",
                           write_enabled=True, wait_for_events=True,
                           min_tool_calls=3):
        """运行自由活动循环；wait_for_events=True 等到事件写入，False 等到循环跑满
        min_tool_calls 轮后取消（用于断言"不该写"的场景）。返回 (mem_saves, tool_calls)。"""
        mem_saves = []
        tool_calls = {"n": 0}

        async def _fake_tool_loop(*args, **kwargs):
            tool_calls["n"] += 1
            return activity, SYNTHETIC_FREE_LOG

        def _fake_save(title, content, category="流水", mood="平静", tags=""):
            mem_saves.append({"title": title, "content": content, "tags": tags})
            return True

        def _fake_now():
            return datetime.datetime(2026, 9, 12, 12, 0, 0)

        async def _fake_ctx(query="", **kwargs):
            return ""

        async def _fake_check_cat(now_bj):
            return None

        async def main():
            task = asyncio.create_task(heartbeat.async_free_activity())
            if wait_for_events:
                ok = await _run_until(lambda: bool(_event_rows(fake_service)))
                self.assertTrue(ok, "自由活动循环未产生 memory_events 写入")
            else:
                ok = await _run_until(lambda: tool_calls["n"] >= min_tool_calls)
                self.assertTrue(ok, "自由活动循环未执行（tool_loop 未被调用）")
            await _cancel_quietly(task)

        with patch.dict(os.environ, {"FREE_ACTIVITY_INTERVAL": "0",
                                     "FREE_ACTIVITY_ENABLED": "true"}), \
             _patched_fast_sleep(), \
             patch.object(heartbeat, "_free_activity_check_cat", _fake_check_cat), \
             patch.object(activity_log_mod, "get_recent_completed_free_activities",
                          lambda limit=2: []), \
             patch.object(activity_log_mod, "start_activity_log",
                          lambda *a, **k: {"ok": True}), \
             patch.object(activity_log_mod, "finalize_activity_log",
                          lambda *a, **k: {"ok": True}), \
             patch.object(activity_log_mod, "fail_activity_log",
                          lambda *a, **k: {"ok": True}), \
             patch.object(server, "supabase", None), \
             patch.object(server, "supabase_service", fake_service), \
             patch.object(server, "_resolve_pinecone_user_id", lambda: TEST_USER_ID), \
             patch.object(server, "_get_now_bj", _fake_now), \
             patch.object(server, "_build_channel_context", _fake_ctx), \
             patch.object(server, "_save_memory_to_db", _fake_save), \
             patch.object(server, "_push_wechat", lambda *a, **k: None), \
             patch.object(gateway, "_emotion_enabled", lambda: False), \
             patch.object(gateway, "_chat_write_enabled", lambda: write_enabled), \
             patch.object(desire_bridge, "seconds_until_next_heartbeat",
                          lambda *a, **k: None), \
             patch.object(tool_loop, "run_free_activity_tool_loop", _fake_tool_loop):
            asyncio.run(main())
        return mem_saves, tool_calls

    def test_free_activity_event_written(self):
        fake_service = RecordingService()
        mem_saves, _ = self._run_free_activity(fake_service)

        _assert_single_event_rows(fake_service, f"网上冲浪: {SYNTHETIC_FREE_LOG}")
        # 旧逻辑回归：自由活动 memories 流水保持原样
        self.assertTrue(any(m["title"] == "🎈 自由活动·网上冲浪" for m in mem_saves))

    def test_secret_diary_not_written(self):
        """「写秘密日记」不落 memories，也不落事件账本（隐私语义一致）。"""
        fake_service = RecordingService()
        mem_saves, tool_calls = self._run_free_activity(
            fake_service, activity="写秘密日记", wait_for_events=False,
            min_tool_calls=5)

        self.assertGreaterEqual(tool_calls["n"], 5, "循环确实跑了多轮")
        self.assertEqual(_event_rows(fake_service), [], "秘密日记不得写事件账本")
        self.assertEqual(mem_saves, [], "秘密日记不得写 memories（既有行为）")

    def test_free_activity_not_gated_by_chat_history_write_enabled(self):
        """后台自主活动不受 chat_history_write_enabled 门控。"""
        fake_service = RecordingService()
        self._run_free_activity(fake_service, write_enabled=False)

        _assert_single_event_rows(fake_service, f"网上冲浪: {SYNTHETIC_FREE_LOG}")

    def test_free_activity_service_missing_skips(self):
        mem_saves, _ = self._run_free_activity(None, wait_for_events=False,
                                               min_tool_calls=3)
        self.assertGreaterEqual(len(mem_saves), 3, "service 缺失时 memories 写入不受影响")

    def test_free_activity_insert_failure_does_not_break_loop(self):
        fake_service = RecordingService(insert_exc=RuntimeError("mock insert failure"))
        mem_saves, _ = self._run_free_activity(fake_service, wait_for_events=False,
                                               min_tool_calls=3)
        self.assertGreaterEqual(len(mem_saves), 3, "insert 失败时 memories 写入不受影响")
        ops = {path[0][0] for tbl, path in fake_service.calls if tbl == "memory_events"}
        self.assertTrue(ops.issubset({"insert"}), f"memory_events 只允许 insert，实际: {ops}")


# ==========================================
# 源码约束
# ==========================================

class TestSourceConstraints(unittest.TestCase):

    def test_memory_events_write_only_in_channels(self):
        """napcat.py / heartbeat.py 对 memory_events 只写不读（无 select/update/delete）。"""
        base = os.path.dirname(__file__)
        for fname in ("napcat.py", "heartbeat.py"):
            with open(os.path.join(base, fname), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn('.table("memory_events").select', src,
                             f"{fname} 不得读取 memory_events")
            self.assertNotIn('.table("memory_events").update', src)
            self.assertNotIn('.table("memory_events").delete', src)

    def test_channel_blocks_reuse_service_client(self):
        """三渠道事件写入不得新建客户端（create_client 只允许出现在 server.py）。"""
        base = os.path.dirname(__file__)
        for fname in ("napcat.py", "heartbeat.py"):
            with open(os.path.join(base, fname), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("create_client", src,
                             f"{fname} 事件写入必须复用 server.supabase_service")


if __name__ == "__main__":
    unittest.main()
