"""
test_pet_feed_priority.py — 阶段 C1：hungry_cat 代码驱动喂食优先级测试
=====================================================================
覆盖目标（阶段 C1）：
- A. Home 菜品优先：有可用菜品时 feed_member，且不再 cat_feed/购买
- B. 多菜品选择：忽略 servings=0 / 缺 id，选择规则稳定，不从 text 解析
- C. 无 Home 菜品 → 用 pet_inventory 已有食物 cat_feed
- D. 两边都无库存 → cat_shop_buy 后 cat_feed，顺序正确
- E. 购买成功但喂食失败 → care_effective=False，不重复购买，日志不假成功
- F. Home 菜品资源竞争失败（DISH_NOT_AVAILABLE）→ 回退宠物库存，不购买
- G. Home 系统/映射类失败 → 停止本轮，不购买掩盖，care_effective=False
- H. pantry_observe 只读失败 → 不阻断旧库存喂食，日志注明"厨房状态未确认"
- I. 非 hungry 事件（dirty/tired/unhappy）行为不变，不触 Home 工具
- J. 无效库存项（玩具/清洁/qty=0/未知）不进 cat_feed
- K. text 与 raw 不一致时以 raw 结构为准

纯 mock 测试，不触生产数据/数据库。
运行：python -m unittest test_pet_feed_priority -v
"""

import datetime
import unittest
from unittest.mock import AsyncMock, patch

import tool_loop


# ============================================================
# 测试基建：可编程的 call_tool 假体（记录调用 + 按工具名出队 raw）
# ============================================================
_CAT_STATUS_OK = {"ok": True, "pet": {"hunger": 25, "happiness": 60, "cleanliness": 80},
                  "inventory": []}


class _FakeCallTool:
    """call_tool 假体：每个工具名对应一个响应队列。

    队列元素：
      ("raw", {...})   → 返回 {"ok": True, "text": <可自定>, "raw": {...}}
      ("fail", "文本") → 返回 {"ok": False, "text": "文本"}（call_tool 层失败，无 raw）
      ("bare", "文本") → 返回 {"ok": True, "text": "文本"}（成功但无 raw，结构缺失）
    队列空后重复返回最后一个元素；未配置的工具返回默认成功空 raw。
    """

    def __init__(self, plan=None):
        self.plan = plan or {}
        self.calls = []  # [(name, args)]

    async def __call__(self, name, args=None):
        self.calls.append((name, dict(args or {})))
        queue = self.plan.get(name) or [("raw", {"ok": True, "message": "成功"})]
        kind, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        if kind == "fail":
            return {"ok": False, "text": payload}
        if kind == "bare":
            return {"ok": True, "text": payload}
        return {"ok": True, "text": payload.get("_text", "成功"), "raw": payload}


def _status_raw(inventory=None):
    return {"ok": True, "pet": {"hunger": 25, "happiness": 60, "cleanliness": 80},
            "inventory": inventory if inventory is not None else []}


def _pantry_raw(dishes):
    return {"ok": True, "message": "库存观察完成",
            "data": {"inventory": [], "dishes": dishes, "available_recipes": []}}


def _dish(id="dish-uuid-1", name="小鱼干", servings=2):
    return {"id": id, "name": name, "servings": servings, "quality": 4}


def _fed_ok():
    return {"ok": True, "message": "已喂食", "data": {"intimacy": 36.5}}


def _rpc_err(code, msg="失败"):
    return {"ok": False, "message": msg, "error_code": code}


def _now():
    return datetime.datetime(2026, 8, 31, 10, 0)


async def _run_hungry(ct, ask=None):
    """跑一次 hungry_cat 照料循环，返回 (result, ask)。"""
    if ask is None:
        ask = AsyncMock(return_value="照顾小满的日记")
    with patch.object(tool_loop, "call_tool", ct):
        result = await tool_loop.run_pet_care_tool_loop(
            object(), ask, "sys_ctx", _now(), event_type="hungry_cat",
        )
    return result, ask


def _called(ct, name):
    return [args for n, args in ct.calls if n == name]


# ============================================================
# 纯函数单测：菜品 / 宠物库存选择
# ============================================================
class TestPickAvailableDish(unittest.TestCase):
    def test_b2_ignores_zero_servings_and_missing_id(self):
        """B：servings=0 与缺 id 的菜品被忽略。"""
        dishes = [_dish(id="", servings=3), _dish(id="d2", servings=0), _dish(id="d3", servings=1)]
        picked = tool_loop._pick_available_dish(dishes)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["id"], "d3")

    def test_b2_stable_selection(self):
        """B：同样输入多次选择结果稳定（份数多者优先，同份按 id 字典序）。"""
        dishes = [_dish(id="b-aaa", servings=2), _dish(id="a-zzz", servings=5), _dish(id="c-xxx", servings=2)]
        first = tool_loop._pick_available_dish(dishes)
        for _ in range(5):
            again = tool_loop._pick_available_dish(list(reversed(dishes)))
            self.assertEqual(again["id"], first["id"])
        self.assertEqual(first["id"], "a-zzz")

    def test_b2_none_when_all_invalid(self):
        """B：全部无效 → None。"""
        self.assertIsNone(tool_loop._pick_available_dish([_dish(id="", servings=1), _dish(id="d", servings=0)]))
        self.assertIsNone(tool_loop._pick_available_dish([]))
        self.assertIsNone(tool_loop._pick_available_dish("not-a-list"))
        self.assertIsNone(tool_loop._pick_available_dish([None, 42]))

    def test_b2_nonpositive_servings_rejected(self):
        """B：负数份数视为无效。"""
        self.assertIsNone(tool_loop._pick_available_dish([_dish(id="d", servings=-1)]))


class TestPickPetFood(unittest.TestCase):
    def test_j_toy_clean_zero_unknown_rejected(self):
        """J：玩具/清洁/qty=0/未知 item 不作为食物。"""
        inv = [{"item_id": "ball", "qty": 3}, {"item_id": "soap", "qty": 2},
               {"item_id": "fish", "qty": 0}, {"item_id": "mystery_snack", "qty": 5}]
        self.assertIsNone(tool_loop._pick_pet_food(inv))

    def test_priority_order_stable(self):
        """按固定优先序选第一个有库存的食物；输入顺序不影响结果。"""
        inv = [{"item_id": "apple", "qty": 1}, {"item_id": "tuna_can", "qty": 1}]
        self.assertEqual(tool_loop._pick_pet_food(inv), "tuna_can")
        self.assertEqual(tool_loop._pick_pet_food(list(reversed(inv))), "tuna_can")

    def test_food_priority_only_selects_inventory(self):
        """优先序仅用于挑选已有库存，不声称改变食物效果（注释契约）。"""
        self.assertEqual(tool_loop._PET_FOOD_PRIORITY, ["tuna_can", "wet_food", "fish", "cat_milk", "apple"])

    def test_bad_inventory_shapes(self):
        """库存结构异常时安全返回 None。"""
        self.assertIsNone(tool_loop._pick_pet_food(None))
        self.assertIsNone(tool_loop._pick_pet_food("x"))
        self.assertIsNone(tool_loop._pick_pet_food([{"item_id": "fish"}]))  # 缺 qty
        self.assertIsNone(tool_loop._pick_pet_food([{"item_id": "fish", "qty": "many"}]))


# ============================================================
# A. Home 菜品优先
# ============================================================
class TestHomeDishFirst(unittest.IsolatedAsyncioTestCase):
    async def test_a_home_dish_fed_no_cat_feed_no_buy(self):
        """A：有可用菜品 → feed_member；不 cat_feed、不购买；care_effective=True。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([{"item_id": "fish", "qty": 2}]))],
            "pantry_observe": [("raw", _pantry_raw([_dish(id="dish-uuid-1", servings=2)]))],
            "feed_member": [("raw", _fed_ok())],
        })
        result, ask = await _run_hungry(ct)
        self.assertIsNotNone(result)
        _, log, care_effective, cat_status_ok = result
        fm = _called(ct, "feed_member")
        self.assertEqual(len(fm), 1)
        self.assertEqual(fm[0]["target_key"], "pet_xiaoman")
        self.assertEqual(fm[0]["dish_id"], "dish-uuid-1")
        self.assertTrue(fm[0]["action_key"].startswith("auto_feed_member_"))
        self.assertEqual(_called(ct, "cat_feed"), [])
        self.assertEqual(_called(ct, "cat_shop_buy"), [])
        self.assertTrue(care_effective)
        # actor_key 固定注入仍是既有注册契约
        self.assertEqual(tool_loop.TOOL_REGISTRY["feed_member"]["fixed_args"], {"actor_key": "ai_primary"})
        # 日志 prompt 基于真实成功结果
        prompt = ask.call_args_list[0].args[1]
        self.assertIn("Home", prompt)
        self.assertNotIn("dish-uuid-1", prompt)
        self.assertNotIn("auto_feed_member_", prompt)

    async def test_a2_pantry_text_truncated_but_raw_decides(self):
        """K：text 截断/退化时以 raw 结构为准取 dish_id。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw())],
            "pantry_observe": [("raw", dict(_pantry_raw([_dish(id="dish-uuid-9", servings=1)]), _text="成功"))],
            "feed_member": [("raw", _fed_ok())],
        })
        result, _ = await _run_hungry(ct)
        fm = _called(ct, "feed_member")
        self.assertEqual(len(fm), 1)
        self.assertEqual(fm[0]["dish_id"], "dish-uuid-9")
        self.assertTrue(result[2])


# ============================================================
# C. 无 Home 菜品 → 已有宠物食物
# ============================================================
class TestInventoryFallback(unittest.IsolatedAsyncioTestCase):
    async def test_c_no_dish_uses_inventory(self):
        """C：厨房无菜、库存有鱼 → cat_feed；不 feed_member、不购买。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([{"item_id": "fish", "qty": 2}, {"item_id": "ball", "qty": 1}]))],
            "pantry_observe": [("raw", _pantry_raw([]))],
            "cat_feed": [("raw", {"ok": True, "message": "喂食成功"})],
        })
        result, _ = await _run_hungry(ct)
        self.assertEqual(_called(ct, "feed_member"), [])
        cf = _called(ct, "cat_feed")
        self.assertEqual(len(cf), 1)
        self.assertEqual(cf[0]["item_id"], "fish")
        self.assertEqual(_called(ct, "cat_shop_buy"), [])
        self.assertTrue(result[2])

    async def test_c2_all_dishes_zero_servings_counts_as_no_dish(self):
        """C：菜品全部 servings=0 → 走库存路径。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([{"item_id": "cat_milk", "qty": 1}]))],
            "pantry_observe": [("raw", _pantry_raw([_dish(id="d1", servings=0), _dish(id="d2", servings=0)]))],
            "cat_feed": [("raw", {"ok": True, "message": "喂食成功"})],
        })
        result, _ = await _run_hungry(ct)
        self.assertEqual(_called(ct, "feed_member"), [])
        self.assertEqual(_called(ct, "cat_feed")[0]["item_id"], "cat_milk")
        self.assertTrue(result[2])


# ============================================================
# D. 两边都无库存 → 购买后喂食
# ============================================================
class TestPurchasePath(unittest.IsolatedAsyncioTestCase):
    async def test_d_buy_then_feed_in_order(self):
        """D：先 cat_shop_buy 再 cat_feed，顺序正确，care_effective=True。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([]))],
            "pantry_observe": [("raw", _pantry_raw([]))],
            "cat_shop_buy": [("raw", {"ok": True, "message": "购买成功"})],
            "cat_feed": [("raw", {"ok": True, "message": "喂食成功"})],
        })
        result, _ = await _run_hungry(ct)
        buy = _called(ct, "cat_shop_buy")
        feed = _called(ct, "cat_feed")
        self.assertEqual(len(buy), 1)
        self.assertEqual(buy[0]["item_id"], tool_loop._PET_FOOD_PRIORITY[0])
        self.assertEqual(len(feed), 1)
        buy_idx = ct.calls.index(("cat_shop_buy", buy[0]))
        feed_idx = ct.calls.index(("cat_feed", feed[0]))
        self.assertLess(buy_idx, feed_idx)
        self.assertTrue(result[2])
        # 只买一次
        self.assertEqual(len(_called(ct, "cat_shop_buy")), 1)


# ============================================================
# E. 购买成功但喂食失败
# ============================================================
class TestBuyFeedFail(unittest.IsolatedAsyncioTestCase):
    async def test_e_buy_ok_feed_fail_no_effective_no_rebuy(self):
        """E：购买成功但 cat_feed 失败 → care_effective=False，不重复购买，日志不假成功。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([]))],
            "pantry_observe": [("raw", _pantry_raw([]))],
            "cat_shop_buy": [("raw", {"ok": True, "message": "购买成功"})],
            "cat_feed": [("raw", _rpc_err("RPC_ERROR", "喂食事务失败"))],
        })
        ask = AsyncMock(return_value="")
        result, ask = await _run_hungry(ct, ask)
        _, log, care_effective, _ = result
        self.assertFalse(care_effective)
        self.assertEqual(len(_called(ct, "cat_shop_buy")), 1)  # 不重复购买
        self.assertEqual(len(_called(ct, "cat_feed")), 1)      # 不无限重试
        for bad in ("喂饱", "吃完了", "不饿了"):
            self.assertNotIn(bad, log)
        self.assertIn("没有成功喂", log)  # 兜底文案如实记录


# ============================================================
# F. Home 菜品资源竞争失败 → 回退宠物库存
# ============================================================
class TestDishCompetitionFallback(unittest.IsolatedAsyncioTestCase):
    async def test_f_dish_not_available_falls_back_to_inventory(self):
        """F：DISH_NOT_AVAILABLE → 回退 cat_feed；不购买；结果含两边记录。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([{"item_id": "tuna_can", "qty": 1}]))],
            "pantry_observe": [("raw", _pantry_raw([_dish(id="dish-uuid-1", servings=1)]))],
            "feed_member": [("raw", _rpc_err("DISH_NOT_AVAILABLE", "菜品已不可用"))],
            "cat_feed": [("raw", {"ok": True, "message": "喂食成功"})],
        })
        result, ask = await _run_hungry(ct)
        _, log, care_effective, _ = result
        self.assertEqual(_called(ct, "cat_shop_buy"), [])
        cf = _called(ct, "cat_feed")
        self.assertEqual(len(cf), 1)
        self.assertEqual(cf[0]["item_id"], "tuna_can")
        self.assertTrue(care_effective)
        prompt = ask.call_args_list[0].args[1]
        self.assertIn("DISH_NOT_AVAILABLE", prompt)  # Home 菜品失败被如实记录


# ============================================================
# G. Home 系统/映射故障 → 停止，不购买掩盖
# ============================================================
class TestSystemFailuresStop(unittest.IsolatedAsyncioTestCase):
    async def _check(self, code):
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([{"item_id": "fish", "qty": 2}]))],
            "pantry_observe": [("raw", _pantry_raw([_dish(id="dish-uuid-1", servings=1)]))],
            "feed_member": [("raw", _rpc_err(code, f"错误 {code}"))],
        })
        ask = AsyncMock(return_value="")
        result, _ = await _run_hungry(ct, ask)
        _, log, care_effective, _ = result
        self.assertEqual(_called(ct, "cat_feed"), [], f"{code} 不应回退 cat_feed")
        self.assertEqual(_called(ct, "cat_shop_buy"), [], f"{code} 不应购买掩盖")
        self.assertFalse(care_effective, f"{code} 不算照料成功")
        for bad in ("喂饱", "吃完了", "不饿了"):
            self.assertNotIn(bad, log)

    async def test_g_mapping_and_system_codes_stop(self):
        """G：映射/系统类错误码全部停止本轮，不购买掩盖。"""
        for code in ("PET_MAPPING_NOT_FOUND", "PET_NOT_FOUND", "PET_NOT_FEEDABLE",
                     "HOME_STATE_NOT_FOUND", "SERVICE_KEY_MISSING", "RPC_ERROR",
                     "RPC_EMPTY", "DB_UNAVAILABLE", "SOME_UNKNOWN_FAILURE"):
            with self.subTest(code=code):
                await self._check(code)

    async def test_g2_calltool_level_exception_stops(self):
        """G：call_tool 层异常（无 raw）视为未知系统失败 → 停止不掩盖。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw())],
            "pantry_observe": [("raw", _pantry_raw([_dish(id="d", servings=1)]))],
            "feed_member": [("fail", "❌ feed_member 执行失败: boom")],
        })
        result, _ = await _run_hungry(ct)
        self.assertEqual(_called(ct, "cat_feed"), [])
        self.assertEqual(_called(ct, "cat_shop_buy"), [])
        self.assertFalse(result[2])


# ============================================================
# H. pantry_observe 查询失败
# ============================================================
class TestPantryUnavailable(unittest.IsolatedAsyncioTestCase):
    async def test_h_pantry_fail_inventory_ok_falls_back(self):
        """H：厨房查询失败但旧库存有食物 → cat_feed；日志注明厨房状态未确认。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([{"item_id": "fish", "qty": 2}]))],
            "pantry_observe": [("fail", "❌ pantry_observe 执行失败: timeout")],
            "cat_feed": [("raw", {"ok": True, "message": "喂食成功"})],
        })
        result, ask = await _run_hungry(ct)
        _, log, care_effective, _ = result
        self.assertEqual(_called(ct, "feed_member"), [])
        self.assertEqual(len(_called(ct, "cat_feed")), 1)
        self.assertEqual(_called(ct, "cat_shop_buy"), [])
        self.assertTrue(care_effective)
        prompt = ask.call_args_list[0].args[1]
        self.assertIn("未确认", prompt)

    async def test_h2_pantry_fail_inventory_empty_allows_buy(self):
        """H：厨房查询失败且库存空 → 允许按现有流程购买，日志注明未确认。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([]))],
            "pantry_observe": [("fail", "❌ pantry_observe 执行失败: timeout")],
            "cat_shop_buy": [("raw", {"ok": True, "message": "购买成功"})],
            "cat_feed": [("raw", {"ok": True, "message": "喂食成功"})],
        })
        result, ask = await _run_hungry(ct)
        self.assertEqual(len(_called(ct, "cat_shop_buy")), 1)
        self.assertTrue(result[2])
        prompt = ask.call_args_list[0].args[1]
        self.assertIn("未确认", prompt)


# ============================================================
# I. 非 hungry 事件行为不变
# ============================================================
class TestNonHungryUnchanged(unittest.IsolatedAsyncioTestCase):
    async def _check(self, event, tool, raw):
        ask = AsyncMock(side_effect=[
            f'{{"tool_calls": [{{"name": "{tool}", "args": {{}}}}]}}',
            "照料完成",
        ])
        ct = _FakeCallTool({
            "cat_status": [("raw", _CAT_STATUS_OK)],
            tool: [("raw", raw)],
        })
        with patch.object(tool_loop, "call_tool", ct):
            result = await tool_loop.run_pet_care_tool_loop(
                object(), ask, "sys_ctx", _now(), event_type=event,
            )
        names = [n for n, _ in ct.calls]
        self.assertNotIn("pantry_observe", names, f"{event} 不应查询厨房")
        self.assertNotIn("feed_member", names, f"{event} 不应喂 Home 菜")
        self.assertNotIn("cat_shop_buy", names, f"{event} 不应购买")
        self.assertEqual(ask.call_count, 2)  # 阶段2决策 + 阶段3日志（原流程）
        self.assertEqual(result[0], event)

    async def test_i_dirty_cat(self):
        await self._check("dirty_cat", "cat_clean", {"ok": True, "message": "清洁完成"})

    async def test_i_tired_cat(self):
        await self._check("tired_cat", "cat_restore_energy", {"ok": True, "message": "精力恢复"})

    async def test_i_unhappy_cat(self):
        await self._check("unhappy_cat", "cat_pet", {"ok": True, "message": "抚摸成功，快乐+5"})

    async def test_i2_llm_failure_returns_none_for_non_hungry(self):
        """非 hungry 事件 LLM 决策失败 → 仍返回 None（原契约保留）。"""
        ask = AsyncMock(side_effect=RuntimeError("LLM down"))
        ct = _FakeCallTool({"cat_status": [("raw", _CAT_STATUS_OK)]})
        with patch.object(tool_loop, "call_tool", ct):
            result = await tool_loop.run_pet_care_tool_loop(
                object(), ask, "sys_ctx", _now(), event_type="dirty_cat",
            )
        self.assertIsNone(result)


# ============================================================
# 日志安全：不泄露 UUID/action_key；失败不假成功
# ============================================================
class TestLogSafety(unittest.IsolatedAsyncioTestCase):
    async def test_no_uuid_or_action_key_in_prompt_and_log(self):
        """最终日志 prompt 与兜底文案不含 dish UUID / action_key 全文。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw())],
            "pantry_observe": [("raw", _pantry_raw([_dish(id="550e8400-e29b-41d4-a716-446655440000", servings=1)]))],
            "feed_member": [("raw", _fed_ok())],
        })
        result, ask = await _run_hungry(ct)
        prompt = ask.call_args_list[0].args[1]
        self.assertNotIn("550e8400", prompt)
        self.assertNotIn("auto_feed_member_", prompt)

    async def test_all_fail_log_honest(self):
        """全失败（厨房无菜+无库存+购买失败）→ 日志不声称喂食成功。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([]))],
            "pantry_observe": [("raw", _pantry_raw([]))],
            "cat_shop_buy": [("raw", _rpc_err("INSUFFICIENT_FUNDS", "钱包余额不足"))],
        })
        ask = AsyncMock(return_value="")
        result, _ = await _run_hungry(ct, ask)
        _, log, care_effective, _ = result
        self.assertFalse(care_effective)
        for bad in ("喂饱", "吃完了", "不饿了", "喂了"):
            self.assertNotIn(bad, log)
        self.assertIn("没有成功喂", log)


# ============================================================
# C1.1 修复 2：cat_status 无法确认 → 本轮完全停止
# ============================================================
class TestCatStatusUnconfirmed(unittest.IsolatedAsyncioTestCase):
    """cat_status 调用失败/raw.ok=false/raw 结构异常/缺 pet/pet 非 dict →
    不观察厨房、不喂食、不购买，care_effective=False，日志不声称喂食。"""

    async def _check(self, status_plan):
        ct = _FakeCallTool({"cat_status": [status_plan]})
        ask = AsyncMock(return_value="")
        result, _ = await _run_hungry(ct, ask)
        _, log, care_effective, cat_status_ok = result
        names = [n for n, _ in ct.calls]
        self.assertEqual(names, ["cat_status"], f"应只调用 cat_status，实际 {names}")
        self.assertFalse(care_effective)
        self.assertFalse(cat_status_ok)
        for bad in ("喂饱", "吃完了", "不饿了", "喂了"):
            self.assertNotIn(bad, log)
        self.assertIn("没有贸然喂食", log)

    async def test_a1_calltool_fail(self):
        """A：cat_status call_tool 层失败 → 全停。"""
        await self._check(("fail", "❌ cat_status 执行失败: boom"))

    async def test_a2_raw_ok_false(self):
        """A：raw.ok=false → 全停。"""
        await self._check(("raw", {"ok": False, "message": "数据库未连接",
                                   "error_code": "SERVICE_KEY_MISSING"}))

    async def test_b1_raw_missing(self):
        """B：ok=true 但无 raw（结构缺失）→ 全停。"""
        await self._check(("bare", "成功"))

    async def test_b2_raw_empty(self):
        """B：raw={} → 全停。"""
        await self._check(("raw", {}))

    async def test_b3_raw_ok_no_pet(self):
        """B：raw.ok=true 但缺 pet → 全停。"""
        await self._check(("raw", {"ok": True, "inventory": []}))

    async def test_b4_pet_not_dict(self):
        """B：pet 不是 dict → 全停。"""
        await self._check(("raw", {"ok": True, "pet": "小满", "inventory": []}))

    async def test_status_fail_beats_inventory(self):
        """cat_status 失败 ≠ 库存为空：即使 fake 默认工具都会成功也不得消费。"""
        # 不配置任何 plan：所有工具默认返回成功 raw → 若误继续会走完整购买链
        ct = _FakeCallTool()
        result, _ = await _run_hungry(ct)
        names = [n for n, _ in ct.calls]
        self.assertEqual(names, ["cat_status"])
        self.assertFalse(result[2])


# ============================================================
# C1.1 修复 1：cat_feed 只有明确资源类错误才允许购买
# ============================================================
class TestCatFeedFailureClassification(unittest.IsolatedAsyncioTestCase):
    """cat_feed 失败分类：INSUFFICIENT_INVENTORY → 购买；
    系统/映射/参数/未知/无错误码/call_tool 层失败 → 停止不购买。"""

    async def _base(self, cat_feed_queue, expect_buy):
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([{"item_id": "fish", "qty": 1}]))],
            "pantry_observe": [("raw", _pantry_raw([]))],
            "cat_feed": cat_feed_queue,
            "cat_shop_buy": [("raw", {"ok": True, "message": "购买成功"}),
                             ("raw", {"ok": True, "message": "购买成功"})],
            "feed_member": [("raw", _fed_ok())],
        })
        ask = AsyncMock(return_value="")
        result, ask = await _run_hungry(ct, ask)
        buys = _called(ct, "cat_shop_buy")
        self.assertEqual(len(buys), 1 if expect_buy else 0,
                         f"购买次数应为 {1 if expect_buy else 0}，实际 {len(buys)}")
        _, log, care_effective, _ = result
        for bad in ("喂饱", "吃完了", "不饿了"):
            self.assertNotIn(bad, log)
        return result, ct, ask

    async def test_c_insufficient_inventory_buy_then_feed(self):
        """C：库存快照有货但并发消费导致不足 → 购买一次 → 喂成 → care_effective=True。"""
        result, ct, _ = await self._base(
            [("raw", _rpc_err("INSUFFICIENT_INVENTORY", "库存不足")),
             ("raw", {"ok": True, "message": "喂食成功"})],
            expect_buy=True,
        )
        feeds = _called(ct, "cat_feed")
        self.assertEqual(len(feeds), 2)
        self.assertEqual(feeds[0]["item_id"], "fish")  # 先用快照库存尝试
        self.assertEqual(feeds[1]["item_id"], tool_loop._PET_FOOD_PRIORITY[0])  # 买后喂
        self.assertTrue(result[2])
        self.assertEqual(len(_called(ct, "cat_shop_buy")), 1)  # 不重复购买

    async def test_d_system_codes_stop_no_buy(self):
        """D：系统/映射/参数类错误逐项 → 不购买、care_effective=False。

        错误码来源：migrations/20240811_004_cat_rpc.sql（rpc_cat_feed 实际返回
        PET_NOT_FOUND/ITEM_NOT_IN_WHITELIST/NOT_FOOD_ITEM）+ home_system._rpc
        （SERVICE_KEY_MISSING/RPC_ERROR/RPC_EMPTY）+ 入参校验（INVALID_USER）。
        """
        for code in ("SERVICE_KEY_MISSING", "DB_UNAVAILABLE", "RPC_ERROR", "RPC_EMPTY",
                     "PET_NOT_FOUND", "INVALID_USER", "ITEM_NOT_IN_WHITELIST",
                     "NOT_FOOD_ITEM"):
            with self.subTest(code=code):
                result, ct, _ = await self._base(
                    [("raw", _rpc_err(code, f"错误 {code}"))], expect_buy=False)
                self.assertFalse(result[2])
                self.assertEqual(_called(ct, "cat_feed"), [{"item_id": "fish"}])  # 不重试喂食

    async def test_e_unknown_code_stops(self):
        """E：未知错误码 → 保守停止，不购买。"""
        result, ct, _ = await self._base(
            [("raw", {"ok": False, "message": "未识别失败", "error_code": "SOMETHING_NEW"})],
            expect_buy=False)
        self.assertFalse(result[2])

    async def test_e2_no_error_code_stops(self):
        """E：ok=false 且无 error_code → 保守停止，结果注明"原因未确认"。"""
        result, ct, ask = await self._base(
            [("raw", {"ok": False, "message": "喂食失败"})], expect_buy=False)
        self.assertFalse(result[2])
        prompt = ask.call_args_list[0].args[1]
        self.assertIn("原因未确认", prompt)

    async def test_f_calltool_fail_stops(self):
        """F：cat_feed call_tool 层失败（无 raw，业务结果无法确认）→ 停止不购买，不崩。"""
        result, ct, _ = await self._base(
            [("fail", "❌ cat_feed 执行失败: boom")], expect_buy=False)
        self.assertFalse(result[2])

    async def test_g_pantry_fail_status_ok_inventory_used(self):
        """G（回归确认）：pantry 失败但 cat_status 正常 → 仍可用已确认库存，不购买。"""
        ct = _FakeCallTool({
            "cat_status": [("raw", _status_raw([{"item_id": "fish", "qty": 2}]))],
            "pantry_observe": [("fail", "❌ pantry_observe 执行失败: timeout")],
            "cat_feed": [("raw", {"ok": True, "message": "喂食成功"})],
        })
        result, _ = await _run_hungry(ct)
        self.assertEqual(_called(ct, "cat_shop_buy"), [])
        self.assertTrue(result[2])


if __name__ == "__main__":
    unittest.main()
