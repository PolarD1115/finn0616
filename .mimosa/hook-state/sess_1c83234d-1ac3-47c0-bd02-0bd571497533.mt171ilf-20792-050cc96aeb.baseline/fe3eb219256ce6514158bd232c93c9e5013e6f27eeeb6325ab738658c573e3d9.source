"""
test_phase6_1_wallet_api.py — Phase 6.1 钱包 API 与记忆搜索隐私测试
=================================================================
覆盖：
- search_memory 排除 Secret_Diary
- wallet_earn MCP 移除 bypass_cap
- tool_loop wallet_earn schema 不含 bypass_cap
- 前端不再 sb.rpc 钱包调用
"""

import unittest
import os


# ============================================================
# 1. search_memory 排除 Secret_Diary
# ============================================================

class Test01SearchMemoryPrivacy(unittest.TestCase):
    """确认 search_memory 在 SQL 查询和服务层都排除 Secret_Diary。"""

    def test_supabase_query_excludes_secret_diary(self):
        """server.py 中 search_memory 的 Supabase 查询应排除 Secret_Diary（Phase 6.2: 使用 or_ 而非 neq）"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def search_memory")
        self.assertGreater(idx, 0, "search_memory 函数应存在")
        func_body = src[idx:idx + 3000]
        # Phase 6.2: 使用 or_ 包含 NULL 标签，同时排除 Secret_Diary
        self.assertIn("tags.neq.Secret_Diary", func_body,
                       "search_memory 的 Supabase 查询应排除 Secret_Diary")
        self.assertIn("tags.is.null", func_body,
                       "search_memory 应包含 NULL 标签记忆")

    def test_pinecone_results_filtered(self):
        """Pinecone 结果应有服务层二次过滤。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def search_memory")
        func_body = src[idx:idx + 2000]
        self.assertIn("_PRIVATE_TAGS", func_body,
                       "search_memory 应有私密标签黑名单")

    def test_no_include_private_param(self):
        """search_memory 不应有 include_private 参数。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def search_memory")
        func_body = src[idx:idx + 2000]
        self.assertNotIn("include_private", func_body.lower())


# ============================================================
# 2. wallet_earn MCP 移除 bypass_cap
# ============================================================

class Test02WalletEarnNoBypassCap(unittest.TestCase):
    """确认 MCP wallet_earn 不暴露 bypass_cap 参数。"""

    def test_mcp_signature_no_bypass(self):
        """server.py 中 wallet_earn 函数签名不应含 bypass_cap。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def wallet_earn")
        self.assertGreater(idx, 0)
        # 读取函数签名行
        sig_end = src.find("\n", idx)
        sig_line = src[idx:sig_end]
        self.assertNotIn("bypass_cap", sig_line,
                         "wallet_earn MCP 签名不应含 bypass_cap")

    def test_mcp_body_fixed_false(self):
        """wallet_earn 函数体应固定 bypass_cap=False。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def wallet_earn")
        func_body = src[idx:idx + 1500]
        self.assertIn("bypass_cap=False", func_body,
                       "wallet_earn 应固定 bypass_cap=False")


# ============================================================
# 3. tool_loop schema 不含 bypass_cap
# ============================================================

class Test03ToolLoopNoBypassCap(unittest.TestCase):
    """确认 tool_loop wallet_earn schema 不含 bypass_cap。"""

    def test_schema_no_bypass(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "tool_loop.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find('"wallet_earn"')
        self.assertGreater(idx, 0)
        # 读取该工具定义块（到下一个工具定义前）
        block_end = src.find('"wallet_spend"', idx)
        block = src[idx:block_end]
        # properties 中的 bypass_cap（schema 暴露给 LLM）不应存在
        # 但 fixed_args 中的 bypass_cap: False（固定值）是允许的
        # 检查 properties 块内不含 bypass_cap
        props_start = block.find('"properties"')
        props_end = block.find('"required"')
        if props_start > 0 and props_end > 0:
            props_block = block[props_start:props_end]
            self.assertNotIn('"bypass_cap"', props_block,
                             "tool_loop wallet_earn properties schema 不应含 bypass_cap")

    def test_fixed_args_has_bypass_false(self):
        """fixed_args 应固定 bypass_cap=False。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "tool_loop.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find('"wallet_earn"')
        block_end = src.find('"wallet_spend"', idx)
        block = src[idx:block_end]
        self.assertIn('"bypass_cap": False', block,
                       "fixed_args 应固定 bypass_cap=False")

    def test_call_tool_no_bypass_check(self):
        """call_tool 中不应有 bypass_cap 条件判断。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "tool_loop.py"), "r", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("if name == \"wallet_earn\"")
        self.assertGreater(idx, 0)
        block = src[idx:idx + 300]
        self.assertNotIn("bypass_cap", block,
                         "call_tool 不应有 bypass_cap 条件判断")


# ============================================================
# 4. 前端不再 sb.rpc 钱包调用
# ============================================================

class Test04FrontendNoSbRpcWallet(unittest.TestCase):
    """确认 console.html 和 miniapp.html 不再直接 sb.rpc 钱包 RPC。"""

    def test_console_no_wallet_rpc(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "console.html"), "r", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('sb.rpc("rpc_wallet_earn"', src,
                         "console.html 不应直调 rpc_wallet_earn")
        self.assertNotIn("_walletEarnRpc", src,
                         "console.html 不应有 _walletEarnRpc 函数")

    def test_miniapp_no_wallet_rpc(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "miniapp.html"), "r", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('sb.rpc("rpc_wallet_earn"', src,
                         "miniapp.html 不应直调 rpc_wallet_earn")
        self.assertNotIn("_walletEarnRpc", src,
                         "miniapp.html 不应有 _walletEarnRpc 函数")

    def test_console_uses_backend_api(self):
        """console.html 钱包操作应走 /api/wallet/ 后端 API。"""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "console.html"), "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("/api/wallet/allowance", src)
        self.assertIn("/api/wallet/tip", src)

    def test_miniapp_uses_backend_api(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "miniapp.html"), "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("/api/wallet/allowance", src)
        self.assertIn("/api/wallet/tip", src)

    def test_no_service_role_in_frontend(self):
        """前端不应含 service_role key。"""
        here = os.path.dirname(os.path.abspath(__file__))
        for fname in ("console.html", "miniapp.html"):
            with open(os.path.join(here, fname), "r", encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("service_role", src.lower(),
                             f"{fname} 不应含 service_role")


# ============================================================
# 5. gateway.py 钱包 API 路由存在
# ============================================================

class Test05GatewayWalletApiRoutes(unittest.TestCase):
    """确认 gateway.py 新增了钱包 API 路由。"""

    def test_routes_exist(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "gateway.py"), "r", encoding="utf-8") as f:
            src = f.read()
        for route in ["/api/wallet", "/api/wallet/log", "/api/wallet/allowance",
                      "/api/wallet/tip", "/api/wallet/spend"]:
            self.assertIn(route, src, f"gateway.py 应含路由 {route}")

    def test_handler_exists(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "gateway.py"), "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_handle_wallet_api", src,
                       "gateway.py 应有 _handle_wallet_api 方法")


if __name__ == "__main__":
    unittest.main(verbosity=2)
