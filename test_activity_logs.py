"""
test_activity_logs.py — 阶段 C3：结构化行动日志（activity_logs）测试
====================================================================
覆盖：
- sanitize_thought_summary：普通摘要保留截长；<think>/<thinking> 整体丢弃；
  非字符串为空
- sanitize_tools_used：字段白名单；非 dict 跳过；上限；status 推断
- start/finalize/fail 幂等：同 key 不重复；已完成不覆盖；NOT_FOUND；
  service 缺失；非法 status
- 自由活动接入：start 失败阻断；正常完成顺序 start→loop→memories→finalize；
  状态映射；秘密日记/外向/搜索类脱敏；返回 None → skipped；异常 → failed
- Home 接入：start 失败阻断；observed/partial/failed/succeeded 映射；
  tools_used 只含真实成功；None → skipped；memories 仍写

纯 mock 测试，不触真实 Supabase。
运行：python -m unittest test_activity_logs -v
"""

import datetime
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import heartbeat
import home.activity_log as alog
import tool_loop


# ============================================================
# Fake service client（模拟 supabase-py 链式调用）
# ============================================================
class _FakeTable:
    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._ops = []       # 本表的调用记录 [("select"/"insert"/"update", payload)]
        self._eq = {}
        self._select_cols = None

    def select(self, cols):
        self._ops.append(("select", cols))
        self._select_cols = cols
        return self

    def insert(self, payload):
        self._ops.append(("insert", payload))
        self._payload = payload
        return self

    def update(self, payload):
        self._ops.append(("update", payload))
        self._payload = payload
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def limit(self, n):
        return self

    def execute(self):
        class _R:
            data = []
        rows = self._store.setdefault(self._table, [])
        op = self._ops[-1][0]
        if op == "select":
            _R.data = [dict(r) for r in rows
                       if all(r.get(k) == v for k, v in self._eq.items())]
        elif op == "insert":
            rows.append(dict(self._payload))
            _R.data = [dict(self._payload)]
        elif op == "update":
            matched = [r for r in rows
                       if all(r.get(k) == v for k, v in self._eq.items())]
            for r in matched:
                r.update(self._payload)
            _R.data = [dict(r) for r in matched]
        return _R()


class _FakeSB:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeTable(self.store, name)


def _with_client(fn):
    """把 FakeSB 注入 activity_log 的 service 客户端。"""
    fake = _FakeSB()
    with patch.object(alog, "_get_service_client", return_value=fake):
        fn(fake)
    return fake


# ============================================================
# thought_summary 清洗（8.3 / 场景 H）
# ============================================================
class TestSanitizeThought(unittest.TestCase):
    def test_normal_kept_and_clipped(self):
        """普通摘要保留并截长到 500。"""
        s = "想给它找点吃的。" * 200
        out = alog.sanitize_thought_summary(s)
        self.assertEqual(len(out), 500)
        self.assertTrue(out.startswith("想给它找点吃的"))

    def test_think_tag_dropped_entirely(self):
        """<think>...</think> 载体 → 整体丢弃，不提取标签内容。"""
        self.assertEqual(alog.sanitize_thought_summary("<think>内部推理 secret</think>"), "")
        self.assertEqual(
            alog.sanitize_thought_summary("正常开头 <thinking>secret</thinking> 结尾"), "")

    def test_reasoning_field_dropped(self):
        """reasoning 字段伪装 → 丢弃。"""
        self.assertEqual(alog.sanitize_thought_summary("reasoning_content: 内部推理"), "")

    def test_non_string_empty(self):
        """非字符串 → 空。"""
        self.assertEqual(alog.sanitize_thought_summary(None), "")
        self.assertEqual(alog.sanitize_thought_summary(123), "")
        self.assertEqual(alog.sanitize_thought_summary({"a": 1}), "")

    def test_whitespace_stripped(self):
        self.assertEqual(alog.sanitize_thought_summary("  念头  "), "念头")


# ============================================================
# tools_used 归一化（任务七）
# ============================================================
class TestSanitizeTools(unittest.TestCase):
    def test_whitelist_fields_only(self):
        """只保留 name/ok/status/error_code；args/raw/text 一律丢弃。"""
        out = alog.sanitize_tools_used([{
            "name": "water_plant", "ok": True, "status": "succeeded",
            "args": {"plant_id": "p-uuid"}, "raw": {"x": 1}, "text": "正文"}])
        self.assertEqual(out, [{"name": "water_plant", "ok": True,
                                "status": "succeeded", "error_code": ""}])

    def test_status_inferred_and_clamped(self):
        """非法 status 按 ok 推断；name/error_code 截长。"""
        out = alog.sanitize_tools_used([
            {"name": "t" * 100, "ok": False, "error_code": "E" * 100}])
        self.assertEqual(out[0]["name"], "t" * 60)
        self.assertEqual(out[0]["error_code"], "E" * 60)
        self.assertEqual(out[0]["status"], "failed")

    def test_non_dict_and_no_name_skipped(self):
        """非 dict / 无 name 跳过；超上限截断。"""
        items = ["x", {"ok": True}] + [{"name": f"t{i}", "ok": True} for i in range(30)]
        out = alog.sanitize_tools_used(items, max_items=5)
        self.assertEqual(len(out), 5)
        self.assertTrue(all("name" in o for o in out))

    def test_bad_input(self):
        self.assertEqual(alog.sanitize_tools_used(None), [])
        self.assertEqual(alog.sanitize_tools_used("x"), [])


# ============================================================
# start / finalize / fail 幂等
# ============================================================
class TestStartFinalize(unittest.TestCase):
    def test_start_creates_running(self):
        """start 成功创建 running 行。"""
        def body(fake):
            r = alog.start_activity_log("fa_k1", "free_activity")
            self.assertTrue(r["ok"])
            self.assertTrue(r["created"])
            rows = fake.store["activity_logs"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "running")
            self.assertEqual(rows[0]["source"], "free_activity")
        _with_client(body)

    def test_start_same_key_no_duplicate(self):
        """同一 key 重试不产生第二行（幂等）。"""
        def body(fake):
            alog.start_activity_log("fa_k1", "free_activity")
            r2 = alog.start_activity_log("fa_k1", "free_activity")
            self.assertTrue(r2["ok"])
            self.assertFalse(r2["created"])
            self.assertFalse(r2["already_final"])
            self.assertEqual(len(fake.store["activity_logs"]), 1)
        _with_client(body)

    def test_start_after_final_not_reset(self):
        """已完成记录不回置 running。"""
        def body(fake):
            alog.start_activity_log("fa_k1", "free_activity")
            alog.finalize_activity_log("fa_k1", status="succeeded")
            r = alog.start_activity_log("fa_k1", "free_activity")
            self.assertTrue(r["already_final"])
            self.assertEqual(fake.store["activity_logs"][0]["status"], "succeeded")
        _with_client(body)

    def test_start_validation(self):
        def body(fake):
            self.assertFalse(alog.start_activity_log("", "free_activity")["ok"])
            self.assertEqual(alog.start_activity_log("k", "bad_source")["error_code"],
                             "INVALID_SOURCE")
        _with_client(body)

    def test_start_service_missing(self):
        """service_role 缺失 → 明确失败（调用方必须停止活动）。"""
        with patch.object(alog, "_get_service_client", return_value=None):
            r = alog.start_activity_log("fa_k1", "free_activity")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "SERVICE_KEY_MISSING")

    def test_finalize_running_row(self):
        """finalize 更新 running 行：状态/摘要/tools_used/finished_at。"""
        def body(fake):
            alog.start_activity_log("fa_k1", "free_activity")
            r = alog.finalize_activity_log(
                "fa_k1", activity_id="free:weather", activity_name="查天气",
                status="succeeded", thought_summary="想看看天气",
                result_summary="看了天气。", tools_used=[{"name": "get_weather", "ok": True}])
            self.assertTrue(r["ok"])
            self.assertTrue(r["finalized"])
            row = fake.store["activity_logs"][0]
            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(row["activity_id"], "free:weather")
            self.assertEqual(row["activity_name"], "查天气")
            self.assertEqual(row["tools_used"][0]["name"], "get_weather")
            self.assertIsNotNone(row["finished_at"])
        _with_client(body)

    def test_finalize_twice_idempotent(self):
        """重复 finalize：不覆盖第一次结果、不插第二行。"""
        def body(fake):
            alog.start_activity_log("fa_k1", "free_activity")
            alog.finalize_activity_log("fa_k1", status="succeeded", result_summary="第一次")
            r2 = alog.finalize_activity_log("fa_k1", status="failed", result_summary="第二次")
            self.assertTrue(r2["ok"])
            self.assertTrue(r2["already_final"])
            rows = fake.store["activity_logs"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "succeeded")
            self.assertEqual(rows[0]["result_summary"], "第一次")
        _with_client(body)

    def test_finalize_not_found(self):
        """start 未成功时 finalize → NOT_FOUND，不插已完成记录掩盖。"""
        def body(fake):
            r = alog.finalize_activity_log("fa_missing", status="succeeded")
            self.assertFalse(r["ok"])
            self.assertEqual(r["error_code"], "NOT_FOUND")
            self.assertEqual(len(fake.store.get("activity_logs", [])), 0)
        _with_client(body)

    def test_finalize_invalid_status_and_clamps(self):
        def body(fake):
            alog.start_activity_log("fa_k1", "free_activity")
            self.assertEqual(alog.finalize_activity_log("fa_k1", status="running")["error_code"],
                             "INVALID_STATUS")
            self.assertEqual(alog.finalize_activity_log("fa_k1", status="bogus")["error_code"],
                             "INVALID_STATUS")
            r = alog.finalize_activity_log("fa_k1", status="succeeded",
                                           thought_summary="长" * 600,
                                           result_summary="长" * 1200)
            self.assertTrue(r["ok"])
            row = fake.store["activity_logs"][0]
            self.assertEqual(len(row["thought_summary"]), 500)
            self.assertEqual(len(row["result_summary"]), 1000)
        _with_client(body)

    def test_fail_activity_log(self):
        """fail_activity_log → failed + 安全摘要。"""
        def body(fake):
            alog.start_activity_log("fa_k1", "home_autonomy")
            r = alog.fail_activity_log("fa_k1", "自由活动异常：RuntimeError")
            self.assertTrue(r["ok"])
            row = fake.store["activity_logs"][0]
            self.assertEqual(row["status"], "failed")
            self.assertIn("RuntimeError", row["result_summary"])
        _with_client(body)

    def test_db_error_swallowed_and_logged(self):
        """数据库异常 → 返回失败 + 记日志（不崩溃、不假装成功）。"""
        bad = MagicMock()
        bad.table.side_effect = RuntimeError("db down")
        with patch.object(alog, "_get_service_client", return_value=bad), \
             patch.object(alog.logger, "warning") as m_warn:
            r = alog.start_activity_log("fa_k1", "free_activity")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "DB_ERROR")
        m_warn.assert_called_once()



# ============================================================
# 自由活动接入（真实驱动 heartbeat.async_free_activity 单轮）
# ============================================================
_SECRET_BODY = "这是绝不能进行动日志的秘密正文xyz"
_OUT_MSG = "这句是发给她的原话内容abc"


class _FreeActivityHarness:
    """驱动 async_free_activity 恰好一轮：第二次 asyncio.sleep 抛 _Stop(BaseException)
    穿透 `except Exception` 终止 while，全部依赖打桩，activity_log 函数记录调用序列。"""

    class _Stop(BaseException):
        pass

    def __init__(self, loop_ret=None, loop_exc=None, start_result=None):
        self.now_bj = datetime.datetime(2026, 8, 31, 10, 0)
        self.seq = []          # [("start", key) / "loop" / "memories" / ("finalize", kw) / ("fail", brief)]
        self.finalize_kwargs = []
        self.fail_briefs = []
        self._loop_ret = loop_ret
        self._loop_exc = loop_exc
        self._start_result = start_result or {"ok": True, "created": True}

    async def _sleep(self, secs):
        if getattr(self, "_sleep_fired", False):
            raise self._Stop()
        self._sleep_fired = True

    def _start(self, key, source, started_at=None):
        self.seq.append(("start", key))
        return dict(self._start_result)

    def _finalize(self, key, **kw):
        self.seq.append(("finalize", kw))
        self.finalize_kwargs.append(dict(kw))
        return {"ok": True, "finalized": True}

    def _fail(self, key, brief):
        self.seq.append(("fail", brief))
        self.fail_briefs.append(brief)
        return {"ok": True}

    async def run(self):
        async def fake_loop(**kw):
            self.seq.append("loop")
            self.loop_meta = kw.get("meta_out")
            if self._loop_exc:
                raise self._loop_exc
            if isinstance(self._loop_ret, tuple) and len(self._loop_ret) == 2:
                # 模拟 C3 meta_out 填充（heartbeat 只消费返回值 + meta）
                if self.loop_meta is not None and not self.loop_meta:
                    self.loop_meta.update({"activity_name": self._loop_ret[0],
                                           "thought_summary": "", "tools_used": [],
                                           "tool_total": 0, "tool_ok": 0,
                                           "tool_fail": 0, "tool_skip": 0})
            return self._loop_ret

        fake_alog = MagicMock()
        fake_alog.start_activity_log.side_effect = self._start
        fake_alog.finalize_activity_log.side_effect = self._finalize
        fake_alog.fail_activity_log.side_effect = self._fail

        with patch("asyncio.sleep", new=self._sleep), \
             patch("server._get_now_bj", return_value=self.now_bj), \
             patch.object(heartbeat, "_free_activity_check_cat", new=AsyncMock()), \
             patch("server.supabase", None), \
             patch("gateway._emotion_enabled", return_value=False), \
             patch("server._build_channel_context",
                   new=AsyncMock(return_value="ctx")), \
             patch("server._save_memory_to_db",
                   new=MagicMock(side_effect=lambda *a, **k: self.seq.append("memories"))), \
             patch("server._push_wechat", new=MagicMock()), \
             patch("tool_loop.run_free_activity_tool_loop", new=fake_loop), \
             patch("home.activity_log.start_activity_log",
                   new=fake_alog.start_activity_log), \
             patch("home.activity_log.finalize_activity_log",
                   new=fake_alog.finalize_activity_log), \
             patch("home.activity_log.fail_activity_log",
                   new=fake_alog.fail_activity_log):
            try:
                await heartbeat.async_free_activity()
            except self._Stop:
                pass


class TestFreeActivityReal(unittest.IsolatedAsyncioTestCase):
    async def test_l_start_failure_blocks_all(self):
        """L：start 失败 → 不进工具循环、不写 memories、不推送。"""
        h = _FreeActivityHarness(start_result={"ok": False,
                                               "error_code": "SERVICE_KEY_MISSING"})
        await h.run()
        seq_names = [s if isinstance(s, str) else s[0] for s in h.seq]
        self.assertEqual(seq_names, ["start"])
        self.assertNotIn("loop", seq_names)
        self.assertNotIn("memories", seq_names)

    async def test_n_normal_completion_order(self):
        """N：start → loop → memories → finalize；只 finalize 一次。"""
        h = _FreeActivityHarness(loop_ret=("查天气", "阳光很好"))
        await h.run()
        names = [s if isinstance(s, str) else s[0] for s in h.seq]
        self.assertEqual(names, ["start", "loop", "memories", "finalize"])
        kw = h.finalize_kwargs[0]
        self.assertEqual(kw["activity_id"], "free:weather")
        self.assertEqual(kw["activity_name"], "查天气")
        self.assertEqual(kw["status"], "succeeded")

    async def test_i_secret_diary_redacted(self):
        """I：秘密日记正文不进入 thought_summary/result_summary（固定文案）。"""
        h = _FreeActivityHarness(loop_ret=("写秘密日记", _SECRET_BODY))
        await h.run()
        kw = h.finalize_kwargs[0]
        self.assertEqual(kw["activity_id"], "free:secret_diary")
        self.assertEqual(kw["result_summary"], "写了一篇秘密日记。")
        self.assertEqual(kw["thought_summary"], "想写点只给自己看的记录")
        self.assertNotIn(_SECRET_BODY, str(kw))

    async def test_j_outgoing_redacted(self):
        """J：外向消息正文不进 result_summary（固定摘要）。"""
        h = _FreeActivityHarness(loop_ret=("想对方了", _OUT_MSG))
        await h.run()
        kw = h.finalize_kwargs[0]
        self.assertEqual(kw["result_summary"], "生成并发送了一条主动消息。")
        self.assertNotIn(_OUT_MSG, str(kw))

    async def test_k_search_activities_redacted(self):
        """K：淘宝/冲浪/翻旧回忆的结果与查询词不进 result_summary。"""
        leak = "含搜索词与记忆正文与商品名的内容"
        for activity, expected in (("翻旧回忆", "翻了翻旧回忆。"),
                                   ("逛淘宝", "逛了逛淘宝找礼物灵感。"),
                                   ("网上冲浪", "浏览了一些网页内容。")):
            with self.subTest(activity=activity):
                h = _FreeActivityHarness(loop_ret=(activity, leak))
                await h.run()
                self.assertEqual(h.finalize_kwargs[0]["result_summary"], expected)
                self.assertNotIn(leak, str(h.finalize_kwargs[0]))

    async def test_s_none_finalize_skipped(self):
        """S：循环返回 None → finalize 为 skipped，不留永久 running。"""
        h = _FreeActivityHarness(loop_ret=None)
        await h.run()
        names = [s if isinstance(s, str) else s[0] for s in h.seq]
        self.assertEqual(names, ["start", "loop", "finalize"])
        self.assertEqual(h.finalize_kwargs[0]["status"], "skipped")

    async def test_t_exception_finalize_failed(self):
        """T：活动抛异常 → fail finalize（failed），异常不被吞掉（继续下一轮→Stop）。"""
        h = _FreeActivityHarness(loop_exc=RuntimeError("boom"))
        await h.run()
        names = [s if isinstance(s, str) else s[0] for s in h.seq]
        self.assertEqual(names[:3], ["start", "loop", "fail"])
        self.assertEqual(h.finalize_kwargs, [])  # 无正常 finalize
        self.assertIn("RuntimeError", h.fail_briefs[0])

    async def test_v_start_before_loop(self):
        """V：start 严格发生在工具循环之前。"""
        h = _FreeActivityHarness(loop_ret=("查天气", "日记"))
        await h.run()
        names = [s if isinstance(s, str) else s[0] for s in h.seq]
        self.assertLess(names.index("start"), names.index("loop"))

    async def test_status_mapping_partial(self):
        """部分成功 → partial（meta 由工具循环填充后映射）。"""
        h = _FreeActivityHarness(loop_ret=("记点小账", "记了账"))
        holder = h

        async def fake_loop(**kw):
            holder.seq.append("loop")
            m = kw.get("meta_out")
            if m is not None:
                m.update({"activity_name": "记点小账", "thought_summary": "记一下",
                          "tools_used": [{"name": "wallet_check", "ok": True,
                                          "status": "succeeded", "error_code": ""},
                                         {"name": "wallet_spend", "ok": False,
                                          "status": "failed", "error_code": "WALLET_NOT_FOUND"}],
                          "tool_total": 2, "tool_ok": 1, "tool_fail": 1, "tool_skip": 0})
            return ("记点小账", "记了账")

        fake_alog = MagicMock()
        fake_alog.start_activity_log.side_effect = holder._start
        fake_alog.finalize_activity_log.side_effect = holder._finalize
        fake_alog.fail_activity_log.side_effect = holder._fail
        with patch("asyncio.sleep", new=holder._sleep), \
             patch("server._get_now_bj", return_value=holder.now_bj), \
             patch.object(heartbeat, "_free_activity_check_cat", new=AsyncMock()), \
             patch("server.supabase", None), \
             patch("gateway._emotion_enabled", return_value=False), \
             patch("server._build_channel_context",
                   new=AsyncMock(return_value="ctx")), \
             patch("server._save_memory_to_db",
                   new=MagicMock(side_effect=lambda *a, **k: holder.seq.append("memories"))), \
             patch("server._push_wechat", new=MagicMock()), \
             patch("tool_loop.run_free_activity_tool_loop", new=fake_loop), \
             patch("home.activity_log.start_activity_log",
                   new=fake_alog.start_activity_log), \
             patch("home.activity_log.finalize_activity_log",
                   new=fake_alog.finalize_activity_log), \
             patch("home.activity_log.fail_activity_log",
                   new=fake_alog.fail_activity_log):
            try:
                await heartbeat.async_free_activity()
            except holder._Stop:
                pass
        self.assertEqual(holder.finalize_kwargs[0]["status"], "partial")


# ============================================================
# Home 接入（真实驱动 heartbeat.async_home_autonomy_tick 单轮）
# ============================================================
class _HomeHarness:
    """驱动 async_home_autonomy_tick 恰好一轮（同 _Stop 技巧）。"""

    class _Stop(BaseException):
        pass

    def __init__(self, loop_ret=("在家待着", []), loop_meta=None, loop_exc=None,
                 start_result=None):
        self.now_bj = datetime.datetime(2026, 8, 31, 10, 0)
        self.seq = []
        self.finalize_kwargs = []
        self.fail_briefs = []
        self._loop_ret = loop_ret
        self._loop_meta = loop_meta or {}
        self._loop_exc = loop_exc
        self._start_result = start_result or {"ok": True, "created": True}

    async def _sleep(self, secs):
        if getattr(self, "_sleep_fired", False):
            raise self._Stop()
        self._sleep_fired = True

    def _start(self, key, source, started_at=None):
        self.seq.append(("start", key))
        return dict(self._start_result)

    def _finalize(self, key, **kw):
        self.seq.append(("finalize", kw))
        self.finalize_kwargs.append(dict(kw))
        return {"ok": True, "finalized": True}

    def _fail(self, key, brief):
        self.seq.append(("fail", brief))
        self.fail_briefs.append(brief)
        return {"ok": True}

    async def run(self):
        async def fake_loop(**kw):
            self.seq.append("loop")
            m = kw.get("meta_out")
            if m is not None and self._loop_ret is not None:
                m.clear()
                m.update(self._loop_meta)
            if self._loop_exc:
                raise self._loop_exc
            return self._loop_ret

        fake_alog = MagicMock()
        fake_alog.start_activity_log.side_effect = self._start
        fake_alog.finalize_activity_log.side_effect = self._finalize
        fake_alog.fail_activity_log.side_effect = self._fail

        with patch.dict(os.environ, {"HOME_AUTONOMY_ENABLED": "true"}), \
             patch("asyncio.sleep", new=self._sleep), \
             patch("server._get_now_bj", return_value=self.now_bj), \
             patch("server._build_channel_context",
                   new=AsyncMock(return_value="ctx")), \
             patch("server._save_memory_to_db",
                   new=MagicMock(side_effect=lambda *a, **k: self.seq.append("memories"))), \
             patch("tool_loop.run_home_autonomy_tool_loop", new=fake_loop), \
             patch("home.activity_log.start_activity_log",
                   new=fake_alog.start_activity_log), \
             patch("home.activity_log.finalize_activity_log",
                   new=fake_alog.finalize_activity_log), \
             patch("home.activity_log.fail_activity_log",
                   new=fake_alog.fail_activity_log):
            try:
                await heartbeat.async_home_autonomy_tick()
            except self._Stop:
                pass


class TestHomeReal(unittest.IsolatedAsyncioTestCase):
    async def test_m_start_failure_blocks(self):
        """M：start 失败 → 不进 Home 循环、不写 memories。"""
        h = _HomeHarness(start_result={"ok": False, "error_code": "SERVICE_KEY_MISSING"})
        await h.run()
        names = [s if isinstance(s, str) else s[0] for s in h.seq]
        self.assertEqual(names, ["start"])
        self.assertNotIn("loop", names)

    async def test_o_succeeded_memories_still_written(self):
        """O：全部写成功 → succeeded；memories 仍写；tools_used 只含真实成功。"""
        h = _HomeHarness(loop_ret=("浇了水", ["water_plant"]),
                         loop_meta={"planning_failed": False, "has_write_ok": True,
                                    "write_fail": 0, "skip_count": 0,
                                    "thought_summary": "植物有点渴",
                                    "tools_used": [{"name": "water_plant", "ok": True,
                                                    "status": "succeeded",
                                                    "error_code": ""}]})
        await h.run()
        names = [s if isinstance(s, str) else s[0] for s in h.seq]
        self.assertEqual(names, ["start", "loop", "memories", "finalize"])
        kw = h.finalize_kwargs[0]
        self.assertEqual(kw["status"], "succeeded")
        self.assertEqual(kw["activity_id"], "home:autonomy")
        self.assertEqual(kw["tools_used"][0]["name"], "water_plant")

    async def test_p_observed(self):
        """P：无写成功（仅观察）→ observed；result_summary 明确只是观察。"""
        h = _HomeHarness(loop_ret=("看了看", []),
                         loop_meta={"planning_failed": False, "has_write_ok": False,
                                    "write_fail": 0, "skip_count": 0,
                                    "thought_summary": "", "tools_used": []})
        await h.run()
        kw = h.finalize_kwargs[0]
        self.assertEqual(kw["status"], "observed")
        self.assertIn("无", kw["result_summary"])

    async def test_q_partial(self):
        """Q：有成功也有失败 → partial。"""
        h = _HomeHarness(loop_ret=("做了些事", ["water_plant"]),
                         loop_meta={"planning_failed": False, "has_write_ok": True,
                                    "write_fail": 1, "skip_count": 0,
                                    "thought_summary": "", "tools_used": []})
        await h.run()
        self.assertEqual(h.finalize_kwargs[0]["status"], "partial")

    async def test_r_all_failed(self):
        """R：全部写失败 → failed。"""
        h = _HomeHarness(loop_ret=("没做成", []),
                         loop_meta={"planning_failed": False, "has_write_ok": False,
                                    "write_fail": 2, "skip_count": 0,
                                    "tools_used": []})
        await h.run()
        self.assertEqual(h.finalize_kwargs[0]["status"], "failed")

    async def test_planning_failed_maps_failed(self):
        """规划 JSON 解析失败 → failed。"""
        h = _HomeHarness(loop_ret=("在家看了一圈，但这次没有执行具体操作。", []),
                         loop_meta={"planning_failed": True, "has_write_ok": False,
                                    "write_fail": 0, "skip_count": 0, "tools_used": []})
        await h.run()
        self.assertEqual(h.finalize_kwargs[0]["status"], "failed")

    async def test_s_none_finalize_skipped(self):
        """S：循环返回 None → finalize skipped。"""
        h = _HomeHarness(loop_ret=None, loop_meta={})
        await h.run()
        names = [s if isinstance(s, str) else s[0] for s in h.seq]
        self.assertEqual(names, ["start", "loop", "finalize"])
        self.assertEqual(h.finalize_kwargs[0]["status"], "skipped")

    async def test_t_exception_fail_finalize(self):
        """T：循环抛异常 → fail finalize（failed）。"""
        h = _HomeHarness(loop_exc=RuntimeError("home boom"))
        await h.run()
        names = [s if isinstance(s, str) else s[0] for s in h.seq]
        self.assertEqual(names[:3], ["start", "loop", "fail"])
        self.assertIn("RuntimeError", h.fail_briefs[0])


if __name__ == "__main__":
    unittest.main()
