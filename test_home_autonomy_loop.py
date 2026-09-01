"""
test_home_autonomy_loop.py — 阶段 C2：Home 多轮工具执行与真实结果约束测试
==========================================================================
覆盖目标：
- A/B. plant_id/dish_id 保真（不再被 300 字文本截断丢失，不从 text 解析）
- C/D. 多轮"观察→执行→回传→再决策"，总轮数与总调用有限
- E/F. 冷却/熔断在规划前可见，模型调用仍被代码拒绝
- G.   前置缺失/状态未知可区分，查询失败不解释为"没有资源"
- H/I/J. raw.ok 业务成功判定：失败不加冷却、加 fail_count、不进 tools_used；
         成功清零 fail_count、更新 last_fire、进 tools_used；结构异常保守失败
- K/L/M. 空 tool_calls / 全失败 / JSON 解析失败的日志约束
- N/O.  MAX_TOOL_CALLS 跨轮累计预算 + _HOME_MAX_DECISION_ROUNDS 最大轮数
- P/Q.  单次运行内相同成功写操作去重，不同对象不误拦
- R.    phase 1-4 工具边界保持

纯 mock 测试，不触生产数据/数据库。
运行：python -m unittest test_home_autonomy_loop -v
"""

import datetime
import time
import unittest
from unittest.mock import AsyncMock, patch

import tool_loop


# ============================================================
# 观察工具标准 raw（与 home/service.py 返回结构一致）
# ============================================================
_ROOMS = [{"stable_key": "living_room", "name": "客厅", "emoji": "",
           "room_type": "", "description": ""}]
_MEMBERS = [
    {"stable_key": "ai_primary", "name": "Finn", "member_type": "ai",
     "lifecycle_status": "active", "current_room_name": "客厅"},
    {"stable_key": "pet_xiaoman", "name": "小满", "member_type": "pet",
     "lifecycle_status": "active", "current_room_name": "客厅"},
]


def _plant(pid, name="草莓", stage="seedling", mature=False):
    return {"id": pid, "name": name, "seed_key": "strawberry", "stage": stage,
            "health": 90, "water_level": 50, "status": "growing",
            "is_mature": mature, "planted_at": "2026-08-30"}


def _dish(did, name="薄荷茶", servings=1):
    return {"id": did, "name": name, "servings": servings, "quality": 4}


def _home_raw(rooms=None, members=None):
    return {"ok": True, "message": "家庭观察完成",
            "data": {"rooms": _ROOMS if rooms is None else rooms,
                     "members": _MEMBERS if members is None else members,
                     "recent_events": [], "pending_jobs_count": 0}}


def _garden_raw(plants=None, seeds=None):
    return {"ok": True, "message": "花园观察完成",
            "data": {"plants": [_plant("p-1")] if plants is None else plants,
                     "available_seeds": [{"stable_key": "strawberry", "name": "草莓"}]
                     if seeds is None else seeds,
                     "recent_events": []}}


def _pantry_raw(dishes=None, recipes=None):
    return {"ok": True, "message": "库存观察完成",
            "data": {"inventory": [],
                     "dishes": [_dish("d-1")] if dishes is None else dishes,
                     "available_recipes": [{"stable_key": "mint_tea", "name": "薄荷茶"}]
                     if recipes is None else recipes}}


def _letters_raw():
    return {"ok": True, "message": "信件列表",
            "data": {"letters": [{"letter_key": "l-1", "title": "你好", "preview": "",
                                  "status": "unopened", "is_unopened": True,
                                  "created_at": None}], "count": 1}}


def _err_raw(code, msg="失败"):
    return {"ok": False, "message": msg, "error_code": code}


_OBS_DEFAULTS = {"home_observe": _home_raw, "garden_observe": _garden_raw,
                 "pantry_observe": _pantry_raw, "list_letters": _letters_raw}


class _FakeCallTool:
    """call_tool 假体：观察工具默认返回标准成功 raw；按名出队计划响应。"""

    def __init__(self, plan=None):
        self.plan = plan or {}
        self.calls = []

    async def __call__(self, name, args=None):
        self.calls.append((name, dict(args or {})))
        queue = self.plan.get(name)
        if not queue:
            factory = _OBS_DEFAULTS.get(name)
            payload = factory() if factory else {"ok": True, "message": "操作成功"}
            return {"ok": True, "text": "成功", "raw": payload}
        kind, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        if kind == "fail":
            return {"ok": False, "text": payload}
        if kind == "bare":
            return {"ok": True, "text": payload}
        return {"ok": True, "text": payload.get("_text", "成功"), "raw": payload}


def _make_ask(plans, log_text="在家的一天"):
    """按调用序号返回：前 len(plans) 次为规划 JSON，之后为最终日志文本。"""
    state = {"n": 0, "prompts": []}

    async def ask(client, prompt, system_prompt="", temperature=0.7):
        state["n"] += 1
        state["prompts"].append(prompt)
        if state["n"] <= len(plans):
            return plans[state["n"] - 1]
        return log_text

    return ask, state


def _now():
    return datetime.datetime(2026, 8, 31, 10, 0)


def _called(ct, name):
    return [args for n, args in ct.calls if n == name]


class _HomeLoopCase(unittest.IsolatedAsyncioTestCase):
    """公共运行器：干净冷却/熔断状态 + 可编程 phase/MAX_TOOL_CALLS。

    no_cooldown=True 时将 _HOME_TOOL_COOLDOWN 置空（测试隔离生产冷却默认值，
    用于同工具跨轮多次调用场景 N/O/P/Q；不修改生产默认值）。
    """

    async def run_loop(self, ct, ask, phase=4, max_calls=None,
                       last_fire=None, fail_count=None, no_cooldown=False):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(tool_loop, "HOME_AUTONOMY_PHASE", phase))
            stack.enter_context(patch.object(tool_loop, "call_tool", ct))
            stack.enter_context(patch.object(tool_loop, "MAX_TOOL_CALLS",
                                             max_calls or tool_loop.MAX_TOOL_CALLS))
            stack.enter_context(patch.dict(tool_loop._home_tool_last_fire,
                                           last_fire or {}, clear=True))
            stack.enter_context(patch.dict(tool_loop._home_tool_fail_count,
                                           fail_count or {}, clear=True))
            if no_cooldown:
                stack.enter_context(patch.object(tool_loop, "_HOME_TOOL_COOLDOWN", {}))
            result = await tool_loop.run_home_autonomy_tool_loop(
                object(), ask, "sys_ctx", _now())
            # patch.dict 退出会恢复模块状态，执行后快照供断言使用
            self._state_after = {
                "last_fire": dict(tool_loop._home_tool_last_fire),
                "fail_count": dict(tool_loop._home_tool_fail_count),
            }
        return result


# ============================================================
# A/B. ID 保真
# ============================================================
class TestIdPreservation(_HomeLoopCase):
    async def test_a_plant_id_beyond_old_truncation(self):
        """A：目标 plant_id 位于旧 300 字截断之外 → 上下文仍完整，water 用真实 ID。"""
        target = "p-target-9f8e7d6c-aaaa-bbbb-cccc-dddddddddddd"
        filler = "很长的植物名字占位" * 20
        plants = [_plant(f"p-fill{i}", name=filler) for i in range(3)] + [_plant(target)]
        ct = _FakeCallTool({"garden_observe": [("raw", _garden_raw(plants=plants))],
                            "water_plant": [("raw", {"ok": True, "message": "浇水完成"})]})
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "%s"}}]}' % target,
            '{"done": true, "tool_calls": []}',
        ])
        result = await self.run_loop(ct, ask, phase=4)
        first_prompt = state["prompts"][0]
        # 旧实现的 text[:300] 会丢掉 target——结构化视图必须完整保留
        self.assertIn(target, first_prompt)
        wp = _called(ct, "water_plant")
        self.assertEqual(len(wp), 1)
        self.assertEqual(wp[0]["plant_id"], target)
        self.assertEqual(result[1], ["water_plant"])

    async def test_b_dish_id_beyond_old_truncation(self):
        """B：目标 dish_id 位于旧截断之外 → feed_member 用真实 dish_id。"""
        target = "d-target-11223344-5566-7788-aabb-ccddeeff0011"
        dishes = [_dish(f"d-fill{i}", name="很长的菜名占位" * 20) for i in range(3)] + [_dish(target)]
        ct = _FakeCallTool({"pantry_observe": [("raw", _pantry_raw(dishes=dishes))],
                            "feed_member": [("raw", {"ok": True, "message": "已喂食"})]})
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "feed_member", "args": '
            '{"target_key": "pet_xiaoman", "dish_id": "%s"}}]}' % target,
            '{"done": true, "tool_calls": []}',
        ])
        result = await self.run_loop(ct, ask, phase=4)
        self.assertIn(target, state["prompts"][0])
        fm = _called(ct, "feed_member")
        self.assertEqual(len(fm), 1)
        self.assertEqual(fm[0]["dish_id"], target)
        self.assertEqual(fm[0]["target_key"], "pet_xiaoman")
        self.assertTrue(fm[0]["action_key"].startswith("auto_feed_member_"))
        self.assertEqual(result[1], ["feed_member"])

    async def test_ab_not_parsing_ids_from_text(self):
        """A/B：观察 text 为垃圾时逻辑仍以 raw 为准（不从 text 解析 UUID）。"""
        target = "p-target-99998888-7777-6666-5555-444433332222"
        plants = [_plant("p-x", name="占位" * 200), _plant(target)]
        ct = _FakeCallTool({"garden_observe": [("raw", dict(_garden_raw(plants=plants), _text="成功"))],
                            "water_plant": [("raw", {"ok": True, "message": "浇水完成"})]})
        ask, _ = _make_ask([
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "%s"}}]}' % target,
            '{"done": true, "tool_calls": []}',
        ])
        result = await self.run_loop(ct, ask, phase=4)
        self.assertEqual(_called(ct, "water_plant")[0]["plant_id"], target)
        self.assertEqual(result[1], ["water_plant"])


# ============================================================
# C/D. 多轮观察后执行
# ============================================================
class TestMultiRound(_HomeLoopCase):
    async def test_c_observe_then_act_then_done(self):
        """C：第1轮刷新 garden_observe → 第2轮 water_plant → 第3轮 done。"""
        ct = _FakeCallTool({
            "garden_observe": [("raw", _garden_raw(plants=[_plant("p-1")])),
                               ("raw", _garden_raw(plants=[_plant("p-1")]))],
            "water_plant": [("raw", {"ok": True, "message": "浇水完成"})],
        })
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "garden_observe", "args": {}}]}',
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-1"}}]}',
            '{"done": true, "tool_calls": []}',
        ])
        result = await self.run_loop(ct, ask, phase=4)
        names = [n for n, _ in ct.calls]
        self.assertEqual(names, ["home_observe", "garden_observe", "pantry_observe",
                                 "list_letters", "garden_observe", "water_plant"])
        self.assertEqual(state["n"], 4)  # 3 次决策（含最终 done 轮）+ 1 次日志
        # 第二轮决策 prompt 含刷新后的观察（第 2 个 prompt 是第 2 轮规划）
        self.assertIn("p-1", state["prompts"][1])
        self.assertEqual(result[1], ["water_plant"])

    async def test_d_harvest_then_refresh_pantry_then_cook(self):
        """D：harvest → pantry_observe 刷新 → cook_recipe，全部真实成功且轮数有限。"""
        ct = _FakeCallTool({
            "garden_observe": [("raw", _garden_raw(plants=[_plant("p-1", stage="mature", mature=True)]))],
            "harvest_plant": [("raw", {"ok": True, "message": "收获了3个草莓"})],
            "pantry_observe": [("raw", _pantry_raw()),
                               ("raw", _pantry_raw(dishes=[_dish("d-9", servings=1)],
                                                   recipes=[{"stable_key": "mint_tea", "name": "薄荷茶"}]))],
            "cook_recipe": [("raw", {"ok": True, "message": "做了薄荷茶"})],
        })
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "harvest_plant", "args": {"plant_id": "p-1"}}]}',
            '{"done": false, "tool_calls": [{"name": "pantry_observe", "args": {}}]}',
            '{"done": false, "tool_calls": [{"name": "cook_recipe", "args": {"recipe_key": "mint_tea"}}]}',
        ])
        result = await self.run_loop(ct, ask, phase=4)
        self.assertEqual(result[1], ["harvest_plant", "cook_recipe"])  # 观察不算
        self.assertEqual(len(_called(ct, "harvest_plant")), 1)
        self.assertEqual(len(_called(ct, "cook_recipe")), 1)
        self.assertEqual(state["n"], 4)  # 3 轮决策（达上限）+ 1 次日志


# ============================================================
# E/F. 冷却与熔断可见 + 强制拒绝
# ============================================================
class TestVisibilityAndEnforcement(_HomeLoopCase):
    async def test_e_cooldown_visible_and_enforced(self):
        """E：冷却在规划 prompt 可见；模型调用也被拒绝，不产生 action_key/fail_count。"""
        ct = _FakeCallTool({"cook_recipe": [("raw", {"ok": True, "message": "不应被执行"})]})
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "cook_recipe", "args": {"recipe_key": "mint_tea"}}]}',
            '{"done": true, "tool_calls": []}',
        ])
        injected = time.time() - 10  # 10 秒前刚成功 → 冷却中
        result = await self.run_loop(ct, ask, phase=3,
                                     last_fire={"cook_recipe": injected})
        first_prompt = state["prompts"][0]
        self.assertIn("cooldown", first_prompt)
        self.assertIn("cook_recipe", first_prompt)
        self.assertEqual(_called(ct, "cook_recipe"), [])  # 未执行
        self.assertEqual(result[1], [])
        self.assertEqual(self._state_after["fail_count"].get("cook_recipe", 0), 0)
        # last_fire 保持注入值不变（冷却跳过不更新成功时间戳）
        self.assertEqual(self._state_after["last_fire"].get("cook_recipe"), injected)

    async def test_f_breaker_visible_and_enforced(self):
        """F：fail_count 达阈值 → breaker_open；模型调用被拒绝。"""
        ct = _FakeCallTool({"water_plant": [("raw", {"ok": True, "message": "不应被执行"})]})
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-1"}}]}',
            '{"done": true, "tool_calls": []}',
        ])
        result = await self.run_loop(ct, ask, phase=3,
                                     fail_count={"water_plant": tool_loop._HOME_BREAKER_THRESHOLD})
        self.assertIn("breaker_open", state["prompts"][0])
        self.assertEqual(_called(ct, "water_plant"), [])
        self.assertEqual(result[1], [])
        self.assertNotIn("water_plant", self._state_after["last_fire"])
        self.assertEqual(self._state_after["fail_count"].get("water_plant"),
                         tool_loop._HOME_BREAKER_THRESHOLD)  # 不增加


# ============================================================
# G. 前置缺失与状态未知
# ============================================================
class TestPrerequisites(_HomeLoopCase):
    async def test_g_garden_fail_marks_unknown_and_rejects(self):
        """G：garden_observe 查询失败 → water/harvest 状态未知（非"没有资源"），调用被拒。"""
        ct = _FakeCallTool({"garden_observe": [("raw", _err_raw("RPC_ERROR"))],
                            "harvest_plant": [("raw", {"ok": True, "message": "不应执行"})]})
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "harvest_plant", "args": {"plant_id": "p-1"}}]}',
            '{"done": true, "tool_calls": []}',
        ])
        result = await self.run_loop(ct, ask, phase=3)
        first_prompt = state["prompts"][0]
        self.assertIn("status_unknown", first_prompt)
        self.assertNotIn("确认没有植物", first_prompt)
        self.assertEqual(_called(ct, "harvest_plant"), [])
        self.assertEqual(result[1], [])

    async def test_g2_no_mature_plant_missing_prerequisite(self):
        """G：garden 成功但无成熟植物 → harvest missing_prerequisite，调用被拒。"""
        ct = _FakeCallTool({"garden_observe": [("raw", _garden_raw(plants=[_plant("p-1", stage="seedling")]))],
                            "harvest_plant": [("raw", {"ok": True, "message": "不应执行"})]})
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "harvest_plant", "args": {"plant_id": "p-1"}}]}',
            '{"done": true, "tool_calls": []}',
        ])
        result = await self.run_loop(ct, ask, phase=3)
        self.assertIn("missing_prerequisite", state["prompts"][0])
        self.assertEqual(_called(ct, "harvest_plant"), [])
        self.assertEqual(result[1], [])

    async def test_g3_pantry_fail_unknown_no_dish_confirm(self):
        """G：pantry 查询失败 → eat/feed 状态未知，不得解释为"确认没有菜"，调用被拒。"""
        ct = _FakeCallTool({"pantry_observe": [("raw", _err_raw("RPC_ERROR"))],
                            "eat_dish": [("raw", {"ok": True, "message": "不应执行"})]})
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "eat_dish", "args": {"dish_id": "d-1"}}]}',
            '{"done": true, "tool_calls": []}',
        ])
        result = await self.run_loop(ct, ask, phase=3)
        first_prompt = state["prompts"][0]
        self.assertIn("status_unknown", first_prompt)
        self.assertNotIn("暂无可食用菜品", first_prompt)
        self.assertEqual(_called(ct, "eat_dish"), [])
        self.assertEqual(result[1], [])


# ============================================================
# H/I/J. raw 业务成功判定
# ============================================================
class TestBusinessOkSemantics(_HomeLoopCase):
    async def test_h_raw_ok_false_is_failure(self):
        """H：外层 ok=True 但 raw.ok=false → 业务失败：不加冷却、fail_count+1、不进 tools_used。"""
        ct = _FakeCallTool({"water_plant": [("raw", _err_raw("PLANT_NOT_FOUND"))]})
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-1"}}]}',
            '{"done": true, "tool_calls": []}',
        ])
        result = await self.run_loop(ct, ask, phase=3, fail_count={"water_plant": 1})
        self.assertEqual(result[1], [])
        self.assertEqual(self._state_after["fail_count"].get("water_plant"), 2)
        self.assertNotIn("water_plant", self._state_after["last_fire"])
        prompt = state["prompts"][-1]  # 日志 prompt
        self.assertIn("PLANT_NOT_FOUND", prompt)
        self.assertIn("has_successful_write: false", prompt)

    async def test_i_raw_ok_true_updates_state(self):
        """I：raw.ok=true → 更新 last_fire、清零 fail_count、进 tools_used。"""
        ct = _FakeCallTool({"water_plant": [("raw", {"ok": True, "message": "浇水完成"})]})
        ask, _ = _make_ask([
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-1"}}]}',
            '{"done": true, "tool_calls": []}',
        ])
        result = await self.run_loop(ct, ask, phase=3, fail_count={"water_plant": 2})
        self.assertEqual(result[1], ["water_plant"])
        self.assertIn("water_plant", self._state_after["last_fire"])
        self.assertEqual(self._state_after["fail_count"].get("water_plant"), 0)

    async def test_j_abnormal_raw_counts_as_failure(self):
        """J：raw=None / raw={} / 无 ok 字段 → 保守失败：fail_count+1、不进 tools_used。"""
        for label, item in (("raw_none", ("bare", "成功")),
                            ("raw_empty", ("raw", {})),
                            ("no_ok_field", ("raw", {"message": "完成"}))):
            with self.subTest(case=label):
                ct = _FakeCallTool({"water_plant": [item]})
                ask, _ = _make_ask([
                    '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-1"}}]}',
                    '{"done": true, "tool_calls": []}',
                ])
                result = await self.run_loop(ct, ask, phase=3)
                self.assertEqual(result[1], [])
                self.assertEqual(self._state_after["fail_count"].get("water_plant"), 1)
                self.assertNotIn("water_plant", self._state_after["last_fire"])


# ============================================================
# K/L/M. 空调用 / 全失败 / 解析失败 的日志约束
# ============================================================
class TestLogConstraints(_HomeLoopCase):
    async def test_k_empty_tool_calls_observation_only(self):
        """K：done=true + 空 tool_calls → 无写调用、tools_used=[]、观察型日志。"""
        ct = _FakeCallTool()
        ask, state = _make_ask(['{"done": true, "tool_calls": []}'], log_text="")
        result = await self.run_loop(ct, ask, phase=4)
        log_text, tools_used = result
        self.assertEqual(tools_used, [])
        self.assertEqual([n for n, _ in ct.calls],
                         ["home_observe", "garden_observe", "pantry_observe", "list_letters"])
        for bad in ("浇了", "做饭", "收获", "喂了", "写信", "休息"):
            self.assertNotIn(bad, log_text)
        self.assertIn("没有执行具体操作", log_text)
        self.assertIn("has_successful_write: false", state["prompts"][-1])

    async def test_l_all_writes_fail_no_success_log(self):
        """L：写操作全部业务失败 → tools_used=[]，兜底日志不含成功表达。"""
        ct = _FakeCallTool({"water_plant": [("raw", _err_raw("PLANT_NOT_FOUND")),
                                            ("raw", _err_raw("PLANT_NOT_FOUND")),
                                            ("raw", _err_raw("PLANT_NOT_FOUND"))]})
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-1"}}]}',
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-2"}}]}',
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-3"}}]}',
        ], log_text="")
        result = await self.run_loop(ct, ask, phase=3,
                                     fail_count={"water_plant": 0})
        log_text, tools_used = result
        self.assertEqual(tools_used, [])
        self.assertEqual(len(_called(ct, "water_plant")), 3)
        for bad in ("浇了水", "已经浇水", "已经做饭", "已经收获"):
            self.assertNotIn(bad, log_text)
        self.assertIn("没有成功", log_text)
        self.assertIn("has_successful_write: false", state["prompts"][-1])

    async def test_m_json_parse_fail_safe_stop(self):
        """M：规划 JSON 解析失败 → 不执行工具、有限停止、观察型兜底日志、不抛异常。"""
        ct = _FakeCallTool()
        ask, _ = _make_ask(["这不是JSON{{{"], log_text="")
        result = await self.run_loop(ct, ask, phase=4)
        self.assertIsNotNone(result)
        log_text, tools_used = result
        self.assertEqual(tools_used, [])
        self.assertEqual(len([n for n, _ in ct.calls if n in tool_loop._HOME_WRITE_TOOLS]), 0)
        for bad in ("浇了", "做饭", "收获"):
            self.assertNotIn(bad, log_text)
        self.assertIn("没有执行具体操作", log_text)


# ============================================================
# N/O. 预算与轮数上限
# ============================================================
class TestBudgetAndRounds(_HomeLoopCase):
    async def test_n_total_calls_capped_across_rounds(self):
        """N：跨轮累计不超过 MAX_TOOL_CALLS（不是每轮各自 5 个）。"""
        plants = [_plant(f"p-{i}") for i in range(1, 7)]
        ct = _FakeCallTool({"garden_observe": [("raw", _garden_raw(plants=plants))],
                            "water_plant": [("raw", {"ok": True, "message": "浇水完成"})]})
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-1"}},'
            ' {"name": "water_plant", "args": {"plant_id": "p-2"}},'
            ' {"name": "water_plant", "args": {"plant_id": "p-3"}}]}',
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-4"}},'
            ' {"name": "water_plant", "args": {"plant_id": "p-5"}},'
            ' {"name": "water_plant", "args": {"plant_id": "p-6"}}]}',
        ])
        result = await self.run_loop(ct, ask, phase=3, max_calls=4, no_cooldown=True)
        self.assertEqual(len(_called(ct, "water_plant")), 4)  # 跨轮累计 4
        self.assertEqual(state["n"], 3)  # 2 轮决策 + 1 日志
        self.assertEqual(len(result[1]), 4)

    async def test_o_max_decision_rounds(self):
        """O：模型持续返回调用 → 最多 _HOME_MAX_DECISION_ROUNDS 轮后停止。"""
        plants = [_plant(f"p-{i}") for i in range(1, 6)]
        ct = _FakeCallTool({"garden_observe": [("raw", _garden_raw(plants=plants))],
                            "water_plant": [("raw", {"ok": True, "message": "浇水完成"})]})
        plans = ['{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-%d"}}]}' % i
                 for i in range(1, 7)]
        ask, state = _make_ask(plans)
        result = await self.run_loop(ct, ask, phase=3, no_cooldown=True)
        self.assertEqual(state["n"], tool_loop._HOME_MAX_DECISION_ROUNDS + 1)  # 3 决策 + 1 日志
        self.assertEqual(len(_called(ct, "water_plant")), tool_loop._HOME_MAX_DECISION_ROUNDS)


# ============================================================
# P/Q. 单次运行内重复写操作去重
# ============================================================
class TestDuplicateWrites(_HomeLoopCase):
    async def test_p_duplicate_write_skipped(self):
        """P：同一 plant_id 成功后再次出现相同调用 → 不执行、不生成 action_key。"""
        ct = _FakeCallTool({"water_plant": [("raw", {"ok": True, "message": "浇水完成"})]})
        ask, _ = _make_ask([
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-1"}}]}',
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-1"}}]}',
        ])
        result = await self.run_loop(ct, ask, phase=3, no_cooldown=True)
        self.assertEqual(len(_called(ct, "water_plant")), 1)  # 第二次未执行
        self.assertEqual(result[1], ["water_plant"])          # tools_used 不重复

    async def test_q_different_targets_not_blocked(self):
        """Q：不同 plant_id 不被去重拦截（测试隔离冷却状态）。"""
        ct = _FakeCallTool({"water_plant": [("raw", {"ok": True, "message": "浇水完成"}),
                                            ("raw", {"ok": True, "message": "浇水完成"})]})
        ask, _ = _make_ask([
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-1"}}]}',
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-2"}}]}',
        ])
        result = await self.run_loop(ct, ask, phase=3, no_cooldown=True)
        calls = _called(ct, "water_plant")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["plant_id"], "p-1")
        self.assertEqual(calls[1]["plant_id"], "p-2")
        self.assertEqual(result[1], ["water_plant", "water_plant"])


# ============================================================
# R. phase 分层保持 + home_observe 失败跳过
# ============================================================
class TestPhaseLayers(_HomeLoopCase):
    async def test_r_phase1_observe_only(self):
        """R：phase=1 无写工具暴露；模型调写工具被白名单拒绝。"""
        ct = _FakeCallTool()
        ask, state = _make_ask([
            '{"done": false, "tool_calls": [{"name": "water_plant", "args": {"plant_id": "p-1"}}]}',
        ], log_text="看看家里")
        result = await self.run_loop(ct, ask, phase=1)
        first_prompt = state["prompts"][0]
        self.assertNotIn("water_plant", first_prompt)
        self.assertNotIn("write_letter", first_prompt)
        self.assertEqual(_called(ct, "water_plant"), [])
        self.assertEqual([n for n, _ in ct.calls], ["home_observe"])  # phase1 只观察家庭
        self.assertEqual(result[1], [])

    async def test_r2_phase2_no_garden_tools(self):
        """R：phase=2 有信件但无种植烹饪工具。"""
        ct = _FakeCallTool()
        ask, state = _make_ask(['{"done": true, "tool_calls": []}'])
        await self.run_loop(ct, ask, phase=2)
        first_prompt = state["prompts"][0]
        self.assertIn("write_letter", first_prompt)
        self.assertNotIn("plant_seed", first_prompt)
        self.assertNotIn("harvest_plant", first_prompt)

    async def test_r3_phase3_no_basic_life_tools(self):
        """R：phase=3 有种植烹饪但无 phase4 基础生活工具。"""
        ct = _FakeCallTool()
        ask, state = _make_ask(['{"done": true, "tool_calls": []}'])
        await self.run_loop(ct, ask, phase=3)
        first_prompt = state["prompts"][0]
        self.assertIn("harvest_plant", first_prompt)
        self.assertNotIn("home_enter_room", first_prompt)
        self.assertNotIn("home_sleep", first_prompt)

    async def test_r4_phase4_all_visible(self):
        """R：phase=4 全部工具可见。"""
        ct = _FakeCallTool()
        ask, state = _make_ask(['{"done": true, "tool_calls": []}'])
        await self.run_loop(ct, ask, phase=4)
        first_prompt = state["prompts"][0]
        for tool in ("plant_seed", "cook_recipe", "feed_member",
                     "home_enter_room", "home_rest", "home_sleep", "home_spend_time"):
            self.assertIn(tool, first_prompt)

    async def test_home_observe_business_fail_returns_none(self):
        """home_observe 业务失败（raw.ok=false）→ 本轮停止返回 None，不进模型规划。"""
        ct = _FakeCallTool({"home_observe": [("raw", _err_raw("RPC_ERROR"))]})
        ask, state = _make_ask(['{"done": true, "tool_calls": []}'])
        result = await self.run_loop(ct, ask, phase=4)
        self.assertIsNone(result)
        self.assertEqual(state["n"], 0)  # 未请求模型
        self.assertEqual([n for n, _ in ct.calls], ["home_observe"])

    async def test_partial_observe_fail_marks_unknown(self):
        """附属观察（garden/pantry/letters）失败不崩溃，视图标记状态未知。"""
        ct = _FakeCallTool({"pantry_observe": [("raw", _err_raw("RPC_ERROR"))]})
        ask, state = _make_ask(['{"done": true, "tool_calls": []}'])
        result = await self.run_loop(ct, ask, phase=4)
        self.assertIsNotNone(result)
        self.assertIn("读取失败", state["prompts"][0])
        self.assertIn("状态未知", state["prompts"][0])


if __name__ == "__main__":
    unittest.main()
