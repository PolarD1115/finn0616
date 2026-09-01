# -*- coding: utf-8 -*-
"""
test_home_api_c6.py — 阶段 C6 专项测试。

覆盖：
- A. /api/home/state 聚合（安全投影、无内部 UUID、区块失败不伪造为空、鉴权）；
- B. /api/activity-logs（分页/过滤/白名单投影/无 id/activity_key/鉴权/DB 错误）；
- C. /api/secret-diaries 统一索引（新旧元数据、全局分页、无正文、鉴权）；
- D. /api/secret-diaries/:reference 受保护正文读取（legacy 只读 Secret_Diary、
     home 直查零副作用、可重复读取、无 ACTION_EXISTS、无 embedding、鉴权）；
- E. C4 回归锚点（隔离不变式：gateway.py 不引用新表；受保护读取不走读 RPC）；
- F. C3 回归锚点（API 读取对 activity_logs 只读，零写操作）；
- G. 前端静态检查（导航/页面/loader/API 路径/转义/无密钥/Node 语法检查）。

全部使用 mock/fake：不连接真实 Supabase、不调用真实模型、不发外部 HTTP、
不读取 credentials/token、不创建测试数据、零数据库写入。
"""

import asyncio
import io
import json
import os
import re
import unittest
from unittest.mock import patch

import gateway
from home import repository as repo
import home.activity_log as activity_log
import home.service as home_service

API_SECRET = "c6-test-secret"
UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"


# ============================================================
# Fake Supabase 客户端（记录调用链，可预设返回数据/计数/异常）
# ============================================================
class _FakeExec:
    def __init__(self, data=None, count=None):
        self.data = data if data is not None else []
        self.count = count


class _FakeQuery:
    def __init__(self, owner, table_name):
        self._owner = owner
        self._table = table_name
        self._ops = []
        self._count_kw = None

    def select(self, columns, count=None):
        self._ops.append(("select", columns))
        self._count_kw = count
        self._owner.selects.append((self._table, columns))
        return self

    def eq(self, k, v):
        self._ops.append(("eq", k, v)); return self

    def neq(self, k, v):
        self._ops.append(("neq", k, v)); return self

    def in_(self, k, v):
        self._ops.append(("in", k, v)); return self

    def gt(self, k, v):
        self._ops.append(("gt", k, v)); return self

    def order(self, k, desc=False):
        self._ops.append(("order", k, desc)); return self

    def limit(self, n):
        self._ops.append(("limit", n)); return self

    def range(self, a, b):
        self._ops.append(("range", a, b)); return self

    def offset(self, n):
        self._ops.append(("offset", n)); return self

    def insert(self, payload):
        self._ops.append(("insert", payload)); return self

    def update(self, payload):
        self._ops.append(("update", payload)); return self

    def delete(self):
        self._ops.append(("delete",)); return self

    def execute(self):
        if self._owner.raise_on_execute:
            raise RuntimeError("simulated db failure")
        self._owner.executed.append((self._table, list(self._ops)))
        count = self._owner.count if self._count_kw else None
        return _FakeExec(data=self._owner.data.get(self._table, []), count=count)


class FakeSupabase:
    """最小链式 fake：记录所有操作供断言；rpc 仅记录不执行。"""

    def __init__(self, data=None, count=None, raise_on_execute=False):
        self.data = data or {}
        self.count = count
        self.raise_on_execute = raise_on_execute
        self.executed = []
        self.selects = []
        self.rpc_calls = []

    def table(self, name):
        return _FakeQuery(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return _FakeQuery(self, "rpc:" + name)


def _src(*parts):
    here = os.path.dirname(os.path.abspath(__file__))
    with io.open(os.path.join(here, *parts), "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# A. Home 聚合视图（home_service.home_state_overview）
# ============================================================
class TestHomeStateOverview(unittest.TestCase):
    def _patch_repos(self, plants=None, events=None, notes=None, runs=None,
                     letters_ok=True):
        members_raw = [
            {"id": UUID_A, "stable_key": "finn", "name": "Finn", "member_type": "ai",
             "lifecycle_status": "alive", "is_active": True, "profile": {}},
            {"id": UUID_B, "stable_key": "pet_xiaoman", "name": "小满", "member_type": "pet",
             "lifecycle_status": "alive", "is_active": True,
             "profile": {"legacy_source": "pets", "legacy_id": "pet-legacy"}},
        ]
        states_raw = [
            {"member_id": UUID_A, "hunger": 40, "energy": 70, "mood": 66, "comfort": 60,
             "connection": 30, "intimacy": 30, "health": 90, "cleanliness": 70,
             "current_room_id": "room-uuid-1", "last_settled_at": None},
            {"member_id": UUID_B, "comfort": 55, "connection": 20, "intimacy": 10,
             "last_settled_at": None},
        ]
        rooms_raw = [
            {"id": "room-uuid-1", "stable_key": "living_room", "name": "客厅",
             "emoji": "🛋️", "room_type": "common", "description": "客厅",
             "sort_order": 1, "is_enabled": True, "is_hidden": False},
        ]
        pet = {"hunger": 30, "happiness": 80, "health": 95, "energy": 60,
               "cleanliness": 75, "status": "idle", "mood": "happy",
               "current_room": "living_room"}
        living_room = {"id": "room-uuid-1", "stable_key": "living_room", "name": "客厅",
                       "emoji": "🛋️", "room_type": "common", "description": "客厅",
                       "is_enabled": True, "is_hidden": False}
        long_summary = "s" * 300
        events_raw = events if events is not None else [
            {"event_type": "planted", "summary": long_summary, "visibility": "home",
             "room_id": "room-uuid-1", "actor_member_id": UUID_A,
             "target_member_id": UUID_B, "occurred_at": "2026-09-01T08:00:00+00:00"},
            {"event_type": "secret", "summary": "私密", "visibility": "private",
             "room_id": "room-uuid-1", "actor_member_id": UUID_A,
             "target_member_id": None, "occurred_at": "2026-09-01T09:00:00+00:00"},
            {"event_type": "system_thing", "summary": "系统", "visibility": "system",
             "room_id": "room-uuid-1", "actor_member_id": None,
             "target_member_id": None, "occurred_at": "2026-09-01T10:00:00+00:00"},
        ]
        patches = [
            patch("home.repository.fetch_rooms", return_value=rooms_raw),
            patch("home.repository.fetch_members", return_value=members_raw),
            patch("home.repository.fetch_member_states", return_value=states_raw),
            patch("home.repository.fetch_pet_by_member", return_value=pet),
            patch("home.repository.fetch_room_by_key",
                  side_effect=lambda key: living_room if key == "living_room" else None),
            patch("home.repository.fetch_plants",
                  return_value=plants if plants is not None else [
                      {"id": "plant-uuid", "name": "番茄", "seed_key": "tomato",
                       "stage": "mature", "health": 90, "water_level": 55,
                       "status": "active", "planted_at": "2026-08-30T00:00:00+00:00"}]),
            patch("home.repository.fetch_seed_catalog", return_value=[
                {"stable_key": "tomato", "name": "番茄种子", "emoji": "🌱"}]),
            patch("home.repository.fetch_inventory", return_value=[
                {"item_key": "番茄", "item_kind": "ingredient",
                 "storage_location": "pantry", "quantity": 3, "unit": "个"}]),
            patch("home.repository.fetch_dishes", return_value=[
                {"id": "dish-uuid", "name": "番茄沙拉", "emoji": "🥗",
                 "servings": 2, "quality": 70}]),
            patch("home.repository.fetch_recipe_catalog", return_value=[
                {"stable_key": "salad", "name": "沙拉", "emoji": "🥗"}]),
            patch("home.repository.fetch_letters", return_value=[
                {"letter_key": "lt1", "title": "信", "preview": "预览",
                 "status": "unopened", "created_at": "2026-09-01T00:00:00+00:00"}]),
            patch("home.repository.fetch_unopened_letter_count", return_value=1),
            patch("home.repository.fetch_recent_notes",
                  return_value=notes if notes is not None else [
                      {"note_key": "n1", "room_id": "room-uuid-1",
                       "preview": "买猫粮", "status": "active",
                       "created_at": "2026-09-01T00:00:00+00:00"}]),
            patch("home.repository.fetch_recent_events", return_value=events_raw),
            patch("home.repository.fetch_recent_action_runs",
                  return_value=runs if runs is not None else [
                      {"action_type": "cook_recipe", "status": "succeeded",
                       "error_code": None, "requested_at": "2026-09-01T07:00:00+00:00",
                       "finished_at": "2026-09-01T07:01:00+00:00"}]),
            patch("home.repository.fetch_pending_jobs", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a1_normal_projection_structure(self):
        self._patch_repos()
        r = home_service.home_state_overview()
        self.assertTrue(r["ok"])
        data = r["data"]
        for key in ("rooms", "members", "plants", "seeds", "kitchen", "letters",
                    "notes", "recent_events", "recent_runs", "counts", "errors"):
            self.assertIn(key, data)
        self.assertEqual(data["errors"], {})

    def test_a2_no_internal_uuid_leak(self):
        self._patch_repos()
        blob = json.dumps(home_service.home_state_overview(), ensure_ascii=False, default=str)
        for secret in (UUID_A, UUID_B, "room-uuid-1", "plant-uuid", "dish-uuid",
                       "current_room_id", "actor_member_id", "room_id"):
            self.assertNotIn(secret, blob)

    def test_a3_no_private_diary_or_letter_note_fulltext(self):
        self._patch_repos()
        blob = json.dumps(home_service.home_state_overview(), ensure_ascii=False, default=str)
        self.assertNotIn("home_private_diaries", blob)
        self.assertNotIn("Secret_Diary", blob)
        # 便利贴只有预览，无全文键
        notes = home_service.home_state_overview()["data"]["notes"]
        for n in notes:
            self.assertNotIn("content", n)
            self.assertLessEqual(len(n["preview"]), 60)

    def test_a4_events_private_and_system_filtered_and_summary_truncated(self):
        self._patch_repos()
        events = home_service.home_state_overview()["data"]["recent_events"]
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["event_type"], "planted")
        self.assertLessEqual(len(ev["summary"]), 200)
        self.assertEqual(ev["actor_name"], "Finn")
        self.assertEqual(ev["target_name"], "小满")
        self.assertEqual(ev["room_name"], "客厅")

    def test_a5_member_view_pets_authoritative_and_room_name(self):
        self._patch_repos()
        members = home_service.home_state_overview()["data"]["members"]
        by_key = {m["stable_key"]: m for m in members}
        pet = by_key["pet_xiaoman"]
        self.assertEqual(pet["state"]["physiology_source"], "pets")
        self.assertEqual(pet["current_room_name"], "客厅")
        ai = by_key["finn"]
        self.assertNotIn("current_room_id", ai["state"])
        self.assertEqual(ai["current_room_name"], "客厅")

    def test_a6_single_block_failure_not_fabricated_empty(self):
        def _boom():
            raise RuntimeError("boom")
        self._patch_repos()
        with patch("home.repository.fetch_plants", side_effect=_boom):
            r = home_service.home_state_overview()
        self.assertTrue(r["ok"])
        data = r["data"]
        self.assertIsNone(data["plants"])
        self.assertEqual(data["errors"].get("garden"), "UNAVAILABLE")
        # 其他区块照常
        self.assertTrue(data["members"])
        self.assertTrue(data["kitchen"])

    def test_a7_letters_block_failure_marks_error(self):
        def _boom():
            raise RuntimeError("boom")
        self._patch_repos()
        with patch("home.repository.fetch_letters", side_effect=_boom):
            r = home_service.home_state_overview()
        self.assertIsNone(r["data"]["letters"])
        self.assertEqual(r["data"]["errors"].get("letters"), "UNAVAILABLE")

    def test_a8_garden_has_no_settle_side_effect(self):
        """聚合视图不得触发 rpc_home_settle_plants（GET 零写副作用）。"""
        self._patch_repos()
        with patch("home.repository.fetch_plants_settled",
                   side_effect=AssertionError("不应触发结算 RPC")):
            with patch("home.repository._get_supabase_service", return_value=FakeSupabase()):
                r = home_service.home_state_overview()
        self.assertTrue(r["ok"])

    def test_a9_counts_block(self):
        self._patch_repos()
        counts = home_service.home_state_overview()["data"]["counts"]
        self.assertEqual(counts["member_count"], 2)
        self.assertEqual(counts["room_count"], 1)
        self.assertEqual(counts["plant_count"], 1)
        self.assertEqual(counts["dish_count"], 1)
        self.assertEqual(counts["unread_letter_count"], 1)
        self.assertEqual(counts["note_count"], 1)


# ============================================================
# A2. /api/home/state 网关 handler（ASGI 假收发）
# ============================================================
async def _fail_downstream(scope, receive, send):
    raise AssertionError("downstream app should not be called")


def _scope(path, method="GET", query=b"", with_secret=True):
    headers = [(b"host", b"localhost")]
    if with_secret:
        headers.append((b"x-api-key", API_SECRET.encode()))
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode("utf-8"), "query_string": query,
        "headers": headers, "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 80),
    }


class _SendRecorder:
    def __init__(self):
        self.started = None
        self.body = b""

    async def __call__(self, msg):
        if msg["type"] == "http.response.start":
            self.started = msg
        elif msg["type"] == "http.response.body":
            self.body += msg.get("body", b"")

    @property
    def status(self):
        return self.started["status"] if self.started else None

    def json(self):
        return json.loads(self.body.decode("utf-8")) if self.body else {}


async def _receive_empty():
    return {"type": "http.request", "body": b"", "more_body": False}


def _dispatch(path, method="GET", query=b"", with_secret=True):
    mw = gateway.HostFixMiddleware(_fail_downstream)
    recorder = _SendRecorder()
    asyncio.run(mw(_scope(path, method=method, query=query, with_secret=with_secret),
                   _receive_empty, recorder))
    return recorder


class TestGatewayAuthAndDispatch(unittest.TestCase):
    """鉴权与分发：新路由全部受全局 API_SECRET 拦截保护。"""

    NEW_PATHS = ["/api/home/state", "/api/activity-logs",
                 "/api/secret-diaries", "/api/secret-diaries/legacy%3A1"]

    def test_auth_missing_secret_503(self):
        env_without = {k: v for k, v in os.environ.items() if k != "API_SECRET"}
        with patch.object(gateway.os, "environ", env_without):
            for p in self.NEW_PATHS:
                rec = _dispatch(p)
                self.assertEqual(rec.status, 503, p)

    def test_auth_wrong_key_401_and_downstream_not_called(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            mw = gateway.HostFixMiddleware(_fail_downstream)
            rec = _SendRecorder()
            scope = _scope("/api/home/state", with_secret=False)
            scope["headers"] = [(b"host", b"localhost"), (b"x-api-key", b"wrong")]
            asyncio.run(mw(scope, _receive_empty, rec))
            self.assertEqual(rec.status, 401)

    def test_auth_missing_key_401(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            for p in self.NEW_PATHS:
                rec = _dispatch(p, with_secret=False)
                self.assertEqual(rec.status, 401, p)

    def test_options_preflight_204(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            rec = _dispatch("/api/home/state", method="OPTIONS", with_secret=False)
            self.assertEqual(rec.status, 204)


class TestHomeStateApi(unittest.TestCase):
    def test_ok_response(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with patch("home.service.home_state_overview",
                       return_value={"ok": True, "data": {"rooms": [1], "members": []}}):
                rec = _dispatch("/api/home/state")
        self.assertEqual(rec.status, 200)
        body = rec.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["rooms"], [1])

    def test_service_error_result_500(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with patch("home.service.home_state_overview",
                       return_value={"ok": False, "error_code": "X", "message": "m"}):
                rec = _dispatch("/api/home/state")
        self.assertEqual(rec.status, 500)
        self.assertFalse(rec.json()["ok"])

    def test_service_exception_500_sanitized(self):
        def _boom():
            raise RuntimeError("db password leaked")
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with patch("home.service.home_state_overview", side_effect=_boom):
                rec = _dispatch("/api/home/state")
        self.assertEqual(rec.status, 500)
        body = rec.json()
        self.assertEqual(body["error_code"], "INTERNAL_ERROR")
        self.assertNotIn("db password", json.dumps(body))

    def test_post_405(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            rec = _dispatch("/api/home/state", method="POST")
        self.assertEqual(rec.status, 405)


# ============================================================
# B. /api/activity-logs
# ============================================================
class TestActivityLogsQuery(unittest.TestCase):
    """home.activity_log.query_activity_logs 单元测试（fake 客户端）。"""

    def _rows(self):
        return [{
            "id": "row-uuid", "activity_key": "act-key-1", "activity_id": "浏览花园",
            "activity_name": "去花园看了看", "source": "home_autonomy",
            "status": "succeeded", "thought_summary": "想看看植物",
            "result_summary": "看了一眼", "started_at": "2026-09-01T01:00:00+00:00",
            "finished_at": "2026-09-01T01:05:00+00:00",
            "tools_used": [{"name": "garden_observe", "ok": True,
                            "status": "succeeded", "error_code": "",
                            "params": {"secret": "x"}, "raw": "should-drop"}],
        }]

    def _run(self, fake_sb, **kw):
        with patch.object(activity_log, "_get_service_client", return_value=fake_sb):
            return activity_log.query_activity_logs(**kw)

    def test_b1_whitelist_projection_no_id_no_activity_key_no_tool_params(self):
        sb = FakeSupabase(data={"activity_logs": self._rows()}, count=1)
        r = self._run(sb, page=1, size=20)
        self.assertTrue(r["ok"])
        item = r["items"][0]
        self.assertNotIn("id", item)
        self.assertNotIn("activity_key", item)
        self.assertEqual(item["activity_id"], "浏览花园")
        tool = item["tools_used"][0]
        self.assertEqual(set(tool.keys()), {"name", "ok", "status", "error_code"})
        self.assertNotIn("params", tool)
        self.assertNotIn("raw", tool)

    def test_b2_paging_order_and_filters(self):
        sb = FakeSupabase(data={"activity_logs": []}, count=0)
        r = self._run(sb, page=3, size=10, source="home_autonomy",
                      status="failed", activity_id="浇水")
        self.assertTrue(r["ok"])
        table, ops = sb.executed[0]
        self.assertEqual(table, "activity_logs")
        self.assertIn(("order", "started_at", True), ops)
        self.assertIn(("range", 20, 29), ops)
        self.assertIn(("eq", "source", "home_autonomy"), ops)
        self.assertIn(("eq", "status", "failed"), ops)
        self.assertIn(("eq", "activity_id", "浇水"), ops)
        self.assertEqual(r["page"], 3)
        self.assertEqual(r["size"], 10)

    def test_b3_total_and_has_more(self):
        sb = FakeSupabase(data={"activity_logs": self._rows()}, count=25)
        r = self._run(sb, page=1, size=20)
        self.assertEqual(r["total"], 25)
        self.assertTrue(r["has_more"])
        sb2 = FakeSupabase(data={"activity_logs": self._rows()}, count=20)
        r2 = self._run(sb2, page=1, size=20)
        self.assertFalse(r2["has_more"])

    def test_b4_invalid_params(self):
        for kw, code in (({"page": 0}, "INVALID_PAGE"),
                         ({"page": "x"}, "INVALID_PAGE"),
                         ({"size": 0}, "INVALID_SIZE"),
                         ({"size": 101}, "INVALID_SIZE"),
                         ({"source": "hacker"}, "INVALID_SOURCE"),
                         ({"status": "nope"}, "INVALID_STATUS")):
            r = self._run(FakeSupabase(), **dict({"page": 1, "size": 20}, **kw))
            self.assertFalse(r["ok"], kw)
            self.assertEqual(r["error_code"], code, kw)

    def test_b5_service_key_missing_and_db_error(self):
        with patch.object(activity_log, "_get_service_client", return_value=None):
            r = activity_log.query_activity_logs(page=1, size=20)
        self.assertEqual(r["error_code"], "SERVICE_KEY_MISSING")
        sb = FakeSupabase(raise_on_execute=True)
        r2 = self._run(sb, page=1, size=20)
        self.assertEqual(r2["error_code"], "DB_ERROR")

    def test_b6_read_only_no_insert_update_delete(self):
        sb = FakeSupabase(data={"activity_logs": self._rows()}, count=1)
        self._run(sb, page=1, size=20)
        for _table, ops in sb.executed:
            for op in ops:
                self.assertNotIn(op[0], ("insert", "update", "delete"))


class TestActivityLogsApi(unittest.TestCase):
    def _patch_query(self, result, captured=None):
        def fake(page, size, source, status, activity_id):
            if captured is not None:
                captured.update({"page": page, "size": size, "source": source,
                                 "status": status, "activity_id": activity_id})
            return result
        return patch("home.activity_log.query_activity_logs", side_effect=fake)

    def test_ok_with_filters(self):
        captured = {}
        result = {"ok": True, "items": [{"activity_name": "x", "tools_used": []}],
                  "total": 1, "page": 2, "size": 5, "has_more": False}
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with self._patch_query(result, captured):
                rec = _dispatch("/api/activity-logs",
                                query=b"page=2&size=5&source=home_autonomy&status=failed&activity_id=%E6%B5%87%E6%B0%B4")
        self.assertEqual(rec.status, 200)
        body = rec.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["total"], 1)
        self.assertTrue(body["has_more"] is False)
        self.assertEqual(captured, {"page": 2, "size": 5, "source": "home_autonomy",
                                    "status": "failed", "activity_id": "浇水"})

    def test_invalid_page_400(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            rec = _dispatch("/api/activity-logs", query=b"page=abc")
        self.assertEqual(rec.status, 400)
        self.assertEqual(rec.json()["error_code"], "INVALID_REQUEST")

    def test_invalid_source_400(self):
        result = {"ok": False, "error_code": "INVALID_SOURCE"}
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with self._patch_query(result):
                rec = _dispatch("/api/activity-logs", query=b"source=nope")
        self.assertEqual(rec.status, 400)

    def test_db_error_500_sanitized(self):
        result = {"ok": False, "error_code": "DB_ERROR"}
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with self._patch_query(result):
                rec = _dispatch("/api/activity-logs")
        self.assertEqual(rec.status, 500)
        self.assertEqual(rec.json()["error_code"], "DB_ERROR")

    def test_post_405(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            rec = _dispatch("/api/activity-logs", method="POST")
        self.assertEqual(rec.status, 405)


# ============================================================
# C. /api/secret-diaries 统一索引
# ============================================================
class TestSecretDiaryIndexService(unittest.TestCase):
    def test_c1_service_role_param_routes_to_service_fetch(self):
        legacy = [{"id": 3, "title": "旧日记", "mood": "平静",
                   "created_at": "2026-08-01T00:00:00+00:00"}]
        home_rows = [{"diary_key": "d1", "title": "新日记", "mood": "开心",
                      "status": "active", "created_at": "2026-09-01T00:00:00+00:00"}]
        with patch("home.repository.fetch_legacy_secret_diaries", return_value=legacy), \
             patch("home.repository.count_legacy_secret_diaries", return_value=1), \
             patch("home.repository.fetch_private_diaries_service",
                   return_value=home_rows) as m_svc, \
             patch("home.repository.fetch_private_diaries",
                   side_effect=AssertionError("不应走 anon 查询")):
            r = home_service.list_private_diary_index(limit=20, offset=0,
                                                      use_service_role=True)
        self.assertTrue(r["ok"])
        m_svc.assert_called_once()
        data = r["data"]
        self.assertEqual(data["total"], 2)
        refs = [it["reference"] for it in data["items"]]
        self.assertIn("legacy:3", refs)
        self.assertIn("home:d1", refs)
        # 全局时间倒序：新日记在前
        self.assertEqual(data["items"][0]["reference"], "home:d1")

    def test_c2_default_param_keeps_legacy_behavior(self):
        with patch("home.repository.fetch_legacy_secret_diaries", return_value=[]), \
             patch("home.repository.count_legacy_secret_diaries", return_value=0), \
             patch("home.repository.fetch_private_diaries", return_value=[]) as m_anon:
            r = home_service.list_private_diary_index(limit=10, offset=0)
        self.assertTrue(r["ok"])
        m_anon.assert_called_once()

    def test_c3_items_never_contain_content(self):
        legacy = [{"id": 3, "title": "旧日记", "mood": "平静",
                   "created_at": "2026-08-01T00:00:00+00:00"}]
        home_rows = [{"diary_key": "d1", "title": "新日记", "mood": "开心",
                      "status": "active", "created_at": "2026-09-01T00:00:00+00:00"}]
        with patch("home.repository.fetch_legacy_secret_diaries", return_value=legacy), \
             patch("home.repository.count_legacy_secret_diaries", return_value=1), \
             patch("home.repository.fetch_private_diaries_service", return_value=home_rows):
            r = home_service.list_private_diary_index(limit=20, offset=0,
                                                      use_service_role=True)
        for it in r["data"]["items"]:
            self.assertNotIn("content", it)
            self.assertNotIn("embedding", it)
            self.assertNotIn("diary_key", it)
            self.assertNotIn("action_key", it)


class TestSecretDiariesApi(unittest.TestCase):
    def _patch_index(self, captured=None):
        def fake(limit, offset, use_service_role):
            if captured is not None:
                captured.update({"limit": limit, "offset": offset,
                                 "use_service_role": use_service_role})
            return {"ok": True, "data": {
                "items": [{"reference": "legacy:3", "source": "legacy", "title": "t",
                           "mood": "m", "created_at": "2026-08-01T00:00:00+00:00",
                           "status": "active", "is_archived": False}],
                "legacy_count": 1, "home_count": 0, "total": 1, "has_more": False}}
        return patch("home.service.list_private_diary_index", side_effect=fake)

    def test_ok_and_paging_params(self):
        captured = {}
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with self._patch_index(captured):
                rec = _dispatch("/api/secret-diaries", query=b"page=2&size=10")
        self.assertEqual(rec.status, 200)
        body = rec.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["page"], 2)
        self.assertEqual(body["size"], 10)
        self.assertEqual(body["total"], 1)
        self.assertEqual(captured, {"limit": 10, "offset": 10, "use_service_role": True})
        self.assertNotIn("content", json.dumps(body))

    def test_size_clamped(self):
        captured = {}
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with self._patch_index(captured):
                rec = _dispatch("/api/secret-diaries", query=b"page=1&size=500")
        self.assertEqual(rec.status, 200)
        self.assertEqual(captured["limit"], 100)

    def test_invalid_page_400(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            rec = _dispatch("/api/secret-diaries", query=b"page=x")
        self.assertEqual(rec.status, 400)

    def test_service_exception_500(self):
        def _boom(limit, offset, use_service_role):
            raise RuntimeError("boom")
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with patch("home.service.list_private_diary_index", side_effect=_boom):
                rec = _dispatch("/api/secret-diaries")
        self.assertEqual(rec.status, 500)

    def test_post_405(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            rec = _dispatch("/api/secret-diaries", method="POST")
        self.assertEqual(rec.status, 405)


# ============================================================
# D. 秘密日记受保护正文读取
# ============================================================
class TestFetchPrivateDiaryByReference(unittest.TestCase):
    """home.repository.fetch_private_diary_by_reference（fake 客户端）。"""

    def _legacy_sb(self, rows):
        return FakeSupabase(data={"memories": rows})

    def test_d1_legacy_reads_only_secret_diary_tag_with_int_id(self):
        rows = [{"id": 5, "title": "旧", "content": "正文", "mood": "平静",
                 "created_at": "2026-08-01T00:00:00+00:00"}]
        sb = self._legacy_sb(rows)
        with patch.object(repo, "_get_supabase_service", return_value=sb):
            item = repo.fetch_private_diary_by_reference("legacy:5")
        self.assertEqual(item["reference"], "legacy:5")
        self.assertEqual(item["source"], "legacy")
        self.assertEqual(item["content"], "正文")
        table, ops = sb.executed[0]
        self.assertEqual(table, "memories")
        self.assertIn(("eq", "tags", "Secret_Diary"), ops)
        self.assertIn(("eq", "id", 5), ops)
        sel_cols = sb.selects[0][1]
        self.assertIn("content", sel_cols)
        self.assertNotIn("embedding", sel_cols)

    def test_d2_legacy_plain_memory_not_readable(self):
        sb = self._legacy_sb([])
        with patch.object(repo, "_get_supabase_service", return_value=sb):
            item = repo.fetch_private_diary_by_reference("legacy:999")
        self.assertIsNone(item)
        table, ops = sb.executed[0]
        self.assertIn(("eq", "tags", "Secret_Diary"), ops)

    def test_d3_home_direct_select_by_diary_key(self):
        rows = [{"id": UUID_A, "diary_key": "d1", "title": "新", "content": "正文",
                 "mood": "开心", "status": "active",
                 "created_at": "2026-09-01T00:00:00+00:00"}]
        sb = FakeSupabase(data={"home_private_diaries": rows})
        with patch.object(repo, "_get_supabase_service", return_value=sb):
            item = repo.fetch_private_diary_by_reference("home:d1")
        self.assertEqual(item["reference"], "home:d1")
        self.assertEqual(item["source"], "home")
        self.assertEqual(item["content"], "正文")
        table, ops = sb.executed[0]
        self.assertEqual(table, "home_private_diaries")
        self.assertIn(("eq", "diary_key", "d1"), ops)
        self.assertNotIn("embedding", sb.selects[0][1])

    def test_d4_invalid_references_raise_value_error(self):
        for ref in ("no-colon", "", "unknown:1", "legacy:abc", "legacy:0",
                    "legacy:-2", "legacy:", "home:", "legacy:" + "k" * 301,
                    None, 123):
            with patch.object(repo, "_get_supabase_service", return_value=FakeSupabase()):
                with self.assertRaises(ValueError, msg=repr(ref)):
                    repo.fetch_private_diary_by_reference(ref)

    def test_d5_service_key_missing_raises_runtime_error(self):
        with patch.object(repo, "_get_supabase_service", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                repo.fetch_private_diary_by_reference("legacy:1")
        self.assertIn("SERVICE_KEY_MISSING", str(ctx.exception))

    def test_d6_no_rpc_and_no_action_runs(self):
        sb = self._legacy_sb([])
        with patch.object(repo, "_get_supabase_service", return_value=sb):
            repo.fetch_private_diary_by_reference("legacy:1")
        self.assertEqual(sb.rpc_calls, [])
        # 从未触碰 home_action_runs 表
        self.assertTrue(all(t != "home_action_runs" for t, _ in sb.executed))


class TestReadPrivateDiaryBodyService(unittest.TestCase):
    def test_d7_ok_returns_content_without_logging(self):
        item = {"reference": "home:d1", "source": "home", "title": "t",
                "content": "正文", "mood": "m", "created_at": None}
        with patch("home.repository.fetch_private_diary_by_reference",
                   return_value=item) as m_fetch, \
             patch("home.repository.rpc_read_private_diary",
                   side_effect=AssertionError("不得走读 RPC")):
            r = home_service.read_private_diary_body("home:d1")
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["content"], "正文")
        m_fetch.assert_called_once_with("home:d1")

    def test_d8_error_mapping(self):
        cases = [
            (ValueError("INVALID_REFERENCE"), "INVALID_REFERENCE"),
            (RuntimeError("SERVICE_KEY_MISSING"), "SERVICE_KEY_MISSING"),
            (RuntimeError("other"), "DB_ERROR"),
            (Exception("db"), "DB_ERROR"),
        ]
        for exc, code in cases:
            with patch("home.repository.fetch_private_diary_by_reference", side_effect=exc):
                r = home_service.read_private_diary_body("legacy:1")
            self.assertFalse(r["ok"], code)
            self.assertEqual(r["error_code"], code, code)

    def test_d9_not_found_undistinguishable(self):
        with patch("home.repository.fetch_private_diary_by_reference", return_value=None):
            r = home_service.read_private_diary_body("legacy:404")
        self.assertEqual(r["error_code"], "NOT_FOUND_OR_FORBIDDEN")

    def test_d10_repeated_reads_all_succeed(self):
        """多次读取同一 home 日记均成功（不产生 home_action_runs / ACTION_EXISTS）。"""
        item = {"reference": "home:d1", "source": "home", "title": "t",
                "content": "正文", "mood": "m", "created_at": None}
        with patch("home.repository.fetch_private_diary_by_reference",
                   return_value=dict(item)) as m_fetch, \
             patch("home.repository.rpc_read_private_diary",
                   side_effect=AssertionError("不得走读 RPC")):
            for _ in range(3):
                r = home_service.read_private_diary_body("home:d1")
                self.assertTrue(r["ok"])
        self.assertEqual(m_fetch.call_count, 3)


class TestSecretDiaryBodyApi(unittest.TestCase):
    def _patch_body(self, result, captured=None, exc=None):
        def fake(ref):
            if captured is not None:
                captured["ref"] = ref
            if exc is not None:
                raise exc
            return result
        return patch("home.service.read_private_diary_body", side_effect=fake)

    def test_ok_with_unquoted_reference(self):
        captured = {}
        result = {"ok": True, "data": {"reference": "legacy:123", "source": "legacy",
                                       "title": "t", "content": "c", "mood": "m",
                                       "created_at": None}}
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with self._patch_body(result, captured):
                rec = _dispatch("/api/secret-diaries/legacy%3A123")
        self.assertEqual(rec.status, 200)
        body = rec.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["item"]["content"], "c")
        self.assertEqual(captured["ref"], "legacy:123")
        self.assertNotIn("embedding", json.dumps(body))

    def test_not_found_404(self):
        result = {"ok": False, "error_code": "NOT_FOUND_OR_FORBIDDEN"}
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with self._patch_body(result):
                rec = _dispatch("/api/secret-diaries/legacy%3A999")
        self.assertEqual(rec.status, 404)

    def test_invalid_reference_400(self):
        result = {"ok": False, "error_code": "INVALID_REFERENCE"}
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with self._patch_body(result):
                rec = _dispatch("/api/secret-diaries/bad")
        self.assertEqual(rec.status, 400)

    def test_db_error_500_sanitized(self):
        result = {"ok": False, "error_code": "DB_ERROR"}
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with self._patch_body(result):
                rec = _dispatch("/api/secret-diaries/home%3Ad1")
        self.assertEqual(rec.status, 500)
        self.assertNotIn("Exception", rec.json().get("message", ""))

    def test_exception_500(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with self._patch_body(None, exc=RuntimeError("boom")):
                rec = _dispatch("/api/secret-diaries/home%3Ad1")
        self.assertEqual(rec.status, 500)

    def test_repeated_reads_ok(self):
        result = {"ok": True, "data": {"reference": "home:d1", "source": "home",
                                       "title": "t", "content": "c", "mood": "m",
                                       "created_at": None}}
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            with self._patch_body(result):
                for _ in range(2):
                    rec = _dispatch("/api/secret-diaries/home%3Ad1")
                    self.assertEqual(rec.status, 200)

    def test_post_405(self):
        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            rec = _dispatch("/api/secret-diaries/legacy%3A1", method="POST")
        self.assertEqual(rec.status, 405)


# ============================================================
# E/F. C4 / C3 回归锚点（静态 + 行为）
# ============================================================
class TestIsolationAndReadOnlyAnchors(unittest.TestCase):
    def test_e1_gateway_still_isolated_from_new_table(self):
        """C4 静态不变式保持：gateway.py / server.py 不引用新私密日记表。"""
        self.assertNotIn("home_private_diaries", _src("gateway.py"))
        self.assertNotIn("home_private_diaries", _src("server.py"))

    def test_e2_gateway_new_routes_registered(self):
        src = _src("gateway.py")
        for route in ('"/api/home/state"', '"/api/activity-logs"',
                      '"/api/secret-diaries"', '"/api/secret-diaries/"'):
            self.assertIn(route, src)

    def test_e3_protected_body_read_never_calls_read_rpc(self):
        with patch("home.repository.rpc_read_private_diary",
                   side_effect=AssertionError("C6 受保护读取不得走读 RPC")), \
             patch("home.repository.fetch_private_diary_by_reference",
                   return_value=None):
            r = home_service.read_private_diary_body("home:d1")
        self.assertEqual(r["error_code"], "NOT_FOUND_OR_FORBIDDEN")

    def test_f1_activity_logs_read_is_select_only(self):
        sb = FakeSupabase(data={"activity_logs": []}, count=0)
        with patch.object(activity_log, "_get_service_client", return_value=sb):
            activity_log.query_activity_logs(page=1, size=20)
        for _table, ops in sb.executed:
            for op in ops:
                self.assertNotIn(op[0], ("insert", "update", "delete"))


# ============================================================
# G. 前端静态检查
# ============================================================
def _extract_inline_scripts(html):
    blocks = []
    for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S):
        blocks.append(m.group(1))
    return blocks


class TestFrontendStatic(unittest.TestCase):
    def _html(self, name):
        return _src(name)

    def test_g1_nav_and_pages_present_both_frontends(self):
        for name in ("console.html", "miniapp.html"):
            html = self._html(name)
            self.assertIn('data-p="home"', html, name)
            self.assertIn('data-p="logs"', html, name)
            self.assertIn('data-p="diary"', html, name)
            self.assertIn('id="p-home"', html, name)
            self.assertIn('id="p-logs"', html, name)
            self.assertIn('id="p-diary"', html, name)
            self.assertIn("home:loadHome", html, name)
            self.assertIn("logs:loadLogs", html, name)
            self.assertIn("home:\"家\"", html, name)
            self.assertIn("logs:\"行动日志\"", html, name)

    def test_g2_secret_diary_uses_new_api_only(self):
        for name in ("console.html", "miniapp.html"):
            html = self._html(name)
            self.assertIn("/api/secret-diaries", html, name)
            self.assertNotIn("category=secret_diary", html, name)
            self.assertNotIn("openDiaryModal", html, name)
            self.assertNotIn("delDiary(", html, name)

    def test_g3_home_page_sections_present(self):
        for name in ("console.html", "miniapp.html"):
            html = self._html(name)
            for el in ("homeMembers", "homeRooms", "homePlants", "homeInventory",
                       "homeDishes", "homeRecipes", "homeLetters", "homeNotes",
                       "homeTimeline", "homeStats"):
                self.assertIn(el, html, name)

    def test_g4_logs_page_sections_and_filters(self):
        for name in ("console.html", "miniapp.html"):
            html = self._html(name)
            for el in ("logsList", "logsPager", "logsSource", "logsStatus",
                       "logToolChip", "thought_summary", "result_summary",
                       "tools_used"):
                self.assertIn(el, html, name)

    def test_g5_no_service_role_or_hardcoded_secret(self):
        for name in ("console.html", "miniapp.html"):
            html = self._html(name)
            self.assertNotIn("service_role", html, name)
            self.assertNotIn("SUPABASE_SERVICE_KEY", html, name)
            # API_SECRET 只能来自输入框/localStorage，不允许硬编码字面量
            self.assertIsNone(re.search(r"API_SECRET[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_\-]{8,}",
                                        html), name)
            self.assertIn('"X-Api-Key":getSecret()', html, name)

    def test_g6_content_cleared_on_modal_close(self):
        for name in ("console.html", "miniapp.html"):
            html = self._html(name)
            self.assertIn(
                'function closeModal(){document.getElementById("modalRoot").innerHTML="";}',
                html, name)

    def test_g7_no_content_preload_or_localstorage_of_diary(self):
        for name in ("console.html", "miniapp.html"):
            html = self._html(name)
            # 列表接口不得渲染正文（loadDiary 内不得出现 .content）
            load_diary = html.split("async function loadDiary")[1].split("async function readDiary")[0]
            self.assertNotIn(".content", load_diary, name)
            # 正文只经 readDiary 的受保护接口读取
            read_diary = html.split("async function readDiary")[1].split("/* ============ 6.")[0]
            self.assertIn("/api/secret-diaries/", read_diary, name)
            self.assertNotIn("localStorage.setItem(\"diary", read_diary, name)

    def test_g8_dynamic_text_escaped_in_new_loaders(self):
        for name in ("console.html", "miniapp.html"):
            html = self._html(name)
            for fn in ("loadHome", "loadLogs", "loadDiary", "readDiary"):
                body = html.split("function " + fn)[1]
                body = body.split("\nfunction ")[0] if "\nfunction " in body else body[:4000]
                self.assertIn("esc(", body, name + ":" + fn)

    def test_g9_node_syntax_check_when_available(self):
        import shutil
        import subprocess
        import tempfile
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js 不可用，跳过 JS 语法检查（HTML 结构检查已覆盖）")
        for name in ("console.html", "miniapp.html"):
            scripts = "\n;\n".join(_extract_inline_scripts(self._html(name)))
            self.assertTrue(scripts.strip(), name)
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as f:
                f.write(scripts)
                tmp = f.name
            try:
                proc = subprocess.run([node, "--check", tmp], capture_output=True,
                                      text=True, timeout=60)
                self.assertEqual(proc.returncode, 0,
                                 name + " JS 语法错误:\n" + proc.stderr[-2000:])
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
