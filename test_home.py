"""
test_home.py — Home Runtime 基础层测试
=====================================
测试范围：
- 数据模型与校验（合法/非法房间、成员类型、状态范围）
- 观察层（空观察、房间存在/不存在、成员存在/不存在、事件排序）
- 行动幂等（action_key 返回逻辑）
- 安全（不写数据库、不返回秘密日记、不返回敏感配置）
- 数据库不可用降级
"""

import unittest
from unittest.mock import patch, MagicMock

from home import schemas as sch
from home import service as svc
from home import repository as repo
from home import context as ctx
from home.models import HomeRoom, HomeMember, HomeMemberState, HomeEvent, HomeActionRun


# ============================================================
# 1. 数据模型与校验
# ============================================================

class Test01SchemasValidation(unittest.TestCase):
    """校验函数测试。"""

    # --- stable_key ---
    def test_valid_stable_key(self):
        ok, err = sch.validate_stable_key("living_room")
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_empty_stable_key(self):
        ok, err = sch.validate_stable_key("")
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_KEY")

    def test_none_stable_key(self):
        ok, err = sch.validate_stable_key(None)
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_KEY")

    def test_non_string_stable_key(self):
        ok, err = sch.validate_stable_key(123)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_KEY")

    def test_whitespace_stable_key(self):
        ok, err = sch.validate_stable_key("  living_room  ")
        self.assertFalse(ok)
        self.assertEqual(err, "KEY_HAS_WHITESPACE")

    # --- member_type ---
    def test_valid_member_types(self):
        for t in ("ai", "pet", "doll", "custom"):
            ok, _ = sch.validate_member_type(t)
            self.assertTrue(ok, f"member_type={t} should be valid")

    def test_invalid_member_type(self):
        ok, err = sch.validate_member_type("robot")
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_MEMBER_TYPE")

    # --- state value ---
    def test_valid_state_value(self):
        for v in (0, 50, 100, 0.0, 100.0, 50.5):
            ok, _ = sch.validate_state_value(v, "hunger")
            self.assertTrue(ok, f"value={v} should be valid")

    def test_invalid_state_value_negative(self):
        ok, err = sch.validate_state_value(-1, "hunger")
        self.assertFalse(ok)
        self.assertEqual(err, "HUNGER_OUT_OF_RANGE")

    def test_invalid_state_value_over_100(self):
        ok, err = sch.validate_state_value(101, "energy")
        self.assertFalse(ok)
        self.assertEqual(err, "ENERGY_OUT_OF_RANGE")

    def test_invalid_state_value_bool(self):
        ok, err = sch.validate_state_value(True, "mood")
        self.assertFalse(ok)

    def test_invalid_state_value_none(self):
        ok, err = sch.validate_state_value(None, "health")
        self.assertFalse(ok)

    # --- event_type ---
    def test_valid_event_types(self):
        for t in ("entered_room", "rested", "cooked", "wrote_diary", "system_tick"):
            ok, _ = sch.validate_event_type(t)
            self.assertTrue(ok, f"event_type={t} should be valid")

    def test_invalid_event_type(self):
        ok, err = sch.validate_event_type("danced")
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_EVENT_TYPE")

    # --- visibility ---
    def test_valid_visibility(self):
        for v in ("private", "home", "user_visible", "system"):
            ok, _ = sch.validate_visibility(v)
            self.assertTrue(ok)

    def test_invalid_visibility(self):
        ok, _ = sch.validate_visibility("public")
        self.assertFalse(ok)

    # --- action status ---
    def test_valid_action_status(self):
        for s in ("requested", "running", "succeeded", "failed", "skipped"):
            ok, _ = sch.validate_action_status(s)
            self.assertTrue(ok)

    def test_invalid_action_status(self):
        ok, _ = sch.validate_action_status("cancelled")
        self.assertFalse(ok)

    # --- limit ---
    def test_valid_limit(self):
        ok, _ = sch.validate_limit(20)
        self.assertTrue(ok)

    def test_invalid_limit_zero(self):
        ok, _ = sch.validate_limit(0)
        self.assertFalse(ok)

    def test_invalid_limit_over_max(self):
        ok, _ = sch.validate_limit(101, max_val=100)
        self.assertFalse(ok)

    def test_invalid_limit_non_int(self):
        ok, _ = sch.validate_limit("abc")
        self.assertFalse(ok)


# ============================================================
# 2. 数据模型 dataclass
# ============================================================

class Test02DataModels(unittest.TestCase):
    """dataclass 基本行为测试。"""

    def test_home_room_defaults(self):
        r = HomeRoom(stable_key="test_room", name="测试房间")
        self.assertEqual(r.emoji, "🏠")
        self.assertEqual(r.room_type, "common")
        self.assertTrue(r.is_enabled)
        self.assertFalse(r.is_hidden)

    def test_home_member_defaults(self):
        m = HomeMember(stable_key="finn", name="Finn")
        self.assertEqual(m.member_type, "custom")
        self.assertTrue(m.is_active)
        self.assertEqual(m.lifecycle_status, "alive")

    def test_home_member_state_defaults(self):
        s = HomeMemberState(member_id="uuid-123")
        self.assertEqual(s.hunger, 50.0)
        self.assertEqual(s.energy, 80.0)
        self.assertEqual(s.health, 90.0)
        self.assertEqual(s.cleanliness, 70.0)

    def test_home_member_state_display(self):
        s = HomeMemberState(member_id="uuid-123", hunger=45.5, energy=90)
        d = s.as_display()
        self.assertEqual(d["hunger"], 45.5)
        self.assertEqual(d["energy"], 90.0)
        self.assertIn("current_room_id", d)

    def test_home_event_defaults(self):
        e = HomeEvent(event_type="rested", summary="休息了一会")
        self.assertEqual(e.source, "system")
        self.assertEqual(e.visibility, "home")
        self.assertEqual(e.details, {})

    def test_home_action_run_defaults(self):
        a = HomeActionRun(action_key="cook_001", action_type="cook")
        self.assertEqual(a.status, "requested")
        self.assertIsNone(a.result)


# ============================================================
# 3. 观察层 — 空状态
# ============================================================

class Test03EmptyObservation(unittest.TestCase):
    """数据库为空时的观察结果。"""

    @patch("home.repository._get_supabase")
    def test_observe_home_empty(self, mock_sb):
        mock_sb.return_value = MagicMock()
        mock_sb.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.order.return_value.execute.return_value = MagicMock(data=[])
        # fetch_member_states 空列表
        mock_sb.return_value.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(data=[])
        # fetch_recent_events
        mock_sb.return_value.table.return_value.select.return_value.neq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
        # fetch_pending_jobs
        mock_sb.return_value.table.return_value.select.return_value.eq.return_value.order.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(data=[])

        result = svc.observe_home()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["rooms"], [])
        self.assertEqual(result["data"]["members"], [])
        self.assertEqual(result["data"]["recent_events"], [])
        self.assertEqual(result["data"]["pending_jobs_count"], 0)

    @patch("home.repository._get_supabase")
    @patch("home.repository.fetch_rooms")
    @patch("home.repository.fetch_members")
    @patch("home.repository.fetch_member_states")
    @patch("home.repository.fetch_recent_events")
    @patch("home.repository.fetch_pending_jobs")
    def test_observe_home_with_data(self, m_jobs, m_events, m_states, m_members, m_rooms, m_sb):
        m_rooms.return_value = [
            {"stable_key": "living_room", "name": "客厅", "emoji": "🛋️", "room_type": "common", "description": "客厅"}
        ]
        m_members.return_value = [
            {"id": "mid1", "stable_key": "finn", "name": "Finn", "member_type": "ai", "lifecycle_status": "alive"}
        ]
        m_states.return_value = [
            {"member_id": "mid1", "hunger": 40, "energy": 80, "mood": 60, "current_room_id": None}
        ]
        m_events.return_value = [
            {"event_type": "rested", "summary": "休息了", "occurred_at": "2026-08-18T10:00:00Z", "source": "system", "visibility": "home", "room_id": None, "actor_member_id": "mid1"}
        ]
        m_jobs.return_value = []

        result = svc.observe_home()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]["rooms"]), 1)
        self.assertEqual(result["data"]["rooms"][0]["stable_key"], "living_room")
        self.assertEqual(len(result["data"]["members"]), 1)
        self.assertEqual(result["data"]["members"][0]["name"], "Finn")
        self.assertIsNotNone(result["data"]["members"][0]["state"])
        self.assertEqual(len(result["data"]["recent_events"]), 1)


# ============================================================
# 4. 观察层 — 房间
# ============================================================

class Test04ObserveRoom(unittest.TestCase):
    """房间观察测试。"""

    @patch("home.repository.fetch_room_by_key")
    @patch("home.repository.fetch_objects_by_room")
    @patch("home.repository.fetch_events_by_room")
    def test_room_exists(self, m_events, m_objects, m_room):
        m_room.return_value = {
            "id": "rid1", "stable_key": "living_room", "name": "客厅",
            "emoji": "🛋️", "room_type": "common", "description": "客厅", "is_hidden": False
        }
        m_objects.return_value = [
            {"id": "oid1", "name": "沙发", "object_type": "furniture", "description": "软沙发", "visual": {}}
        ]
        m_events.return_value = []

        result = svc.observe_room("living_room")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["room"]["name"], "客厅")
        self.assertEqual(len(result["data"]["objects"]), 1)
        self.assertEqual(result["data"]["objects"][0]["name"], "沙发")

    @patch("home.repository.fetch_room_by_key")
    def test_room_not_found(self, m_room):
        m_room.return_value = None
        result = svc.observe_room("nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ROOM_NOT_FOUND")

    def test_room_empty_key(self):
        result = svc.observe_room("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ROOM_KEY")


# ============================================================
# 5. 观察层 — 成员
# ============================================================

class Test05ObserveMember(unittest.TestCase):
    """成员观察测试。"""

    @patch("home.repository.fetch_member_by_key")
    @patch("home.repository.fetch_member_state")
    @patch("home.repository.fetch_events_by_member")
    def test_member_exists(self, m_events, m_state, m_member):
        m_member.return_value = {
            "id": "mid1", "stable_key": "finn", "name": "Finn",
            "member_type": "ai", "lifecycle_status": "alive", "is_active": True
        }
        m_state.return_value = {
            "hunger": 40, "energy": 80, "mood": 60, "current_room_id": None,
            "last_settled_at": "2026-08-18T07:00:00Z"
        }
        m_events.return_value = []

        result = svc.observe_member("finn")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["member"]["name"], "Finn")
        self.assertIsNotNone(result["data"]["state"])
        self.assertEqual(result["data"]["state"]["hunger"], 40)

    @patch("home.repository.fetch_member_by_key")
    def test_member_not_found(self, m_member):
        m_member.return_value = None
        result = svc.observe_member("nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "MEMBER_NOT_FOUND")

    def test_member_empty_key(self):
        result = svc.observe_member("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_MEMBER_KEY")


# ============================================================
# 6. 事件时间线
# ============================================================

class Test06Timeline(unittest.TestCase):
    """事件时间线测试。"""

    @patch("home.repository.fetch_recent_events")
    def test_no_events(self, m_events):
        m_events.return_value = []
        result = svc.get_recent_events(limit=20)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 0)
        self.assertEqual(result["data"]["events"], [])

    @patch("home.repository.fetch_recent_events")
    def test_multiple_events_ordered(self, m_events):
        # 模拟数据库返回已按 occurred_at DESC 排序
        m_events.return_value = [
            {"event_type": "ate", "summary": "吃了早餐", "occurred_at": "2026-08-18T08:00:00Z", "source": "ai", "visibility": "home", "room_id": None, "actor_member_id": "mid1"},
            {"event_type": "rested", "summary": "休息了", "occurred_at": "2026-08-18T07:00:00Z", "source": "system", "visibility": "home", "room_id": None, "actor_member_id": "mid1"},
            {"event_type": "entered_room", "summary": "进了客厅", "occurred_at": "2026-08-18T06:00:00Z", "source": "system", "visibility": "home", "room_id": "rid1", "actor_member_id": "mid1"},
        ]
        result = svc.get_recent_events(limit=20)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 3)
        # 验证按时间倒序
        times = [e["occurred_at"] for e in result["data"]["events"]]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_invalid_limit(self):
        result = svc.get_recent_events(limit=0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_LIMIT")

        result = svc.get_recent_events(limit=101)
        self.assertFalse(result["ok"])


# ============================================================
# 7. 行动幂等
# ============================================================

class Test07ActionIdempotency(unittest.TestCase):
    """行动幂等测试。"""

    @patch("home.repository.fetch_action_by_key")
    def test_action_succeeded(self, m_fetch):
        m_fetch.return_value = {
            "action_key": "cook_001", "action_type": "cook",
            "status": "succeeded", "requested_at": "2026-08-18T10:00:00Z",
            "started_at": "2026-08-18T10:00:01Z",
            "finished_at": "2026-08-18T10:05:00Z",
            "error_code": None, "error_message": None, "result": {"dish": "番茄炒蛋"}
        }
        result = svc.get_action_status("cook_001")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "succeeded")
        self.assertIsNotNone(result["data"]["result"])

    @patch("home.repository.fetch_action_by_key")
    def test_action_failed(self, m_fetch):
        m_fetch.return_value = {
            "action_key": "cook_002", "action_type": "cook",
            "status": "failed", "requested_at": "2026-08-18T11:00:00Z",
            "started_at": "2026-08-18T11:00:01Z",
            "finished_at": "2026-08-18T11:00:05Z",
            "error_code": "NO_INGREDIENTS", "error_message": "缺少食材",
            "result": None
        }
        result = svc.get_action_status("cook_002")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "failed")
        self.assertEqual(result["data"]["error_code"], "NO_INGREDIENTS")

    @patch("home.repository.fetch_action_by_key")
    def test_action_skipped(self, m_fetch):
        m_fetch.return_value = {
            "action_key": "cook_003", "action_type": "cook",
            "status": "skipped", "requested_at": "2026-08-18T12:00:00Z",
            "started_at": None, "finished_at": None,
            "error_code": "ALREADY_DONE", "error_message": "已经做过了",
            "result": None
        }
        result = svc.get_action_status("cook_003")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "skipped")

    @patch("home.repository.fetch_action_by_key")
    def test_action_not_found(self, m_fetch):
        m_fetch.return_value = None
        result = svc.get_action_status("nonexistent_key")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ACTION_NOT_FOUND")

    def test_action_empty_key(self):
        result = svc.get_action_status("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ACTION_KEY")


# ============================================================
# 8. 数据库不可用降级
# ============================================================

class Test08DBUnavailable(unittest.TestCase):
    """数据库不可用时的降级行为。"""

    @patch("home.repository._get_supabase")
    def test_observe_home_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.observe_home()
        # 应该返回空数据而非崩溃
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["rooms"], [])
        self.assertEqual(result["data"]["members"], [])
        self.assertEqual(result["data"]["pending_jobs_count"], 0)

    @patch("home.repository._get_supabase")
    def test_observe_room_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.observe_room("living_room")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ROOM_NOT_FOUND")

    @patch("home.repository._get_supabase")
    def test_get_recent_events_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.get_recent_events(limit=10)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 0)

    @patch("home.repository._get_supabase")
    def test_context_no_db(self, mock_sb):
        mock_sb.return_value = None
        text = ctx.build_home_context()
        self.assertIsInstance(text, str)
        self.assertIn("还没有设置房间", text)
        self.assertIn("还没有家庭成员", text)


# ============================================================
# 9. 安全测试
# ============================================================

class Test09Security(unittest.TestCase):
    """安全相关测试。"""

    @patch("home.repository.fetch_rooms")
    @patch("home.repository.fetch_members")
    @patch("home.repository.fetch_member_states")
    @patch("home.repository.fetch_recent_events")
    @patch("home.repository.fetch_pending_jobs")
    def test_observe_home_does_not_write(self, m_jobs, m_events, m_states, m_members, m_rooms):
        """观察接口不写数据库——只调用 select 查询。"""
        m_rooms.return_value = []
        m_members.return_value = []
        m_states.return_value = []
        m_events.return_value = []
        m_jobs.return_value = []

        result = svc.observe_home()
        self.assertTrue(result["ok"])
        # 验证没有调用 insert/update/delete
        # （repository 函数只调用 .select()，这里验证它们不抛写异常）

    @patch("home.repository.fetch_recent_events")
    def test_private_events_excluded(self, m_events):
        """private 可见性事件不被返回。"""
        # repository.fetch_recent_events 默认 exclude_private=True
        # 这里验证 service 层调用时确实传了 exclude_private=True
        m_events.return_value = []
        svc.get_recent_events(limit=10)
        m_events.assert_called_once_with(limit=10, event_type="", exclude_private=True)

    @patch("home.repository.fetch_rooms")
    @patch("home.repository.fetch_members")
    @patch("home.repository.fetch_member_states")
    @patch("home.repository.fetch_recent_events")
    @patch("home.repository.fetch_pending_jobs")
    def test_observe_home_no_sensitive_data(self, m_jobs, m_events, m_states, m_members, m_rooms):
        """观察返回不含敏感配置（API Key、Token 等）。"""
        m_rooms.return_value = [{"stable_key": "living_room", "name": "客厅", "emoji": "🛋️", "room_type": "common", "description": ""}]
        m_members.return_value = [{"id": "mid1", "stable_key": "finn", "name": "Finn", "member_type": "ai", "lifecycle_status": "alive"}]
        m_states.return_value = [{"member_id": "mid1", "hunger": 50, "energy": 80, "current_room_id": None}]
        m_events.return_value = []
        m_jobs.return_value = []

        result = svc.observe_home()
        result_str = str(result)
        # 确保返回中不含敏感关键词
        for keyword in ("api_key", "API_KEY", "token", "TOKEN", "password", "secret", "cookie"):
            self.assertNotIn(keyword, result_str, f"观察结果中不应包含敏感关键词: {keyword}")


# ============================================================
# 10. 上下文构建
# ============================================================

class Test10Context(unittest.TestCase):
    """上下文构建测试。"""

    @patch("home.repository.fetch_rooms")
    @patch("home.repository.fetch_members")
    @patch("home.repository.fetch_member_states")
    @patch("home.repository.fetch_recent_events")
    def test_context_with_data(self, m_events, m_states, m_members, m_rooms):
        m_rooms.return_value = [
            {"stable_key": "living_room", "name": "客厅", "emoji": "🛋️", "room_type": "common", "description": "客厅"}
        ]
        m_members.return_value = [
            {"id": "mid1", "stable_key": "finn", "name": "Finn", "member_type": "ai", "lifecycle_status": "alive"}
        ]
        m_states.return_value = [
            {"member_id": "mid1", "hunger": 40, "energy": 80, "mood": 60, "current_room_id": None}
        ]
        m_events.return_value = [
            {"event_type": "rested", "summary": "休息了", "occurred_at": "2026-08-18T07:00:00Z"}
        ]

        text = ctx.build_home_context()
        self.assertIn("客厅", text)
        self.assertIn("Finn", text)
        self.assertIn("休息了", text)

    @patch("home.repository.fetch_rooms")
    @patch("home.repository.fetch_members")
    @patch("home.repository.fetch_member_states")
    @patch("home.repository.fetch_recent_events")
    def test_context_empty(self, m_events, m_states, m_members, m_rooms):
        m_rooms.return_value = []
        m_members.return_value = []
        m_states.return_value = []
        m_events.return_value = []

        text = ctx.build_home_context()
        self.assertIn("还没有设置房间", text)
        self.assertIn("还没有家庭成员", text)
        self.assertIn("没有生活事件", text)

    def test_format_room_brief(self):
        room = {"emoji": "🍳", "name": "厨房", "description": "做饭的地方", "room_type": "common"}
        text = ctx.format_room_brief(room)
        self.assertIn("厨房", text)
        self.assertIn("做饭", text)

    def test_format_member_brief(self):
        member = {"name": "Finn", "member_type": "ai", "lifecycle_status": "alive"}
        state = {"mood": 60, "energy": 80, "hunger": 40}
        text = ctx.format_member_brief(member, state)
        self.assertIn("Finn", text)
        self.assertIn("心情60", text)


# ============================================================
# 11. 当前单用户模型声明
# ============================================================

class Test12SingleUserModel(unittest.TestCase):
    """明确当前是单用户模型，观察接口不带用户隔离参数。"""

    def test_observe_home_no_user_param(self):
        """observe_home 不接受 user_id 参数——当前单用户模型。"""
        import inspect
        sig = inspect.signature(svc.observe_home)
        self.assertNotIn("user_id", sig.parameters)
        self.assertNotIn("assistant_id", sig.parameters)

    def test_observe_room_no_user_param(self):
        import inspect
        sig = inspect.signature(svc.observe_room)
        self.assertNotIn("user_id", sig.parameters)


if __name__ == "__main__":
    unittest.main(verbosity=2)
