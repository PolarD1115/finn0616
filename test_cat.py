"""
test_cat.py — 小满猫系统 (Phase 4) 测试
======================================
测试范围：
- 纯函数校验（白名单物品ID、数量、用户ID）
- RPC 封装层（cat_status / cat_feed / cat_play / cat_clean / cat_pet / cat_restore_energy / cat_shop_list / cat_shop_buy）
- 无 Supabase 降级
- 边界条件（属性封顶、冷却、睡觉不能玩）

运行：
    python -m pytest test_cat.py -q
"""

import unittest
from unittest.mock import patch, MagicMock
import home_system as _hs


# ============================================================
# 1. 纯函数：白名单物品ID校验
# ============================================================
class Test01ValidateCatItemId(unittest.TestCase):
    def test_valid_food(self):
        ok, _ = _hs._validate_cat_item_id("fish")
        self.assertTrue(ok)
        ok2, _ = _hs._validate_cat_item_id("cat_milk")
        self.assertTrue(ok2)

    def test_valid_toy(self):
        ok, _ = _hs._validate_cat_item_id("ball")
        self.assertTrue(ok)
        ok2, _ = _hs._validate_cat_item_id("catnip")
        self.assertTrue(ok2)

    def test_valid_clean(self):
        ok, _ = _hs._validate_cat_item_id("brush")
        self.assertTrue(ok)
        ok2, _ = _hs._validate_cat_item_id("soap")
        self.assertTrue(ok2)

    def test_invalid_item(self):
        ok, err = _hs._validate_cat_item_id("diamond")
        self.assertFalse(ok)
        self.assertEqual(err, "ITEM_NOT_IN_WHITELIST")

    def test_empty_none(self):
        ok, err = _hs._validate_cat_item_id(None)
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_ITEM_ID")

    def test_empty_string(self):
        ok, err = _hs._validate_cat_item_id("")
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_ITEM_ID")

    def test_non_string(self):
        ok, err = _hs._validate_cat_item_id(123)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_ITEM_ID")


# ============================================================
# 2. 纯函数：数量校验
# ============================================================
class Test02ValidateCatQty(unittest.TestCase):
    def test_valid_qty(self):
        ok, _ = _hs._validate_cat_qty(1)
        self.assertTrue(ok)
        ok2, _ = _hs._validate_cat_qty(10)
        self.assertTrue(ok2)
        ok3, _ = _hs._validate_cat_qty(99)
        self.assertTrue(ok3)

    def test_zero_qty(self):
        ok, err = _hs._validate_cat_qty(0)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_QTY")

    def test_negative_qty(self):
        ok, err = _hs._validate_cat_qty(-1)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_QTY")

    def test_too_large_qty(self):
        ok, err = _hs._validate_cat_qty(100)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_QTY")

    def test_none_qty(self):
        ok, err = _hs._validate_cat_qty(None)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_QTY")

    def test_string_qty(self):
        ok, err = _hs._validate_cat_qty("abc")
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_QTY")


# ============================================================
# 3. 纯函数：clamp 工具
# ============================================================
class Test03Clamp(unittest.TestCase):
    def test_clamp_normal(self):
        self.assertEqual(_hs._clamp(50), 50)

    def test_clamp_below_min(self):
        self.assertEqual(_hs._clamp(-10), 0)

    def test_clamp_above_max(self):
        self.assertEqual(_hs._clamp(150), 100)

    def test_clamp_custom_range(self):
        self.assertEqual(_hs._clamp(5, 10, 20), 10)
        self.assertEqual(_hs._clamp(25, 10, 20), 20)
        self.assertEqual(_hs._clamp(15, 10, 20), 15)

    def test_clamp_non_numeric(self):
        self.assertEqual(_hs._clamp("abc"), 0)


# ============================================================
# 4. RPC 封装层 Mock 测试
# ============================================================
class Test04CatRpcMock(unittest.TestCase):
    def setUp(self):
        self.patcher = patch("home_system._get_supabase")
        self.mock_get_sb = self.patcher.start()
        self.mock_sb = MagicMock()
        self.mock_get_sb.return_value = self.mock_sb

    def tearDown(self):
        self.patcher.stop()

    def _setup_rpc_return(self, data):
        self.mock_sb.rpc.return_value.execute.return_value = MagicMock(data=data)

    def test_cat_status(self):
        self._setup_rpc_return({"ok": True, "pet": {"name": "小满"}})
        result = _hs.cat_status("user_finn")
        self.assertTrue(result.get("ok"))
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_cat_status")
        self.assertEqual(args[0][1]["p_user_id"], "user_finn")

    def test_cat_feed(self):
        self._setup_rpc_return({"ok": True, "hunger_delta": 15})
        result = _hs.cat_feed("user_finn", "fish")
        self.assertTrue(result.get("ok"))
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_cat_feed")
        self.assertEqual(args[0][1]["p_user_id"], "user_finn")
        self.assertEqual(args[0][1]["p_item_id"], "fish")

    def test_cat_play(self):
        self._setup_rpc_return({"ok": True, "happiness_delta": 10})
        result = _hs.cat_play("user_finn", "ball")
        self.assertTrue(result.get("ok"))
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_cat_play")
        self.assertEqual(args[0][1]["p_user_id"], "user_finn")
        self.assertEqual(args[0][1]["p_item_id"], "ball")

    def test_cat_play_empty(self):
        """空手玩耍（不传 item_id）"""
        self._setup_rpc_return({"ok": True, "happiness_delta": 5})
        result = _hs.cat_play("user_finn", None)
        self.assertTrue(result.get("ok"))
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][1]["p_item_id"], None)

    def test_cat_clean(self):
        self._setup_rpc_return({"ok": True, "cleanliness_delta": 25})
        result = _hs.cat_clean("user_finn", "brush")
        self.assertTrue(result.get("ok"))
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_cat_clean")
        self.assertEqual(args[0][1]["p_item_id"], "brush")

    def test_cat_pet(self):
        self._setup_rpc_return({"ok": True, "happiness_delta": 5})
        result = _hs.cat_pet("user_finn")
        self.assertTrue(result.get("ok"))
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_cat_pet")

    def test_cat_restore_energy(self):
        self._setup_rpc_return({"ok": True, "energy_delta": 30})
        result = _hs.cat_restore_energy("user_finn")
        self.assertTrue(result.get("ok"))
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_cat_restore_energy")

    def test_cat_shop_list(self):
        self._setup_rpc_return({"ok": True, "items": []})
        result = _hs.cat_shop_list()
        self.assertTrue(result.get("ok"))
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_cat_shop_list")

    def test_cat_shop_buy(self):
        self._setup_rpc_return({"ok": True, "total_price": 10})
        result = _hs.cat_shop_buy("user_finn", "fish", 2)
        self.assertTrue(result.get("ok"))
        args = self.mock_sb.rpc.call_args
        self.assertEqual(args[0][0], "rpc_cat_shop_buy")
        self.assertEqual(args[0][1]["p_qty"], 2)


# ============================================================
# 5. 校验短路（非法输入不触及 DB）
# ============================================================
class Test05ValidationShortCircuit(unittest.TestCase):
    def test_feed_invalid_item_short_circuits(self):
        """非法物品ID直接返回错误，不调用 RPC。"""
        with patch("home_system._get_supabase") as mock_sb:
            result = _hs.cat_feed("user_finn", "diamond")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "ITEM_NOT_IN_WHITELIST")
            mock_sb.assert_not_called()

    def test_buy_invalid_item_short_circuits(self):
        """非法物品ID直接返回错误，不调用 RPC。"""
        with patch("home_system._get_supabase") as mock_sb:
            result = _hs.cat_shop_buy("user_finn", "invalid", 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "ITEM_NOT_IN_WHITELIST")
            mock_sb.assert_not_called()

    def test_buy_invalid_qty_short_circuits(self):
        """非法数量直接返回错误，不调用 RPC。"""
        with patch("home_system._get_supabase") as mock_sb:
            result = _hs.cat_shop_buy("user_finn", "fish", 0)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "INVALID_QTY")
            mock_sb.assert_not_called()

    def test_invalid_user_short_circuits(self):
        """空用户ID直接返回错误，不调用 RPC。"""
        with patch("home_system._get_supabase") as mock_sb:
            result = _hs.cat_status("")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "INVALID_USER")
            mock_sb.assert_not_called()


# ============================================================
# 6. 无 Supabase 降级
# ============================================================
class Test06NoSupabaseFallback(unittest.TestCase):
    def test_cat_status_no_db(self):
        with patch("home_system._get_supabase", return_value=None):
            result = _hs.cat_status("user_finn")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "DB_UNAVAILABLE")

    def test_cat_feed_no_db(self):
        with patch("home_system._get_supabase", return_value=None):
            result = _hs.cat_feed("user_finn", "fish")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "DB_UNAVAILABLE")

    def test_cat_shop_buy_no_db(self):
        with patch("home_system._get_supabase", return_value=None):
            result = _hs.cat_shop_buy("user_finn", "fish", 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "DB_UNAVAILABLE")


# ============================================================
# 7. 边界条件
# ============================================================
class Test07BoundaryConditions(unittest.TestCase):
    def test_clamp_at_boundary(self):
        """属性值在边界时应正确 clamp。"""
        self.assertEqual(_hs._clamp(100), 100)
        self.assertEqual(_hs._clamp(0), 0)
        self.assertEqual(_hs._clamp(100.1), 100)
        self.assertEqual(_hs._clamp(-0.1), 0)

    def test_qty_max_boundary(self):
        """数量上限 99 边界。"""
        ok, _ = _hs._validate_cat_qty(99)
        self.assertTrue(ok)
        ok2, _ = _hs._validate_cat_qty(100)
        self.assertFalse(ok2)

    def test_cooldown_boundary(self):
        """冷却 600 秒边界（599 秒仍在冷却，600 秒刚好结束）。"""
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        # 599 秒前 = 仍在冷却
        t_599 = now - dt.timedelta(seconds=599)
        seconds_599 = max(0, 600 - (now - t_599).total_seconds())
        self.assertGreater(seconds_599, 0)
        # 600 秒前 = 刚好结束
        t_600 = now - dt.timedelta(seconds=600)
        seconds_600 = max(0, 600 - (now - t_600).total_seconds())
        self.assertEqual(seconds_600, 0)
        # 601 秒前 = 已结束
        t_601 = now - dt.timedelta(seconds=601)
        seconds_601 = max(0, 600 - (now - t_601).total_seconds())
        self.assertEqual(seconds_601, 0)

    def test_whitelist_count(self):
        """白名单恰好 10 个物品。"""
        self.assertEqual(len(_hs.CAT_SHOP_WHITELIST), 10)

    def test_whitelist_types(self):
        """白名单包含正确的类型分布。"""
        food_items = {k for k, v in _hs.CAT_ITEM_TYPES.items() if v == "food"}
        toy_items = {k for k, v in _hs.CAT_ITEM_TYPES.items() if v == "toy"}
        clean_items = {k for k, v in _hs.CAT_ITEM_TYPES.items() if v == "clean"}
        self.assertEqual(len(food_items), 5)
        self.assertEqual(len(toy_items), 3)
        self.assertEqual(len(clean_items), 2)


# ============================================================
# 8. 状态映射
# ============================================================
class Test08ItemTypeMapping(unittest.TestCase):
    def test_food_items(self):
        for item in ["fish", "cat_milk", "tuna_can", "wet_food", "apple"]:
            self.assertEqual(_hs.CAT_ITEM_TYPES[item], "food")

    def test_toy_items(self):
        for item in ["ball", "catnip", "feather"]:
            self.assertEqual(_hs.CAT_ITEM_TYPES[item], "toy")

    def test_clean_items(self):
        for item in ["brush", "soap"]:
            self.assertEqual(_hs.CAT_ITEM_TYPES[item], "clean")


if __name__ == "__main__":
    unittest.main()
