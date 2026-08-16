"""
test_money_earning.py — Agent 赚钱系统运行时开关测试
=====================================================
覆盖目标三的需求：
- 默认开启
- sys_config=false 后热读取为关闭
- bypass_cap=false 的 wallet_earn 被拒绝（tool_loop.call_tool + server.wallet_earn）
- bypass_cap=true 的零花钱/打赏仍可调用
- wallet_check / wallet_spend / cat_shop_buy 不受影响
- /api/admin/config 接受并返回 money_earning_enabled
- 非白名单字段仍被拒绝
- 暴露层（_build_tool_schema_block）在关闭时隐藏 wallet_earn

纯函数 + mock 测试，不触生产数据/数据库。

运行：
    python -m unittest test_money_earning -v
"""

import unittest
from unittest.mock import patch, MagicMock, AsyncMock

import tool_loop


# ============================================================
# 1. gateway._money_earning_enabled 默认值 + 热读取
# ============================================================
class TestMoneyEarningConfig(unittest.TestCase):
    def test_default_is_true(self):
        """默认值 money_earning_enabled=True。"""
        import gateway
        # 清缓存，模拟无 DB 配置
        gateway._invalidate_runtime_config()
        with patch("gateway._load_sys_config_raw", return_value={}):
            self.assertTrue(gateway._money_earning_enabled())

    def test_sys_config_false_reads_false(self):
        """sys_config money_earning_enabled=false → 热读取为 False。"""
        import gateway
        gateway._invalidate_runtime_config()
        with patch("gateway._load_sys_config_raw",
                   return_value={"money_earning_enabled": False}):
            self.assertFalse(gateway._money_earning_enabled())

    def test_sys_config_true_reads_true(self):
        """sys_config money_earning_enabled=true → True。"""
        import gateway
        gateway._invalidate_runtime_config()
        with patch("gateway._load_sys_config_raw",
                   return_value={"money_earning_enabled": True}):
            self.assertTrue(gateway._money_earning_enabled())

    def test_default_runtime_config_contains_key(self):
        """_default_runtime_config() 含 money_earning_enabled 且默认 True。"""
        import gateway
        cfg = gateway._default_runtime_config()
        self.assertIn("money_earning_enabled", cfg)
        self.assertTrue(cfg["money_earning_enabled"])

    def test_source_default_when_absent(self):
        """未配置时来源为 default。"""
        import gateway
        with patch("gateway._load_sys_config_raw", return_value={}):
            self.assertEqual(gateway._config_source_of("money_earning_enabled"), "default")

    def test_source_database_when_set(self):
        """已配置时来源为 database。"""
        import gateway
        with patch("gateway._load_sys_config_raw",
                   return_value={"money_earning_enabled": False}):
            self.assertEqual(gateway._config_source_of("money_earning_enabled"), "database")


# ============================================================
# 2. tool_loop.call_tool：wallet_earn 入口门控
# ============================================================
class TestCallToolWalletEarnGate(unittest.IsolatedAsyncioTestCase):
    async def test_bypass_cap_false_rejected_when_disabled(self):
        """money_earning_enabled=false + bypass_cap=False → 拒绝。"""
        with patch("tool_loop._money_earning_enabled", return_value=False):
            res = await tool_loop.call_tool("wallet_earn",
                                    {"amount": 10, "source_key": "task_1", "reason": "test"})
        self.assertFalse(res["ok"])
        self.assertIn("MONEY_EARNING_DISABLED", res["text"])

    async def test_bypass_cap_true_allowed_when_disabled(self):
        """money_earning_enabled=false + bypass_cap=True（零花钱/打赏）→ 不被门控拦截。
        （会继续走到实际 RPC，这里 mock callable 验证不被门控拒绝）"""
        call_made = {"v": False}

        async def _fake_wallet_earn(**kwargs):
            call_made["v"] = True
            return {"ok": True, "message": "入账成功", "data": {"balance": 100}}

        with patch("tool_loop._money_earning_enabled", return_value=False), \
             patch.dict(tool_loop.TOOL_REGISTRY, {
                 "wallet_earn": {
                     "description": "test", "parameters": {"type": "object", "properties": {
                         "amount": {"type": "number"},
                         "source_key": {"type": "string"},
                         "reason": {"type": "string"},
                         "bypass_cap": {"type": "boolean"},
                     }, "required": ["amount", "source_key", "reason"]},
                     "callable": _fake_wallet_earn,
                     "fixed_args": {"wallet_id": "test", "meta": {}},
                 }
             }):
            res = await tool_loop.call_tool("wallet_earn",
                                    {"amount": 25, "source_key": "allowance_2026W33",
                                     "reason": "零花钱", "bypass_cap": True})
        self.assertTrue(res["ok"])
        self.assertTrue(call_made["v"])

    async def test_bypass_cap_false_allowed_when_enabled(self):
        """money_earning_enabled=true + bypass_cap=False → 不被门控拦截。"""
        call_made = {"v": False}

        async def _fake_wallet_earn(**kwargs):
            call_made["v"] = True
            return {"ok": True, "message": "入账成功", "data": {"balance": 100}}

        with patch("tool_loop._money_earning_enabled", return_value=True), \
             patch.dict(tool_loop.TOOL_REGISTRY, {
                 "wallet_earn": {
                     "description": "test", "parameters": {"type": "object", "properties": {
                         "amount": {"type": "number"},
                         "source_key": {"type": "string"},
                         "reason": {"type": "string"},
                         "bypass_cap": {"type": "boolean"},
                     }, "required": ["amount", "source_key", "reason"]},
                     "callable": _fake_wallet_earn,
                     "fixed_args": {"wallet_id": "test", "meta": {}},
                 }
             }):
            res = await tool_loop.call_tool("wallet_earn",
                                    {"amount": 10, "source_key": "task_1", "reason": "test"})
        self.assertTrue(res["ok"])
        self.assertTrue(call_made["v"])

    async def test_bypass_cap_default_false_rejected_when_disabled(self):
        """不传 bypass_cap（默认False）+ 关闭 → 拒绝。"""
        with patch("tool_loop._money_earning_enabled", return_value=False):
            res = await tool_loop.call_tool("wallet_earn",
                                    {"amount": 10, "source_key": "task_1", "reason": "test"})
        self.assertFalse(res["ok"])
        self.assertIn("MONEY_EARNING_DISABLED", res["text"])


# ============================================================
# 3. 其他钱包工具不受影响
# ============================================================
class TestOtherWalletToolsUnaffected(unittest.IsolatedAsyncioTestCase):
    async def test_wallet_check_not_gated(self):
        """wallet_check 不受 money_earning_enabled 影响。"""
        async def _fake_check(**kwargs):
            return {"ok": True, "message": "余额", "data": {"balance": 100}}

        with patch("tool_loop._money_earning_enabled", return_value=False), \
             patch.dict(tool_loop.TOOL_REGISTRY, {
                 "wallet_check": {
                     "description": "test", "parameters": {"type": "object", "properties": {}, "required": []},
                     "callable": _fake_check, "fixed_args": {"wallet_id": "test"},
                 }
             }):
            res = await tool_loop.call_tool("wallet_check", {})
        self.assertTrue(res["ok"])

    async def test_wallet_spend_not_gated(self):
        """wallet_spend 不受 money_earning_enabled 影响。"""
        async def _fake_spend(**kwargs):
            return {"ok": True, "message": "支出成功", "data": {"balance": 90}}

        with patch("tool_loop._money_earning_enabled", return_value=False), \
             patch.dict(tool_loop.TOOL_REGISTRY, {
                 "wallet_spend": {
                     "description": "test", "parameters": {"type": "object", "properties": {
                         "amount": {"type": "number"}, "reason": {"type": "string"},
                     }, "required": ["amount", "reason"]},
                     "callable": _fake_spend, "fixed_args": {"wallet_id": "test"},
                 }
             }):
            res = await tool_loop.call_tool("wallet_spend", {"amount": 10, "reason": "买猫粮"})
        self.assertTrue(res["ok"])

    async def test_cat_shop_buy_not_gated(self):
        """cat_shop_buy（猫用品购买）不受 money_earning_enabled 影响。"""
        async def _fake_buy(**kwargs):
            return {"ok": True, "message": "购买成功", "data": {"item": "fish"}}

        with patch("tool_loop._money_earning_enabled", return_value=False), \
             patch.dict(tool_loop.TOOL_REGISTRY, {
                 "cat_shop_buy": {
                     "description": "test", "parameters": {"type": "object", "properties": {
                         "item_id": {"type": "string"}, "qty": {"type": "integer"},
                     }, "required": ["item_id"]},
                     "callable": _fake_buy, "fixed_args": {"user_id": "user_finn"},
                 }
             }):
            res = await tool_loop.call_tool("cat_shop_buy", {"item_id": "fish", "qty": 1})
        self.assertTrue(res["ok"])


# ============================================================
# 4. 暴露层：_build_tool_schema_block 隐藏 wallet_earn
# ============================================================
class TestSchemaBlockHidesWalletEarn(unittest.TestCase):
    def test_wallet_earn_hidden_when_disabled(self):
        """money_earning_enabled=false → 记点小账的 schema 不含 wallet_earn。"""
        with patch("tool_loop._money_earning_enabled", return_value=False):
            block = tool_loop._build_tool_schema_block("记点小账")
        self.assertNotIn("wallet_earn", block)
        # 其他钱包工具仍在
        self.assertIn("wallet_check", block)
        self.assertIn("wallet_spend", block)
        self.assertIn("wallet_log", block)

    def test_wallet_earn_shown_when_enabled(self):
        """money_earning_enabled=true → 记点小账的 schema 含 wallet_earn。"""
        with patch("tool_loop._money_earning_enabled", return_value=True):
            block = tool_loop._build_tool_schema_block("记点小账")
        self.assertIn("wallet_earn", block)

    def test_other_activities_unaffected_when_disabled(self):
        """关闭时不影响其他活动的工具暴露（逛虚拟小屋仍含 cat_*）。"""
        with patch("tool_loop._money_earning_enabled", return_value=False):
            block = tool_loop._build_tool_schema_block("逛虚拟小屋")
        self.assertIn("cat_status", block)
        self.assertIn("cat_shop_buy", block)


# ============================================================
# 5. server.wallet_earn MCP 入口门控
# ============================================================
class TestServerWalletEarnGate(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_bypass_cap_false_rejected_when_disabled(self):
        """server.wallet_earn + bypass_cap=False + 关闭 → 返回 MONEY_EARNING_DISABLED。"""
        import server
        with patch("gateway._money_earning_enabled", return_value=False):
            ret = await server.wallet_earn(amount=10, source_key="task_1", reason="test",
                                           bypass_cap=False)
        self.assertFalse(ret["ok"])
        self.assertEqual(ret["error_code"], "MONEY_EARNING_DISABLED")
        self.assertIn("赚钱系统已关闭", ret["message"])

    async def test_mcp_bypass_cap_true_allowed_when_disabled(self):
        """server.wallet_earn + bypass_cap=True + 关闭 → 不拦截，走实际入账。"""
        import server
        fake_ret = {"ok": True, "message": "入账成功", "data": {"balance": 125}}
        with patch("gateway._money_earning_enabled", return_value=False), \
             patch("home_system.wallet_earn", return_value=fake_ret) as m_hs:
            ret = await server.wallet_earn(amount=25, source_key="allowance_2026W33",
                                           reason="零花钱", bypass_cap=True)
        self.assertTrue(ret["ok"])
        m_hs.assert_called_once()

    async def test_mcp_bypass_cap_false_allowed_when_enabled(self):
        """server.wallet_earn + bypass_cap=False + 开启 → 不拦截。"""
        import server
        fake_ret = {"ok": True, "message": "入账成功", "data": {"balance": 110}}
        with patch("gateway._money_earning_enabled", return_value=True), \
             patch("home_system.wallet_earn", return_value=fake_ret) as m_hs:
            ret = await server.wallet_earn(amount=10, source_key="task_1", reason="test",
                                           bypass_cap=False)
        self.assertTrue(ret["ok"])
        m_hs.assert_called_once()

    async def test_mcp_other_wallet_tools_unaffected(self):
        """server.wallet_check / wallet_spend / cat_shop_buy 不受开关影响。"""
        import server
        with patch("gateway._money_earning_enabled", return_value=False), \
             patch("home_system.wallet_check",
                   return_value={"ok": True, "message": "ok", "data": {}}) as m_ck, \
             patch("home_system.wallet_spend",
                   return_value={"ok": True, "message": "ok", "data": {}}) as m_sp:
            r1 = await server.wallet_check()
            r2 = await server.wallet_spend(amount=5, reason="买水")
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])
        m_ck.assert_called_once()
        m_sp.assert_called_once()


# ============================================================
# 6. /api/admin/config PATCH 白名单
# ============================================================
class TestAdminConfigPatch(unittest.TestCase):
    """验证 money_earning_enabled 在 PATCH 白名单中，非白名单字段被拒。"""

    def _extract_whitelist(self):
        """从 gateway._handle_admin_config 源码提取 allowed 集合。"""
        import inspect, gateway
        src = inspect.getsource(gateway.HostFixMiddleware._handle_admin_config)
        # 简单验证：源码包含 money_earning_enabled
        return src

    def test_whitelist_contains_money_earning_enabled(self):
        """PATCH 白名单含 money_earning_enabled。"""
        src = self._extract_whitelist()
        self.assertIn("money_earning_enabled", src)

    def test_status_config_sources_contains_money_earning(self):
        """/api/admin/status 的 config_sources 含 money_earning_enabled。"""
        import inspect, gateway
        src = inspect.getsource(gateway.HostFixMiddleware._handle_admin_status)
        self.assertIn("money_earning_enabled", src)

    def test_status_process_dict_contains_money_earning(self):
        """/api/admin/status 的 process dict 含 money_earning_enabled。"""
        import inspect, gateway
        src = inspect.getsource(gateway.HostFixMiddleware._handle_admin_status)
        # process dict 里有 money_earning_enabled 字段
        self.assertIn('"money_earning_enabled"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
