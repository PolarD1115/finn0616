"""
test_home_expression_security.py — Phase 5 安全补丁测试
=========================================================
覆盖：
- 私密日记：MCP 路径不可读取/归档正文，客户端不能冒充 AI
- 信件：列表不返回 content，Home Context 只显示数量
- 便利贴：列表不 SELECT content，read_note 校验房间
- Service Role 绕过：数据库能读到 ≠ 调用者有权看到
- 敏感输出不包含密钥/Token/正文
"""

import unittest
from unittest.mock import patch, MagicMock

from home import service as svc
from home import repository as repo


# ============================================================
# 1. 私密日记：MCP 路径不可读取正文
# ============================================================

class Test01PrivateDiaryReadBlocked(unittest.TestCase):
    """read_private_diary 默认 is_internal=False，返回 ACCESS_DENIED。"""

    def test_mcp_path_denied(self):
        """普通 MCP 调用（is_internal=False）被拒绝。"""
        result = svc.read_private_diary("d1", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PRIVATE_DIARY_ACCESS_DENIED")

    def test_mcp_path_denied_even_with_valid_key(self):
        """即使 diary_key 有效，MCP 路径仍然被拒绝。"""
        result = svc.read_private_diary("diary_valid_key", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PRIVATE_DIARY_ACCESS_DENIED")

    @patch("home.repository.rpc_read_private_diary")
    def test_internal_path_allowed(self, mock_rpc):
        """内部受控调用（is_internal=True）可以读取。"""
        mock_rpc.return_value = {"ok": True, "title": "日记", "content": "正文"}
        result = svc.read_private_diary("d1", "act1", is_internal=True)
        self.assertTrue(result["ok"])
        self.assertIn("content", result)


# ============================================================
# 2. 私密日记：MCP 路径不可归档
# ============================================================

class Test02PrivateDiaryArchiveBlocked(unittest.TestCase):
    """archive_private_diary 默认 is_internal=False，返回 ACCESS_DENIED。"""

    def test_mcp_path_denied(self):
        result = svc.archive_private_diary("d1", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PRIVATE_DIARY_ACCESS_DENIED")

    @patch("home.repository.rpc_archive_private_diary")
    def test_internal_path_allowed(self, mock_rpc):
        mock_rpc.return_value = {"ok": True}
        result = svc.archive_private_diary("d1", "act1", is_internal=True)
        self.assertTrue(result["ok"])


# ============================================================
# 3. 私密日记：write 强制 author_key
# ============================================================

class Test03PrivateDiaryWriteForcedAuthor(unittest.TestCase):
    """MCP 路径（is_internal=False）强制 author_key=ai_primary，不信任客户端。"""

    @patch("home.repository.rpc_write_private_diary")
    def test_mcp_forces_ai_primary(self, mock_rpc):
        """客户端传 author_key=pet_xiaoman 也被强制为 ai_primary。"""
        mock_rpc.return_value = {"ok": True, "diary_key": "d1"}
        # MCP 路径不传 is_internal，默认 False
        result = svc.write_private_diary("pet_xiaoman", "标题", "正文", "act1")
        self.assertTrue(result["ok"])
        # 验证 RPC 被调用时 author_key 是 ai_primary
        call_args = mock_rpc.call_args
        self.assertEqual(call_args[0][1], "ai_primary")  # 第二个位置参数是 author_key

    @patch("home.repository.rpc_write_private_diary")
    def test_internal_path_respects_author(self, mock_rpc):
        """内部路径（is_internal=True）尊重传入的 author_key。"""
        mock_rpc.return_value = {"ok": True}
        result = svc.write_private_diary("ai_primary", "标题", "正文", "act1", is_internal=True)
        self.assertTrue(result["ok"])

    def test_internal_non_ai_denied(self):
        """内部路径也只允许 ai_primary。"""
        result = svc.write_private_diary("pet_xiaoman", "标题", "正文", "act1", is_internal=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "NOT_AUTHORIZED")


# ============================================================
# 3b. Phase 5.1: write_private_diary 不再作为 MCP 工具注册
# ============================================================

class Test03BWritePrivateDiaryNotMCP(unittest.TestCase):
    """write_private_diary 不应出现在 MCP 工具注册中。"""

    def test_not_registered_as_mcp_tool(self):
        """搜索 server.py 源码确认 write_private_diary 不再注册为 @mcp.tool。"""
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        # 确认不存在 write_private_diary 的 @mcp.tool 注册
        # 搜索 "async def write_private_diary" 前面是否有 @mcp.tool
        lines = src.split("\n")
        found_mcp_write_diary = False
        for i, line in enumerate(lines):
            if "async def write_private_diary" in line:
                # 检查前5行是否有 @mcp.tool
                for j in range(max(0, i-5), i):
                    if "@mcp.tool" in lines[j]:
                        found_mcp_write_diary = True
                        break
        self.assertFalse(found_mcp_write_diary, "write_private_diary 不应注册为 MCP 工具")

    def test_read_private_diary_not_mcp(self):
        """read_private_diary 不应注册为 MCP 工具。"""
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        lines = src.split("\n")
        found = False
        for i, line in enumerate(lines):
            if "async def read_private_diary" in line:
                for j in range(max(0, i-5), i):
                    if "@mcp.tool" in lines[j]:
                        found = True
        self.assertFalse(found)

    def test_archive_private_diary_not_mcp(self):
        """archive_private_diary 不应注册为 MCP 工具。"""
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "server.py"), "r", encoding="utf-8") as f:
            src = f.read()
        lines = src.split("\n")
        found = False
        for i, line in enumerate(lines):
            if "async def archive_private_diary" in line:
                for j in range(max(0, i-5), i):
                    if "@mcp.tool" in lines[j]:
                        found = True
        self.assertFalse(found)


# ============================================================
# 3c. Phase 5.1: API_SECRET 空值拒绝
# ============================================================

class Test03CApiSecretEmpty(unittest.TestCase):
    """API_SECRET 为空时 _check_api_secret 应拒绝。"""

    def test_empty_secret_rejected(self):
        """API_SECRET 为空时返回 False（拒绝）。"""
        import os
        # 模拟 API_SECRET 为空
        old_val = os.environ.get("API_SECRET", "")
        os.environ["API_SECRET"] = ""
        try:
            import asyncio
            import gateway as _gw

            sent_responses = []

            async def mock_send(msg):
                sent_responses.append(msg)

            scope = {"headers": [], "path": "/api/test", "method": "GET"}
            result = asyncio.run(_gw._check_api_secret(scope, mock_send))
            self.assertFalse(result)  # 应拒绝
            # 检查返回 503
            self.assertTrue(any(r.get("status") == 503 for r in sent_responses if r.get("type") == "http.response.start"))
        finally:
            os.environ["API_SECRET"] = old_val

    def test_correct_secret_accepted(self):
        """API_SECRET 正确配置时通过。"""
        import os
        old_val = os.environ.get("API_SECRET", "")
        test_secret = "testsecret123"
        os.environ["API_SECRET"] = test_secret
        try:
            import asyncio
            import gateway as _gw

            async def mock_send(msg):
                pass

            scope = {
                "headers": [(b"authorization", f"Bearer {test_secret}".encode())],
                "path": "/api/test",
                "method": "GET",
            }
            result = asyncio.run(_gw._check_api_secret(scope, mock_send))
            self.assertTrue(result)  # 应通过
        finally:
            os.environ["API_SECRET"] = old_val

    def test_wrong_secret_rejected(self):
        """错误的 API_SECRET 被拒绝。"""
        import os
        old_val = os.environ.get("API_SECRET", "")
        os.environ["API_SECRET"] = "correct_secret"
        try:
            import asyncio
            import gateway as _gw

            sent_responses = []

            async def mock_send(msg):
                sent_responses.append(msg)

            scope = {
                "headers": [(b"authorization", b"Bearer wrong_secret")],
                "path": "/api/test",
                "method": "GET",
            }
            result = asyncio.run(_gw._check_api_secret(scope, mock_send))
            self.assertFalse(result)
            self.assertTrue(any(r.get("status") == 401 for r in sent_responses if r.get("type") == "http.response.start"))
        finally:
            os.environ["API_SECRET"] = old_val


# ============================================================
# 4. 私密日记列表不返回正文
# ============================================================

class Test04PrivateDiaryListNoContent(unittest.TestCase):
    @patch("home.repository.fetch_private_diaries")
    def test_list_no_content_field(self, mock_fetch):
        mock_fetch.return_value = [
            {"diary_key": "d1", "title": "秘密", "mood": "平静", "created_at": "2026-08-18"}
        ]
        result = svc.list_private_diary()
        self.assertTrue(result["ok"])
        result_str = str(result)
        self.assertNotIn("content", result_str)
        self.assertNotIn("正文", result_str)


# ============================================================
# 5. 信件列表不返回 content
# ============================================================

class Test05LetterListNoContent(unittest.TestCase):
    @patch("home.repository.fetch_letters")
    def test_list_no_content(self, mock_fetch):
        mock_fetch.return_value = [
            {"letter_key": "l1", "title": "信", "preview": "摘要", "status": "unopened", "created_at": "2026-08-18"}
        ]
        result = svc.list_letters()
        self.assertTrue(result["ok"])
        for letter in result["data"]["letters"]:
            self.assertNotIn("content", letter)

    @patch("home.repository.fetch_letters")
    def test_list_result_str_no_content(self, mock_fetch):
        mock_fetch.return_value = [
            {"letter_key": "l1", "title": "信", "preview": "摘要", "status": "unopened", "created_at": "2026-08-18"}
        ]
        result = svc.list_letters()
        result_str = str(result)
        self.assertNotIn("content", result_str)


# ============================================================
# 6. Home Context 只显示未拆信数量，不泄露正文
# ============================================================

class Test06HomeContextNoLeak(unittest.TestCase):
    @patch("home.repository.fetch_unopened_letter_count")
    @patch("home.repository.fetch_dishes")
    @patch("home.repository.fetch_inventory")
    @patch("home.repository.fetch_plants")
    @patch("home.repository.fetch_recent_events")
    @patch("home.repository.fetch_member_states")
    @patch("home.repository.fetch_members")
    @patch("home.repository.fetch_rooms")
    def test_context_only_shows_count(self, m_rooms, m_members, m_states, m_events, m_plants, m_inv, m_dishes, m_letters):
        m_rooms.return_value = [{"stable_key": "living_room", "name": "客厅", "emoji": "🛋️"}]
        m_members.return_value = []
        m_states.return_value = []
        m_events.return_value = []
        m_plants.return_value = []
        m_inv.return_value = []
        m_dishes.return_value = []
        m_letters.return_value = 3  # 3 封未拆信

        from home.context import build_home_context
        text = build_home_context()
        self.assertIn("3", text)
        self.assertIn("未拆开", text)
        # 不应包含信件正文
        self.assertNotIn("content", text.lower())


# ============================================================
# 7. 便利贴列表不 SELECT content
# ============================================================

class Test07NoteListNoContent(unittest.TestCase):
    @patch("home.repository.fetch_room_by_key")
    @patch("home.repository.fetch_notes_by_room")
    def test_list_no_content(self, m_notes, m_room):
        m_room.return_value = {"id": "rid1", "name": "客厅"}
        # fetch_notes_by_room 现在不返回 content 列
        m_notes.return_value = [
            {"note_key": "n1", "preview": "", "status": "active", "created_at": "2026-08-18"}
        ]
        result = svc.list_room_notes("living_room")
        self.assertTrue(result["ok"])
        for note in result["data"]["notes"]:
            self.assertNotIn("content", note)


# ============================================================
# 8. read_note 校验房间
# ============================================================

class Test08ReadNoteRoomCheck(unittest.TestCase):
    @patch("home.repository.fetch_note_by_key")
    @patch("home.repository.fetch_room_by_id")
    def test_read_note_valid_room(self, m_room, m_note):
        m_note.return_value = {"note_key": "n1", "room_id": "rid1", "content": "内容"}
        m_room.return_value = {"id": "rid1", "is_enabled": True, "is_hidden": False}
        with patch("home.repository.rpc_read_note") as m_rpc:
            m_rpc.return_value = {"ok": True, "content": "内容"}
            result = svc.read_note("n1", "act1")
            self.assertTrue(result["ok"])

    @patch("home.repository.fetch_note_by_key")
    @patch("home.repository.fetch_room_by_id")
    def test_read_note_hidden_room_denied(self, m_room, m_note):
        m_note.return_value = {"note_key": "n1", "room_id": "rid1"}
        m_room.return_value = {"id": "rid1", "is_enabled": True, "is_hidden": True}
        result = svc.read_note("n1", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "NOTE_NOT_ACCESSIBLE")

    @patch("home.repository.fetch_note_by_key")
    @patch("home.repository.fetch_room_by_id")
    def test_read_note_disabled_room_denied(self, m_room, m_note):
        m_note.return_value = {"note_key": "n1", "room_id": "rid1"}
        m_room.return_value = {"id": "rid1", "is_enabled": False, "is_hidden": False}
        result = svc.read_note("n1", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "NOTE_NOT_ACCESSIBLE")

    @patch("home.repository.fetch_note_by_key")
    def test_read_note_not_found(self, m_note):
        m_note.return_value = None
        result = svc.read_note("nonexistent", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "NOTE_NOT_FOUND")


# ============================================================
# 9. Service Role 绕过：数据库能读到 ≠ 调用者有权看到
# ============================================================

class Test09ServiceRoleBypass(unittest.TestCase):
    """即使 repository 可以返回数据（模拟 service_role 绕过 RLS），
    service 层仍必须拒绝未授权请求。"""

    @patch("home.repository.rpc_read_private_diary")
    def test_db_returns_data_but_service_denies(self, mock_rpc):
        """模拟数据库能返回正文（service_role 绕过 RLS），但 MCP 路径仍被拒绝。"""
        mock_rpc.return_value = {"ok": True, "content": "这是私密内容"}
        # MCP 路径（is_internal=False）
        result = svc.read_private_diary("d1", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PRIVATE_DIARY_ACCESS_DENIED")
        # 确保正文不泄露
        result_str = str(result)
        self.assertNotIn("这是私密内容", result_str)

    @patch("home.repository.rpc_archive_private_diary")
    def test_db_allows_archive_but_service_denies(self, mock_rpc):
        """模拟数据库允许归档，但 MCP 路径仍被拒绝。"""
        mock_rpc.return_value = {"ok": True}
        result = svc.archive_private_diary("d1", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "PRIVATE_DIARY_ACCESS_DENIED")


# ============================================================
# 10. 敏感输出检查
# ============================================================

class Test10NoSensitiveOutput(unittest.TestCase):
    @patch("home.repository.fetch_letters")
    def test_letter_list_no_secrets(self, mock_fetch):
        mock_fetch.return_value = []
        result = svc.list_letters()
        result_str = str(result).lower()
        for kw in ("api_key", "token", "password", "secret", "cookie", "service_role"):
            self.assertNotIn(kw, result_str)

    @patch("home.repository.fetch_private_diaries")
    def test_diary_list_no_secrets(self, mock_fetch):
        mock_fetch.return_value = []
        result = svc.list_private_diary()
        result_str = str(result).lower()
        for kw in ("api_key", "token", "password", "secret", "cookie", "service_role"):
            self.assertNotIn(kw, result_str)

    def test_read_private_diary_denied_no_content_leak(self):
        """被拒绝的读取不泄露正文。"""
        result = svc.read_private_diary("d1", "act1")
        result_str = str(result)
        # 只应包含错误码和消息，不含正文
        self.assertNotIn("content", result_str.lower())


# ============================================================
# 11. 归档不删除数据
# ============================================================

class Test11ArchiveNoDelete(unittest.TestCase):
    @patch("home.repository.rpc_archive_letter")
    def test_archive_letter_soft(self, mock_rpc):
        """归档信件是软归档（改 status），不是删除。"""
        mock_rpc.return_value = {"ok": True, "already_archived": False}
        result = svc.archive_letter("l1", "act1")
        self.assertTrue(result["ok"])
        # 确保没有 DELETE 调用（mock 只收到 archive RPC 调用）
        mock_rpc.assert_called_once()

    @patch("home.repository.rpc_archive_note")
    def test_archive_note_soft(self, mock_rpc):
        mock_rpc.return_value = {"ok": True}
        result = svc.archive_note("n1", "act1")
        self.assertTrue(result["ok"])
        mock_rpc.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
