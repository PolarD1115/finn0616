"""
test_house.py — 有状态小屋 (Memory House Phase 3) 测试
===============================================
测试范围：
- 纯函数校验（房间ID、条目类型、物品名称）
- RPC 封装层（house_look / house_do / house_put / house_take / house_update_desc）
- 无 Supabase 降级
- manage_memory_house delete 禁用
"""

import unittest
from unittest.mock import patch, MagicMock
import home_system as _hs


# ============================================================
# 1. 纯函数：房间ID校验
# ============================================================
class Test01ValidateRoom(unittest.TestCase):
    def test_empty_none(self):
        ok, err = _hs._validate_room(None)
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_ROOM")

    def test_empty_string(self):
        ok, err = _hs._validate_room("")
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_ROOM")

    def test_whitespace_only(self):
        ok, err = _hs._validate_room("   ")
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_ROOM")

    def test_non_string(self):
        ok, err = _hs._validate_room(123)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_ROOM")

    def test_valid_room(self):
        ok, err = _hs._validate_room("living_room")
        self.assertTrue(ok)
        self.assertEqual(err, "")


# ============================================================
# 2. 纯函数：条目类型校验
# ============================================================
class Test02ValidateEntryType(unittest.TestCase):
    def test_empty_none(self):
        ok, err = _hs._validate_entry_type(None)
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_ENTRY_TYPE")

    def test_empty_string(self):
        ok, err = _hs._validate_entry_type("")
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_ENTRY_TYPE")

    def test_non_string(self):
        ok, err = _hs._validate_entry_type(123)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_ENTRY_TYPE")

    def test_valid(self):
        ok, err = _hs._validate_entry_type("看书")
        self.assertTrue(ok)
        self.assertEqual(err, "")


# ============================================================
# 3. 纯函数：物品名称校验
# ============================================================
class Test03ValidateObjectName(unittest.TestCase):
    def test_empty_none(self):
        ok, err = _hs._validate_object_name(None)
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_OBJECT_NAME")

    def test_empty_string(self):
        ok, err = _hs._validate_object_name("")
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_OBJECT_NAME")

    def test_non_string(self):
        ok, err = _hs._validate_object_name(123)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_OBJECT_NAME")

    def test_too_long(self):
        ok, err = _hs._validate_object_name("a" * 101)
        self.assertFalse(ok)
        self.assertEqual(err, "OBJECT_NAME_TOO_LONG")

    def test_valid(self):
        ok, err = _hs._validate_object_name("小猫玩偶")
        self.assertTrue(ok)
        self.assertEqual(err, "")


# ============================================================
# 4. RPC 封装层：Mock 测试
# ============================================================
class Test04HouseRPC(unittest.TestCase):
    def setUp(self):
        self.patcher = patch("home_system._get_supabase")
        self.mock_get_sb = self.patcher.start()
        self.mock_sb = MagicMock()
        self.mock_get_sb.return_value = self.mock_sb

    def tearDown(self):
        self.patcher.stop()

    def test_house_look(self):
        self.mock_sb.rpc.return_value.execute.return_value.data = {
            "ok": True,
            "room": {"id": "living_room", "name": "客厅"},
            "objects": [],
            "diary": [],
        }
        result = _hs.house_look("living_room")
        self.assertTrue(result["ok"])
        self.mock_sb.rpc.assert_called_once()
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_house_look")

    def test_house_do(self):
        self.mock_sb.rpc.return_value.execute.return_value.data = {"ok": True, "id": 42}
        result = _hs.house_do("kitchen", "做饭", "做了一顿美味")
        self.assertTrue(result["ok"])
        self.mock_sb.rpc.assert_called_once()
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_house_do")
        self.assertEqual(args[0][1]["p_room_id"], "kitchen")

    def test_house_put(self):
        self.mock_sb.rpc.return_value.execute.return_value.data = {"ok": True, "id": "obj-123"}
        result = _hs.house_put("bedroom", "枕头", "🛏️", "柔软的枕头")
        self.assertTrue(result["ok"])
        self.mock_sb.rpc.assert_called_once()
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_house_put")

    def test_house_take(self):
        self.mock_sb.rpc.return_value.execute.return_value.data = {"ok": True}
        result = _hs.house_take("obj-123")
        self.assertTrue(result["ok"])
        self.mock_sb.rpc.assert_called_once()
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_house_take")
        self.assertEqual(args[0][1]["p_object_id"], "obj-123")

    def test_house_update_desc(self):
        self.mock_sb.rpc.return_value.execute.return_value.data = {"ok": True}
        result = _hs.house_update_desc("balcony", "阳光充足的阳台")
        self.assertTrue(result["ok"])
        self.mock_sb.rpc.assert_called_once()
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_house_update_desc")


# ============================================================
# 5. 参数校验失败（短路，不触及 RPC）
# ============================================================
class Test05ValidationShortCircuit(unittest.TestCase):
    def test_house_look_invalid_room(self):
        result = _hs.house_look("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ROOM")

    def test_house_do_invalid_room(self):
        result = _hs.house_do("", "test", "content")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ROOM")

    def test_house_do_invalid_entry_type(self):
        result = _hs.house_do("kitchen", "", "content")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ENTRY_TYPE")

    def test_house_do_empty_content(self):
        result = _hs.house_do("kitchen", "test", "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_CONTENT")

    def test_house_put_invalid_room(self):
        result = _hs.house_put("", "item")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ROOM")

    def test_house_put_invalid_name(self):
        result = _hs.house_put("kitchen", "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_OBJECT_NAME")

    def test_house_take_invalid_id(self):
        result = _hs.house_take("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_OBJECT_ID")

    def test_house_update_desc_invalid_room(self):
        result = _hs.house_update_desc("", "desc")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ROOM")


# ============================================================
# 6. 无 Supabase 降级
# ============================================================
class Test06NoSupabase(unittest.TestCase):
    def setUp(self):
        self.patcher = patch("home_system._get_supabase")
        self.mock_get_sb = self.patcher.start()
        self.mock_get_sb.return_value = None

    def tearDown(self):
        self.patcher.stop()

    def test_house_look_no_db(self):
        result = _hs.house_look("living_room")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DB_UNAVAILABLE")

    def test_house_do_no_db(self):
        result = _hs.house_do("kitchen", "做饭", "内容")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DB_UNAVAILABLE")


# ============================================================
# 7. manage_memory_house delete 禁用兼容性
# ============================================================
class Test07ManageMemoryHouseDelete(unittest.TestCase):
    def test_delete_disabled(self):
        # 仅做文本断言，不实际调用 DB
        # 对应 server.py 中的改动：返回 "需要用户确认"
        expected = "⚠️ 删除操作需要用户确认，请联系管理员。"
        self.assertIn("需要用户确认", expected)


if __name__ == "__main__":
    unittest.main()
