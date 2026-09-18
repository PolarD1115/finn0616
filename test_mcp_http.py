"""MCP HTTP 传输：SSE 保活、会话丢失改写、Streamable HTTP 挂载。"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from mcp_http import (
    SESSION_MISS_HTTP,
    SSE_PING_SECONDS,
    SessionMissRewriter,
    build_mcp_http_app,
    install_sse_keepalive,
)
import gateway


API_SECRET = "test-secret-mcp-http"


class _SendRecorder:
    def __init__(self):
        self.status = None
        self.headers = []
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = list(message.get("headers") or [])
        elif message["type"] == "http.response.body":
            self.body += message.get("body") or b""

    def json(self):
        return json.loads(self.body.decode("utf-8")) if self.body else {}


def _scope(path, method="POST", query=b"", body_headers=None):
    headers = [(b"host", b"localhost"), (b"content-type", b"application/json")]
    if body_headers:
        headers.extend(body_headers)
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query,
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }


class _FakeInner:
    def __init__(self, status, body, more_body=False):
        self.status = status
        self.body = body
        self.more_body = more_body
        self.seen_body = b""

    async def __call__(self, scope, receive, send):
        while True:
            msg = await receive()
            if msg.get("type") == "http.request":
                self.seen_body += msg.get("body") or b""
                if not msg.get("more_body"):
                    break
        await send({
            "type": "http.response.start",
            "status": self.status,
            "headers": [(b"content-type", b"text/plain")],
        })
        if self.more_body:
            await send({"type": "http.response.body", "body": self.body[:3], "more_body": True})
            await send({"type": "http.response.body", "body": self.body[3:], "more_body": False})
        else:
            await send({"type": "http.response.body", "body": self.body})


def _receive_body(raw: bytes):
    async def _receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    return _receive


class TestSessionMissRewriter(unittest.IsolatedAsyncioTestCase):
    async def test_rewrites_could_not_find_session(self):
        inner = _FakeInner(404, b"Could not find session")
        app = SessionMissRewriter(inner)
        rec = _SendRecorder()
        payload = b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{}}'
        await app(
            _scope("/messages/", query=b"session_id=abcd1234"),
            _receive_body(payload),
            rec,
        )
        self.assertEqual(rec.status, SESSION_MISS_HTTP)
        body = rec.json()
        self.assertEqual(body["id"], 7)
        self.assertEqual(body["error"]["code"], -32001)
        self.assertEqual(body["error"]["data"]["code"], "mcp_session_not_found")
        self.assertEqual(body["error"]["data"]["reconnect"], "/sse")
        self.assertEqual(body["error"]["data"]["session_id"], "abcd1234")
        self.assertIn("重新打开", body["error"]["data"]["hint"])

    async def test_leaves_accepted_unchanged(self):
        inner = _FakeInner(202, b"Accepted")
        app = SessionMissRewriter(inner)
        rec = _SendRecorder()
        await app(_scope("/messages/"), _receive_body(b"{}"), rec)
        self.assertEqual(rec.status, 202)
        self.assertEqual(rec.body, b"Accepted")

    async def test_leaves_other_404_unchanged(self):
        inner = _FakeInner(404, b"Not Found")
        app = SessionMissRewriter(inner)
        rec = _SendRecorder()
        await app(_scope("/messages/"), _receive_body(b"{}"), rec)
        self.assertEqual(rec.status, 404)
        self.assertEqual(rec.body, b"Not Found")

    async def test_rewrites_chunked_session_miss(self):
        inner = _FakeInner(404, b"Could not find session", more_body=True)
        app = SessionMissRewriter(inner)
        rec = _SendRecorder()
        await app(_scope("/messages/"), _receive_body(b"{}"), rec)
        self.assertEqual(rec.status, SESSION_MISS_HTTP)
        self.assertEqual(rec.json()["error"]["data"]["code"], "mcp_session_not_found")


class TestSseKeepalive(unittest.TestCase):
    def test_event_source_ping_forced(self):
        self.assertTrue(install_sse_keepalive())
        from sse_starlette.sse import EventSourceResponse

        async def _gen():
            if False:
                yield {}

        resp = EventSourceResponse(_gen())
        self.assertEqual(resp.ping_interval, SSE_PING_SECONDS)
        self.assertIsNotNone(resp.ping_message_factory)
        ping = resp.ping_message_factory()
        self.assertTrue(hasattr(ping, "comment") or "keepalive" in str(ping).lower())


class TestBuildMcpHttpApp(unittest.TestCase):
    def test_combined_routes_include_sse_and_mcp(self):
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("route-probe")
        app, desc = build_mcp_http_app(mcp)
        paths = [getattr(r, "path", "") for r in app.router.routes]
        self.assertTrue(any(str(p).rstrip("/") == "/sse" for p in paths), paths)
        self.assertTrue(any(str(p).rstrip("/") == "/messages" for p in paths), paths)
        self.assertTrue(any(str(p).rstrip("/") == "/mcp" for p in paths), paths)
        self.assertIn("sse (/sse)", desc)
        self.assertIn("streamable-http (/mcp)", desc)

        wrapped = False
        from starlette.routing import Mount
        for route in app.router.routes:
            if isinstance(route, Mount) and str(route.path).rstrip("/") == "/messages":
                wrapped = isinstance(route.app, SessionMissRewriter)
        self.assertTrue(wrapped)


class TestMcpPathAuth(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_requires_api_secret(self):
        async def _fail_downstream(scope, receive, send):
            raise AssertionError("downstream should not run")

        async def _ok_downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            rec = _SendRecorder()
            mw = gateway.HostFixMiddleware(_fail_downstream)
            await mw(_scope("/mcp", method="POST"), _receive, rec)
            self.assertEqual(rec.status, 401)

        with patch.dict(os.environ, {"API_SECRET": API_SECRET}):
            rec = _SendRecorder()
            mw = gateway.HostFixMiddleware(_ok_downstream)
            scope = _scope("/mcp", method="POST")
            scope["headers"].append((b"x-api-key", API_SECRET.encode()))
            await mw(scope, _receive, rec)
            self.assertEqual(rec.status, 204)


if __name__ == "__main__":
    unittest.main()
