"""
test_home_state.py — Home Runtime Phase 3 测试
===============================================
覆盖：
- state.py 纯函数（elapsed-time/clamp/衰减/恢复/陪伴/宠物策略）
- service.py 写操作（初始化/结算/进入房间/休息/睡眠/陪伴）参数校验
- 行动幂等逻辑（action_key 重复返回）
"""

import unittest
from unittest.mock import patch, MagicMock

from home import state as st
from home import service as svc
from home import schemas as sch


# ============================================================
# 1. 纯函数：clamp
# ============================================================

class Test01Clamp(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(st.clamp(50), 50.0)

    def test_below_min(self):
        self.assertEqual(st.clamp(-10), 0.0)

    def test_above_max(self):
        self.assertEqual(st.clamp(150), 100.0)

    def test_boundary_zero(self):
        self.assertEqual(st.clamp(0), 0.0)

    def test_boundary_hundred(self):
        self.assertEqual(st.clamp(100), 100.0)

    def test_non_numeric(self):
        self.assertEqual(st.clamp("abc"), 0.0)

    def test_none(self):
        self.assertEqual(st.clamp(None), 0.0)

    def test_custom_range(self):
        self.assertEqual(st.clamp(50, 10, 90), 50.0)
        self.assertEqual(st.clamp(5, 10, 90), 10.0)


# ============================================================
# 2. 纯函数：elapsed time
# ============================================================

class Test02ElapsedHours(unittest.TestCase):
    def test_normal(self):
        # 1 小时
        h = st.compute_elapsed_hours(3600.0, 0.0)
        self.assertAlmostEqual(h, 1.0, places=2)

    def test_negative_time(self):
        # 时钟回拨
        h = st.compute_elapsed_hours(0.0, 3600.0)
        self.assertEqual(h, 0.0)

    def test_zero_diff(self):
        h = st.compute_elapsed_hours(100.0, 100.0)
        self.assertEqual(h, 0.0)

    def test_none_inputs(self):
        self.assertEqual(st.compute_elapsed_hours(None, 100.0), 0.0)
        self.assertEqual(st.compute_elapsed_hours(100.0, None), 0.0)


class Test03ShouldSettle(unittest.TestCase):
    def test_zero_elapsed(self):
        ok, reason = st.should_settle(0.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_elapsed")

    def test_negative_elapsed(self):
        ok, reason = st.should_settle(-1.0)
        self.assertFalse(ok)

    def test_too_short(self):
        # 30 秒 = 0.00833 小时
        ok, reason = st.should_settle(30.0 / 3600.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "interval_too_short")

    def test_enough(self):
        ok, reason = st.should_settle(1.0)
        self.assertTrue(ok)


class Test04CapElapsed(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(st.cap_elapsed(10.0), 10.0)

    def test_over_cap(self):
        self.assertEqual(st.cap_elapsed(100.0), 48.0)

    def test_exactly_cap(self):
        self.assertEqual(st.cap_elapsed(48.0), 48.0)


# ============================================================
# 3. 纯函数：AI 清醒衰减
# ============================================================

class Test05SettleAiAwake(unittest.TestCase):
    def test_normal_decay(self):
        state = {"hunger": 70, "energy": 70, "comfort": 60, "connection": 60, "cleanliness": 80}
        result = st.settle_ai_awake(state, elapsed_hours=2.0)
        # hunger: 70 - 1.5*2 = 67
        self.assertAlmostEqual(result["new_state"]["hunger"], 67.0, places=1)
        # energy: 70 - 1.0*2 = 68
        self.assertAlmostEqual(result["new_state"]["energy"], 68.0, places=1)

    def test_hunger_floor_zero(self):
        state = {"hunger": 1, "energy": 70, "comfort": 60, "connection": 60, "cleanliness": 80}
        result = st.settle_ai_awake(state, elapsed_hours=10.0)
        self.assertEqual(result["new_state"]["hunger"], 0.0)

    def test_connection_floor(self):
        state = {"hunger": 70, "energy": 70, "comfort": 60, "connection": 25, "cleanliness": 80}
        result = st.settle_ai_awake(state, elapsed_hours=10.0)
        # connection: 25 - 0.1*10 = 24, but floor is 20
        self.assertGreaterEqual(result["new_state"]["connection"], 20.0)

    def test_connection_never_below_floor(self):
        state = {"hunger": 70, "energy": 70, "comfort": 60, "connection": 21, "cleanliness": 80}
        result = st.settle_ai_awake(state, elapsed_hours=100.0)
        self.assertGreaterEqual(result["new_state"]["connection"], 20.0)

    def test_cap_elapsed(self):
        state = {"hunger": 100, "energy": 100, "comfort": 100, "connection": 100, "cleanliness": 100}
        result = st.settle_ai_awake(state, elapsed_hours=100.0)  # 超过 48h 封顶
        # hunger: 100 - 1.5*48 = 28
        self.assertAlmostEqual(result["new_state"]["hunger"], 28.0, places=1)

    def test_does_not_decay_intimacy(self):
        state = {"hunger": 70, "energy": 70, "comfort": 60, "connection": 60, "cleanliness": 80}
        result = st.settle_ai_awake(state, elapsed_hours=5.0)
        self.assertNotIn("intimacy", result["new_state"])

    def test_does_not_decay_health(self):
        state = {"hunger": 70, "energy": 70, "comfort": 60, "connection": 60, "cleanliness": 80}
        result = st.settle_ai_awake(state, elapsed_hours=5.0)
        self.assertNotIn("health", result["new_state"])

    def test_changes_list_populated(self):
        state = {"hunger": 70, "energy": 70, "comfort": 60, "connection": 60, "cleanliness": 80}
        result = st.settle_ai_awake(state, elapsed_hours=1.0)
        self.assertGreater(len(result["changes"]), 0)
        for change in result["changes"]:
            self.assertIn("field", change)
            self.assertIn("before", change)
            self.assertIn("after", change)
            self.assertIn("delta", change)


# ============================================================
# 4. 纯函数：休息/睡眠恢复
# ============================================================

class Test06RestRecovery(unittest.TestCase):
    def test_rest_energy_recovery(self):
        state = {"energy": 50, "comfort": 50, "hunger": 50}
        result = st.compute_rest_recovery(state, duration_minutes=60, mode="rest")
        # energy: 50 + 1.0*1 = 51
        self.assertAlmostEqual(result["new_state"]["energy"], 51.0, places=1)

    def test_sleep_energy_recovery(self):
        state = {"energy": 50, "comfort": 50, "hunger": 50}
        result = st.compute_rest_recovery(state, duration_minutes=60, mode="sleep")
        # energy: 50 + 2.0*1 = 52
        self.assertAlmostEqual(result["new_state"]["energy"], 52.0, places=1)

    def test_sleep_hunger_decreases(self):
        state = {"energy": 50, "comfort": 50, "hunger": 50}
        result = st.compute_rest_recovery(state, duration_minutes=120, mode="sleep")
        # hunger: 50 - 0.5*2 = 49
        self.assertLess(result["new_state"]["hunger"], 50)

    def test_rest_hunger_unchanged(self):
        state = {"energy": 50, "comfort": 50, "hunger": 50}
        result = st.compute_rest_recovery(state, duration_minutes=60, mode="rest")
        # rest 模式 hunger 不在 changes 中（不额外变化）
        change_fields = [c["field"] for c in result["changes"]]
        self.assertNotIn("hunger", change_fields)

    def test_energy_capped_at_100(self):
        state = {"energy": 99, "comfort": 50, "hunger": 50}
        result = st.compute_rest_recovery(state, duration_minutes=600, mode="sleep")
        self.assertLessEqual(result["new_state"]["energy"], 100.0)

    def test_does_not_recover_intimacy(self):
        state = {"energy": 50, "comfort": 50, "hunger": 50}
        result = st.compute_rest_recovery(state, duration_minutes=60, mode="rest")
        self.assertNotIn("intimacy", result["new_state"])

    def test_does_not_recover_health(self):
        state = {"energy": 50, "comfort": 50, "hunger": 50}
        result = st.compute_rest_recovery(state, duration_minutes=60, mode="sleep")
        self.assertNotIn("health", result["new_state"])


# ============================================================
# 5. 纯函数：陪伴互动
# ============================================================

class Test07SpendTimeGains(unittest.TestCase):
    def test_comfort_connection_increase(self):
        actor = {"comfort": 50, "connection": 30, "intimacy": 30}
        target = {"comfort": 50, "connection": 30, "intimacy": 30}
        result = st.compute_spend_time_gains(actor, target, today_intimacy_gain=0.0)
        self.assertGreater(result["new_actor_state"]["comfort"], 50)
        self.assertGreater(result["new_actor_state"]["connection"], 30)

    def test_intimacy_daily_cap(self):
        actor = {"comfort": 50, "connection": 30, "intimacy": 30}
        target = {"comfort": 50, "connection": 30, "intimacy": 30}
        # 今日已增长 3.0 = 上限
        result = st.compute_spend_time_gains(actor, target, today_intimacy_gain=3.0)
        self.assertEqual(result["intimacy_delta"], 0.0)
        self.assertEqual(result["new_actor_state"]["intimacy"], 30)

    def test_intimacy_partial_cap(self):
        actor = {"comfort": 50, "connection": 30, "intimacy": 30}
        target = {"comfort": 50, "connection": 30, "intimacy": 30}
        # 今日已增长 2.5，剩余 0.5
        result = st.compute_spend_time_gains(actor, target, today_intimacy_gain=2.5)
        self.assertAlmostEqual(result["intimacy_delta"], 0.5, places=2)

    def test_both_members_updated(self):
        actor = {"comfort": 50, "connection": 30, "intimacy": 30}
        target = {"comfort": 40, "connection": 20, "intimacy": 25}
        result = st.compute_spend_time_gains(actor, target, today_intimacy_gain=0.0)
        self.assertGreater(result["new_target_state"]["comfort"], 40)
        self.assertGreater(result["new_target_state"]["connection"], 20)


# ============================================================
# 6. 纯函数：宠物策略
# ============================================================

class Test08PetStrategy(unittest.TestCase):
    def test_pet_not_settled(self):
        self.assertFalse(st.should_settle_pet("pet"))

    def test_ai_settled(self):
        self.assertTrue(st.should_settle_pet("ai"))

    def test_doll_settled(self):
        self.assertTrue(st.should_settle_pet("doll"))


# ============================================================
# 7. 纯函数：默认初始值
# ============================================================

class Test09DefaultStates(unittest.TestCase):
    def test_ai_defaults(self):
        s = st.default_ai_initial_state()
        self.assertEqual(s["hunger"], 70)
        self.assertEqual(s["energy"], 70)
        self.assertEqual(s["health"], 100)
        self.assertEqual(s["intimacy"], 50)
        # 不应该是满值或濒危值
        for v in s.values():
            self.assertGreater(v, 0)
            self.assertLessEqual(v, 100)

    def test_pet_defaults(self):
        s = st.default_pet_initial_state()
        self.assertEqual(s["hunger"], 50)
        self.assertEqual(s["energy"], 80)
        for v in s.values():
            self.assertGreater(v, 0)
            self.assertLessEqual(v, 100)


# ============================================================
# 8. service 写操作参数校验
# ============================================================

class Test10ServiceValidation(unittest.TestCase):
    def test_settle_empty_key(self):
        result = svc.settle_member("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_MEMBER_KEY")

    @patch("home.repository.rpc_settle_member")
    def test_settle_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "settled": True}
        result = svc.settle_member("ai_primary")
        self.assertTrue(result["ok"])

    def test_enter_room_empty_action_key(self):
        result = svc.enter_room("ai_primary", "living_room", "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ACTION_KEY")

    def test_enter_room_empty_member(self):
        result = svc.enter_room("", "living_room", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_MEMBER_KEY")

    def test_enter_room_empty_room(self):
        result = svc.enter_room("ai_primary", "", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ROOM_KEY")

    @patch("home.repository.rpc_enter_room")
    def test_enter_room_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "status": "succeeded"}
        result = svc.enter_room("ai_primary", "living_room", "act1")
        self.assertTrue(result["ok"])

    def test_rest_empty_action_key(self):
        result = svc.rest("ai_primary", 30, "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ACTION_KEY")

    def test_rest_invalid_mode(self):
        result = svc.rest("ai_primary", 30, "act1", mode="dance")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_MODE")

    def test_rest_duration_too_short(self):
        result = svc.rest("ai_primary", 0, "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DURATION_TOO_SHORT")

    def test_rest_duration_too_long(self):
        result = svc.rest("ai_primary", 1441, "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DURATION_TOO_LONG")

    def test_rest_invalid_duration_type(self):
        result = svc.rest("ai_primary", "abc", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_DURATION")

    @patch("home.repository.rpc_rest")
    def test_sleep_calls_rest_with_sleep_mode(self, mock_rpc):
        mock_rpc.return_value = {"ok": True}
        svc.sleep("ai_primary", 480, "act1")
        mock_rpc.assert_called_once_with("act1", "ai_primary", 480, "sleep")

    def test_spend_time_empty_action_key(self):
        result = svc.spend_time("ai_primary", "pet_xiaoman", "摸摸头", 30, "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ACTION_KEY")

    def test_spend_time_empty_target(self):
        result = svc.spend_time("ai_primary", "", "摸摸头", 30, "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_TARGET_KEY")

    def test_spend_time_empty_activity(self):
        result = svc.spend_time("ai_primary", "pet_xiaoman", "", 30, "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ACTIVITY")

    def test_spend_time_duration_out_of_range(self):
        result = svc.spend_time("ai_primary", "pet_xiaoman", "摸摸头", 0, "act1")
        self.assertFalse(result["ok"])
        result = svc.spend_time("ai_primary", "pet_xiaoman", "摸摸头", 481, "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_spend_time")
    def test_spend_time_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True}
        result = svc.spend_time("ai_primary", "pet_xiaoman", "摸摸头", 30, "act1")
        self.assertTrue(result["ok"])


# ============================================================
# 9. service 初始化
# ============================================================

class Test11InitializeMembers(unittest.TestCase):
    @patch("home.repository.rpc_initialize_members")
    def test_initialize_calls_rpc(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "ai_created": True, "pet_created": True}
        result = svc.initialize_members()
        self.assertTrue(result["ok"])
        mock_rpc.assert_called_once()

    @patch("home.repository.rpc_initialize_members")
    def test_initialize_idempotent(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "ai_created": False, "pet_created": False, "message": "成员已存在"}
        result = svc.initialize_members()
        self.assertTrue(result["ok"])


# ============================================================
# 10. 行动幂等（mock RPC 返回 ACTION_EXISTS）
# ============================================================

class Test12ActionIdempotency(unittest.TestCase):
    @patch("home.repository.rpc_enter_room")
    def test_duplicate_action_returns_exists(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS", "action_key": "act1"}
        result = svc.enter_room("ai_primary", "living_room", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ACTION_EXISTS")

    @patch("home.repository.rpc_rest")
    def test_duplicate_rest_returns_exists(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.rest("ai_primary", 30, "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ACTION_EXISTS")

    @patch("home.repository.rpc_spend_time")
    def test_duplicate_spend_time_returns_exists(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.spend_time("ai_primary", "pet_xiaoman", "摸摸头", 30, "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ACTION_EXISTS")


# ============================================================
# 11. 数据库不可用降级
# ============================================================

class Test13DBUnavailable(unittest.TestCase):
    @patch("home.repository._get_supabase")
    def test_settle_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.settle_member("ai_primary")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DB_UNAVAILABLE")

    @patch("home.repository._get_supabase")
    def test_enter_room_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.enter_room("ai_primary", "living_room", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DB_UNAVAILABLE")

    @patch("home.repository._get_supabase")
    def test_initialize_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.initialize_members()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DB_UNAVAILABLE")


# ============================================================
# 12. 安全：不泄露敏感信息
# ============================================================

class Test14Security(unittest.TestCase):
    @patch("home.repository.rpc_enter_room")
    def test_enter_room_result_no_secrets(self, mock_rpc):
        mock_rpc.return_value = {
            "ok": True, "action_key": "act1", "status": "succeeded",
            "narrative_facts": ["进入了客厅"]
        }
        result = svc.enter_room("ai_primary", "living_room", "act1")
        result_str = str(result)
        for kw in ("api_key", "token", "password", "secret", "cookie"):
            self.assertNotIn(kw, result_str.lower())

    @patch("home.repository.rpc_rest")
    def test_rest_result_no_secrets(self, mock_rpc):
        mock_rpc.return_value = {
            "ok": True, "action_key": "act1", "status": "succeeded",
            "changes": [{"field": "energy", "before": 50, "after": 70}]
        }
        result = svc.rest("ai_primary", 60, "act1")
        result_str = str(result)
        for kw in ("api_key", "token", "password", "secret", "cookie"):
            self.assertNotIn(kw, result_str.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
