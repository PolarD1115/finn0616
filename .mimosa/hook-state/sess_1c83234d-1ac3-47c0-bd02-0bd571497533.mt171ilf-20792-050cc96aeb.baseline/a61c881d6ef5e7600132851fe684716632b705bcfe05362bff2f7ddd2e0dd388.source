"""
test_cat_check.py — 自由活动猫状态检查（3 轮规则）+ 宠物照料循环测试
=====================================================================
覆盖目标一的需求：
- 启动后第一轮检查
- 检查成功后最多间隔 3 轮再次检查
- cat_status 失败后下一轮重试（不重置计数）
- 宠物 tick 已检查后自由活动计数重置
- 低 hunger/happiness/cleanliness 触发照料循环
- unhappy_cat 事件存在且提示含 cat_pet/cat_play
- 模型空 tool_calls 不算照料成功，保留待重试状态
- cat_status 返回结构异常（缺 pet）不算成功

纯函数 + mock 测试，不触生产数据/数据库。

运行：
    python -m unittest test_cat_check -v
"""

import unittest
import datetime
import time
from unittest.mock import AsyncMock, patch, MagicMock

import tool_loop


# ============================================================
# 1. _format_cat_status_for_llm：正确解析 pet 子对象
# ============================================================
class TestFormatCatStatus(unittest.TestCase):
    """cat_status RPC 返回 {ok, pet:{...}, inventory:[...]}，指标在 pet 子对象里。"""

    def test_normal_pet_structure(self):
        """正常 pet 结构 → 提取各项指标，cat_status_ok=True。"""
        raw = {
            "ok": True,
            "pet": {"hunger": 25, "happiness": 60, "cleanliness": 80,
                    "health": 90, "energy": 50, "status": "idle", "mood": "happy"},
            "inventory": [{"item_id": "fish", "qty": 2}, {"item_id": "toy_ball", "qty": 1}],
        }
        text, ok = tool_loop._format_cat_status_for_llm(raw, "成功", True)
        self.assertTrue(ok)
        self.assertIn("饱食度=25", text)
        self.assertIn("快乐=60", text)
        self.assertIn("清洁=80", text)
        self.assertIn("fish×2", text)
        self.assertIn("toy_ball×1", text)

    def test_missing_pet_field(self):
        """ok=True 但缺 pet 字段 → cat_status_ok=False。"""
        raw = {"ok": True, "inventory": []}
        text, ok = tool_loop._format_cat_status_for_llm(raw, "成功", True)
        self.assertFalse(ok)
        self.assertIn("异常", text)

    def test_ok_false(self):
        """raw.ok=False → cat_status_ok=False，返回错误文本。"""
        raw = {"ok": False, "message": "数据库未连接", "error_code": "DB_UNAVAILABLE"}
        text, ok = tool_loop._format_cat_status_for_llm(raw, "❌ 失败", False)
        self.assertFalse(ok)
        self.assertIn("失败", text)

    def test_raw_none_degraded_text_success(self):
        """raw=None 且 text 退化为'成功' → 无法判断真实指标，cat_status_ok=False。"""
        text, ok = tool_loop._format_cat_status_for_llm(None, "成功", True)
        self.assertFalse(ok)

    def test_raw_none_real_text(self):
        """raw=None 但 text 含真实指标文本 → 退回 text，cat_status_ok=True。"""
        text, ok = tool_loop._format_cat_status_for_llm(None, "饱食度=28 状态 idle", True)
        self.assertTrue(ok)
        self.assertIn("饱食度=28", text)

    def test_empty_inventory(self):
        """空库存 → 显示'库存: 空'。"""
        raw = {"ok": True, "pet": {"hunger": 50}, "inventory": []}
        text, ok = tool_loop._format_cat_status_for_llm(raw, "成功", True)
        self.assertTrue(ok)
        self.assertIn("库存: 空", text)

    def test_inventory_missing_qty(self):
        """库存项缺 qty → 只显示名称。"""
        raw = {"ok": True, "pet": {"hunger": 50},
               "inventory": [{"item_id": "fish"}]}
        text, ok = tool_loop._format_cat_status_for_llm(raw, "成功", True)
        self.assertTrue(ok)
        self.assertIn("fish", text)


# ============================================================
# 2. unhappy_cat 事件 + 照料提示
# ============================================================
class TestUnhappyCatEvent(unittest.TestCase):
    def test_unhappy_cat_event_exists(self):
        """unhappy_cat 事件类型已注册。"""
        self.assertIn("unhappy_cat", tool_loop._PET_CARE_EVENT_DESC)

    def test_unhappy_cat_desc_mentions_happiness(self):
        """unhappy_cat 描述提到快乐值低位。"""
        desc = tool_loop._PET_CARE_EVENT_DESC["unhappy_cat"]
        self.assertIn("快乐", desc)
        self.assertIn("<30", desc)

    def test_unhappy_cat_tools_hint(self):
        """unhappy_cat 建议工具含 cat_pet/cat_play。"""
        hint = tool_loop._PET_CARE_EVENT_TOOLS_HINT["unhappy_cat"]
        self.assertIn("cat_pet", hint)
        self.assertIn("cat_play", hint)

    def test_observe_only_tools_excludes_cat_status(self):
        """cat_status 不算实际照料改善工具。"""
        self.assertIn("cat_status", tool_loop._PET_CARE_OBSERVE_ONLY_TOOLS)
        self.assertIn("cat_shop_list", tool_loop._PET_CARE_OBSERVE_ONLY_TOOLS)


# ============================================================
# 3. run_pet_care_tool_loop：care_effective + cat_status_ok 返回
# ============================================================
class TestPetCareLoopReturns(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._snapshot = {n: spec.get("callable") for n, spec in tool_loop.TOOL_REGISTRY.items()}

    def tearDown(self):
        for n, fn in self._snapshot.items():
            tool_loop.TOOL_REGISTRY[n]["callable"] = fn

    @staticmethod
    def _client():
        return object()

    async def test_returns_4_tuple_with_care_effective_true(self):
        """成功调用改善工具 → care_effective=True, cat_status_ok=True。"""
        ask = AsyncMock(side_effect=[
            '{"tool_calls": [{"name": "cat_feed", "args": {"item_id": "fish"}}]}',
            '喂了鱼',
        ])
        cat_status_raw = {"ok": True, "pet": {"hunger": 20, "happiness": 60, "cleanliness": 80},
                          "inventory": []}
        m_ct = AsyncMock(side_effect=[
            {"ok": True, "text": "成功", "raw": cat_status_raw},
            {"ok": True, "text": "喂食成功"},
        ])
        with patch.object(tool_loop, "call_tool", m_ct):
            result = await tool_loop.run_pet_care_tool_loop(
                self._client(), ask, "sys_ctx",
                datetime.datetime(2026, 8, 17, 10, 0), event_type="hungry_cat",
            )
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 4)
        event_type, log, care_effective, cat_status_ok = result
        self.assertEqual(event_type, "hungry_cat")
        self.assertTrue(care_effective)
        self.assertTrue(cat_status_ok)

    async def test_empty_tool_calls_care_effective_false(self):
        """模型空 tool_calls → care_effective=False（不算照料成功）。"""
        ask = AsyncMock(side_effect=[
            '{"tool_calls": []}',
            '什么也没做',
        ])
        cat_status_raw = {"ok": True, "pet": {"hunger": 20}, "inventory": []}
        m_ct = AsyncMock(return_value={"ok": True, "text": "成功", "raw": cat_status_raw})
        with patch.object(tool_loop, "call_tool", m_ct):
            result = await tool_loop.run_pet_care_tool_loop(
                self._client(), ask, "sys_ctx",
                datetime.datetime(2026, 8, 17, 10, 0), event_type="hungry_cat",
            )
        _, _, care_effective, cat_status_ok = result
        self.assertFalse(care_effective)
        self.assertTrue(cat_status_ok)

    async def test_only_cat_status_care_effective_false(self):
        """模型只调 cat_status（查看）→ care_effective=False。"""
        ask = AsyncMock(side_effect=[
            '{"tool_calls": [{"name": "cat_status", "args": {}}]}',
            '看了一眼',
        ])
        cat_status_raw = {"ok": True, "pet": {"hunger": 20}, "inventory": []}
        m_ct = AsyncMock(side_effect=[
            {"ok": True, "text": "成功", "raw": cat_status_raw},  # 阶段1
            {"ok": True, "text": "成功", "raw": cat_status_raw},  # 阶段3 模型又调 cat_status
        ])
        with patch.object(tool_loop, "call_tool", m_ct):
            result = await tool_loop.run_pet_care_tool_loop(
                self._client(), ask, "sys_ctx",
                datetime.datetime(2026, 8, 17, 10, 0), event_type="dirty_cat",
            )
        _, _, care_effective, _ = result
        self.assertFalse(care_effective)

    async def test_cat_status_failed_cat_status_ok_false(self):
        """阶段1 cat_status 失败 → cat_status_ok=False，照料仍尝试（不阻断）。"""
        ask = AsyncMock(side_effect=[
            '{"tool_calls": [{"name": "cat_feed", "args": {"item_id": "fish"}}]}',
            '喂了鱼',
        ])
        m_ct = AsyncMock(side_effect=[
            {"ok": False, "text": "❌ 数据库未连接"},  # cat_status 失败
            {"ok": True, "text": "喂食成功"},
        ])
        with patch.object(tool_loop, "call_tool", m_ct):
            result = await tool_loop.run_pet_care_tool_loop(
                self._client(), ask, "sys_ctx",
                datetime.datetime(2026, 8, 17, 10, 0), event_type="hungry_cat",
            )
        _, _, care_effective, cat_status_ok = result
        self.assertFalse(cat_status_ok)
        self.assertTrue(care_effective)  # cat_feed 成功了

    async def test_low_happiness_triggers_unhappy_cat(self):
        """unhappy_cat 事件能正常走完照料循环，cat_pet 成功 → care_effective=True。"""
        ask = AsyncMock(side_effect=[
            '{"tool_calls": [{"name": "cat_pet", "args": {}}]}',
            '摸了摸小满',
        ])
        cat_status_raw = {"ok": True, "pet": {"happiness": 15}, "inventory": []}
        m_ct = AsyncMock(side_effect=[
            {"ok": True, "text": "成功", "raw": cat_status_raw},
            {"ok": True, "text": "抚摸成功，快乐+5"},
        ])
        with patch.object(tool_loop, "call_tool", m_ct):
            result = await tool_loop.run_pet_care_tool_loop(
                self._client(), ask, "sys_ctx",
                datetime.datetime(2026, 8, 17, 10, 0), event_type="unhappy_cat",
            )
        event_type, log, care_effective, cat_status_ok = result
        self.assertEqual(event_type, "unhappy_cat")
        self.assertTrue(care_effective)
        self.assertTrue(cat_status_ok)

    async def test_stage2_prompt_requires_action_tool(self):
        """阶段2 prompt 含'必须尝试调用至少一个能改善'的要求。"""
        ask = AsyncMock(side_effect=[
            '{"tool_calls": [{"name": "cat_clean", "args": {}}]}',
            '清洁了',
        ])
        captured_prompt = []
        async def _capture(client, prompt, system_prompt="", temperature=0.7):
            captured_prompt.append(prompt)
            idx = len(captured_prompt) - 1
            if idx == 0:
                return '{"tool_calls": [{"name": "cat_clean", "args": {}}]}'
            return '清洁完成'
        cat_status_raw = {"ok": True, "pet": {"cleanliness": 10}, "inventory": []}
        m_ct = AsyncMock(return_value={"ok": True, "text": "成功", "raw": cat_status_raw})
        with patch.object(tool_loop, "call_tool", m_ct):
            await tool_loop.run_pet_care_tool_loop(
                self._client(), _capture, "sys_ctx",
                datetime.datetime(2026, 8, 17, 10, 0), event_type="dirty_cat",
            )
        # stage2 prompt 是第二个 ask 调用（index 0 是 stage2，index 1 是 stage4）
        self.assertIn("必须尝试调用至少一个能改善", captured_prompt[0])


# ============================================================
# 4. heartbeat._free_activity_check_cat：3 轮规则 + 失败重试 + tick 协调
# ============================================================
class TestFreeActivityCatCheck(unittest.IsolatedAsyncioTestCase):
    """测试自由活动猫检查的轮次计数与协调逻辑。
    直接调用 heartbeat._free_activity_check_cat，mock home_system.cat_status 与 _try_pet_care。"""

    @staticmethod
    def _now():
        return datetime.datetime(2026, 8, 17, 10, 0)

    def _reset_heartbeat_state(self):
        """重置 heartbeat 进程内状态（测试隔离）。"""
        import heartbeat
        heartbeat._free_activity_cat_check["rounds_since_check"] = 0
        heartbeat._free_activity_cat_check["care_pending"] = False
        heartbeat._free_activity_cat_check["last_check_ts"] = 0.0
        heartbeat._cat_status_last_ok_ts = 0.0
        heartbeat._pet_care_last_fire.clear()

    async def test_first_round_always_checks(self):
        """进程启动后第一轮必须检查 cat_status。"""
        import heartbeat
        self._reset_heartbeat_state()
        cat_status = {"ok": True, "pet": {"hunger": 80, "happiness": 80, "cleanliness": 80},
                      "inventory": []}
        with patch("home_system.cat_status", return_value=cat_status):
            await heartbeat._free_activity_check_cat(self._now())
        # 检查过 → 计数重置为 0，last_check_ts 已设
        self.assertEqual(heartbeat._free_activity_cat_check["rounds_since_check"], 0)
        self.assertGreater(heartbeat._free_activity_cat_check["last_check_ts"], 0)
        self.assertFalse(heartbeat._free_activity_cat_check["care_pending"])

    async def test_skip_within_3_rounds(self):
        """检查成功后 1~2 轮内不检查（计数累加）。"""
        import heartbeat
        self._reset_heartbeat_state()
        cat_status = {"ok": True, "pet": {"hunger": 80, "happiness": 80, "cleanliness": 80},
                      "inventory": []}
        with patch("home_system.cat_status", return_value=cat_status) as m:
            # 第1轮：检查
            await heartbeat._free_activity_check_cat(self._now())
            self.assertEqual(m.call_count, 1)
            # 第2轮：不检查（计数 1）
            await heartbeat._free_activity_check_cat(self._now())
            self.assertEqual(m.call_count, 1)
            self.assertEqual(heartbeat._free_activity_cat_check["rounds_since_check"], 1)
            # 第3轮：不检查（计数 2）
            await heartbeat._free_activity_check_cat(self._now())
            self.assertEqual(m.call_count, 1)
            self.assertEqual(heartbeat._free_activity_cat_check["rounds_since_check"], 2)

    async def test_check_again_at_round_4(self):
        """第4轮（跳过2轮后）必须再次检查（连续3次内至少1次）。"""
        import heartbeat
        self._reset_heartbeat_state()
        cat_status = {"ok": True, "pet": {"hunger": 80, "happiness": 80, "cleanliness": 80},
                      "inventory": []}
        with patch("home_system.cat_status", return_value=cat_status) as m:
            await heartbeat._free_activity_check_cat(self._now())  # 第1轮检查 (cnt=0)
            await heartbeat._free_activity_check_cat(self._now())  # 第2轮跳过 (cnt=1)
            await heartbeat._free_activity_check_cat(self._now())  # 第3轮跳过 (cnt=2)
            # 第4轮：cnt=2>=2 → 必须检查
            await heartbeat._free_activity_check_cat(self._now())
            self.assertEqual(m.call_count, 2)
            self.assertEqual(heartbeat._free_activity_cat_check["rounds_since_check"], 0)

    async def test_cat_status_failure_no_reset_retry_next(self):
        """cat_status 失败 → 不重置计数，下一轮继续尝试。"""
        import heartbeat
        self._reset_heartbeat_state()
        fail_status = {"ok": False, "message": "数据库未连接", "error_code": "DB_UNAVAILABLE"}
        with patch("home_system.cat_status", return_value=fail_status) as m:
            # 第1轮：尝试检查，失败
            await heartbeat._free_activity_check_cat(self._now())
            self.assertEqual(m.call_count, 1)
            # last_check_ts 不应被设置（仍为 0）
            self.assertEqual(heartbeat._free_activity_cat_check["last_check_ts"], 0.0)
            # 第2轮：失败后继续尝试（因为 last_check_ts==0 仍触发 need_check）
            await heartbeat._free_activity_check_cat(self._now())
            self.assertEqual(m.call_count, 2)

    async def test_cat_status_missing_pet_no_reset(self):
        """cat_status 返回缺 pet 字段 → 不重置计数。"""
        import heartbeat
        self._reset_heartbeat_state()
        bad_status = {"ok": True, "inventory": []}  # 缺 pet
        with patch("home_system.cat_status", return_value=bad_status) as m:
            await heartbeat._free_activity_check_cat(self._now())
            self.assertEqual(m.call_count, 1)
            self.assertEqual(heartbeat._free_activity_cat_check["last_check_ts"], 0.0)
            # 下一轮继续尝试
            await heartbeat._free_activity_check_cat(self._now())
            self.assertEqual(m.call_count, 2)

    async def test_tick_coordination_resets_counter(self):
        """宠物 tick 侧成功 cat_status 后，自由活动计数重置。"""
        import heartbeat
        self._reset_heartbeat_state()
        cat_status = {"ok": True, "pet": {"hunger": 80, "happiness": 80, "cleanliness": 80},
                      "inventory": []}
        with patch("home_system.cat_status", return_value=cat_status) as m:
            # 第1轮：自由活动检查
            await heartbeat._free_activity_check_cat(self._now())
            self.assertEqual(m.call_count, 1)
            fa_ts = heartbeat._free_activity_cat_check["last_check_ts"]

            # 模拟 tick 侧照料成功 cat_status（更新全局时间戳）
            time.sleep(0.01)
            heartbeat._cat_status_last_ok_ts = time.time()

            # 第2轮：本应跳过，但因 tick 侧更新了时间戳 → 重置计数，但仍不重复检查
            # （因为 last_check_ts 已被 tick 更新覆盖，need_check=False）
            await heartbeat._free_activity_check_cat(self._now())
            # call_count 仍为 1（协调后计数重置，但本轮不需要检查）
            self.assertEqual(m.call_count, 1)
            # 协调重置后本轮作为一次跳过轮，cnt 递增到 1
            self.assertEqual(heartbeat._free_activity_cat_check["rounds_since_check"], 1)

    async def test_low_hunger_triggers_care(self):
        """hunger<30 → 触发 hungry_cat 照料循环。"""
        import heartbeat
        self._reset_heartbeat_state()
        cat_status = {"ok": True, "pet": {"hunger": 20, "happiness": 80, "cleanliness": 80},
                      "inventory": []}
        care_ret = {"ran": True, "care_effective": True, "cat_status_ok": True,
                    "skipped_cooldown": False}
        with patch("home_system.cat_status", return_value=cat_status), \
             patch("heartbeat._try_pet_care", new=AsyncMock(return_value=care_ret)) as m_care:
            await heartbeat._free_activity_check_cat(self._now())
            m_care.assert_awaited_once()
            args = m_care.call_args
            self.assertEqual(args.args[0], "hungry_cat")
            # 照料生效 → care_pending=False
            self.assertFalse(heartbeat._free_activity_cat_check["care_pending"])

    async def test_low_happiness_triggers_unhappy_cat(self):
        """happiness<30 → 触发 unhappy_cat 照料循环。"""
        import heartbeat
        self._reset_heartbeat_state()
        cat_status = {"ok": True, "pet": {"hunger": 80, "happiness": 15, "cleanliness": 80},
                      "inventory": []}
        care_ret = {"ran": True, "care_effective": True, "cat_status_ok": True,
                    "skipped_cooldown": False}
        with patch("home_system.cat_status", return_value=cat_status), \
             patch("heartbeat._try_pet_care", new=AsyncMock(return_value=care_ret)) as m_care:
            await heartbeat._free_activity_check_cat(self._now())
            m_care.assert_awaited_once()
            self.assertEqual(m_care.call_args.args[0], "unhappy_cat")

    async def test_low_cleanliness_triggers_dirty_cat(self):
        """cleanliness<30 → 触发 dirty_cat 照料循环。"""
        import heartbeat
        self._reset_heartbeat_state()
        cat_status = {"ok": True, "pet": {"hunger": 80, "happiness": 80, "cleanliness": 10},
                      "inventory": []}
        care_ret = {"ran": True, "care_effective": True, "cat_status_ok": True,
                    "skipped_cooldown": False}
        with patch("home_system.cat_status", return_value=cat_status), \
             patch("heartbeat._try_pet_care", new=AsyncMock(return_value=care_ret)) as m_care:
            await heartbeat._free_activity_check_cat(self._now())
            m_care.assert_awaited_once()
            self.assertEqual(m_care.call_args.args[0], "dirty_cat")

    async def test_empty_tool_calls_keeps_care_pending(self):
        """照料未生效（空 tool_calls）→ care_pending=True，下一轮重试。"""
        import heartbeat
        self._reset_heartbeat_state()
        cat_status = {"ok": True, "pet": {"hunger": 20, "happiness": 80, "cleanliness": 80},
                      "inventory": []}
        care_ret = {"ran": True, "care_effective": False, "cat_status_ok": True,
                    "skipped_cooldown": False}  # 照料未生效
        with patch("home_system.cat_status", return_value=cat_status), \
             patch("heartbeat._try_pet_care", new=AsyncMock(return_value=care_ret)):
            await heartbeat._free_activity_check_cat(self._now())
            # care_pending 应为 True
            self.assertTrue(heartbeat._free_activity_cat_check["care_pending"])
            # 计数仍重置（检查本身已完成）
            self.assertEqual(heartbeat._free_activity_cat_check["rounds_since_check"], 0)

    async def test_care_pending_forces_next_round_check(self):
        """care_pending=True → 下一轮必须再次检查（即使计数<3）。"""
        import heartbeat
        self._reset_heartbeat_state()
        cat_status = {"ok": True, "pet": {"hunger": 20, "happiness": 80, "cleanliness": 80},
                      "inventory": []}
        # 第一次照料未生效
        care_fail = {"ran": True, "care_effective": False, "cat_status_ok": True,
                     "skipped_cooldown": False}
        # 第二次照料生效
        care_ok = {"ran": True, "care_effective": True, "cat_status_ok": True,
                   "skipped_cooldown": False}
        with patch("home_system.cat_status", return_value=cat_status) as m, \
             patch("heartbeat._try_pet_care",
                   new=AsyncMock(side_effect=[care_fail, care_ok])) as m_care:
            # 第1轮：检查 + 照料未生效 → care_pending=True
            await heartbeat._free_activity_check_cat(self._now())
            self.assertEqual(m.call_count, 1)
            self.assertTrue(heartbeat._free_activity_cat_check["care_pending"])
            # 第2轮：care_pending → 必须再次检查
            await heartbeat._free_activity_check_cat(self._now())
            self.assertEqual(m.call_count, 2)
            # 第二次照料生效 → care_pending=False
            self.assertFalse(heartbeat._free_activity_cat_check["care_pending"])

    async def test_no_llm_keeps_care_pending(self):
        """无 background LLM（care_ret.ran=False）→ care_pending=True。"""
        import heartbeat
        self._reset_heartbeat_state()
        cat_status = {"ok": True, "pet": {"hunger": 20, "happiness": 80, "cleanliness": 80},
                      "inventory": []}
        care_ret = {"ran": False, "care_effective": False, "cat_status_ok": False,
                    "skipped_cooldown": False}
        with patch("home_system.cat_status", return_value=cat_status), \
             patch("heartbeat._try_pet_care", new=AsyncMock(return_value=care_ret)):
            await heartbeat._free_activity_check_cat(self._now())
            self.assertTrue(heartbeat._free_activity_cat_check["care_pending"])

    async def test_care_skipped_cooldown_clears_pending(self):
        """照料因冷却期跳过（tick 刚处理过）→ care_pending=False。"""
        import heartbeat
        self._reset_heartbeat_state()
        # 先设 care_pending=True 模拟上一轮未生效
        heartbeat._free_activity_cat_check["care_pending"] = True
        cat_status = {"ok": True, "pet": {"hunger": 20, "happiness": 80, "cleanliness": 80},
                      "inventory": []}
        care_ret = {"ran": False, "care_effective": False, "cat_status_ok": False,
                    "skipped_cooldown": True}
        with patch("home_system.cat_status", return_value=cat_status), \
             patch("heartbeat._try_pet_care", new=AsyncMock(return_value=care_ret)):
            await heartbeat._free_activity_check_cat(self._now())
            self.assertFalse(heartbeat._free_activity_cat_check["care_pending"])


# ============================================================
# 5. _try_pet_care 返回值结构
# ============================================================
class TestTryPetCareReturn(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_skipped_returns_skipped_cooldown(self):
        """冷却期内跳过 → 返回 skipped_cooldown=True。"""
        import heartbeat
        heartbeat._pet_care_last_fire.clear()
        # 设一个最近的触发时间（在冷却期内）
        heartbeat._pet_care_last_fire["hungry_cat"] = time.time()
        ret = await heartbeat._try_pet_care("hungry_cat", datetime.datetime(2026, 8, 17, 10, 0))
        self.assertIsInstance(ret, dict)
        self.assertTrue(ret.get("skipped_cooldown"))
        self.assertFalse(ret.get("ran"))
        heartbeat._pet_care_last_fire.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
