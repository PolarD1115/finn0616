"""
钱包系统测试套件 (test_wallet.py)
=================================
纯函数 + mock 测试，不写真实生产数据。
覆盖：金额校验、周界/生日周计算、RPC 调用、无 Supabase 降级。

运行：
    python -m pytest test_wallet.py -q
"""

import os
import sys
import datetime
import unittest
from unittest.mock import patch, MagicMock

# 确保能导入 home_system
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import home_system as hs


# ============================================================
# 1. 纯函数：金额校验
# ============================================================
class Test01AmountValidation(unittest.TestCase):
    def test_positive_amount(self):
        ok, _ = hs._validate_amount(50)
        self.assertTrue(ok)

    def test_zero_amount(self):
        ok, err = hs._validate_amount(0)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_AMOUNT")

    def test_negative_amount(self):
        ok, err = hs._validate_amount(-10)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_AMOUNT")

    def test_bool_amount(self):
        """bool 是 int 子类，必须拒绝。"""
        ok, err = hs._validate_amount(True)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_AMOUNT")
        ok2, _ = hs._validate_amount(False)
        self.assertFalse(ok2)

    def test_string_amount(self):
        ok, err = hs._validate_amount("abc")
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_AMOUNT")

    def test_none_amount(self):
        ok, err = hs._validate_amount(None)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_AMOUNT")

    def test_oversized_amount(self):
        ok, err = hs._validate_amount(1e9 + 1)
        self.assertFalse(ok)
        self.assertEqual(err, "OVERSIZED_AMOUNT")

    def test_exactly_max(self):
        ok, _ = hs._validate_amount(1e9)
        self.assertTrue(ok)


# ============================================================
# 2. 纯函数：原因校验
# ============================================================
class Test02ReasonValidation(unittest.TestCase):
    def test_valid_reason(self):
        ok, _ = hs._validate_reason("test reason")
        self.assertTrue(ok)

    def test_empty_string(self):
        ok, err = hs._validate_reason("")
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_REASON")

    def test_whitespace_only(self):
        ok, err = hs._validate_reason("   ")
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_REASON")

    def test_none_reason(self):
        ok, err = hs._validate_reason(None)
        self.assertFalse(ok)
        self.assertEqual(err, "EMPTY_REASON")


# ============================================================
# 3. 纯函数：limit 校验
# ============================================================
class Test03LimitValidation(unittest.TestCase):
    def test_valid_limit(self):
        ok, _ = hs._validate_limit(20)
        self.assertTrue(ok)

    def test_limit_too_low(self):
        ok, err = hs._validate_limit(0)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_LIMIT")

    def test_limit_too_high(self):
        ok, err = hs._validate_limit(101)
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_LIMIT")

    def test_limit_non_int(self):
        ok, _ = hs._validate_limit("10")
        self.assertTrue(ok)  # int("10") works


# ============================================================
# 4. 纯函数：兑换目标校验
# ============================================================
class Test04TargetValidation(unittest.TestCase):
    def test_tea(self):
        ok, _ = hs._validate_target("tea")
        self.assertTrue(ok)

    def test_gift(self):
        ok, _ = hs._validate_target("gift")
        self.assertTrue(ok)

    def test_invalid_target(self):
        ok, err = hs._validate_target("coffee")
        self.assertFalse(ok)
        self.assertEqual(err, "INVALID_TARGET")

    def test_none_target(self):
        ok, err = hs._validate_target(None)
        self.assertFalse(ok)


# ============================================================
# 5. 纯函数：北京时间周一计算
# ============================================================
class Test05BJWeekStart(unittest.TestCase):
    def _make_utc(self, year, month, day, hour=0, minute=0):
        return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)

    def test_regular_monday(self):
        """2024-04-08 是周一，北京时间周一 00:00 应该还是当天"""
        # 2024-04-08 00:00 UTC
        dt = self._make_utc(2024, 4, 8, 0, 0)
        result = hs._bj_week_start(dt)
        # 北京时间周一 00:00 的 UTC 是周日 16:00
        expected = datetime.datetime(2024, 4, 7, 16, 0)
        self.assertEqual(result, expected)

    def test_sunday(self):
        """周日应该回到本周一"""
        # 2024-04-14 (周日) 00:00 UTC
        dt = self._make_utc(2024, 4, 14, 0, 0)
        result = hs._bj_week_start(dt)
        expected = datetime.datetime(2024, 4, 7, 16, 0)
        self.assertEqual(result, expected)

    def test_mid_week(self):
        """周三应该回到本周一"""
        # 2024-04-10 (周三) 12:00 UTC
        dt = self._make_utc(2024, 4, 10, 12, 0)
        result = hs._bj_week_start(dt)
        expected = datetime.datetime(2024, 4, 7, 16, 0)
        self.assertEqual(result, expected)

    def test_year_boundary(self):
        """跨年周"""
        # 2024-01-01 (周一) 00:00 UTC
        dt = self._make_utc(2024, 1, 1, 0, 0)
        result = hs._bj_week_start(dt)
        # 北京时间 2024-01-01 00:00 的周一就是 2024-01-01
        expected = datetime.datetime(2023, 12, 31, 16, 0)
        self.assertEqual(result, expected)


# ============================================================
# 6. 纯函数：生日周检测
# ============================================================
class Test06BirthdayWeek(unittest.TestCase):
    def _make_utc(self, year, month, day, hour=0, minute=0):
        return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)

    def test_april_5_exact(self):
        """4月5日当天"""
        dt = self._make_utc(2024, 4, 5, 0, 0)
        self.assertTrue(hs._is_birthday_week(dt))

    def test_april_5_monday(self):
        """4月5日所在周的周一"""
        # 2024年4月5日是周五，周一是4月1日
        dt = self._make_utc(2024, 4, 1, 0, 0)
        self.assertTrue(hs._is_birthday_week(dt))

    def test_april_5_sunday(self):
        """4月5日所在周的周日"""
        # 2024年4月5日周五，周日是4月7日
        dt = self._make_utc(2024, 4, 7, 0, 0)
        self.assertTrue(hs._is_birthday_week(dt))

    def test_november_15_exact(self):
        """11月15日当天"""
        dt = self._make_utc(2024, 11, 15, 0, 0)
        self.assertTrue(hs._is_birthday_week(dt))

    def test_not_birthday_week(self):
        """普通周"""
        dt = self._make_utc(2024, 6, 1, 0, 0)
        self.assertFalse(hs._is_birthday_week(dt))

    def test_april_4_not_birthday_week(self):
        """4月4日（4月5日前一天）"""
        # 2024年4月4日周四，所在周包含4月5日
        dt = self._make_utc(2024, 4, 4, 0, 0)
        self.assertTrue(hs._is_birthday_week(dt))

    def test_april_6_not_birthday_week(self):
        """4月6日（4月5日后一天），所在周仍包含4月5日"""
        dt = self._make_utc(2024, 4, 6, 0, 0)
        self.assertTrue(hs._is_birthday_week(dt))

    def test_april_8_not_birthday_week(self):
        """4月8日（下周一），不在生日周"""
        dt = self._make_utc(2024, 4, 8, 0, 0)
        self.assertFalse(hs._is_birthday_week(dt))


# ============================================================
# 7. Mock：RPC 调用层
# ============================================================
class Test07RPCCalls(unittest.TestCase):
    def setUp(self):
        self.patch_get_sb = patch("home_system._get_supabase")
        self.mock_get_sb = self.patch_get_sb.start()
        self.mock_sb = MagicMock()
        self.mock_get_sb.return_value = self.mock_sb

    def tearDown(self):
        self.patch_get_sb.stop()

    def _setup_rpc_return(self, data):
        """设置 mock Supabase RPC 返回。"""
        resp = MagicMock()
        resp.data = data
        self.mock_sb.rpc.return_value.execute.return_value = resp

    def test_wallet_check_success(self):
        """wallet_check 成功返回。"""
        self._setup_rpc_return({"ok": True, "data": {"balance": 100}})
        result = hs.wallet_check()
        self.mock_sb.rpc.assert_called_once()
        self.assertTrue(result["ok"])

    def test_wallet_check_no_supabase(self):
        """无 Supabase 时返回结构化错误。"""
        self.mock_get_sb.return_value = None
        result = hs.wallet_check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DB_UNAVAILABLE")

    def test_wallet_earn_invalid_amount(self):
        """earn 负数金额直接返回错误，不走 RPC。"""
        result = hs.wallet_earn("finn_wallet", -10, "src", "reason")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_AMOUNT")
        self.mock_sb.rpc.assert_not_called()

    def test_wallet_earn_empty_reason(self):
        """earn 空原因直接返回错误。"""
        result = hs.wallet_earn("finn_wallet", 10, "src", "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_REASON")

    def test_wallet_spend_success(self):
        """spend 成功。"""
        self._setup_rpc_return({"ok": True, "data": {"balance_after": 90}})
        result = hs.wallet_spend("finn_wallet", 10, "coffee")
        self.assertTrue(result["ok"])

    def test_wallet_exchange_invalid_target(self):
        """exchange 非法 target 直接返回错误。"""
        result = hs.wallet_exchange("finn_wallet", "coffee", "reason")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_TARGET")

    def test_wallet_overtime_withdraw_success(self):
        """overtime_withdraw 成功。"""
        self._setup_rpc_return({"ok": True, "data": {"balance_after": 110}})
        result = hs.wallet_overtime_withdraw("finn_wallet", 10, "withdraw")
        self.assertTrue(result["ok"])

    def test_wallet_log_limit_out_of_range(self):
        """log limit 超范围直接返回错误。"""
        result = hs.wallet_log("finn_wallet", limit=200)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_LIMIT")

    def test_wallet_log_success(self):
        """log 查询成功。"""
        self._setup_rpc_return({"ok": True, "data": {"total": 0, "logs": []}})
        result = hs.wallet_log("finn_wallet", limit=10, offset=0)
        self.assertTrue(result["ok"])


# ============================================================
# 8. Mock：source_key 幂等
# ============================================================
class Test08Idempotency(unittest.TestCase):
    def setUp(self):
        self.patch_get_sb = patch("home_system._get_supabase")
        self.mock_get_sb = self.patch_get_sb.start()
        self.mock_sb = MagicMock()
        self.mock_get_sb.return_value = self.mock_sb

    def tearDown(self):
        self.patch_get_sb.stop()

    def test_duplicate_source_key(self):
        """RPC 返回 source_key 重复错误。"""
        resp = MagicMock()
        resp.data = {"ok": False, "error_code": "DUPLICATE_SOURCE", "message": "重复"}
        self.mock_sb.rpc.return_value.execute.return_value = resp

        result = hs.wallet_earn("finn_wallet", 10, "dup_key", "test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DUPLICATE_SOURCE")


# ============================================================
# 9. 格式函数
# ============================================================
class Test09FormatResult(unittest.TestCase):
    def test_success(self):
        r = hs._format_result(True, "ok", {"x": 1})
        self.assertTrue(r["ok"])
        self.assertEqual(r["message"], "ok")
        self.assertEqual(r["data"], {"x": 1})
        self.assertNotIn("error_code", r)

    def test_error(self):
        r = hs._format_result(False, "fail", error_code="ERR")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "ERR")
        self.assertNotIn("data", r)


if __name__ == "__main__":
    unittest.main()
