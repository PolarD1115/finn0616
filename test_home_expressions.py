"""
test_home_expressions.py — Phase 5 信件/便利贴/私密日记测试
=============================================================
覆盖：
- 信件参数校验 + 幂等 + 未拆信不返回正文
- 便利贴参数校验 + 幂等 + 房间绑定
- 私密日记参数校验 + 仅AI可写 + 不进入普通上下文
- 可见性安全
- 数据库不可用降级
- 回归
"""

import unittest
from unittest.mock import patch, MagicMock

from home import service as svc


# ============================================================
# 1. 信件参数校验
# ============================================================

class Test01WriteLetterValidation(unittest.TestCase):
    def test_empty_action_key(self):
        result = svc.write_letter("ai_primary", "标题", "正文", "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ACTION_KEY")

    def test_empty_author(self):
        result = svc.write_letter("", "标题", "正文", "act1")
        self.assertFalse(result["ok"])

    def test_empty_title(self):
        result = svc.write_letter("ai_primary", "", "正文", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_TITLE")

    def test_empty_content(self):
        result = svc.write_letter("ai_primary", "标题", "", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_CONTENT")

    def test_content_too_long(self):
        result = svc.write_letter("ai_primary", "标题", "x" * 10001, "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "CONTENT_TOO_LONG")

    @patch("home.repository.rpc_write_letter")
    def test_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "letter_key": "letter_act1"}
        result = svc.write_letter("ai_primary", "给昕的信", "这是正文", "act1")
        self.assertTrue(result["ok"])


# ============================================================
# 2. 信件列表不返回正文
# ============================================================

class Test02ListLettersNoContent(unittest.TestCase):
    @patch("home.repository.fetch_letters")
    def test_list_returns_no_content(self, mock_fetch):
        mock_fetch.return_value = [
            {"letter_key": "l1", "title": "信1", "preview": "摘要", "status": "unopened", "created_at": "2026-08-18T10:00:00Z"}
        ]
        result = svc.list_letters()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]["letters"]), 1)
        letter = result["data"]["letters"][0]
        self.assertIn("title", letter)
        self.assertIn("preview", letter)
        self.assertNotIn("content", letter)
        self.assertTrue(letter["is_unopened"])

    @patch("home.repository.fetch_letters")
    def test_list_empty(self, mock_fetch):
        mock_fetch.return_value = []
        result = svc.list_letters()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 0)


# ============================================================
# 3. 信件打开和归档
# ============================================================

class Test03OpenArchiveLetter(unittest.TestCase):
    def test_open_empty_action_key(self):
        result = svc.open_letter("l1", "")
        self.assertFalse(result["ok"])

    def test_open_empty_letter_key(self):
        result = svc.open_letter("", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_open_letter")
    def test_open_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "title": "信1", "content": "正文"}
        result = svc.open_letter("l1", "act1")
        self.assertTrue(result["ok"])
        self.assertIn("content", result)

    @patch("home.repository.rpc_archive_letter")
    def test_archive_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "already_archived": False}
        result = svc.archive_letter("l1", "act1")
        self.assertTrue(result["ok"])

    @patch("home.repository.rpc_archive_letter")
    def test_archive_already(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "already_archived": True}
        result = svc.archive_letter("l1", "act1")
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("already_archived", False))


# ============================================================
# 4. 便利贴参数校验
# ============================================================

class Test04LeaveNoteValidation(unittest.TestCase):
    def test_empty_action_key(self):
        result = svc.leave_note("ai_primary", "living_room", "内容", "")
        self.assertFalse(result["ok"])

    def test_empty_room(self):
        result = svc.leave_note("ai_primary", "", "内容", "act1")
        self.assertFalse(result["ok"])

    def test_empty_content(self):
        result = svc.leave_note("ai_primary", "living_room", "", "act1")
        self.assertFalse(result["ok"])

    def test_content_too_long(self):
        result = svc.leave_note("ai_primary", "living_room", "x" * 2001, "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_leave_note")
    def test_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "note_key": "note_act1"}
        result = svc.leave_note("ai_primary", "living_room", "记得浇水", "act1")
        self.assertTrue(result["ok"])


# ============================================================
# 5. 便利贴列表和读取
# ============================================================

class Test05NoteListRead(unittest.TestCase):
    @patch("home.repository.fetch_room_by_key")
    @patch("home.repository.fetch_notes_by_room")
    def test_list_room_notes(self, m_notes, m_room):
        m_room.return_value = {"id": "rid1", "name": "客厅"}
        m_notes.return_value = [
            {"note_key": "n1", "preview": "记得浇水", "status": "active", "created_at": "2026-08-18T10:00:00Z"}
        ]
        result = svc.list_room_notes("living_room")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]["notes"]), 1)
        note = result["data"]["notes"][0]
        self.assertIn("preview", note)
        self.assertNotIn("content", note)

    @patch("home.repository.fetch_room_by_key")
    def test_list_room_notes_not_found(self, m_room):
        m_room.return_value = None
        result = svc.list_room_notes("nonexistent")
        self.assertFalse(result["ok"])

    @patch("home.repository.fetch_note_by_key")
    @patch("home.repository.fetch_room_by_id")
    @patch("home.repository.rpc_read_note")
    def test_read_note_valid(self, mock_rpc, m_room, m_note):
        m_note.return_value = {"note_key": "n1", "room_id": "rid1"}
        m_room.return_value = {"id": "rid1", "is_enabled": True, "is_hidden": False}
        mock_rpc.return_value = {"ok": True, "content": "记得浇水"}
        result = svc.read_note("n1", "act1")
        self.assertTrue(result["ok"])

    @patch("home.repository.rpc_archive_note")
    def test_archive_note_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True}
        result = svc.archive_note("n1", "act1")
        self.assertTrue(result["ok"])


# ============================================================
# 6. 私密日记参数校验 + 仅 AI 可写
# ============================================================

class Test06PrivateDiaryValidation(unittest.TestCase):
    def test_empty_action_key(self):
        result = svc.write_private_diary("ai_primary", "标题", "正文", "")
        self.assertFalse(result["ok"])

    def test_non_ai_author(self):
        # 安全补丁：MCP 路径强制 author_key=ai_primary，pet_xiaoman 被覆盖
        # 内部路径才检查 author_key
        result = svc.write_private_diary("pet_xiaoman", "标题", "正文", "act1", is_internal=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "NOT_AUTHORIZED")

    def test_empty_title(self):
        result = svc.write_private_diary("ai_primary", "", "正文", "act1")
        self.assertFalse(result["ok"])

    def test_empty_content(self):
        result = svc.write_private_diary("ai_primary", "标题", "", "act1")
        self.assertFalse(result["ok"])

    def test_content_too_long(self):
        result = svc.write_private_diary("ai_primary", "标题", "x" * 10001, "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_write_private_diary")
    def test_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "diary_key": "diary_act1"}
        result = svc.write_private_diary("ai_primary", "今天的心情", "内容", "act1", "开心")
        self.assertTrue(result["ok"])


# ============================================================
# 7. 私密日记列表不返回正文
# ============================================================

class Test07PrivateDiaryList(unittest.TestCase):
    @patch("home.repository.fetch_private_diaries")
    def test_list_no_content(self, mock_fetch):
        mock_fetch.return_value = [
            {"diary_key": "d1", "title": "今天", "mood": "开心", "created_at": "2026-08-18T10:00:00Z"}
        ]
        result = svc.list_private_diary()
        self.assertTrue(result["ok"])
        diary = result["data"]["diaries"][0]
        self.assertIn("title", diary)
        self.assertIn("mood", diary)
        self.assertNotIn("content", diary)

    @patch("home.repository.rpc_read_private_diary")
    def test_read_valid(self, mock_rpc):
        # 安全补丁：read_private_diary 需要 is_internal=True
        mock_rpc.return_value = {"ok": True, "title": "今天", "content": "正文", "mood": "开心"}
        result = svc.read_private_diary("d1", "act1", is_internal=True)
        self.assertTrue(result["ok"])
        self.assertIn("content", result)

    @patch("home.repository.rpc_archive_private_diary")
    def test_archive_valid(self, mock_rpc):
        # 安全补丁：archive_private_diary 需要 is_internal=True
        mock_rpc.return_value = {"ok": True}
        result = svc.archive_private_diary("d1", "act1", is_internal=True)
        self.assertTrue(result["ok"])


# ============================================================
# 8. 幂等测试
# ============================================================

class Test08Idempotency(unittest.TestCase):
    @patch("home.repository.rpc_write_letter")
    def test_duplicate_write(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.write_letter("ai_primary", "标题", "正文", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ACTION_EXISTS")

    @patch("home.repository.rpc_leave_note")
    def test_duplicate_note(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.leave_note("ai_primary", "living_room", "内容", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_write_private_diary")
    def test_duplicate_diary(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.write_private_diary("ai_primary", "标题", "正文", "act1")
        self.assertFalse(result["ok"])


# ============================================================
# 9. 安全：不泄露敏感信息
# ============================================================

class Test09Security(unittest.TestCase):
    @patch("home.repository.rpc_write_letter")
    def test_write_no_secrets(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "letter_key": "l1"}
        result = svc.write_letter("ai_primary", "标题", "正文", "act1")
        result_str = str(result)
        for kw in ("api_key", "token", "password", "secret", "cookie"):
            self.assertNotIn(kw, result_str.lower())

    @patch("home.repository.fetch_letters")
    def test_list_no_secrets(self, mock_fetch):
        mock_fetch.return_value = []
        result = svc.list_letters()
        result_str = str(result)
        for kw in ("api_key", "token", "password", "secret", "cookie"):
            self.assertNotIn(kw, result_str.lower())

    @patch("home.repository.fetch_private_diaries")
    def test_diary_list_no_content(self, mock_fetch):
        mock_fetch.return_value = [
            {"diary_key": "d1", "title": "秘密", "mood": "平静", "created_at": "2026-08-18"}
        ]
        result = svc.list_private_diary()
        result_str = str(result)
        # 确保列表返回中没有 content 字段
        self.assertNotIn("content", result_str)


# ============================================================
# 10. 数据库不可用降级
# ============================================================

class Test10DBUnavailable(unittest.TestCase):
    @patch("home.repository._get_supabase")
    def test_write_letter_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.write_letter("ai_primary", "标题", "正文", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DB_UNAVAILABLE")

    @patch("home.repository._get_supabase")
    def test_list_letters_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.list_letters()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 0)

    @patch("home.repository._get_supabase")
    def test_list_private_diary_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.list_private_diary()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
