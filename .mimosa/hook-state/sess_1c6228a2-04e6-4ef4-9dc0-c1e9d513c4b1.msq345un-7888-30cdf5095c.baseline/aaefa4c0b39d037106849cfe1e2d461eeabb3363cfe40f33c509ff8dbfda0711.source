"""最小 ASGI 测试壳：直接挂载 HostFixMiddleware 验证网关路由。
不依赖 server.py 的 MCP SDK，仅验证 gateway.py 的路由/鉴权/控制台/管理 API。"""
import os
os.environ.setdefault("API_SECRET", "testsecret123")
import gateway


class DummyApp:
    async def __call__(self, scope, receive, send):
        await gateway._send_json_resp(send, 404, {"error": "downstream not available in test harness"})


app = gateway.HostFixMiddleware(DummyApp())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=18765, log_level="warning")
