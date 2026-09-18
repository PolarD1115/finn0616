"""MCP HTTP 传输组装：SSE 保活、会话丢失提示、Streamable HTTP。

约束（已用 mcp 1.x / sse-starlette 源码核对）：
- SSE 的工具调用响应必须走仍活着的 GET /sse，POST /messages/ 只返回 202。
  会话不在了就无法代发工具结果，不能假装成功。
- sse-starlette EventSourceResponse 默认 ping=15s；这里显式固定为 20s。
- Streamable HTTP 的 lifespan 必须跑 session_manager.run()，否则 /mcp 不可用。
"""

from __future__ import annotations

import inspect
import json
import logging
import traceback
from urllib.parse import parse_qs

logger = logging.getLogger("mcp_http")

SSE_PING_SECONDS = 20
SESSION_MISS_BODIES = (b"Could not find session", b"Could not find session\n")
SESSION_MISS_HTTP = 409
SESSION_MISS_RPC_CODE = -32001


def _safe_print(msg: str) -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


def _log_warn(where: str, exc: BaseException | None = None, **fields):
    """会话/传输排障日志：位置 + 关键字段；不含密钥。"""
    extras = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, ""))
    if exc is None:
        _safe_print(f"⚠️ [MCP] {where} {extras}".rstrip())
        logger.warning("%s %s", where, extras)
        return
    _safe_print(f"⚠️ [MCP] {where} {extras} err={type(exc).__name__}: {exc}".rstrip())
    logger.warning("%s %s\n%s", where, extras, traceback.format_exc())


def _keepalive_event():
    from sse_starlette.event import ServerSentEvent
    return ServerSentEvent(comment="keepalive")


def install_sse_keepalive(ping_seconds: int = SSE_PING_SECONDS) -> bool:
    """给 EventSourceResponse 强制加上定时 comment ping（SSE 注释心跳）。

    MCP SDK 创建 EventSourceResponse 时不传 ping，依赖库默认值。
    这里显式固定间隔，并带 `: keepalive` 注释，避免反代按空闲掐断长连接。
    Streamable HTTP 的 GET 流同样走 EventSourceResponse，会一起受益。
    """
    try:
        from sse_starlette.sse import EventSourceResponse
    except Exception as exc:
        _log_warn("install_sse_keepalive:import", exc)
        return False

    if getattr(EventSourceResponse, "_gateway_keepalive_patched", False):
        return True

    orig_init = EventSourceResponse.__init__
    params = inspect.signature(orig_init).parameters

    def _patched_init(self, *args, **kwargs):
        if "ping" in params and kwargs.get("ping") is None:
            kwargs["ping"] = ping_seconds
        if "ping_message_factory" in params and kwargs.get("ping_message_factory") is None:
            kwargs["ping_message_factory"] = _keepalive_event
        return orig_init(self, *args, **kwargs)

    EventSourceResponse.__init__ = _patched_init
    EventSourceResponse._gateway_keepalive_patched = True
    EventSourceResponse._gateway_keepalive_orig_init = orig_init
    _safe_print(f"🟢 [MCP] SSE keepalive ping={ping_seconds}s")
    return True


def _extract_jsonrpc_id(raw: bytes):
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and "id" in data:
        return data.get("id")
    return None


def _session_miss_payload(request_id, session_id: str) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": SESSION_MISS_RPC_CODE,
                "message": "MCP SSE session expired. Reconnect GET /sse then retry.",
                "data": {
                    "code": "mcp_session_not_found",
                    "reconnect": "/sse",
                    "session_id": session_id or None,
                    "hint": "会话已失效。请在客户端关闭后重新打开该 MCP 服务，再试一次。",
                },
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _is_session_miss(status: int, body: bytes) -> bool:
    if status != 404:
        return False
    text = body.strip()
    if text in SESSION_MISS_BODIES:
        return True
    if text == b"Could not find session":
        return True
    return False


class SessionMissRewriter:
    """把 MCP SSE「会话不存在」的硬 404 改成 409 + JSON-RPC 重连提示。

    不能在这里新建会话并继续执行工具：SSE 协议要求结果从原 GET /sse 推回，
    POST 本身不会带回工具结果。返回非 2xx，避免客户端空等。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        req_chunks: list[bytes] = []
        start_msg = None
        body_parts: list[bytes] = []
        forwarded = False

        async def receive_wrapper():
            message = await receive()
            try:
                if message.get("type") == "http.request":
                    req_chunks.append(message.get("body") or b"")
            except Exception as exc:
                _log_warn("SessionMissRewriter.receive", exc)
            return message

        def _query_session_id() -> str:
            try:
                qs = parse_qs((scope.get("query_string") or b"").decode("utf-8", "replace"))
                vals = qs.get("session_id") or []
                return str(vals[0]) if vals else ""
            except Exception as exc:
                _log_warn("SessionMissRewriter.query", exc)
                return ""

        async def capture(message):
            nonlocal start_msg, forwarded
            try:
                if message["type"] == "http.response.start":
                    start_msg = message
                    return
                if message["type"] != "http.response.body":
                    await send(message)
                    return
                body_parts.append(message.get("body") or b"")
                if message.get("more_body"):
                    return
                body = b"".join(body_parts)
                status = int((start_msg or {}).get("status") or 500)
                headers = list((start_msg or {}).get("headers") or [])
                if _is_session_miss(status, body):
                    session_id = _query_session_id()
                    req_id = _extract_jsonrpc_id(b"".join(req_chunks))
                    body = _session_miss_payload(req_id, session_id)
                    status = SESSION_MISS_HTTP
                    headers = [
                        (k, v) for k, v in headers
                        if k.lower() not in (b"content-type", b"content-length")
                    ]
                    headers.extend([
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"access-control-allow-origin", b"*"),
                    ])
                    miss_log = True
                else:
                    session_id = ""
                    miss_log = False
                await send({
                    "type": "http.response.start",
                    "status": status,
                    "headers": headers,
                })
                await send({"type": "http.response.body", "body": body})
                forwarded = True
                if miss_log:
                    _log_warn(
                        "session_miss",
                        path=scope.get("path"),
                        session_id=session_id or "-",
                    )
            except Exception as exc:
                _log_warn("SessionMissRewriter.send", exc, path=scope.get("path"))
                if not forwarded:
                    if start_msg is not None:
                        await send(start_msg)
                    await send(message)

        await self.app(scope, receive_wrapper, capture)


def _wrap_sse_message_routes(app) -> None:
    try:
        from starlette.routing import Mount
    except Exception as exc:
        _log_warn("wrap_sse_message_routes:import", exc)
        return
    try:
        routes = getattr(getattr(app, "router", None), "routes", None) or []
        for route in routes:
            if not isinstance(route, Mount):
                continue
            path = str(getattr(route, "path", "") or "").rstrip("/")
            if path == "/messages":
                route.app = SessionMissRewriter(route.app)
    except Exception as exc:
        _log_warn("wrap_sse_message_routes", exc)


def _route_path(route) -> str:
    return str(getattr(route, "path", "") or "")


def build_mcp_http_app(mcp_server):
    """组装可挂到 HostFixMiddleware 下游的 MCP HTTP app。

    返回 (asgi_app, 传输说明)。
    """
    install_sse_keepalive()

    if not hasattr(mcp_server, "sse_app"):
        raise SystemExit("❌ 当前 MCP SDK 不提供 sse_app()，请锁定 mcp>=1.10,<2.0")

    sse_app = mcp_server.sse_app()
    _wrap_sse_message_routes(sse_app)
    transports = ["sse (/sse)"]

    stream_app = None
    if hasattr(mcp_server, "streamable_http_app"):
        try:
            stream_app = mcp_server.streamable_http_app()
        except Exception as exc:
            _log_warn("streamable_http_app", exc)
            stream_app = None

    if stream_app is None:
        return sse_app, " + ".join(transports)

    try:
        from starlette.applications import Starlette
        sse_routes = list(sse_app.router.routes)
        stream_routes = [
            route for route in stream_app.router.routes
            if _route_path(route).rstrip("/") == "/mcp"
        ]
        if not stream_routes:
            _log_warn("streamable_http_app:missing_/mcp")
            return sse_app, " + ".join(transports)

        def _lifespan(app):
            return mcp_server.session_manager.run()

        combined = Starlette(routes=sse_routes + stream_routes, lifespan=_lifespan)
        transports.append("streamable-http (/mcp)")
        return combined, " + ".join(transports)
    except Exception as exc:
        _log_warn("build_mcp_http_app:combine", exc)
        return sse_app, " + ".join(transports)
