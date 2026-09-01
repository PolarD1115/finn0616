"""
test_unified_autonomy_c5.py — 阶段 C5：统一自主活动调度与稳定 activity_id 测试
==============================================================================
覆盖：
- A  稳定 activity_id 注册表（唯一性、字段完整性、旧名映射、单一权威源）
- B/C 候选开关组合（FREE only / HOME only）
- D  两者都关闭 → 统一任务退出、不调模型、不留痕
- E  HOME_AUTONOMY_PHASE 分层候选
- F  淘宝/冲浪门控不被 activity_id 绕过
- G  模型选择合法 ID → 只进一个执行器、一条 activity_logs
- H/I 非法 ID / 非 JSON → skipped、不随机兜底、不执行
- J  forced 自由活动（weather）不再二次选择
- K  forced 秘密日记（C4 路径、只写 home_private_diaries、正文不入日志）
- L/M/N Home 工具组边界（phase ∩ 工具组、代码级拒绝、不计 fail_count）
- O  run_background_process 只启动一个顶层自主任务（宠物 tick 保留）
- P  一次唤醒只产生一条 activity_logs（start 一次 / finalize 一次）
- Q/R Home / 普通活动兼容叙事日志
- S  外向推送一次、脱敏、失败不回退另一活动
- T  desire 旧名映射（不在候选丢弃、satisfy 用 want_action）
- U  防重复以 activity_id 计（failed/skipped 不计、memories 补足）
- V  无候选不调模型不兜底
- W  start → 选择 → 执行器 → memories → finalize 严格顺序
- X  start 失败阻断
- Y  执行器异常 → fail_activity_log、不进入第二个执行器
- Z  环境变量间隔规则（FREE only / HOME only / 双开 / 动态心跳有无）

纯 mock 测试，不连接 Supabase，不调用真实 LLM，不发外部 HTTP，
不执行真实工具副作用，不写生产 activity_logs。
运行：python -m unittest test_unified_autonomy_c5 -v
"""

import datetime
import inspect
import os
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import activity_registry as areg
import heartbeat
import home.activity_log as alog
import tool_loop

_NOW = datetime.datetime(2026, 9, 1, 10, 0)


def _all_free_names() -> set:
    """门控全通过时返回的全部活动名（含 legacy 逛虚拟小屋，与旧 _gate_activities 一致）。"""
    return {n for n, _ in tool_loop._FREE_ACTIVITIES}


# ============================================================
# A. 稳定 ID 注册表
# ============================================================
class TestRegistry(unittest.TestCase):
    def test_a_ids_unique(self):
        ids = list(areg.ACTIVITIES.keys())
        self.assertEqual(len(ids), len(set(ids)))
        for aid, entry in areg.ACTIVITIES.items():
            self.assertEqual(entry["activity_id"], aid)

    def test_a_fields_complete(self):
        for aid, entry in areg.ACTIVITIES.items():
            self.assertTrue(aid, entry)
            self.assertIn(entry["category"], ("free", "outgoing", "home", "legacy"))
            self.assertIn(entry["executor"], ("free", "home"))
            self.assertTrue(entry["name"])
            self.assertTrue(entry["description"])
            self.assertIn(entry["name"], entry["legacy_names"])

    def test_a_legacy_mapping(self):
        self.assertEqual(areg.legacy_to_id("做饭"), "home:kitchen")
        self.assertEqual(areg.legacy_to_id("做饭和用餐"), "home:kitchen")
        self.assertEqual(areg.legacy_to_id("照料阳台"), "home:garden")
        self.assertEqual(areg.legacy_to_id("照料花园"), "home:garden")
        self.assertEqual(areg.legacy_to_id("写秘密日记"), "free:secret_diary")
        self.assertEqual(areg.legacy_to_id("查天气"), "free:weather")
        self.assertEqual(areg.legacy_to_id("翻旧回忆"), "free:memory_recall")
        self.assertEqual(areg.legacy_to_id("想对方了"), "outgoing:miss_user")
        self.assertEqual(areg.legacy_to_id("不存在的活动"), "")

    def test_a_single_source_no_drift(self):
        """tool_loop 与 heartbeat 的活动清单都由注册表派生，不是两份漂移列表。"""
        self.assertEqual(tool_loop._FREE_ACTIVITIES, areg.free_activity_entries())
        self.assertEqual(heartbeat._FREE_ACTIVITIES, areg.free_activity_entries())
        self.assertEqual(tool_loop._OUTGOING_ACTIVITIES, areg.outgoing_names())
        self.assertEqual(heartbeat._OUTGOING_ACTIVITIES, areg.outgoing_names())

    def test_a_legacy_virtual_house_not_candidate(self):
        """逛虚拟小屋为 legacy：保留旧执行路径，但不进入统一候选。"""
        cand_ids = {e["activity_id"] for e in areg.unified_free_candidates()}
        self.assertIn("free:secret_diary", cand_ids)
        self.assertIn("free:taobao", cand_ids)
        self.assertNotIn("free:virtual_house", cand_ids)
        self.assertEqual(areg.get("free:virtual_house")["executor"], "free")

    def test_a_home_activities_have_tools(self):
        for entry in areg.ACTIVITIES.values():
            if entry["category"] == "home":
                self.assertTrue(entry.get("home_tool_group"), entry["activity_id"])
                self.assertGreaterEqual(int(entry["min_phase"]), 1)


# ============================================================
# 统一唤醒 harness（真实驱动 heartbeat._unified_autonomy_tick）
# ============================================================
class _TickHarness:
    """打桩统一唤醒全部依赖，记录调用序列（seq）。"""

    def __init__(self, *, free_on=True, home_on=True, gated=None,
                 home_phase=4, home_runtime=True, recent_ids=None,
                 start_result=None, selection='{"activity_id": "free:tarot", "thought_summary": "想抽张塔罗"}',
                 selection_exc=None, free_ret=("抽张塔罗", "log内容"), free_exc=None,
                 home_ret=("做了饭", ["cook_recipe"]), home_exc=None,
                 push_exc=None, emotion_on=False):
        self.now_bj = _NOW
        self.seq = []
        self.finalize_kwargs = []
        self.fail_briefs = []
        self.start_keys = []
        self.memory_calls = []
        self.push_calls = []
        self.satisfy_calls = []
        self.selection_prompts = []
        self.executor_calls = {"free": [], "home": []}
        self._free_on = bool(free_on)
        self._home_on = bool(home_on)
        self._gated = _all_free_names() if gated is None else gated
        self._home_phase = home_phase
        self._home_runtime = home_runtime
        self._recent_ids = recent_ids or []
        self._start_result = start_result or {"ok": True, "created": True}
        self._selection = selection
        self._selection_exc = selection_exc
        self._free_ret = free_ret
        self._free_exc = free_exc
        self._home_ret = home_ret
        self._home_exc = home_exc
        self._push_exc = push_exc
        self._emotion_on = emotion_on

    # ── 打桩函数 ──
    def _start(self, key, source, started_at=None):
        self.seq.append(("start", source))
        self.start_keys.append(key)
        return dict(self._start_result)

    def _finalize(self, key, **kw):
        self.seq.append(("finalize", kw))
        self.finalize_kwargs.append(dict(kw))
        return {"ok": True, "finalized": True}

    def _fail(self, key, brief):
        self.seq.append(("fail", brief))
        self.fail_briefs.append(brief)
        return {"ok": True}

    async def _ask(self, _client, prompt, **kw):
        self.seq.append("select")
        self.selection_prompts.append(prompt)
        if self._selection_exc:
            raise self._selection_exc
        return self._selection

    async def _free_executor(self, **kw):
        self.seq.append("executor:free")
        self.executor_calls["free"].append(kw)
        meta = kw.get("meta_out")
        if meta is not None and not meta and isinstance(self._free_ret, tuple):
            meta.update({"activity_name": self._free_ret[0], "thought_summary": "想抽张塔罗",
                         "tools_used": [], "tool_ok": 0, "tool_fail": 0,
                         "tool_skip": 0, "tool_total": 0})
        if self._free_exc:
            raise self._free_exc
        return self._free_ret

    async def _home_executor(self, **kw):
        self.seq.append("executor:home")
        self.executor_calls["home"].append(kw)
        meta = kw.get("meta_out")
        if meta is not None and not meta and isinstance(self._home_ret, tuple):
            meta.update({"activity_name": "做饭和用餐", "thought_summary": "想做饭",
                         "tools_used": [{"name": "cook_recipe", "ok": True, "status": "succeeded"}],
                         "planning_failed": False, "has_write_ok": True,
                         "write_fail": 0, "skip_count": 0, "total_calls": 1})
        if self._home_exc:
            raise self._home_exc
        return self._home_ret

    def _memories(self, *a, **k):
        self.seq.append("memories")
        self.memory_calls.append((a, k))

    def _push(self, *a, **k):
        self.seq.append("push")
        self.push_calls.append((a, k))
        if self._push_exc:
            raise self._push_exc

    def _satisfy(self, action):
        self.satisfy_calls.append(action)

    async def _gate(self, snap, now_bj):
        return set(self._gated), {}

    async def run(self):
        async def fake_check_cat(_now):
            pass

        async def fake_ctx(*a, **k):
            return "ctx"

        async def fake_gate(snap, now_bj):
            return await self._gate(snap, now_bj)

        env = {
            "FREE_ACTIVITY_ENABLED": self._free_flag,
            "HOME_AUTONOMY_ENABLED": self._home_flag,
        }
        with patch.dict(os.environ, env), \
             patch("server._get_now_bj", return_value=self.now_bj), \
             patch.object(heartbeat, "_free_activity_check_cat", new=fake_check_cat), \
             patch("gateway._emotion_enabled", return_value=self._emotion_on), \
             patch.object(tool_loop, "_gate_activities", new=fake_gate), \
             patch.object(tool_loop, "_HAS_HOME_RUNTIME", self._home_runtime), \
             patch.object(tool_loop, "HOME_AUTONOMY_PHASE", self._home_phase), \
             patch("server._build_channel_context", new=fake_ctx), \
             patch("server._save_memory_to_db", new=self._memories), \
             patch("server._push_wechat", new=self._push), \
             patch.object(heartbeat, "_ask_bg_role", new=self._ask), \
             patch.object(heartbeat, "_recent_activity_ids",
                          new=MagicMock(return_value=list(self._recent_ids))), \
             patch.object(alog, "start_activity_log", new=self._start), \
             patch.object(alog, "finalize_activity_log", new=self._finalize), \
             patch.object(alog, "fail_activity_log", new=self._fail), \
             patch.object(tool_loop, "run_free_activity_tool_loop", new=self._free_executor), \
             patch.object(tool_loop, "run_home_autonomy_tool_loop", new=self._home_executor):
            await heartbeat._unified_autonomy_tick()

    @property
    def _free_flag(self):
        return "true" if self._free_on else "false"

    @property
    def _home_flag(self):
        return "true" if self._home_on else "false"


def _harness(**kw):
    free_on = kw.pop("free_on", True)
    home_on = kw.pop("home_on", True)
    return _TickHarness(free_on=free_on, home_on=home_on, **kw)


def _seq_names(h):
    return [s if isinstance(s, str) else s[0] for s in h.seq]


# ============================================================
# B/C/D. 候选开关组合
# ============================================================
class TestCandidateSwitches(unittest.IsolatedAsyncioTestCase):
    async def test_b_free_only(self):
        """FREE=true HOME=false：只有自由/外向候选，无 Home ID，循环仍运行。"""
        h = _harness(free_on=True, home_on=False)
        await h.run()
        prompt = h.selection_prompts[0]
        self.assertIn("free:weather", prompt)
        self.assertIn("outgoing:miss_user", prompt)
        self.assertNotIn("home:kitchen", prompt)
        self.assertNotIn("home:observe", prompt)
        self.assertIn("start", _seq_names(h))

    async def test_c_home_only(self):
        """FREE=false HOME=true：只有 Home 候选，不因 FREE=false 退出循环。"""
        h = _harness(free_on=False, home_on=True, home_phase=4)
        await h.run()
        prompt = h.selection_prompts[0]
        self.assertIn("home:kitchen", prompt)
        self.assertIn("home:observe", prompt)
        self.assertNotIn("free:weather", prompt)
        self.assertNotIn("outgoing:", prompt)

    async def test_d_both_off_task_exits(self):
        """两者都关：统一任务直接返回，不进循环、不调模型、不留痕。"""

        async def _no_sleep(_s):
            raise AssertionError("统一循环不应启动")

        with patch.dict(os.environ, {"FREE_ACTIVITY_ENABLED": "false",
                                     "HOME_AUTONOMY_ENABLED": "false"}), \
             patch("asyncio.sleep", new=_no_sleep):
            await heartbeat.async_unified_autonomy()
        # tick 层同样无动作
        h = _harness(free_on=False, home_on=False)
        await h.run()
        self.assertEqual(h.seq, [])
        self.assertEqual(h.selection_prompts, [])

    async def test_d_pet_tick_still_registered(self):
        """两者关闭只影响统一循环；宠物 tick 仍由后台主进程启动。"""
        src = inspect.getsource(heartbeat.run_background_process)
        self.assertIn("async_pet_house_tick", src)


# ============================================================
# E. phase 分层
# ============================================================
class TestPhaseLayering(unittest.TestCase):
    def test_e_home_candidates_by_phase(self):
        def ids(phase):
            return [e["activity_id"] for e in
                    areg.home_candidates(phase, tool_loop._HOME_PHASE_TOOLS.get(phase, []))]
        self.assertEqual(ids(0), [])
        self.assertEqual(ids(1), ["home:observe"])
        self.assertEqual(ids(2), ["home:observe", "home:letters"])
        self.assertEqual(ids(3), ["home:observe", "home:letters", "home:garden", "home:kitchen"])
        self.assertEqual(ids(4), ["home:observe", "home:letters", "home:garden",
                                  "home:kitchen", "home:rest", "home:social"])

    async def test_e_phase1_prompt_scoped(self):
        h = _harness(free_on=False, home_on=True, home_phase=1)
        await h.run()
        prompt = h.selection_prompts[0]
        self.assertIn("home:observe", prompt)
        self.assertNotIn("home:garden", prompt)
        self.assertNotIn("home:kitchen", prompt)


# ============================================================
# F. 淘宝/冲浪门控
# ============================================================
class TestTaobaoSurfGating(unittest.IsolatedAsyncioTestCase):
    async def test_f_gated_out_not_in_candidates(self):
        """被门控裁掉的淘宝/冲浪不进入统一候选（activity_id 不能绕过旧门控）。"""
        gated = _all_free_names() - {"逛淘宝", "网上冲浪"}
        h = _harness(free_on=True, home_on=False, gated=gated)
        await h.run()
        prompt = h.selection_prompts[0]
        self.assertNotIn("free:taobao", prompt)
        self.assertNotIn("free:web_surf", prompt)
        self.assertIn("free:weather", prompt)

    async def test_f_unconfigured_url_means_not_candidate(self):
        """TAOBAO_MCP_URL 未配置 → _gate_taobao 拒绝（旧门控原样生效）。"""
        old_url, old_en = tool_loop.TAOBAO_MCP_URL, tool_loop.TOOL_LOOP_ENABLED
        tool_loop.TAOBAO_MCP_URL = ""
        tool_loop.TOOL_LOOP_ENABLED = True
        try:
            r = tool_loop._gate_taobao({}, {}, {"count": 0, "last_success_epoch": None}, 0.0)
            self.assertFalse(r["allowed"])
        finally:
            tool_loop.TAOBAO_MCP_URL, tool_loop.TOOL_LOOP_ENABLED = old_url, old_en


# ============================================================
# G/H/I/P/V/W/X/Y. 统一选择协议与单一 activity_logs
# ============================================================
class TestSelectionProtocol(unittest.IsolatedAsyncioTestCase):
    async def test_g_valid_home_choice_single_executor(self):
        """选 home:kitchen：只进 Home 执行器（带工具组），不进自由执行器，一条日志。"""
        h = _harness(free_on=True, home_on=True, home_phase=3,
                     selection='{"activity_id": "home:kitchen", "thought_summary": "想吃点热的"}')
        await h.run()
        names = _seq_names(h)
        self.assertEqual(names.count("start"), 1)
        self.assertEqual(names.count("finalize"), 1)
        self.assertEqual(names.count("select"), 1)
        self.assertIn("executor:home", names)
        self.assertNotIn("executor:free", names)
        kw = h.executor_calls["home"][0]
        self.assertEqual(kw["activity_id"], "home:kitchen")
        self.assertEqual(kw["allowed_tool_names"],
                         areg.get("home:kitchen")["home_tool_group"])
        fin = h.finalize_kwargs[0]
        self.assertEqual(fin["activity_id"], "home:kitchen")
        self.assertEqual(fin["activity_name"], "做饭和用餐")

    async def test_h_invalid_id_no_fallback(self):
        """非法 ID：不随机替换、不执行、finalize skipped、不写 memories。"""
        h = _harness(free_on=True, home_on=True, home_phase=4,
                     selection='{"activity_id": "free:hack", "thought_summary": "x"}')
        await h.run()
        names = _seq_names(h)
        self.assertNotIn("executor:free", names)
        self.assertNotIn("executor:home", names)
        self.assertNotIn("memories", names)
        self.assertEqual(names.count("start"), 1)
        fin = h.finalize_kwargs[0]
        self.assertEqual(fin["status"], "skipped")
        self.assertEqual(fin["result_summary"], "本轮没有选出可执行的活动。")

    async def test_i_non_json_no_crash(self):
        """非 JSON：同 H，安全结束不抛永久中断。"""
        h = _harness(free_on=True, home_on=False, selection="我觉得今天应该浇花")
        await h.run()
        names = _seq_names(h)
        self.assertNotIn("executor:free", names)
        self.assertEqual(h.finalize_kwargs[0]["status"], "skipped")

    async def test_p_single_log_record(self):
        """一次唤醒：start 一次、finalize 一次、source=unified_autonomy。"""
        h = _harness(free_on=True, home_on=False)
        await h.run()
        names = _seq_names(h)
        self.assertEqual(names.count("start"), 1)
        self.assertEqual(names.count("finalize"), 1)
        self.assertEqual(h.seq[0], ("start", "unified_autonomy"))

    async def test_v_no_candidates_no_model(self):
        """无候选：不调模型、不执行、不留痕、不随机兜底。"""
        h = _harness(free_on=True, home_on=False, gated=set())
        await h.run()
        self.assertEqual(h.seq, [])
        self.assertEqual(h.selection_prompts, [])

    async def test_w_start_before_side_effect_order(self):
        """严格顺序：候选非空 → start → 选择 → 执行器 → memories → finalize。"""
        h = _harness(free_on=True, home_on=False,
                     selection='{"activity_id": "free:tarot", "thought_summary": "想抽张塔罗"}')
        await h.run()
        self.assertEqual(_seq_names(h),
                         ["start", "select", "executor:free", "memories", "finalize"])

    async def test_x_start_failure_blocks_all(self):
        """start 失败：不调选择模型、不执行、不写 memories、不推送。"""
        h = _harness(free_on=True, home_on=False,
                     start_result={"ok": False, "error_code": "SERVICE_KEY_MISSING"})
        await h.run()
        names = _seq_names(h)
        self.assertEqual(names, ["start"])
        self.assertEqual(h.selection_prompts, [])

    async def test_y_executor_exception_fails_log(self):
        """执行器异常：fail_activity_log、不进入第二个执行器、不重试真实动作。"""
        h = _harness(free_on=True, home_on=True, home_phase=4, free_exc=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            await h.run()
        names = _seq_names(h)
        self.assertEqual(names.count("executor:free"), 1)
        self.assertEqual(names.count("executor:home"), 0)
        self.assertIn("fail", names)
        self.assertTrue(h.fail_briefs[0].startswith("统一自主活动异常"))


# ============================================================
# Q/R/S. 兼容叙事日志与外向推送
# ============================================================
class TestCompatLogs(unittest.IsolatedAsyncioTestCase):
    async def test_q_home_compat_log(self):
        """Home 活动：写 Home_Autonomy memories，不写 Free_Activity；ID/名称正确。"""
        h = _harness(free_on=False, home_on=True, home_phase=4,
                     selection='{"activity_id": "home:kitchen", "thought_summary": "想吃点热的"}')
        await h.run()
        (args, _k), = h.memory_calls
        self.assertEqual(args[0], "🏠 家庭自主·做饭和用餐")
        self.assertEqual(args[4], "Home_Autonomy")
        fin = h.finalize_kwargs[0]
        self.assertEqual(fin["activity_id"], "home:kitchen")
        self.assertEqual(fin["activity_name"], "做饭和用餐")

    async def test_r_free_compat_log(self):
        """普通活动：写 Free_Activity memories，不写 Home_Autonomy。"""
        h = _harness(free_on=True, home_on=False,
                     selection='{"activity_id": "free:tarot", "thought_summary": "想抽张塔罗"}')
        await h.run()
        (args, _k), = h.memory_calls
        self.assertEqual(args[0], "🎈 自由活动·抽张塔罗")
        self.assertEqual(args[4], "Free_Activity")

    async def test_s_outgoing_push_once(self):
        """外向活动：推送一次、不重复；日志走 Free_Activity。"""
        h = _harness(free_on=True, home_on=False,
                     selection='{"activity_id": "outgoing:share", "thought_summary": "想分享"}',
                     free_ret=("分享发现", "这句是发给她的原话"))
        await h.run()
        self.assertEqual(len(h.push_calls), 1)
        args, _k = h.push_calls[0]
        self.assertEqual(args[0], "这句是发给她的原话")
        self.assertEqual(args[1], "想你了")
        self.assertEqual(names_count(_seq_names(h), "push"), 1)
        (margs, _mk), = h.memory_calls
        self.assertEqual(margs[4], "Free_Activity")

    async def test_s_push_failure_no_fallback(self):
        """推送失败：finalize failed、不回退执行另一个活动。"""
        h = _harness(free_on=True, home_on=False,
                     selection='{"activity_id": "outgoing:care", "thought_summary": "惦记"}',
                     free_ret=("偷偷关心", "原话"), push_exc=RuntimeError("push down"))
        with self.assertRaises(RuntimeError):
            await h.run()
        self.assertEqual(len(h.push_calls), 1)
        names = _seq_names(h)
        self.assertEqual(names.count("executor:free"), 1)
        self.assertIn("fail", names)


def names_count(seq, name):
    return sum(1 for s in seq if s == name)


# ============================================================
# J/K. forced 自由执行器（无二次选择）
# ============================================================
class TestForcedFreeExecutor(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _ask(side_effect=None, ret=None):
        m = AsyncMock(side_effect=side_effect) if side_effect is not None else AsyncMock(return_value=ret)
        return m

    async def test_j_forced_weather_no_second_selection(self):
        """forced free:weather：不再 stage1 选择，直接走天气专用路径；thought 来自统一选择。"""
        ask = self._ask(ret="ignored")
        meta = {}
        with patch.object(tool_loop, "_finalize_weather_activity",
                          new=AsyncMock(return_value=("查天气", "今天晴"))):
            result = await tool_loop.run_free_activity_tool_loop(
                None, ask, "sys_ctx", _NOW, avoid="", desire_hint="",
                forced_activity_id="free:weather", selection_thought_summary="想看看窗外",
                gate_hints={}, meta_out=meta, activity_key="k1")
        self.assertEqual(result, ("查天气", "今天晴"))
        self.assertEqual(ask.call_count, 0)          # 无任何模型选择调用
        self.assertEqual(meta["activity_name"], "查天气")
        self.assertEqual(meta["thought_summary"], "想看看窗外")

    async def test_j_forced_light_activity_single_call(self):
        """forced 轻量活动（塔罗）：一次内容生成，prompt 固定活动，不做选择。"""
        ask = self._ask(ret="抽了张牌，心静了不少。")
        meta = {}
        result = await tool_loop.run_free_activity_tool_loop(
            None, ask, "sys_ctx", _NOW, avoid="", desire_hint="",
            forced_activity_id="free:tarot", selection_thought_summary="想抽张塔罗",
            gate_hints={}, meta_out=meta, activity_key="k1")
        self.assertEqual(result, ("抽张塔罗", "抽了张牌，心静了不少。"))
        self.assertEqual(ask.call_count, 1)
        args, _kw = ask.call_args
        self.assertIn("抽张塔罗", args[1])
        self.assertNotIn("activity", args[1])        # 不再要求模型输出 activity 字段
        self.assertEqual(meta["thought_summary"], "想抽张塔罗")

    async def test_k_forced_secret_diary_c4(self):
        """forced free:secret_diary：C4 路径，只写 home_private_diaries；正文不入 meta 日志。"""
        persist_calls = []

        def fake_persist(activity_key, content, now_bj, log_prefix):
            persist_calls.append({"activity_key": activity_key, "content": content})
            return True

        ask = self._ask(ret="这是绝不能进入行动日志的正文xyz")
        meta = {}
        with patch.object(tool_loop, "_load_recent_private_diaries", return_value=[]), \
             patch.object(tool_loop, "_persist_secret_diary", new=fake_persist):
            result = await tool_loop.run_free_activity_tool_loop(
                None, ask, "sys_ctx", _NOW, avoid="", desire_hint="",
                forced_activity_id="free:secret_diary", selection_thought_summary="想记一笔",
                gate_hints={}, meta_out=meta, activity_key="fa_k1")
        self.assertEqual(result[0], "写秘密日记")
        self.assertTrue(meta.get("diary_persist_ok"))
        # action_key 由 _persist_secret_diary 内部派生（diary_<activity_key>），桩收到原始 key
        self.assertEqual(persist_calls[0]["activity_key"], "fa_k1")
        self.assertEqual(meta["thought_summary"], "")   # C3 固定文案，正文不进 thought
        self.assertNotIn("这是绝不能进入行动日志的正文xyz", str(meta))

    async def test_k_diary_tick_writes_no_memories(self):
        """统一唤醒选秘密日记：不写 memories、一条 activity_logs、正文不入 finalize。"""
        h = _harness(free_on=True, home_on=False,
                     selection='{"activity_id": "free:secret_diary", "thought_summary": "想记一笔"}',
                     free_ret=("写秘密日记", "秘密正文abc"),
                     )
        # 手动把 free executor 的 meta 标记成 diary_persist_ok=True（模拟 C4 写入成功）
        orig = h._free_executor

        def free_executor(**kw):
            r = orig(**kw)
            if kw.get("meta_out") is not None:
                kw["meta_out"]["diary_persist_ok"] = True
            return r

        h._free_executor = free_executor
        await h.run()
        self.assertEqual(h.memory_calls, [])            # 不写 memories.Secret_Diary / Free_Activity
        fin = h.finalize_kwargs[0]
        self.assertEqual(fin["activity_id"], "free:secret_diary")
        self.assertEqual(fin["status"], "succeeded")
        self.assertNotIn("秘密正文abc", str(fin))

    async def test_forced_unknown_id_returns_none(self):
        """forced 未知 ID：不执行、返回 None（由调度器记 skipped）。"""
        ask = self._ask(ret="x")
        result = await tool_loop.run_free_activity_tool_loop(
            None, ask, "sys_ctx", _NOW, avoid="", desire_hint="",
            forced_activity_id="free:nonexistent", selection_thought_summary="",
            gate_hints={}, meta_out={}, activity_key="")
        self.assertIsNone(result)
        self.assertEqual(ask.call_count, 0)


# ============================================================
# L/M/N. Home 工具组边界（phase ∩ 工具组 ∩ 白名单）
# ============================================================
_EXPECTED_HOME_MATRIX = {
    "home:observe": {"home_observe", "garden_observe", "pantry_observe", "list_letters"},
    "home:letters": {"home_observe", "list_letters", "write_letter", "leave_note"},
    "home:garden": {"home_observe", "garden_observe", "plant_seed", "water_plant", "harvest_plant"},
    "home:kitchen": {"home_observe", "pantry_observe", "cook_recipe", "eat_dish", "feed_member"},
    "home:rest": {"home_observe", "home_enter_room", "home_rest", "home_sleep"},
    "home:social": {"home_observe", "home_spend_time"},
}


class TestHomeToolBoundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tool_loop._home_tool_last_fire.clear()
        tool_loop._home_tool_fail_count.clear()
        self._old_phase = tool_loop.HOME_AUTONOMY_PHASE
        self._old_rt = tool_loop._HAS_HOME_RUNTIME
        tool_loop.HOME_AUTONOMY_PHASE = 4
        tool_loop._HAS_HOME_RUNTIME = True

    def tearDown(self):
        tool_loop.HOME_AUTONOMY_PHASE = self._old_phase
        tool_loop._HAS_HOME_RUNTIME = self._old_rt
        tool_loop._home_tool_last_fire.clear()
        tool_loop._home_tool_fail_count.clear()

    @staticmethod
    def _avail_all(allowed_tools, obs, now):
        return [{"tool": t, "status": "available", "reason": "",
                 "cooldown_remaining_seconds": 0} for t in allowed_tools]

    async def _run(self, activity_id, plans, ct):
        ask = AsyncMock(side_effect=list(plans) + ["生活日记"])
        with patch.object(tool_loop, "_home_tool_availability", new=self._avail_all), \
             patch.object(tool_loop, "call_tool", new=ct):
            return await tool_loop.run_home_autonomy_tool_loop(
                None, ask, "sys_ctx", _NOW, activity_id=activity_id,
                allowed_tool_names=areg.get(activity_id)["home_tool_group"],
                selection_thought_summary="统一选择的念头", meta_out={})

    def test_n_matrix_matches_spec(self):
        """六个 Home ID 的工具组与 phase 工具集取交集 == 规格矩阵。"""
        phase4 = set(tool_loop._HOME_PHASE_TOOLS[4])
        for aid, expected in _EXPECTED_HOME_MATRIX.items():
            group = set(areg.get(aid)["home_tool_group"])
            self.assertEqual(group, expected, aid)
            self.assertEqual(group & phase4, expected, aid)

    async def test_l_garden_boundary_rejects_out_of_group(self):
        """home:garden：组内工具执行；cook_recipe/home_sleep 代码拒绝、不生成 action_key、
        不计 fail_count。"""
        ct = AsyncMock(return_value={"ok": True, "text": "ok", "raw": {"ok": True}})
        plans = ['{"done": false, "thought_summary": "浇个水", "tool_calls": ['
                 '{"name": "water_plant", "args": {"plant_id": "p-1"}},'
                 '{"name": "cook_recipe", "args": {"dish_name": "鱼"}},'
                 '{"name": "home_sleep", "args": {}}]}']
        result = await self._run("home:garden", plans, ct)
        called_names = [c[0][0] for c in ct.call_args_list]
        self.assertIn("water_plant", called_names)
        self.assertNotIn("cook_recipe", called_names)
        self.assertNotIn("home_sleep", called_names)
        # 组外拒绝：不生成 action_key、不计 fail_count
        for name, args in [(c[0][0], c[0][1]) for c in ct.call_args_list]:
            if name == "water_plant":
                self.assertTrue(args.get("action_key"))
        self.assertEqual(tool_loop._home_tool_fail_count.get("cook_recipe"), None)
        self.assertEqual(tool_loop._home_tool_fail_count.get("home_sleep"), None)
        self.assertEqual(result[1], ["water_plant"])

    async def test_l_garden_schema_scoped(self):
        ct = AsyncMock(return_value={"ok": True, "text": "ok", "raw": {"ok": True}})
        ask = AsyncMock(side_effect=[
            '{"done": true, "thought_summary": "看看", "tool_calls": []}', "生活日记"])
        with patch.object(tool_loop, "_home_tool_availability", new=self._avail_all), \
             patch.object(tool_loop, "call_tool", new=ct):
            await tool_loop.run_home_autonomy_tool_loop(
                None, ask, "sys_ctx", _NOW, activity_id="home:garden",
                allowed_tool_names=areg.get("home:garden")["home_tool_group"],
                meta_out={})
        prompt = ask.call_args_list[0].args[1]
        self.assertIn("water_plant", prompt)
        self.assertIn("harvest_plant", prompt)
        self.assertNotIn("cook_recipe", prompt)
        self.assertNotIn("home_sleep", prompt)
        self.assertNotIn("write_letter", prompt)

    async def test_m_kitchen_boundary(self):
        """home:kitchen：cook/eat/feed 可用；plant/water/sleep 不可用且被拒绝。"""
        ct = AsyncMock(return_value={"ok": True, "text": "ok", "raw": {"ok": True}})
        plans = ['{"done": false, "thought_summary": "做饭", "tool_calls": ['
                 '{"name": "cook_recipe", "args": {"dish_name": "鱼"}},'
                 '{"name": "water_plant", "args": {"plant_id": "p-1"}},'
                 '{"name": "home_sleep", "args": {}}]}']
        result = await self._run("home:kitchen", plans, ct)
        called_names = [c[0][0] for c in ct.call_args_list]
        self.assertIn("cook_recipe", called_names)
        self.assertNotIn("water_plant", called_names)
        self.assertNotIn("home_sleep", called_names)
        self.assertNotIn("plant_seed", called_names)
        self.assertEqual(result[1], ["cook_recipe"])

    async def test_n_letters_rest_social_observe(self):
        """letters/rest/social/observe 各自边界：组外工具一律拒绝。"""
        cases = [
            ("home:letters", "write_letter", "cook_recipe"),
            ("home:rest", "home_sleep", "water_plant"),
            ("home:social", "home_spend_time", "cook_recipe"),
            ("home:observe", "list_letters", "water_plant"),
        ]
        for aid, in_tool, out_tool in cases:
            ct = AsyncMock(return_value={"ok": True, "text": "ok", "raw": {"ok": True}})
            plans = [f'{{"done": false, "thought_summary": "试试", "tool_calls": ['
                     f'{{"name": "{in_tool}", "args": {{}}}},'
                     f'{{"name": "{out_tool}", "args": {{}}}}]}}']
            await self._run(aid, plans, ct)
            called = [c[0][0] for c in ct.call_args_list]
            self.assertIn(in_tool, called, aid)
            self.assertNotIn(out_tool, called, aid)


# ============================================================
# T. desire 映射
# ============================================================
def _fake_intent(want_action="explore", is_wildcard=False):
    return types.SimpleNamespace(want_action=want_action, drive_key="curiosity",
                                 score=0.62, reason="想找点新鲜东西看看",
                                 is_wildcard=is_wildcard)


def _fake_snap(intent):
    return types.SimpleNamespace(intent=intent, driven=True, refractory={},
                                 drive={}, display={})


class TestDesireMapping(unittest.IsolatedAsyncioTestCase):
    async def _run_with_desire(self, gated, suggest, selection,
                               satisfy_recorder=None):
        h = _harness(free_on=True, home_on=False, gated=gated,
                     selection=selection, emotion_on=True)
        fake_intent = _fake_intent()
        fake_snap = _fake_snap(fake_intent)
        satisfy_mock = MagicMock(side_effect=satisfy_recorder or (lambda a: h.satisfy_calls.append(a)))
        with patch("desire_bridge.tick", return_value=fake_snap), \
             patch("desire_bridge.suggest_free_activity", return_value=suggest), \
             patch("desire_bridge.satisfy_action", new=satisfy_mock):
            await h.run()
        return h, fake_intent

    async def test_t_suggestion_mapped_and_kept(self):
        """旧中文建议映射为 activity_id 且在候选中 → 注入 Prompt。"""
        h, _intent = await self._run_with_desire(
            _all_free_names(), "网上冲浪",
            '{"activity_id": "free:web_surf", "thought_summary": "搜搜看"}')
        prompt = h.selection_prompts[0]
        self.assertIn("free:web_surf", prompt)
        self.assertIn("网上冲浪", prompt)

    async def test_t_suggestion_not_in_candidates_dropped(self):
        """建议不在本轮候选（被门控裁掉）→ 丢弃 hint，不绕过门控。"""
        gated = _all_free_names() - {"网上冲浪"}
        h, _intent = await self._run_with_desire(
            gated, "网上冲浪",
            '{"activity_id": "free:weather", "thought_summary": "看看天"}')
        prompt = h.selection_prompts[0]
        self.assertNotIn("优先考虑「网上冲浪」", prompt)

    async def test_t_satisfy_uses_want_action(self):
        """satisfy 仍用 want_action（英文欲望动作，不改成 activity_id）。"""
        recorded = []
        h, intent = await self._run_with_desire(
            _all_free_names(), "网上冲浪",
            '{"activity_id": "free:web_surf", "thought_summary": "搜搜看"}',
            satisfy_recorder=recorded.append)
        self.assertEqual(recorded, [intent.want_action])


# ============================================================
# U. 防重复（activity_id）
# ============================================================
class TestDedup(unittest.IsolatedAsyncioTestCase):
    def test_u_row_to_id_legacy_ids(self):
        """旧行 id 不在注册表时按名称映射（想对方了 → outgoing:miss_user）。"""
        row = {"activity_id": "free:outgoing_missing", "activity_name": "想对方了",
               "started_at": "2026-09-01T01:00:00+00:00", "status": "succeeded"}
        self.assertEqual(heartbeat._activity_row_to_id(row), "outgoing:miss_user")
        row2 = {"activity_id": "home:autonomy", "activity_name": "家庭自主生活"}
        self.assertEqual(heartbeat._activity_row_to_id(row2), "home:autonomy")

    def test_u_merge_ids_window_dedup(self):
        log_rows = [{"activity_id": "free:tarot", "activity_name": "抽张塔罗",
                     "started_at": "2026-09-01T01:00:00+00:00", "status": "succeeded"}]
        memory_rows = [{"title": "🎈 自由活动·抽张塔罗",
                        "created_at": "2026-09-01T01:05:00+00:00"}]
        merged = heartbeat._merge_recent_activity_ids(log_rows, memory_rows, limit=2)
        self.assertEqual(merged, ["free:tarot"])   # 同一活动 15 分钟窗口内去重

    def test_u_merge_memories_fallback(self):
        memory_rows = [
            {"title": "🎈 自由活动·查天气", "created_at": "2026-09-01T01:00:00+00:00"},
            {"title": "🎈 自由活动·抽张塔罗", "created_at": "2026-08-31T23:00:00+00:00"},
            {"title": "🔒 秘密日记·随手记", "created_at": "2026-08-31T22:00:00+00:00"},
        ]
        merged = heartbeat._merge_recent_activity_ids([], memory_rows, limit=2)
        # 旧名可映射的进结果；解析不了的（如旧秘密日记日期标题）自然丢弃
        self.assertEqual(merged, ["free:weather", "free:tarot"])

    async def test_u_recent_same_id_excluded(self):
        """最近两次同 activity_id → 本轮候选排除该 ID。"""
        h = _harness(free_on=True, home_on=False,
                     recent_ids=["free:tarot", "free:tarot"])
        await h.run()
        prompt = h.selection_prompts[0]
        self.assertNotIn("free:tarot「抽张塔罗」", prompt)
        self.assertIn("free:weather", prompt)

    async def test_u_exclusion_empties_candidates_skips(self):
        """排除后无候选 → 本轮 skipped，不从全量被门控候选随机兜底。"""
        h = _harness(free_on=False, home_on=True, home_phase=1,
                     recent_ids=["home:observe", "home:observe"])
        await h.run()
        self.assertEqual(h.seq, [])
        self.assertEqual(h.selection_prompts, [])

    def test_u_get_recent_completed_activities_filters(self):
        """新只读函数：三 source 兼容、仅 succeeded/partial、failed/skipped 不计。"""
        store = {"activity_logs": [
            {"activity_key": "k1", "source": "unified_autonomy", "status": "succeeded",
             "activity_id": "free:tarot", "activity_name": "抽张塔罗",
             "started_at": "2026-09-01T01:00:00+00:00"},
            {"activity_key": "k2", "source": "free_activity", "status": "partial",
             "activity_id": "free:weather", "activity_name": "查天气",
             "started_at": "2026-09-01T02:00:00+00:00"},
            {"activity_key": "k3", "source": "free_activity", "status": "failed",
             "activity_id": "free:idle", "activity_name": "发呆放空",
             "started_at": "2026-09-01T03:00:00+00:00"},
            {"activity_key": "k4", "source": "home_autonomy", "status": "skipped",
             "activity_id": "home:kitchen", "activity_name": "做饭和用餐",
             "started_at": "2026-09-01T04:00:00+00:00"},
            {"activity_key": "k5", "source": "home_autonomy", "status": "succeeded",
             "activity_id": "home:garden", "activity_name": "照料花园",
             "started_at": "2026-09-01T05:00:00+00:00"},
        ]}

        class _FakeTable:
            def __init__(self):
                self._eq = {}
                self._in = {}

            def select(self, cols):
                return self

            def in_(self, col, vals):
                self._in[col] = vals
                return self

            def eq(self, col, val):
                self._eq[col] = val
                return self

            def order(self, *a, **k):
                return self

            def limit(self, n):
                return self

            def execute(self):
                class _R:
                    data = [dict(r) for r in store["activity_logs"]
                            if all(r.get(k) == v for k, v in self._eq.items())
                            and all(r.get(k) in v for k, v in self._in.items())]
                return _R()

        class _FakeSB:
            def table(self, name):
                return _FakeTable()

        with patch.object(alog, "_get_service_client", return_value=_FakeSB()):
            rows = alog.get_recent_completed_activities(limit=4)
        ids = {r["activity_id"] for r in rows}
        self.assertIn("free:tarot", ids)
        self.assertIn("free:weather", ids)
        self.assertIn("home:garden", ids)
        self.assertNotIn("free:idle", ids)       # failed 不计
        self.assertNotIn("home:kitchen", ids)    # skipped 不计


# ============================================================
# start_activity_log source 校验（unified_autonomy）
# ============================================================
class TestSourceValidation(unittest.TestCase):
    def test_start_accepts_unified_autonomy(self):
        class _FakeTable:
            def select(self, cols):
                return self

            def eq(self, col, val):
                return self

            def limit(self, n):
                return self

            def execute(self):
                class _R:
                    data = []
                return _R()

            def insert(self, payload):
                self.payload = payload
                return self

        class _FakeSB:
            def table(self, name):
                return _FakeTable()

        with patch.object(alog, "_get_service_client", return_value=_FakeSB()):
            r = alog.start_activity_log("uni_test_1", "unified_autonomy")
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("created"))
        with patch.object(alog, "_get_service_client", return_value=_FakeSB()):
            r2 = alog.start_activity_log("uni_test_2", "bogus_source")
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["error_code"], "INVALID_SOURCE")


# ============================================================
# O. 只启动一个顶层任务
# ============================================================
class TestSingleTopLevelTask(unittest.TestCase):
    def test_o_run_background_process_single_autonomy(self):
        src = inspect.getsource(heartbeat.run_background_process)
        self.assertIn("async_unified_autonomy", src)
        self.assertNotIn("async_free_activity()", src)
        self.assertNotIn("async_home_autonomy_tick()", src)
        self.assertIn("async_pet_house_tick", src)

    def test_o_old_loops_are_compat_entries(self):
        """旧函数保留为兼容入口（不再被主进程调度）。"""
        self.assertTrue(inspect.iscoroutinefunction(heartbeat.async_free_activity))
        self.assertTrue(inspect.iscoroutinefunction(heartbeat.async_home_autonomy_tick))
        self.assertIn("C5 兼容入口", heartbeat.async_free_activity.__doc__)
        self.assertIn("C5 兼容入口", heartbeat.async_home_autonomy_tick.__doc__)


# ============================================================
# Z. 环境变量间隔
# ============================================================
class TestIntervalRules(unittest.TestCase):
    def setUp(self):
        self._old_dyn = heartbeat._dynamic_heartbeat_secs

    def tearDown(self):
        heartbeat._dynamic_heartbeat_secs = self._old_dyn

    def _run(self, free_on, home_on, dyn):
        heartbeat._dynamic_heartbeat_secs = (lambda: dyn) if dyn is not None else (lambda: None)
        return heartbeat._unified_interval_secs(free_on, home_on)

    def test_z_both_off_none(self):
        self.assertIsNone(self._run(False, False, None))
        self.assertIsNone(self._run(False, False, 1234))

    def test_z_home_only_fixed_interval(self):
        with patch.dict(os.environ, {"HOME_AUTONOMY_INTERVAL": "7200"}):
            self.assertEqual(self._run(False, True, 1234), 7200)  # HOME-only 不用动态心跳

    def test_z_free_only_dynamic(self):
        self.assertEqual(self._run(True, False, 1234), 1234)

    def test_z_free_only_fallback_jitter(self):
        with patch.dict(os.environ, {"FREE_ACTIVITY_INTERVAL": "5400"}):
            v = self._run(True, False, None)
        self.assertGreaterEqual(v, 300)
        self.assertLessEqual(v, 5400 + 900)

    def test_z_both_dynamic(self):
        self.assertEqual(self._run(True, True, 1234), 1234)

    def test_z_both_min_interval(self):
        with patch.dict(os.environ, {"FREE_ACTIVITY_INTERVAL": "5400",
                                     "HOME_AUTONOMY_INTERVAL": "7200"}):
            v = self._run(True, True, None)
        self.assertGreaterEqual(v, 300)
        self.assertLessEqual(v, 5400 + 900)   # min(5400, 7200) + 抖动

    def test_z_home_only_no_free_dependency(self):
        """HOME-only 不依赖 FREE_ACTIVITY_ENABLED 语义。"""
        with patch.dict(os.environ, {"HOME_AUTONOMY_INTERVAL": "3600",
                                     "FREE_ACTIVITY_INTERVAL": "999999"}):
            self.assertEqual(self._run(False, True, None), 3600)


if __name__ == "__main__":
    unittest.main()
