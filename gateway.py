"""
通用 ASGI 网关中间件 (Generic ASGI Gateway Middleware)
======================================================
特性：
- 修正反代场景下的 Host 头
- 统一处理 CORS 预检
- 🔐 全局 API 安全拦截（校验 API_SECRET，对 /sse /messages /api/* 强制鉴权）
- 暴露一组管理 / 健康检查 / 配置接口
- 🧠 OpenAI 兼容代理 (/v1/chat/completions, /v1/models)：
    * 支持纯透传模式（无 Supabase 时）
    * 支持智能体模式（配了 Supabase + 可选 Pinecone）：
      自动注入上文（最近N条对话）、人设、用户画像、阶段总结、向量记忆
    * 流式收集 → 异步双写存库（不阻塞响应）
- 将业务请求转发给下游 MCP 应用

所有配置从环境变量读取，全部"个人化内容"已变量化，无任何硬编码。
未配置的功能会优雅降级，保证最小配置（仅 OPENAI_API_KEY）即可运行。
"""

import os
import json
import asyncio
import time
import datetime
import requests

# ==========================================
# 全局连接（延迟初始化，避免启动时无 Supabase 就崩）
# ==========================================
_supabase_client = None
_system_logs_buffer = []   # 简易日志缓存（用于 /api/logs）
_MAX_LOGS = 200


def _log(msg: str):
    """统一的日志打印 + 内存缓存（供 /api/logs 查询）"""
    line = f"[{datetime.datetime.utcnow().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _system_logs_buffer.append(line)
    if len(_system_logs_buffer) > _MAX_LOGS:
        del _system_logs_buffer[: len(_system_logs_buffer) - _MAX_LOGS]


def _get_supabase():
    """惰性初始化 Supabase 客户端，没配 URL/KEY 就返回 None"""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
        _log(f"✅ Supabase 已连接: {url[:30]}...")
    except Exception as e:
        _log(f"❌ Supabase 连接失败: {e}")
        _supabase_client = None
    return _supabase_client


def _get_pinecone_memory():
    """惰性获取 server 模块的 Pinecone 记忆客户端（延迟导入避免循环依赖）"""
    try:
        import server
        return server.pinecone_memory
    except Exception:
        return None


# ==========================================
# 🆕 设备状态快照（device_data 最新一条 → 注入 system prompt）
# ==========================================

def _parse_json_field(v):
    """PostgREST 可能把 jsonb 以 JSON 字符串返回，统一归一化成 dict/list"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _fmt_duration_cn(ms):
    """毫秒 → 'x小时y分'"""
    total_min = round((ms or 0) / 60000)
    h, m = divmod(total_min, 60)
    if h == 0:
        return f"{m}分钟"
    if m == 0:
        return f"{h}小时"
    return f"{h}小时{m}分"


def _fmt_age_cn(local_wall, now_bj):
    """两者都是北京时间墙钟（naive），相减即真实时长 → 'x 分钟前'"""
    if not local_wall:
        return ""
    mins = max(0, int((now_bj - local_wall).total_seconds() // 60))
    if mins < 1:
        return "刚刚"
    if mins < 60:
        return f"{mins} 分钟前"
    h = mins // 60
    if h < 24:
        return f"{h} 小时前"
    return f"{h // 24} 天前"


def _parse_device_ts(s):
    """device_data.timestamp 是 'YYYY-MM-DD HH:mm:ss'（设备本地=北京时间）"""
    try:
        return datetime.datetime.strptime(str(s)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _app_name_of(app_usage, pkg):
    """通过 packageName 在 app_usage 里找中文应用名"""
    if isinstance(app_usage, list) and pkg:
        for a in app_usage:
            if isinstance(a, dict) and a.get("packageName") == pkg and a.get("appName"):
                return a["appName"]
    return pkg


def _dedupe_notifs(notifs, max_n):
    """通知去重（appName|title|content），按 timestamp 倒序取最近 N 条"""
    if not isinstance(notifs, list) or not notifs or max_n <= 0:
        return []
    sorted_n = sorted(notifs, key=lambda n: (n or {}).get("timestamp", 0) or 0, reverse=True)
    seen, out = set(), []
    for n in sorted_n:
        if len(out) >= max_n:
            break
        n = n or {}
        key = f"{n.get('appName') or n.get('packageName') or ''}|{n.get('title') or ''}|{n.get('content') or ''}"
        if key in seen:
            continue
        seen.add(key)
        label = n.get("appName") or n.get("packageName") or ""
        text = "：".join([x for x in (n.get("title"), n.get("content")) if x])
        out.append(f'{label}「{text}」' if label else f'「{text}」')
    return out


def _fetch_device_snapshot(sb):
    """
    拉取 device_data 最新一条，渲染成可注入 prompt 的文本块。
    只注入最新一条，并标注数据更新时间（设备时间 + 距今多久前）。
    失败/无数据时返回空串，由调用方优雅降级。
    """
    top_apps = int(os.environ.get("DEVICE_CONTEXT_TOP_APPS", "5") or "5")
    max_notifs = int(os.environ.get("DEVICE_CONTEXT_MAX_NOTIFS", "3") or "3")

    res = sb.table("device_data").select("*").order("id", desc=True).limit(1).execute()
    try:
        rows = res.data or []
    except Exception:
        rows = (res or {}).get("data", []) if isinstance(res, dict) else []
    if not rows:
        return ""

    row = rows[0]
    app_usage = _parse_json_field(row.get("app_usage"))
    notifications = _parse_json_field(row.get("notifications"))
    health = _parse_json_field(row.get("health_data")) or {}

    ts_raw = str(row.get("timestamp") or "")[:16]
    parsed = _parse_device_ts(row.get("timestamp"))
    now_bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    age = _fmt_age_cn(parsed, now_bj) if parsed else ""
    updated = f"{ts_raw}（{age}）" if ts_raw and age else (ts_raw or "--")

    lines = [f"【设备状态快照】更新时间：{updated}"]

    # 位置
    loc = "".join([x or "" for x in (row.get("location_city"), row.get("location_district"), row.get("location_street"))])
    if row.get("location_address"):
        lines.append(f"📍 位置：{row['location_address']}")
    elif loc:
        lines.append(f"📍 位置：{loc}")

    # 前台应用
    if row.get("foreground_app"):
        lines.append(f"📱 前台应用：{_app_name_of(app_usage, row.get('foreground_app'))}")

    # 健康数据
    health_parts = []
    if health.get("heartRate") is not None:
        health_parts.append(f"心率 {health['heartRate']}bpm")
    if health.get("stepsToday") is not None:
        health_parts.append(f"步数 {health['stepsToday']}")
    if health.get("caloriesToday") is not None:
        health_parts.append(f"卡路里 {health['caloriesToday']}kcal")
    if health.get("spo2") is not None:
        health_parts.append(f"血氧 {health['spo2']}%")
    if health.get("stress") is not None:
        health_parts.append(f"压力 {health['stress']}")
    if health_parts:
        lines.append("💓 健康：" + "｜".join(health_parts))

    if health.get("sleepTotalMinutes") is not None:
        sleep_bits = []
        if health.get("sleepDeepMinutes"):
            sleep_bits.append(f"深睡{round(health['sleepDeepMinutes'] / 60)}h")
        if health.get("sleepLightMinutes"):
            sleep_bits.append(f"浅睡{round(health['sleepLightMinutes'] / 60)}h")
        if health.get("sleepRemMinutes"):
            sleep_bits.append(f"REM {round(health['sleepRemMinutes'] / 60)}h")
        sleep_line = f"😴 睡眠：{_fmt_duration_cn(health['sleepTotalMinutes'] * 60000)}"
        if sleep_bits:
            sleep_line += "（" + " / ".join(sleep_bits) + "）"

        def _hm(ms):
            # 睡眠起止是 epoch 毫秒，转北京时间（UTC+8）展示
            try:
                return (datetime.datetime.utcfromtimestamp(ms / 1000) + datetime.timedelta(hours=8)).strftime("%H:%M")
            except Exception:
                return None

        st, wk = _hm(health.get("sleepStartMs")), _hm(health.get("sleepWakeupMs"))
        if st and wk:
            sleep_line += f" {st}–{wk}"
        lines.append(sleep_line)

    # 应用使用 Top
    if isinstance(app_usage, list) and app_usage:
        apps = sorted(app_usage, key=lambda a: (a or {}).get("totalTimeInForeground", 0) or 0, reverse=True)[:top_apps]
        apps_txt = "、".join([
            f"{a.get('appName') or a.get('packageName') or '未知'}({_fmt_duration_cn(a.get('totalTimeInForeground'))})"
            for a in apps
        ])
        lines.append(f"📊 应用使用 Top{len(apps)}：{apps_txt}")

    # 通知（去重后最近 N 条）
    notifs = _dedupe_notifs(notifications, max_notifs)
    if notifs:
        lines.append("🔔 最近通知：" + "；".join(notifs))

    # 设备事件
    if row.get("device_event"):
        lines.append(f"⚡ 设备事件：{row['device_event']}")

    return "\n".join(lines)


class HostFixMiddleware:
    """ASGI 中间件：路由分发 + OpenAI 兼容代理 + MCP 下游转发"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # ---------- NapCat 反向 WebSocket 端点 ----------
        if scope["type"] == "websocket" and scope["path"] == "/qq-ws":
            try:
                import napcat
                await napcat.handle_napcat_ws(scope, receive, send)
            except Exception as e:
                _log(f"❌ NapCat WS 处理异常: {e}")
            return

        # 非 HTTP 类型直接透传给下游
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # ---------- 根路径：返回占位（或前端 index.html）----------
        if scope["path"] == "/":
            html = "<h1>🚪 MCP Gateway</h1><p>Endpoints: <code>/health</code> <code>/sse</code> <code>/v1/chat/completions</code></p>"
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/html; charset=utf-8")]})
            await send({"type": "http.response.body", "body": html.encode("utf-8")})
            return

        # ---------- 健康检查 ----------
        if scope["path"] == "/health":
            await _send_json_resp(send, 200, {"status": "ok", "service": "generic-mcp-gateway"})
            return

        # ---------- 🆕 OpenAI 兼容代理 (/v1/*) ----------
        if scope["path"].startswith("/v1/"):
            if scope["method"] == "OPTIONS":
                await _send_cors_preflight(send)
                return
            await self._handle_openai_proxy(scope, receive, send)
            return

        # 🛡️ 全局 API 安全拦截 (涵盖 /api/* /sse /messages)
        if (scope["path"].startswith("/api/") or scope["path"].startswith("/sse") or scope["path"].startswith("/messages")) and scope["method"] != "OPTIONS":
            if not await _check_api_secret(scope, send):
                return

        # ---------- CORS 预检 ----------
        if scope["method"] == "OPTIONS":
            await _send_cors_preflight(send)
            return

        # ---------- 运行日志接口 ----------
        if scope["path"] == "/api/logs":
            await self._handle_logs(send)
            return

        # ---------- 兜底其余请求 (Host Fix → 下游 MCP) ----------
        headers = dict(scope.get("headers", []))
        headers[b"host"] = b"localhost:8000"
        scope["headers"] = list(headers.items())
        await self.app(scope, receive, send)

    # ------------------------------------------
    # 🧠 OpenAI 兼容代理（核心）
    # ------------------------------------------

    async def _handle_openai_proxy(self, scope, receive, send):
        """把 /v1/* 请求转发到上游模型。配了 Supabase 时自动开启智能体模式。"""
        path = scope["path"]
        method = scope["method"]

        # 可选鉴权
        api_secret = os.environ.get("API_SECRET", "").strip()
        if api_secret:
            if not await _check_api_secret(scope, send):
                return

        # ---- /v1/models ----
        if path == "/v1/models" and method == "GET":
            default_model = os.environ.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo")
            models = [{"id": default_model, "object": "model", "created": int(time.time()), "owned_by": "mcp-gateway"}]
            for prefix in ("CHAT_", "SILICON1_", "VISION_", "VOICE_"):
                mn = os.environ.get(f"{prefix}MODEL_NAME", "").strip()
                if mn and mn != default_model:
                    models.append({"id": mn, "object": "model", "created": int(time.time()), "owned_by": "mcp-gateway"})
            await _send_json_resp(send, 200, {"object": "list", "data": models})
            return

        # ---- /v1/chat/completions ----
        if path == "/v1/chat/completions" and method == "POST":
            await self._handle_chat(scope, receive, send)
            return

        await _send_json_resp(send, 404, {"error": {"message": f"Unknown endpoint: {path}"}})

    async def _handle_chat(self, scope, receive, send):
        """聊天核心：透传 + 可选上文注入 + 流式收集双写"""
        # 读请求体
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break

        try:
            req_data = json.loads(body.decode("utf-8"))
        except Exception:
            await _send_json_resp(send, 400, {"error": {"message": "Invalid JSON body"}})
            return

        # 解析上游配置（统一用 OPENAI_*，兼容旧 CHAT_*）
        upstream_base = os.environ.get("OPENAI_BASE_URL", os.environ.get("CHAT_BASE_URL", os.environ.get("DEFAULT_BASE_URL", ""))).strip()
        upstream_key = os.environ.get("OPENAI_API_KEY", os.environ.get("CHAT_API_KEY", os.environ.get("DEFAULT_API_KEY", ""))).strip()
        default_model = os.environ.get("OPENAI_MODEL_NAME", os.environ.get("CHAT_MODEL_NAME", os.environ.get("DEFAULT_MODEL_NAME", "gpt-3.5-turbo")))

        if not upstream_key:
            await _send_json_resp(send, 500, {"error": {"message": "Server 未配置 OPENAI_API_KEY"}})
            return

        if not req_data.get("model"):
            req_data["model"] = default_model

        # 构造上游 URL（兼容用户填或不填 /v1 后缀）
        base = upstream_base.rstrip("/") or "https://api.openai.com/v1"
        if not base.endswith("/v1"):
            upstream_url = f"{base}/v1/chat/completions"
        else:
            upstream_url = f"{base}/chat/completions"

        # ==========================================
        # 🧠 智能体模式：注入上文/人设/记忆（仅当配了 Supabase 时启用）
        # ==========================================
        sb = _get_supabase()
        user_msg = ""
        for m in reversed(req_data.get("messages", [])):
            if m.get("role") == "user":
                user_msg = str(m.get("content", ""))
                break

        if sb and user_msg:
            try:
                await self._inject_context(req_data, sb, user_msg)
            except Exception as e:
                _log(f"⚠️ 上文注入失败（已降级为透传）: {e}")
        else:
            if sb:
                _log("➡️ [透传] 无 user 消息或无 Supabase，直接转发")

        # 强制流式（便于边透传边收集）
        req_data["stream"] = True
        if req_data.get("tools"):
            req_data["tool_choice"] = "auto"

        # 构造请求头（修复 python-requests UA 被拦截 + 透传客户端头）
        client_headers = {k.decode("utf-8", "ignore").lower(): v.decode("utf-8", "ignore") for k, v in scope.get("headers", [])}
        client_ua = client_headers.get("user-agent", "")
        fwd_headers = {
            "Authorization": f"Bearer {upstream_key}",
            "Content-Type": "application/json",
            "User-Agent": client_ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": client_headers.get("accept", "application/json"),
        }
        for h in ("accept-language", "x-requested-with"):
            if h in client_headers:
                fwd_headers[h] = client_headers[h]

        _log(f"➡️ [转发] POST {upstream_url} | model={req_data.get('model')} | key={upstream_key[:6]}***")

        # 启动响应流（通知客户端开始接收 SSE）
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"cache-control", b"no-cache"),
                (b"connection", b"keep-alive"),
                (b"access-control-allow-origin", b"*"),
            ],
        })

        # 后台线程：读取上游流，喂给队列
        import queue
        import threading
        q = queue.Queue()

        def _stream_forward():
            try:
                fwd_headers["Connection"] = "keep-alive"
                with requests.post(upstream_url, headers=fwd_headers, json=req_data, stream=True, timeout=300) as resp:
                    if resp.status_code != 200:
                        q.put({"error": f"HTTP {resp.status_code}: {resp.text[:500]}"})
                        q.put(None)
                        return
                    for line in resp.iter_lines():
                        if line:
                            q.put(line.decode("utf-8"))
                q.put(None)
            except Exception as e:
                q.put({"error": str(e)})
                q.put(None)

        threading.Thread(target=_stream_forward, daemon=True).start()

        collected_content = ""
        collected_reasoning = ""
        tool_calls_dict = {}

        # 主循环：透传 + 收集
        while True:
            chunk = await asyncio.to_thread(q.get)
            if chunk is None:
                break

            if isinstance(chunk, dict) and "error" in chunk:
                _log(f"❌ 上游流式报错: {chunk['error']}")
                err_chunk = {
                    "id": "chatcmpl-error",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": req_data.get("model"),
                    "choices": [{"index": 0, "delta": {"content": f"\n\n[上游错误] {chunk['error']}"}, "finish_reason": "stop"}],
                }
                await send({"type": "http.response.body", "body": f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8"), "more_body": True})
                continue

            await send({"type": "http.response.body", "body": (chunk + "\n\n").encode("utf-8"), "more_body": True})

            if chunk.startswith("data: ") and chunk != "data: [DONE]":
                try:
                    dj = json.loads(chunk[6:])
                    if dj.get("choices"):
                        delta = dj["choices"][0].get("delta", {})
                        if delta.get("content"):
                            collected_content += delta["content"]
                        if delta.get("reasoning_content"):
                            collected_reasoning += delta["reasoning_content"]
                        if delta.get("tool_calls"):
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                if idx not in tool_calls_dict:
                                    tool_calls_dict[idx] = tc
                                else:
                                    if tc.get("function", {}).get("arguments"):
                                        tool_calls_dict[idx]["function"].setdefault("arguments", "")
                                        tool_calls_dict[idx]["function"]["arguments"] += tc["function"]["arguments"]
                except Exception:
                    pass

        # 结束响应
        await send({"type": "http.response.body", "body": b"", "more_body": False})

        # ==========================================
        # 💾 异步双写：把本轮对话存到 Supabase + Pinecone（不阻塞响应）
        # ==========================================
        if sb and user_msg and (collected_content or tool_calls_dict):
            asyncio.create_task(self._save_conversation(sb, user_msg, collected_content, collected_reasoning, tool_calls_dict))

    async def _inject_context(self, req_data, sb, current_query):
        """
        智能体上下文注入（全部变量化，无硬编码）：
        - 系统当前状态（北京时间 / 沉默时长）
        - 用户画像（user_facts 表）
        - 阶段总结（memories 表 tags=Core_Cognition）
        - Pinecone 向量记忆（可选）
        - 最近 N 条对话历史（按 tag 拉，转成 user/assistant 交替）
        """
        ai_name = os.environ.get("AI_NAME", "助手")
        user_name = os.environ.get("USER_NAME", "用户")
        user_id = os.environ.get("USER_ID", "default")
        persona = os.environ.get("AI_PERSONA", "").strip()
        chat_tag = os.environ.get("CHAT_TAG", "Web_Chat")
        now_bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        time_str = now_bj.strftime("%Y-%m-%d %H:%M")

        # 沉默时长（从最近一条对话到现在的小时差，优雅降级）
        silence_hours = 0
        try:
            res = await asyncio.to_thread(lambda: sb.table("memories").select("created_at").eq("tags", chat_tag).order("created_at", desc=True).limit(1).execute())
            if res and res.data:
                last = res.data[0].get("created_at", "")
                if last:
                    try:
                        last_dt = datetime.datetime.strptime(last[:19], "%Y-%m-%dT%H:%M:%S")
                        silence_hours = max(0, round((now_bj - (last_dt + datetime.timedelta(hours=8))).total_seconds() / 3600, 1))
                    except Exception:
                        pass
        except Exception:
            pass

        # 阶段总结
        core_summaries = "无长期记忆"
        try:
            sr = await asyncio.to_thread(lambda: sb.table("memories").select("content").eq("tags", "Core_Cognition").order("created_at", desc=True).limit(3).execute())
            if sr and sr.data:
                core_summaries = "\n".join([f"- {s['content']}" for s in sr.data])
        except Exception:
            pass

        # 用户画像
        user_prof = "暂无"
        try:
            pr = await asyncio.to_thread(lambda: sb.table("user_facts").select("key, value").neq("key", "sys_config").neq("key", "llm_settings").execute())
            if pr and pr.data:
                user_prof = "\n".join([f"- {r['key']}: {str(r['value'])[:200]}" for r in pr.data[:30]])
        except Exception:
            pass

        # Pinecone 向量记忆（可选）
        pinecone_context = "无相关深层记忆"
        mc = _get_pinecone_memory()
        if mc and current_query.strip():
            try:
                def _s():
                    return mc.search(query=str(current_query), user_id=user_id, filters={"user_id": user_id}, limit=5)
                mr = await asyncio.to_thread(_s)
                if mr:
                    rl = mr.get("results", mr) if isinstance(mr, dict) else mr
                    if isinstance(rl, list) and rl:
                        pinecone_context = "\n".join([f"- {m.get('memory', str(m))}" if isinstance(m, dict) else f"- {str(m)}" for m in rl])
            except Exception as e:
                _log(f"Pinecone 检索失败（跳过）: {e}")

        # 最近对话历史（按 tag 拉，转成 user/assistant 交替）
        history_msgs = []
        try:
            _TAGS = [chat_tag, "TG_MSG", "QQ_Chat", "QQ_Group", "Email_Process"]
            hr = await asyncio.to_thread(lambda: sb.table("memories").select("content, tags").in_("tags", _TAGS).order("created_at", desc=True).limit(20).execute())
            if hr and hr.data:
                rows = list(reversed(hr.data))[-10:]
                for row in rows:
                    c = str(row.get("content", "")).strip()
                    if not c:
                        continue
                    if c.startswith(user_name):
                        history_msgs.append({"role": "user", "content": (c.split("：", 1)[-1] if "：" in c else c)[:500]})
                    elif c.startswith("我(") or c.startswith(f"我({ai_name})"):
                        history_msgs.append({"role": "assistant", "content": (c.split("：", 1)[-1] if "：" in c else c)[:500]})
                # 合并相邻同 role
                merged = []
                for m in history_msgs:
                    if merged and merged[-1]["role"] == m["role"]:
                        merged[-1]["content"] += "\n" + m["content"]
                    else:
                        merged.append(m)
                history_msgs = merged
                while history_msgs and history_msgs[0]["role"] != "user":
                    history_msgs.pop(0)
        except Exception as e:
            _log(f"拉取上文失败（跳过）: {e}")

        # 拼装 system prompt
        # 渠道显示名：懒加载 server 模块的映射（避免模块级循环依赖）
        try:
            import server as _srv
            channel_display = _srv._channel_display_name(chat_tag)
        except Exception:
            channel_display = chat_tag

        status_inject = (
            f"\n\n[系统当前状态]\n当前时间:{time_str}(北京时间),距离上次聊天:{silence_hours}h。\n"
            f"当前聊天渠道：{channel_display}\n"
            f"【{user_name}的核心画像】:\n{user_prof}\n\n"
            f"--- 以下为调取的历史背景记忆（请注意这是过去的事，不是现在正在聊的内容） ---\n"
            f"【深层关联记忆】:\n{pinecone_context}\n"
            f"【近3次阶段总结】:\n{core_summaries}\n"
            f"------------------------------------------------\n"
        )

        # 🆕 设备状态快照（device_data 最新一条，含更新时间标注；可开关）
        device_snapshot = ""
        if os.environ.get("DEVICE_CONTEXT_ENABLED", "true").strip().lower() not in ("0", "false", "no"):
            try:
                device_snapshot = _fetch_device_snapshot(sb)
                if device_snapshot:
                    status_inject += f"\n{device_snapshot}\n"
            except Exception as e:
                _log(f"⚠️ 设备快照注入失败（跳过）: {e}")

        if persona:
            status_inject = f"{persona}\n{status_inject}"

        # 注入到 messages：已有 system 就追加，没有就插入
        has_system = False
        for m in req_data.get("messages", []):
            if m.get("role") == "system":
                m["content"] = str(m.get("content", "")) + status_inject
                has_system = True
                break
        if not has_system and req_data.get("messages"):
            req_data["messages"].insert(0, {"role": "system", "content": status_inject.strip()})

        # 清理：去掉末尾的 assistant 尾巴（防止前端误带）
        while req_data.get("messages") and req_data["messages"][-1].get("role") == "assistant":
            req_data["messages"].pop()

        # 把上文历史插到 system 之后、user 之前
        if history_msgs:
            sys_idx = 0
            for i, m in enumerate(req_data["messages"]):
                if m.get("role") == "system":
                    sys_idx = i + 1
                    break
            for j, hm in enumerate(history_msgs):
                req_data["messages"].insert(sys_idx + j, hm)

        _log(f"🧠 [智能体] 注入完成：画像{len(user_prof)}字 + 总结{len(core_summaries)}字 + Pinecone{len(pinecone_context)}字 + 上文{len(history_msgs)}条" + (f" + 设备快照{len(device_snapshot)}字" if device_snapshot else ""))

    async def _save_conversation(self, sb, user_msg, ai_msg, reasoning, tool_calls):
        """异步把本轮对话存到 Supabase memories 表 + Pinecone"""
        ai_name = os.environ.get("AI_NAME", "助手")
        user_name = os.environ.get("USER_NAME", "用户")
        user_id = os.environ.get("USER_ID", "default")
        chat_tag = os.environ.get("CHAT_TAG", "Web_Chat")
        # ⚠️ timestamptz 列必须写显式带时区 ISO，否则无时区字符串按会话时区(UTC)解释
        # 统一写 UTC 时刻，与旧网关数据兼容
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

        final_save_text = ai_msg
        if reasoning:
            final_save_text = f"<think>\n{reasoning}\n</think>\n\n{final_save_text}"
        if not final_save_text and tool_calls:
            tc_names = [tc.get("function", {}).get("name", "unknown") for tc in tool_calls.values()]
            final_save_text = f"[系统记录：调用了工具 {', '.join(tc_names)}]"

        # 1. 存到 memories 表（user + assistant 两条）
        try:
            def _save_user():
                sb.table("memories").insert({
                    "title": f"💬 {user_name}说",
                    "content": f"{user_name}：{user_msg[:2000]}",
                    "category": "流水",
                    "mood": "平静",
                    "tags": chat_tag,
                    "created_at": now_str,
                }).execute()
            await asyncio.to_thread(_save_user)

            def _save_ai():
                sb.table("memories").insert({
                    "title": f"🤖 {ai_name}回复",
                    "content": f"我({ai_name})：{final_save_text[:2000]}",
                    "category": "流水",
                    "mood": "温和",
                    "tags": chat_tag,
                    "created_at": now_str,
                }).execute()
            await asyncio.to_thread(_save_ai)
            _log(f"💾 已存库：{user_name}问({len(user_msg)}字) + {ai_name}答({len(final_save_text)}字)")
        except Exception as e:
            _log(f"❌ 存库失败: {e}")

        # 2. 写入 Pinecone（可选）
        mc = _get_pinecone_memory()
        if mc and user_msg:
            try:
                def _add_m():
                    mc.add([
                        {"role": "user", "content": user_msg},
                        {"role": "assistant", "content": final_save_text},
                    ], user_id=user_id)
                await asyncio.to_thread(_add_m)
                _log("🧠 Pinecone 已写入")
            except Exception as e:
                _log(f"Pinecone 写入失败: {e}")

        # 3. 🧠 异步触发全渠道统一对话总结（不阻塞响应）
        #    监控网页/QQ/TG/邮件等所有渠道的对话流水，
        #    累计达到 SUMMARY_THRESHOLD（默认30条）时自动总结归档。
        try:
            import napcat
            await napcat.check_and_summarize_all()
        except Exception as e:
            _log(f"⚠️ 触发对话总结失败（不影响主流程）: {e}")

    # ------------------------------------------
    # 管理接口
    # ------------------------------------------

    async def _handle_logs(self, send):
        try:
            await _send_json_resp(send, 200, {"logs": "\n".join(_system_logs_buffer[-100:])})
        except Exception as e:
            await _send_json_resp(send, 500, {"error": str(e)})


# ==========================================
# 辅助函数
# ==========================================

async def _check_api_secret(scope, send):
    """校验 API_SECRET。返回 True=通过，False=已拒绝(已发送 401)"""
    api_secret = os.environ.get("API_SECRET", "").strip()
    if not api_secret:
        return True   # 没配就不强制鉴权（保持兼容）
    headers_dict = {k.decode("utf-8").lower(): v.decode("utf-8") for k, v in scope.get("headers", [])}
    auth_token = headers_dict.get("authorization", "").replace("Bearer ", "").replace("bearer ", "").strip()
    x_api_key = headers_dict.get("x-api-key", "").strip()
    if auth_token != api_secret and x_api_key != api_secret:
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"access-control-allow-origin", b"*")]})
        await send({"type": "http.response.body", "body": b'{"error":"Unauthorized: Missing or invalid API key"}'})
        return False
    return True


async def _send_json_resp(send, status: int, data: dict):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"access-control-allow-origin", b"*"),
            (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
            (b"access-control-allow-headers", b"Content-Type, Authorization"),
        ]
    })
    await send({"type": "http.response.body", "body": body})


async def _send_cors_preflight(send):
    await send({
        "type": "http.response.start",
        "status": 204,
        "headers": [
            (b"access-control-allow-origin", b"*"),
            (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
            (b"access-control-allow-headers", b"Content-Type, Authorization"),
            (b"access-control-max-age", b"86400"),
        ]
    })
    await send({"type": "http.response.body", "body": b""})