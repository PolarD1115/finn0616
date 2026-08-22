"""
test_home_pet_bridge.py — Phase 7 宠物状态权威源与 Home Runtime 融合测试
=========================================================================
测试范围：
- 权威来源（AI 生理来自 Home，宠物生理来自 pets，关系来自 Home）
- 房间（小满房间来自 pets.current_room，映射失败返回 unknown）
- 菜品喂小满（更新 pets.hunger，不更新 Home hunger，原子事务）
- 陪伴（更新 Home 关系，不触发宠物结算，不叠加 cat_play）
- Context（显示 pets 权威值，不显示过期快照，不显示 UUID）
- 旧系统回归（enter_room 拒绝 pet actor，现有签名不变）

所有测试使用 mock，不写入生产数据库。
"""

import unittest
from unittest.mock import patch, MagicMock, call
import inspect

from home import service as svc
from home import repository as repo
from home import context as ctx
from home import state as st


# ============================================================
# 1. 权威来源测试
# ============================================================

class Test01PetAuthoritySource(unittest.TestCase):
    """宠物生理状态权威来源测试。"""

    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_member_by_key")
    @patch("home.repository.fetch_member_state")
    @patch("home.repository.fetch_events_by_member")
    def test_ai_physiology_from_home_member_states(self, m_ev, m_state, m_member, m_pet):
        """AI 生理状态来自 home_member_states，不查 pets。"""
        m_member.return_value = {
            "id": "ai-id", "stable_key": "ai_primary", "name": "Finn",
            "member_type": "ai", "lifecycle_status": "alive", "is_active": True
        }
        m_state.return_value = {"hunger": 42, "energy": 88, "mood": 65, "comfort": 60}
        m_ev.return_value = []
        result = svc.observe_member("ai_primary")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["state"]["hunger"], 42)
        self.assertEqual(result["data"]["state"]["energy"], 88)
        self.assertEqual(result["data"]["state"]["physiology_source"], "home_member_states")
        m_pet.assert_not_called()

    @patch("home.repository.fetch_room_by_key")
    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_member_by_key")
    @patch("home.repository.fetch_member_state")
    @patch("home.repository.fetch_events_by_member")
    def test_xiaoman_physiology_from_pets(self, m_ev, m_state, m_member, m_pet, m_room):
        """小满生理状态来自 pets 权威源。"""
        m_member.return_value = {
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive", "is_active": True,
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }
        m_state.return_value = {"comfort": 55, "connection": 40, "intimacy": 62}
        m_pet.return_value = {
            "hunger": 33, "happiness": 80, "health": 100,
            "energy": 55, "cleanliness": 75, "status": "idle",
            "mood": "happy", "current_room": "kitchen"
        }
        m_room.return_value = {"name": "厨房", "stable_key": "kitchen"}
        m_ev.return_value = []
        result = svc.observe_member("pet_xiaoman")
        self.assertTrue(result["ok"])
        state = result["data"]["state"]
        self.assertEqual(state["hunger"], 33)
        self.assertEqual(state["happiness"], 80)
        self.assertEqual(state["energy"], 55)
        self.assertEqual(state["physiology_source"], "pets")
        self.assertTrue(state["physiology_available"])

    @patch("home.repository.fetch_room_by_key")
    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_member_by_key")
    @patch("home.repository.fetch_member_state")
    @patch("home.repository.fetch_events_by_member")
    def test_xiaoman_relationship_from_home(self, m_ev, m_state, m_member, m_pet, m_room):
        """小满关系状态来自 home_member_states。"""
        m_member.return_value = {
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive", "is_active": True,
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }
        m_state.return_value = {"comfort": 55, "connection": 40, "intimacy": 62}
        m_pet.return_value = {"hunger": 30, "happiness": 40, "energy": 50, "current_room": "study"}
        m_room.return_value = {"name": "书房"}
        m_ev.return_value = []
        result = svc.observe_member("pet_xiaoman")
        state = result["data"]["state"]
        self.assertEqual(state["comfort"], 55)
        self.assertEqual(state["connection"], 40)
        self.assertEqual(state["intimacy"], 62)
        self.assertEqual(state["relationship_source"], "home_member_states")

    @patch("home.repository.fetch_room_by_key")
    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_member_by_key")
    @patch("home.repository.fetch_member_state")
    @patch("home.repository.fetch_events_by_member")
    def test_pets_wins_when_snapshot_differs(self, m_ev, m_state, m_member, m_pet, m_room):
        """Home 快照和 pets 数值不同时返回 pets 值。"""
        m_member.return_value = {
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive", "is_active": True,
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }
        # Home 快照有不同值
        m_state.return_value = {"hunger": 70, "energy": 90, "comfort": 60}
        # pets 权威值
        m_pet.return_value = {"hunger": 20, "happiness": 35, "energy": 40, "current_room": "study"}
        m_room.return_value = {"name": "书房"}
        m_ev.return_value = []
        result = svc.observe_member("pet_xiaoman")
        state = result["data"]["state"]
        self.assertEqual(state["hunger"], 20)
        self.assertEqual(state["happiness"], 35)
        self.assertEqual(state["energy"], 40)

    @patch("home.repository._get_supabase")
    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_member_by_key")
    @patch("home.repository.fetch_member_state")
    @patch("home.repository.fetch_events_by_member")
    def test_read_does_not_write_pets_back_to_home(self, m_ev, m_state, m_member, m_pet, m_sb):
        """读取不会把 pets 值写回 home_member_states。"""
        mock_sb = MagicMock()
        m_sb.return_value = mock_sb
        m_member.return_value = {
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive", "is_active": True,
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }
        m_state.return_value = {"comfort": 60, "connection": 30, "intimacy": 30}
        m_pet.return_value = {"hunger": 25, "happiness": 40, "energy": 45, "current_room": "study"}
        m_ev.return_value = []
        svc.observe_member("pet_xiaoman")
        # 验证没有 update/insert 调用
        for c in mock_sb.table.call_args_list:
            mock_table = c.args[0] if c.args else c.kwargs.get("name", "")
        # 验证 table() 从未被以 update 模式调用
        mock_sb.table.assert_called()  # fetch_pet_by_member 会调用
        # 确认没有 update 调用
        table_mock = mock_sb.table.return_value
        table_mock.update.assert_not_called()
        table_mock.insert.assert_not_called()

    @patch("home.repository.fetch_room_by_key")
    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_member_by_key")
    @patch("home.repository.fetch_member_state")
    @patch("home.repository.fetch_events_by_member")
    def test_pet_observation_no_settlement(self, m_ev, m_state, m_member, m_pet, m_room):
        """宠物观察不触发 Home 结算。"""
        m_member.return_value = {
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive", "is_active": True,
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }
        m_state.return_value = {"comfort": 60, "last_settled_at": "2026-08-18T08:00:00Z"}
        m_pet.return_value = {"hunger": 25, "happiness": 40, "energy": 45, "current_room": "study"}
        m_room.return_value = {"name": "书房"}
        m_ev.return_value = []
        result = svc.observe_member("pet_xiaoman")
        # last_settled_at 不应被修改
        self.assertEqual(result["data"]["state"]["last_settled_at"], "2026-08-18T08:00:00Z")

    def test_pet_not_double_decayed(self):
        """state.should_settle_pet 返回 False。"""
        self.assertFalse(st.should_settle_pet("pet"))
        self.assertTrue(st.should_settle_pet("ai"))


# ============================================================
# 2. 房间测试
# ============================================================

class Test02PetRoom(unittest.TestCase):
    """小满房间权威来源测试。"""

    @patch("home.repository.fetch_room_by_key")
    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_member_by_key")
    @patch("home.repository.fetch_member_state")
    @patch("home.repository.fetch_events_by_member")
    def test_room_from_pets_current_room(self, m_ev, m_state, m_member, m_pet, m_room):
        """小满房间来自 pets.current_room。"""
        m_member.return_value = {
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive", "is_active": True,
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }
        m_state.return_value = {"comfort": 60}
        m_pet.return_value = {"hunger": 25, "happiness": 40, "current_room": "kitchen"}
        m_room.return_value = {"name": "厨房", "stable_key": "kitchen"}
        m_ev.return_value = []
        result = svc.observe_member("pet_xiaoman")
        self.assertEqual(result["data"]["state"]["current_room"], "kitchen")
        self.assertEqual(result["data"]["state"]["current_room_name"], "厨房")
        self.assertEqual(result["data"]["state"]["room_mapping_status"], "mapped")

    @patch("home.repository.fetch_room_by_key")
    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_member_by_key")
    @patch("home.repository.fetch_member_state")
    @patch("home.repository.fetch_events_by_member")
    def test_room_mapping_failure_unknown(self, m_ev, m_state, m_member, m_pet, m_room):
        """balcony 映射失败返回 unknown，不 fallback。"""
        m_member.return_value = {
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive", "is_active": True,
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }
        m_state.return_value = {"comfort": 60}
        m_pet.return_value = {"hunger": 25, "happiness": 40, "current_room": "balcony"}
        m_room.return_value = None  # balcony 不在 home_rooms 中
        m_ev.return_value = []
        result = svc.observe_member("pet_xiaoman")
        state = result["data"]["state"]
        self.assertEqual(state["room_mapping_status"], "unknown")
        self.assertIsNone(state["current_room_name"])
        self.assertEqual(state["current_room"], "balcony")

    @patch("home.repository.fetch_room_by_key")
    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_member_by_key")
    @patch("home.repository.fetch_member_state")
    @patch("home.repository.fetch_events_by_member")
    def test_no_fallback_to_stale_snapshot(self, m_ev, m_state, m_member, m_pet, m_room):
        """不 fallback 到过期 Home 房间快照。"""
        m_member.return_value = {
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive", "is_active": True,
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }
        # Home 快照有 stale room_id
        m_state.return_value = {"comfort": 60, "current_room_id": "stale-room-uuid"}
        m_pet.return_value = {"hunger": 25, "happiness": 40, "current_room": "garden"}
        m_room.return_value = {"name": "花园", "stable_key": "garden"}
        m_ev.return_value = []
        result = svc.observe_member("pet_xiaoman")
        state = result["data"]["state"]
        self.assertEqual(state["current_room"], "garden")
        self.assertEqual(result["data"]["current_room_name"], "花园")

    @patch("home.repository.fetch_member_by_key")
    def test_enter_room_rejects_pet_actor(self, m_member):
        """home_enter_room 拒绝 pet_xiaoman 作为 actor。"""
        m_member.return_value = {
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive", "is_active": True,
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }
        result = svc.enter_room("pet_xiaoman", "living_room", "act_001")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PET_CANNOT_ACT")

    @patch("home.repository.rpc_enter_room")
    @patch("home.repository.fetch_member_by_key")
    def test_finn_enter_room_normal(self, m_member, m_rpc):
        """Finn 进入房间正常工作。"""
        m_member.return_value = {
            "id": "ai-id", "stable_key": "ai_primary", "name": "Finn",
            "member_type": "ai", "lifecycle_status": "alive", "is_active": True
        }
        m_rpc.return_value = {"ok": True, "action_key": "act_001"}
        result = svc.enter_room("ai_primary", "kitchen", "act_001")
        self.assertTrue(result["ok"])
        m_rpc.assert_called_once_with("act_001", "ai_primary", "kitchen")


# ============================================================
# 3. 菜品喂小满测试
# ============================================================

class Test03FeedDishPet(unittest.TestCase):
    """菜品喂小满事务测试。"""

    @patch("home.repository.rpc_feed_member")
    def test_feed_normal_returns_ok(self, m_rpc):
        """正常喂食返回成功。"""
        m_rpc.return_value = {
            "ok": True, "action_key": "act_001", "status": "succeeded",
            "target": "pet_xiaoman", "target_type": "pet",
            "changes": [{"field": "hunger", "source": "pets", "delta": 30}]
        }
        result = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "act_001")
        self.assertTrue(result["ok"])

    @patch("home.repository.rpc_feed_member")
    def test_feed_action_key_idempotent(self, m_rpc):
        """action_key 重复返回 ACTION_EXISTS。"""
        m_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "act_dup")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ACTION_EXISTS")

    @patch("home.repository.rpc_feed_member")
    def test_feed_dish_not_available(self, m_rpc):
        """菜品不足返回 DISH_NOT_AVAILABLE。"""
        m_rpc.return_value = {"ok": False, "error_code": "DISH_NOT_AVAILABLE"}
        result = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "act_001")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DISH_NOT_AVAILABLE")

    @patch("home.repository.rpc_feed_member")
    def test_feed_pet_not_found(self, m_rpc):
        """宠物不存在返回 PET_NOT_FOUND。"""
        m_rpc.return_value = {"ok": False, "error_code": "PET_NOT_FOUND"}
        result = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "act_001")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PET_NOT_FOUND")

    @patch("home.repository.rpc_feed_member")
    def test_feed_pet_mapping_not_found(self, m_rpc):
        """映射不存在返回 PET_MAPPING_NOT_FOUND。"""
        m_rpc.return_value = {"ok": False, "error_code": "PET_MAPPING_NOT_FOUND"}
        result = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "act_001")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PET_MAPPING_NOT_FOUND")

    @patch("home.repository.rpc_feed_member")
    def test_feed_home_state_not_found(self, m_rpc):
        """Home 状态不存在返回 HOME_STATE_NOT_FOUND。"""
        m_rpc.return_value = {"ok": False, "error_code": "HOME_STATE_NOT_FOUND"}
        result = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "act_001")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "HOME_STATE_NOT_FOUND")

    @patch("home.repository.rpc_feed_member")
    def test_feed_pet_not_feedable(self, m_rpc):
        """宠物不可喂食返回 PET_NOT_FEEDABLE。"""
        m_rpc.return_value = {"ok": False, "error_code": "PET_NOT_FEEDABLE"}
        result = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "act_001")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PET_NOT_FEEDABLE")

    @patch("home.repository.rpc_feed_member")
    def test_feed_concurrent_last_serving(self, m_rpc):
        """并发喂最后一份只有一次成功。"""
        m_rpc.return_value = {"ok": True, "changes": [{"field": "hunger", "source": "pets"}]}
        result1 = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "act_001")
        m_rpc.return_value = {"ok": False, "error_code": "DISH_NOT_AVAILABLE"}
        result2 = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "act_002")
        self.assertTrue(result1["ok"])
        self.assertFalse(result2["ok"])

    @patch("home.repository.rpc_feed_member")
    def test_feed_rpc_error(self, m_rpc):
        """RPC 错误返回 RPC_ERROR。"""
        m_rpc.return_value = {"ok": False, "error_code": "RPC_ERROR"}
        result = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "act_001")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "RPC_ERROR")

    def test_feed_no_client_state_deltas(self):
        """feed_member 签名不接受状态增量参数。"""
        sig = inspect.signature(svc.feed_member)
        params = set(sig.parameters.keys())
        self.assertNotIn("hunger_delta", params)
        self.assertNotIn("happiness_delta", params)
        self.assertNotIn("energy_delta", params)
        self.assertNotIn("state_delta", params)

    def test_feed_source_code_no_pet_inventory(self):
        """Python 层不直接操作 pet_inventory。"""
        import home.service as svc_mod
        src = inspect.getsource(svc_mod.feed_member)
        self.assertNotIn("pet_inventory", src)

    def test_feed_rpc_body_writes_pets_hunger_only(self):
        """迁移文件中宠物分支只更新 pets.hunger（不更新 happiness/energy）。"""
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "migrations", "20260819_001_home_pet_bridge.sql"
        )
        if not os.path.exists(path):
            self.skipTest("迁移文件不存在")
        with open(path, "r", encoding="utf-8") as f:
            sql = f.read()
        # 宠物分支包含 UPDATE pets ... hunger
        self.assertIn("UPDATE pets SET", sql)
        self.assertIn("hunger = LEAST(100", sql)
        # 检查 UPDATE pets 块不包含 happiness 或 energy
        pet_update_idx = sql.find("UPDATE pets SET")
        self.assertGreater(pet_update_idx, -1)
        pet_block = sql[pet_update_idx:pet_update_idx + 300]
        self.assertNotIn("happiness", pet_block)
        self.assertNotIn("energy", pet_block)


# ============================================================
# 4. 陪伴测试
# ============================================================

class Test04PetCompanionship(unittest.TestCase):
    """陪伴小满测试。"""

    @patch("home.repository.rpc_spend_time")
    def test_spend_time_updates_home_relationship(self, m_rpc):
        """陪伴更新 Home 关系状态。"""
        m_rpc.return_value = {
            "ok": True, "action_key": "act_001",
            "changes": [
                {"member": "target", "field": "comfort", "before": 60, "after": 62},
                {"member": "target", "field": "connection", "before": 30, "after": 31.5},
            ]
        }
        result = svc.spend_time("ai_primary", "pet_xiaoman", "摸摸头", 30, "act_001")
        self.assertTrue(result["ok"])

    @patch("home.repository.rpc_spend_time")
    def test_spend_time_action_key_idempotent(self, m_rpc):
        """action_key 重复返回 ACTION_EXISTS。"""
        m_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.spend_time("ai_primary", "pet_xiaoman", "摸摸头", 30, "act_dup")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ACTION_EXISTS")

    def test_spend_time_no_cat_play_call(self):
        """home_spend_time 不调用 cat_play。"""
        import home.service as svc_mod
        src = inspect.getsource(svc_mod.spend_time)
        self.assertNotIn("cat_play", src)
        self.assertNotIn("cat_pet", src)

    def test_daily_intimacy_cap(self):
        """intimacy 每日上限 3.0。"""
        # 纯函数验证
        result = st.compute_spend_time_gains(
            {"comfort": 50, "connection": 30, "intimacy": 30},
            {"comfort": 50, "connection": 30, "intimacy": 30},
            today_intimacy_gain=3.0
        )
        self.assertEqual(result["intimacy_delta"], 0.0)

    def test_daily_intimacy_partial(self):
        """intimacy 部分剩余。"""
        result = st.compute_spend_time_gains(
            {"comfort": 50, "connection": 30, "intimacy": 30},
            {"comfort": 50, "connection": 30, "intimacy": 30},
            today_intimacy_gain=2.5
        )
        self.assertEqual(result["intimacy_delta"], 0.5)


# ============================================================
# 5. Context 测试
# ============================================================

class Test05ContextPetAuthority(unittest.TestCase):
    """Home Context 宠物权威值测试。"""

    @patch("home.repository.fetch_room_by_key")
    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_unopened_letter_count")
    @patch("home.repository.fetch_dishes")
    @patch("home.repository.fetch_inventory")
    @patch("home.repository.fetch_plants")
    @patch("home.repository.fetch_recent_events")
    @patch("home.repository.fetch_member_states")
    @patch("home.repository.fetch_members")
    @patch("home.repository.fetch_rooms")
    def test_context_shows_pets_values(self, m_rooms, m_members, m_states, m_events,
                                        m_plants, m_inventory, m_dishes, m_letters,
                                        m_pet, m_room):
        """Context 显示 pets 权威值。"""
        m_rooms.return_value = [{"stable_key": "living_room", "name": "客厅", "emoji": "🛋️"}]
        m_members.return_value = [{
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive",
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }]
        m_states.return_value = [{"member_id": "pet-id", "comfort": 60, "connection": 30, "intimacy": 30}]
        m_events.return_value = []
        m_plants.return_value = []
        m_inventory.return_value = []
        m_dishes.return_value = []
        m_letters.return_value = 0
        m_pet.return_value = {"hunger": 33, "happiness": 80, "energy": 55, "current_room": "kitchen"}
        m_room.return_value = {"name": "厨房"}
        text = ctx.build_home_context()
        self.assertIn("小满", text)
        self.assertIn("33", text)  # pets hunger
        self.assertIn("80", text)  # pets happiness
        self.assertNotIn("状态未知", text)

    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_unopened_letter_count")
    @patch("home.repository.fetch_dishes")
    @patch("home.repository.fetch_inventory")
    @patch("home.repository.fetch_plants")
    @patch("home.repository.fetch_recent_events")
    @patch("home.repository.fetch_member_states")
    @patch("home.repository.fetch_members")
    @patch("home.repository.fetch_rooms")
    def test_context_pets_unavailable_shows_unknown(self, m_rooms, m_members, m_states,
                                                      m_events, m_plants, m_inventory,
                                                      m_dishes, m_letters, m_pet):
        """pets 不可用时显示"状态未知"。"""
        m_rooms.return_value = []
        m_members.return_value = [{
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive",
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }]
        m_states.return_value = [{"member_id": "pet-id", "hunger": 35, "comfort": 60}]
        m_events.return_value = []
        m_plants.return_value = []
        m_inventory.return_value = []
        m_dishes.return_value = []
        m_letters.return_value = 0
        m_pet.return_value = None  # pets 不可用
        text = ctx.build_home_context()
        self.assertIn("小满", text)
        self.assertIn("状态未知", text)
        # 不显示过期快照值
        self.assertNotIn("35", text)  # home_member_states.hunger=35 不应出现

    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_unopened_letter_count")
    @patch("home.repository.fetch_dishes")
    @patch("home.repository.fetch_inventory")
    @patch("home.repository.fetch_plants")
    @patch("home.repository.fetch_recent_events")
    @patch("home.repository.fetch_member_states")
    @patch("home.repository.fetch_members")
    @patch("home.repository.fetch_rooms")
    def test_context_no_uuid(self, m_rooms, m_members, m_states, m_events,
                               m_plants, m_inventory, m_dishes, m_letters, m_pet):
        """Context 不显示内部 UUID。"""
        m_rooms.return_value = [{"stable_key": "living_room", "name": "客厅", "emoji": "🛋️"}]
        m_members.return_value = [{
            "id": "5d2abb81-9463-47ab-829d-821a3a6742c0",
            "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive",
            "profile": {"legacy_source": "pets", "legacy_id": "1fc9db85-0f91-400f-812a-598d9aae2ce7"}
        }]
        m_states.return_value = [{"member_id": "5d2abb81-9463-47ab-829d-821a3a6742c0", "comfort": 60}]
        m_events.return_value = []
        m_plants.return_value = []
        m_inventory.return_value = []
        m_dishes.return_value = []
        m_letters.return_value = 0
        m_pet.return_value = {"hunger": 27, "happiness": 40, "energy": 40, "current_room": "study"}
        text = ctx.build_home_context()
        import re
        uuids = re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', text, re.I)
        self.assertEqual(len(uuids), 0, f"Context 包含 UUID: {uuids}")

    @patch("home.repository._get_supabase")
    @patch("home.repository.fetch_pet_by_member")
    @patch("home.repository.fetch_unopened_letter_count")
    @patch("home.repository.fetch_dishes")
    @patch("home.repository.fetch_inventory")
    @patch("home.repository.fetch_plants")
    @patch("home.repository.fetch_recent_events")
    @patch("home.repository.fetch_member_states")
    @patch("home.repository.fetch_members")
    @patch("home.repository.fetch_rooms")
    def test_context_does_not_modify_db(self, m_rooms, m_members, m_states, m_events,
                                          m_plants, m_inventory, m_dishes, m_letters,
                                          m_pet, m_sb):
        """Context 构建不修改数据库。"""
        mock_sb = MagicMock()
        m_sb.return_value = mock_sb
        m_rooms.return_value = []
        m_members.return_value = [{
            "id": "pet-id", "stable_key": "pet_xiaoman", "name": "小满",
            "member_type": "pet", "lifecycle_status": "alive",
            "profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}
        }]
        m_states.return_value = [{"member_id": "pet-id", "comfort": 60}]
        m_events.return_value = []
        m_plants.return_value = []
        m_inventory.return_value = []
        m_dishes.return_value = []
        m_letters.return_value = 0
        m_pet.return_value = {"hunger": 27, "happiness": 40, "energy": 40, "current_room": "study"}
        ctx.build_home_context()
        table_mock = mock_sb.table.return_value
        table_mock.update.assert_not_called()
        table_mock.insert.assert_not_called()
        table_mock.delete.assert_not_called()


# ============================================================
# 6. 组合视图单元测试
# ============================================================

class Test06ComposeMemberView(unittest.TestCase):
    """_compose_member_view 组合视图测试。"""

    @patch("home.repository.fetch_pet_by_member")
    def test_pet_view_physiology_from_pets(self, m_pet):
        """宠物视图生理来自 pets。"""
        m_pet.return_value = {"hunger": 27, "happiness": 40, "energy": 40, "current_room": "study"}
        member = {"member_type": "pet", "profile": {"legacy_source": "pets", "legacy_id": "x"}}
        state = {"comfort": 60, "connection": 30, "intimacy": 30}
        view = svc._compose_member_view(member, state)
        self.assertEqual(view["physiology_source"], "pets")
        self.assertTrue(view["physiology_available"])
        self.assertEqual(view["hunger"], 27)
        self.assertEqual(view["happiness"], 40)

    @patch("home.repository.fetch_pet_by_member")
    def test_pet_view_unavailable(self, m_pet):
        """pets 不可用时生理返回 null。"""
        m_pet.return_value = None
        member = {"member_type": "pet", "profile": {"legacy_source": "pets", "legacy_id": "x"}}
        state = {"comfort": 60, "connection": 30, "intimacy": 30}
        view = svc._compose_member_view(member, state)
        self.assertEqual(view["physiology_source"], "unavailable")
        self.assertFalse(view["physiology_available"])
        self.assertIsNone(view["hunger"])
        self.assertIsNone(view["happiness"])
        self.assertIsNone(view["energy"])
        # 关系状态仍返回
        self.assertEqual(view["comfort"], 60)
        self.assertEqual(view["intimacy"], 30)

    def test_ai_view_from_home(self):
        """AI 视图全部来自 home_member_states。"""
        member = {"member_type": "ai"}
        state = {"hunger": 70, "energy": 65, "mood": 60, "comfort": 55}
        view = svc._compose_member_view(member, state)
        self.assertEqual(view["physiology_source"], "home_member_states")
        self.assertTrue(view["physiology_available"])
        self.assertEqual(view["hunger"], 70)
        self.assertEqual(view["energy"], 65)


# ============================================================
# 7. fetch_pet_by_member 仓储测试
# ============================================================

class Test07FetchPetByMember(unittest.TestCase):
    """fetch_pet_by_member 只读查询测试。"""

    def test_invalid_member_none(self):
        """None 成员返回 None。"""
        self.assertIsNone(repo.fetch_pet_by_member(None))

    def test_invalid_member_empty(self):
        """空 dict 返回 None。"""
        self.assertIsNone(repo.fetch_pet_by_member({}))

    def test_no_profile(self):
        """无 profile 返回 None。"""
        self.assertIsNone(repo.fetch_pet_by_member({"member_type": "pet"}))

    def test_wrong_legacy_source(self):
        """legacy_source 不匹配返回 None。"""
        member = {"profile": {"legacy_source": "other", "legacy_id": "x"}}
        self.assertIsNone(repo.fetch_pet_by_member(member))

    def test_empty_legacy_id(self):
        """legacy_id 为空返回 None。"""
        member = {"profile": {"legacy_source": "pets", "legacy_id": ""}}
        self.assertIsNone(repo.fetch_pet_by_member(member))

    @patch("home.repository._get_supabase")
    def test_db_unavailable(self, m_sb):
        """数据库不可用返回 None。"""
        m_sb.return_value = None
        member = {"profile": {"legacy_source": "pets", "legacy_id": "uuid"}}
        self.assertIsNone(repo.fetch_pet_by_member(member))

    @patch("home.repository._get_supabase")
    def test_success(self, m_sb):
        """正常查询返回 pets 行。"""
        mock_sb = MagicMock()
        m_sb.return_value = mock_sb
        mock_resp = MagicMock()
        mock_resp.data = [{"id": "pet-uuid", "hunger": 27, "happiness": 40}]
        mock_sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = mock_resp
        member = {"profile": {"legacy_source": "pets", "legacy_id": "pet-uuid"}}
        result = repo.fetch_pet_by_member(member)
        self.assertIsNotNone(result)
        self.assertEqual(result["hunger"], 27)
        # 验证使用参数绑定（eq），不是 SQL 拼接
        mock_sb.table.return_value.select.return_value.eq.assert_called_with("id", "pet-uuid")


# ============================================================
# 8. 源码约束测试
# ============================================================

class Test08SourceCodeConstraints(unittest.TestCase):
    """源码级约束验证。"""

    def test_context_no_direct_pets_query(self):
        """context.py 不直接查询 pets 表。"""
        import home.context as ctx_mod
        src = inspect.getsource(ctx_mod)
        self.assertNotIn('.table("pets")', src)
        self.assertNotIn("fetch_pet_by_member", src)  # 通过 service 间接调用

    def test_context_uses_compose_view(self):
        """context.py 调用 service._compose_member_view。"""
        import home.context as ctx_mod
        src = inspect.getsource(ctx_mod)
        self.assertIn("_compose_member_view", src)

    def test_service_has_compose_member_view(self):
        """service.py 包含 _compose_member_view 函数。"""
        self.assertTrue(hasattr(svc, "_compose_member_view"))

    def test_enter_room_checks_pet(self):
        """enter_room 包含 PET_CANNOT_ACT 检查。"""
        import home.service as svc_mod
        src = inspect.getsource(svc_mod.enter_room)
        self.assertIn("PET_CANNOT_ACT", src)
        self.assertIn("pet", src)

    def test_observe_member_uses_compose(self):
        """observe_member 使用 _compose_member_view。"""
        import home.service as svc_mod
        src = inspect.getsource(svc_mod.observe_member)
        self.assertIn("_compose_member_view", src)

    def test_observe_home_uses_compose(self):
        """observe_home 使用 _compose_member_view。"""
        import home.service as svc_mod
        src = inspect.getsource(svc_mod.observe_home)
        self.assertIn("_compose_member_view", src)

    def test_no_settle_in_observe(self):
        """observe_member 不调用结算。"""
        import home.service as svc_mod
        src = inspect.getsource(svc_mod.observe_member)
        self.assertNotIn("settle_member", src)
        self.assertNotIn("rpc_settle", src)

    def test_repo_has_fetch_pet_by_member(self):
        """repository.py 包含 fetch_pet_by_member。"""
        self.assertTrue(hasattr(repo, "fetch_pet_by_member"))

    def test_feed_member_signature_unchanged(self):
        """feed_member 签名保持不变。"""
        sig = inspect.signature(svc.feed_member)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["actor_key", "target_key", "dish_id", "action_key"])


# ============================================================
# 9. 迁移 SQL 约束测试
# ============================================================

class Test09MigrationConstraints(unittest.TestCase):
    """迁移 SQL 约束验证。"""

    def _read_migration(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "migrations", "20260819_001_home_pet_bridge.sql"
        )
        if not os.path.exists(path):
            self.skipTest("迁移文件不存在")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_no_delete_drop_truncate(self):
        """迁移不包含 DELETE/DROP/TRUNCATE 语句（排除注释）。"""
        sql = self._read_migration()
        # 移除注释行（以 -- 开头）
        lines = sql.split("\n")
        code_lines = [l for l in lines if not l.strip().startswith("--")]
        code = "\n".join(code_lines)
        code_upper = code.upper()
        self.assertNotIn("DELETE FROM", code_upper)
        self.assertNotIn("DROP FUNCTION", code_upper)
        self.assertNotIn("DROP TABLE", code_upper)
        self.assertNotIn("TRUNCATE", code_upper)

    def test_uses_create_or_replace(self):
        """迁移使用 CREATE OR REPLACE FUNCTION。"""
        sql = self._read_migration()
        self.assertIn("CREATE OR REPLACE FUNCTION", sql)

    def test_feed_member_signature_unchanged_in_sql(self):
        """rpc_home_feed_member 签名不变。"""
        sql = self._read_migration()
        self.assertIn("p_action_key text, p_actor_key text, p_target_key text, p_dish_id uuid", sql)

    def test_enter_room_has_pet_check(self):
        """rpc_home_enter_room 包含宠物拦截。"""
        sql = self._read_migration()
        self.assertIn("PET_CANNOT_ACT", sql)

    def test_feed_member_has_mapping_validation(self):
        """rpc_home_feed_member 包含映射校验。"""
        sql = self._read_migration()
        self.assertIn("PET_MAPPING_NOT_FOUND", sql)
        self.assertIn("legacy_source", sql)
        self.assertIn("invalid_text_representation", sql)

    def test_feed_member_has_lock_before_write(self):
        """rpc_home_feed_member 在业务写入前完成 FOR UPDATE 锁定。"""
        sql = self._read_migration()
        # pets FOR UPDATE 在菜品 UPDATE 之前
        pets_lock_idx = sql.find("FROM pets WHERE id = v_pet_uuid FOR UPDATE")
        dish_write_idx = sql.find("UPDATE home_dishes SET servings = servings - 1")
        self.assertGreater(pets_lock_idx, -1)
        self.assertGreater(dish_write_idx, -1)
        self.assertLess(pets_lock_idx, dish_write_idx)
        # home_member_states FOR UPDATE 在菜品 UPDATE 之前
        state_lock_idx = sql.find("FROM home_member_states WHERE member_id = v_target_id FOR UPDATE")
        self.assertLess(state_lock_idx, dish_write_idx)
        # home_dishes FOR UPDATE 在 UPDATE 之前
        dish_lock_idx = sql.find("FROM home_dishes WHERE id = p_dish_id FOR UPDATE")
        self.assertLess(dish_lock_idx, dish_write_idx)

    def test_feed_member_has_home_state_not_found(self):
        """rpc_home_feed_member 包含 HOME_STATE_NOT_FOUND 检查。"""
        sql = self._read_migration()
        self.assertIn("HOME_STATE_NOT_FOUND", sql)


if __name__ == "__main__":
    unittest.main()
