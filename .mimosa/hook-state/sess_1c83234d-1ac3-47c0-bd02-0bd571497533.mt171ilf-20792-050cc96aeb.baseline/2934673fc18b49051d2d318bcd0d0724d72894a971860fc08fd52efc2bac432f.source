"""
test_home_diary_compat.py — Phase 6 私密日记兼容与钱包边界测试
================================================================
覆盖：
- 统一私密日记索引（新旧合并、正文不泄露、reference 格式）
- 私密日记正文读取权限（is_internal 门控）
- list_private_diary 不再注册为 MCP 工具
- 钱包权威源（Home Runtime 不保存第二套余额）
- 钱包 RPC anon 权限收紧验证
"""

import unittest
from unittest.mock import patch, MagicMock

from home import service as svc


# ============================================================
# 1. 统一私密日记索引
# ============================================================

class Test01UnifiedDiaryIndex(unittest.TestCase):
    @patch("home.repository.fetch_legacy_secret_diaries")
    @patch("home.repository.count_legacy_secret_diaries")
    @patch("home.repository.fetch_private_diaries")
    def test_index_merges_legacy_and_home(self, m_home, m_count, m_legacy):
        m_legacy.return_value = [
            {"id": 101, "title": "旧日记", "mood": "平静", "created_at": "2026-08-01T10:00:00Z"}
        ]
        m_count.return_value = 1
        m_home.return_value = [
            {"diary_key": "d1", "title": "新日记", "mood": "开心", "status": "active", "created_at": "2026-08-18T10:00:00Z"}
        ]
        result = svc.list_private_diary_index(limit=50, offset=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["legacy_count"], 1)
        self.assertEqual(result["data"]["home_count"], 1)
        self.assertEqual(len(result["data"]["items"]), 2)
        # 验证 reference 格式
        refs = [item["reference"] for item in result["data"]["items"]]
        self.assertIn("legacy:101", refs)
        self.assertIn("home:d1", refs)

    @patch("home.repository.fetch_legacy_secret_diaries")
    @patch("home.repository.count_legacy_secret_diaries")
    @patch("home.repository.fetch_private_diaries")
    def test_index_no_content(self, m_home, m_count, m_legacy):
        m_legacy.return_value = [{"id": 1, "title": "旧", "mood": "平静", "created_at": "2026-08-01"}]
        m_count.return_value = 1
        m_home.return_value = [{"diary_key": "d1", "title": "新", "mood": "开心", "status": "active", "created_at": "2026-08-18"}]
        result = svc.list_private_diary_index()
        result_str = str(result)
        self.assertNotIn("content", result_str.lower())
        self.assertNotIn("embedding", result_str.lower())

    @patch("home.repository.fetch_legacy_secret_diaries")
    @patch("home.repository.count_legacy_secret_diaries")
    @patch("home.repository.fetch_private_diaries")
    def test_index_sorted_by_created_desc(self, m_home, m_count, m_legacy):
        m_legacy.return_value = [
            {"id": 1, "title": "旧", "mood": "", "created_at": "2026-08-01T10:00:00Z"}
        ]
        m_count.return_value = 1
        m_home.return_value = [
            {"diary_key": "d1", "title": "新", "mood": "", "status": "active", "created_at": "2026-08-18T10:00:00Z"}
        ]
        result = svc.list_private_diary_index()
        items = result["data"]["items"]
        # 新日记在前（时间倒序）
        self.assertEqual(items[0]["source"], "home")
        self.assertEqual(items[1]["source"], "legacy")

    def test_index_invalid_limit(self):
        result = svc.list_private_diary_index(limit=0)
        self.assertFalse(result["ok"])
        result = svc.list_private_diary_index(limit=201)
        self.assertFalse(result["ok"])

    def test_index_invalid_offset(self):
        result = svc.list_private_diary_index(offset=-1)
        self.assertFalse(result["ok"])

    @patch("home.repository._get_supabase")
    def test_index_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.list_private_diary_index()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["legacy_count"], 0)
        self.assertEqual(result["data"]["home_count"], 0)


# ============================================================
# 2. 正文读取权限
# ============================================================

class Test02ReadByReference(unittest.TestCase):
    def test_mcp_path_denied(self):
        """通用路径（is_internal=False）被拒绝。"""
        result = svc.read_private_diary_by_reference("legacy:1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PRIVATE_DIARY_ACCESS_DENIED")

    def test_invalid_reference_format(self):
        result = svc.read_private_diary_by_reference("invalid", is_internal=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_REFERENCE")

    def test_unsupported_source(self):
        result = svc.read_private_diary_by_reference("unknown:1", is_internal=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_REFERENCE")

    @patch("home.repository._get_supabase")
    def test_legacy_internal_read(self, mock_sb):
        """内部路径可读旧日记。"""
        mock_sb.return_value = MagicMock()
        mock_sb.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(data=[
            {"id": 101, "title": "旧日记", "content": "正文", "mood": "平静", "created_at": "2026-08-01"}
        ])
        result = svc.read_private_diary_by_reference("legacy:101", is_internal=True)
        self.assertTrue(result["ok"])
        self.assertIn("content", result.get("data", {}))

    @patch("home.repository.rpc_read_private_diary")
    def test_home_internal_read(self, mock_rpc):
        """内部路径可读新日记。"""
        mock_rpc.return_value = {"ok": True, "title": "新日记", "content": "正文"}
        result = svc.read_private_diary_by_reference("home:d1", is_internal=True)
        self.assertTrue(result["ok"])


# ============================================================
# 3. list_private_diary 不再注册为 MCP 工具
# ============================================================

class Test03ListPrivateDiaryNotMCP(unittest.TestCase):
    def test_not_registered(self):
        """搜索 server.py 源码确认 list_private_diary 不再注册为 @mcp.tool。"""
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        lines = src.split("\n")
        found = False
        for i, line in enumerate(lines):
            if "async def list_private_diary" in line:
                for j in range(max(0, i-5), i):
                    if "@mcp.tool" in lines[j]:
                        found = True
        self.assertFalse(found, "list_private_diary 不应注册为 MCP 工具")


# ============================================================
# 4. 钱包权威源——Home Runtime 不保存第二套余额
# ============================================================

class Test04WalletAuthority(unittest.TestCase):
    """确认 Home Runtime 代码中不包含直接修改 wallet 表的代码。"""

    def test_home_repository_no_wallet_write(self):
        """home/repository.py 不应直接操作 wallet 表。"""
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        repo_path = os.path.join(here, "home", "repository.py")
        with open(repo_path, "r", encoding="utf-8") as f:
            src = f.read()
        # 不应直接写 wallet 表（只读查询不在此检查范围）
        # 搜索 .table("wallet") 的 insert/update/delete
        self.assertNotIn('.table("wallet").insert', src)
        self.assertNotIn('.table("wallet").update', src)
        self.assertNotIn('.table("wallet").delete', src)

    def test_home_service_no_wallet_operations(self):
        """home/service.py 不应调用钱包 RPC。"""
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        svc_path = os.path.join(here, "home", "service.py")
        with open(svc_path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("rpc_wallet_earn", src)
        self.assertNotIn("rpc_wallet_spend", src)
        self.assertNotIn("rpc_wallet_exchange", src)

    def test_home_models_no_balance_field(self):
        """home/models.py 不应包含余额字段。"""
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        models_path = os.path.join(here, "home", "models.py")
        with open(models_path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("balance", src.lower())
        self.assertNotIn("coins", src.lower())
        self.assertNotIn("currency", src.lower())


# ============================================================
# 5. Home Runtime 生活行为不隐式收费/赚钱
# ============================================================

class Test05NoImplicitWalletOps(unittest.TestCase):
    """确认种植/烹饪/信件/便利贴服务函数不调用钱包操作。"""

    def test_garden_no_wallet(self):
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        svc_path = os.path.join(here, "home", "service.py")
        with open(svc_path, "r", encoding="utf-8") as f:
            src = f.read()
        # 种植/烹饪/信件/便利贴相关函数不应包含 wallet 调用
        # 检查是否有 wallet_earn/spend 在这些函数中
        for func_name in ["plant_seed", "water_plant", "harvest_plant", "cook_recipe",
                          "cook_freestyle", "eat_dish", "feed_member",
                          "write_letter", "leave_note", "write_private_diary"]:
            # 确认函数存在
            self.assertIn(f"def {func_name}", src)
        # 确认整个文件不含钱包 RPC 调用
        self.assertNotIn("rpc_wallet", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
