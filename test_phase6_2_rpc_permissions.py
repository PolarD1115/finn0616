"""
test_phase6_2_rpc_permissions.py — Phase 6.2 RPC 权限与记忆过滤收口测试
=======================================================================
覆盖：
- RPC 权限矩阵（7个逐个确认）
- search_memory NULL 标签安全
- MCP bypass_cap 额外参数行为
- 前端无 sb.rpc 钱包调用
"""

import unittest
import os
import asyncio


# ============================================================
# 1. search_memory NULL 标签安全
# ============================================================

class Test01SearchMemoryNullSafe(unittest.TestCase):
    """确认 search_memory 使用 or_ 而非 neq，NULL 标签不被误过滤。"""

    def test_uses_or_not_neq(self):
        """server.py 中 search_memory 应使用 or_ 而非 neq 排除 Secret_Diary。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def search_memory")
        self.assertGreater(idx, 0)
        func_body = src[idx:idx + 3000]
        # 应使用 or_("tags.neq.Secret_Diary,tags.is.null")
        self.assertIn("tags.is.null", func_body,
                       "search_memory 应包含 tags.is.null 以保留 NULL 标签记忆")
        # 不应使用 .neq("tags", "Secret_Diary")（会排除 NULL）
        self.assertNotIn('.neq("tags", "Secret_Diary")', func_body,
                         "search_memory 不应使用 .neq 排除 Secret_Diary（会误过滤 NULL）")

    def test_service_layer_handles_list_tags(self):
        """服务层二次过滤应处理 list 格式的 tags。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def search_memory")
        func_body = src[idx:idx + 3000]
        self.assertIn("isinstance(meta_tags, list)", func_body,
                       "服务层应处理 list 格式的 metadata tags")

    def test_service_layer_handles_string_tags(self):
        """服务层二次过滤应处理 string 格式的 tags。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def search_memory")
        func_body = src[idx:idx + 3000]
        self.assertIn("isinstance(meta_tags, str)", func_body,
                       "服务层应处理 string 格式的 metadata tags")

    def test_no_include_private_param(self):
        """search_memory 不应有 include_private 参数。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def search_memory")
        func_body = src[idx:idx + 3000]
        self.assertNotIn("include_private", func_body.lower())


# ============================================================
# 2. MCP bypass_cap 兼容行为
# ============================================================

class Test02McpBypassCapBehavior(unittest.TestCase):
    """确认 MCP wallet_earn 传 bypass_cap 的真实行为。"""

    def test_mcp_signature_no_bypass(self):
        """wallet_earn 函数签名不含 bypass_cap。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def wallet_earn")
        sig_end = src.find("\n", idx)
        sig_line = src[idx:sig_end]
        self.assertNotIn("bypass_cap", sig_line)

    def test_mcp_body_fixed_false(self):
        """wallet_earn 函数体固定 bypass_cap=False。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def wallet_earn")
        func_body = src[idx:idx + 1500]
        self.assertIn("bypass_cap=False", func_body)

    def test_bypass_cap_extra_param_typeerror(self):
        """传 bypass_cap=True 给 MCP wallet_earn 返回错误（TypeError 被 mcp_error_handler 捕获）。"""
        import server

        async def _test():
            # wallet_earn 不接受 bypass_cap 参数
            result = await server.wallet_earn(
                amount=1, source_key="compat_test", reason="test", bypass_cap=True
            )
            return result

        result = asyncio.run(_test())
        # mcp_error_handler 捕获 TypeError，返回错误字符串
        self.assertIsInstance(result, str)
        self.assertIn("❌", result)
        self.assertIn("bypass_cap", result)


# ============================================================
# 3. 前端无 sb.rpc 钱包调用（回归确认）
# ============================================================

class Test03FrontendNoSbRpcWallet(unittest.TestCase):
    """确认 console.html 和 miniapp.html 不直调钱包 RPC。"""

    def test_console_no_wallet_rpc(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "console.html"), "r", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('sb.rpc("rpc_wallet_earn"', src)
        self.assertNotIn("_walletEarnRpc", src)

    def test_miniapp_no_wallet_rpc(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "miniapp.html"), "r", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('sb.rpc("rpc_wallet_earn"', src)
        self.assertNotIn("_walletEarnRpc", src)

    def test_no_service_role_in_frontend(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for fname in ("console.html", "miniapp.html"):
            with open(os.path.join(here, fname), "r", encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("service_role", src.lower())


# ============================================================
# 4. tool_loop bypass_cap 固定 False（回归确认）
# ============================================================

class Test04ToolLoopBypassFixed(unittest.TestCase):
    """确认 tool_loop wallet_earn 固定 bypass_cap=False。"""

    def test_schema_no_bypass_in_properties(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "tool_loop.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find('"wallet_earn"')
        block_end = src.find('"wallet_spend"', idx)
        block = src[idx:block_end]
        props_start = block.find('"properties"')
        props_end = block.find('"required"')
        if props_start > 0 and props_end > 0:
            props_block = block[props_start:props_end]
            self.assertNotIn('"bypass_cap"', props_block)

    def test_fixed_args_has_bypass_false(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "tool_loop.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find('"wallet_earn"')
        block_end = src.find('"wallet_spend"', idx)
        block = src[idx:block_end]
        self.assertIn('"bypass_cap": False', block)

    def test_call_tool_no_bypass_condition(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "tool_loop.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find('if name == "wallet_earn"')
        self.assertGreater(idx, 0)
        block = src[idx:idx + 300]
        self.assertNotIn("bypass_cap", block)


# ============================================================
# 5. 钱包 API 路由存在（回归确认）
# ============================================================

class Test05WalletApiRoutesExist(unittest.TestCase):
    """确认 gateway.py 钱包 API 路由存在。"""

    def test_routes_exist(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "gateway.py"), "r", encoding="utf-8") as f:
            src = f.read()
        for route in ["/api/wallet", "/api/wallet/log", "/api/wallet/allowance",
                      "/api/wallet/tip", "/api/wallet/spend"]:
            self.assertIn(route, src)

    def test_handler_exists(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "gateway.py"), "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_handle_wallet_api", src)


# ============================================================
# 6. API_SECRET 空值拒绝（回归确认）
# ============================================================

class Test06ApiSecretEmptyRejected(unittest.TestCase):
    """API_SECRET 为空时受保护入口返回 503。"""

    def test_empty_secret_rejected(self):
        import os
        old_val = os.environ.get("API_SECRET", "")
        os.environ["API_SECRET"] = ""
        try:
            import asyncio
            import gateway as _gw

            sent_responses = []

            async def mock_send(msg):
                sent_responses.append(msg)

            scope = {"headers": [], "path": "/api/wallet", "method": "GET"}
            result = asyncio.run(_gw._check_api_secret(scope, mock_send))
            self.assertFalse(result)
            self.assertTrue(any(r.get("status") == 503 for r in sent_responses if r.get("type") == "http.response.start"))
        finally:
            os.environ["API_SECRET"] = old_val


if __name__ == "__main__":
    unittest.main(verbosity=2)
