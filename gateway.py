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
import re
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


# ==========================================
# 🎛️ 多模型注册表（前端可自由添加 / 切换）
# ==========================================
# 存储位置：user_facts 表，key='llm_models'，value 为 JSON 字符串：
#   {
#     "models": [
#       {"id": "gpt-4o", "label": "GPT-4o", "base_url": "https://api.openai.com/v1",
#        "api_key": "sk-xxx", "model": "gpt-4o", "enabled": true},
#       ...
#     ],
#     "default": "gpt-4o"     # 默认模型 id（前端不传 model 时用）
#   }
# 设计要点：
#   - 每个模型自带 base_url + api_key + 真实 model 名，路由时按前端传入的 model(id) 命中对应上游
#   - 未命中或注册表为空时，回退到环境变量（完全向后兼容旧部署）
#   - GET /api/models 返回时会屏蔽 api_key（只留前几位），避免密钥泄露

_LLM_REGISTRY_KEY = "llm_models"


def _load_llm_registry():
    """从 Supabase 读取模型注册表；无表/无数据/未配库时返回空结构。"""
    sb = _get_supabase()
    if not sb:
        return {"models": [], "default": ""}
    try:
        res = sb.table("user_facts").select("value").eq("key", _LLM_REGISTRY_KEY).execute()
        if res.data and res.data[0].get("value"):
            reg = json.loads(res.data[0]["value"])
            if isinstance(reg, dict) and isinstance(reg.get("models"), list):
                return {"models": reg["models"], "default": reg.get("default", "")}
    except Exception as e:
        _log(f"⚠️ 读取模型注册表失败: {e}")
    return {"models": [], "default": ""}


def _save_llm_registry(reg: dict) -> bool:
    """把模型注册表写回 Supabase（upsert）。"""
    sb = _get_supabase()
    if not sb:
        return False
    try:
        payload = {
            "key": _LLM_REGISTRY_KEY,
            "value": json.dumps({
                "models": reg.get("models", []),
                "default": reg.get("default", ""),
            }, ensure_ascii=False),
        }
        sb.table("user_facts").upsert(payload).execute()
        return True
    except Exception as e:
        _log(f"❌ 保存模型注册表失败: {e}")
        return False


def _resolve_model(model_id: str):
    """按前端传入的 model(id) 解析出真实上游三元组。
    返回 (base_url, api_key, real_model, matched)；matched=False 表示未命中注册表。
    """
    reg = _load_llm_registry()
    models = [m for m in reg.get("models", []) if m.get("enabled", True)]
    if not models:
        return ("", "", "", False)

    target = None
    # 1) 精确匹配 id
    if model_id:
        for m in models:
            if m.get("id") == model_id:
                target = m
                break
        # 2) 退而匹配真实 model 名（有些客户端直接发 model 名）
        if not target:
            for m in models:
                if m.get("model") == model_id:
                    target = m
                    break
    # 3) 用默认模型
    if not target:
        default_id = reg.get("default", "")
        for m in models:
            if m.get("id") == default_id:
                target = m
                break
    # 4) 兜底取第一个启用的
    if not target:
        target = models[0]

    base_url = str(target.get("base_url", "")).strip()
    api_key = str(target.get("api_key", "")).strip()
    real_model = str(target.get("model", "")).strip() or target.get("id", "")
    return (base_url, api_key, real_model, True)


def _mask_key(k: str) -> str:
    """屏蔽 api_key，仅保留前缀用于识别。"""
    k = str(k or "")
    if len(k) <= 8:
        return "***" if k else ""
    return f"{k[:6]}...{k[-2:]}"


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


def _is_profile_key(key: str) -> bool:
    """
    判断一个 user_facts 的 key 是否属于「用户画像」（应注入 prompt）。

    desire 系列的处理：
    - 运行时状态（desire_drive_state / desire_emotion_state / desire_last_tick_at
      / desire_next_heartbeat_at 等）——每拍都在变，是引擎内部状态，绝不能进画像，
      否则既是噪音、又会因为时间戳每拍变化而击穿缓存前缀 → 排除。
    - 人写的笔记（如 desire_system_tech_debt_2026_08_05）——带日期后缀，属于真画像 → 放行。

    识别方式：desire_ 开头且**结尾带 _YYYY_MM_DD 日期后缀**的视为笔记放行，
    其余 desire_ 开头一律当运行时状态排除。非 desire_ 开头的照常放行。
    """
    if not key:
        return False
    if key.startswith("desire_"):
        # 结尾是 _2026_08_05 这种日期后缀 → 笔记，放行
        return bool(re.search(r"_\d{4}_\d{2}_\d{2}$", key))
    return True


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


# ==========================================
# 🏷️ Claude 思考标签改写：<thinking> → <think>
# ==========================================
# 背景：Claude 系列常把思考过程包成 <thinking>...</thinking> 混在正文里下发，
#      而本项目其余环节（存库剥离、前端渲染）统一按 <think> 处理。
#      为保持一致，转发 Claude 响应时把 thinking 标签统一改写成 think。
# 难点：流式下标签会被切碎（如 "<think" 一块、"ing>" 下一块），
#      因此需要一个「跨 chunk 尾缓冲」状态机：末尾若可能是半个标签就暂存，
#      等后续拼齐再替换，避免漏改。
# 开关：默认对 model 名含 "claude" 的响应启用；可用 REWRITE_THINKING_TAG
#      环境变量强制开启(1/true/yes)或关闭(0/false/no)。

_THINKING_TAG_RE = re.compile(r"</?thinking(\s[^>]*)?>", re.IGNORECASE)
# 尾部可能是半个 "<thinking>" / "</thinking>" 开头，需要暂存等下一块拼齐再判断。
# 含开标签 "<thinking" 与闭标签 "</thinking" 的所有前缀（斜杠后 t 亦可缺失，如 "</"）。
_THINKING_PARTIAL_RE = re.compile(
    r"</?(t(h(i(n(k(i(n(g)?)?)?)?)?)?)?)?$", re.IGNORECASE
)


def _should_rewrite_thinking(model_name: str) -> bool:
    """判断是否需要把 <thinking> 改写成 <think>。"""
    ov = os.environ.get("REWRITE_THINKING_TAG", "").strip().lower()
    if ov in ("1", "true", "yes"):
        return True
    if ov in ("0", "false", "no"):
        return False
    return "claude" in (model_name or "").lower()


def _rewrite_thinking_tags(text: str) -> str:
    """把一段文本里的 <thinking>/</thinking> 换成 <think>/</think>（保留斜杠）。"""
    return _THINKING_TAG_RE.sub(
        lambda m: "</think>" if m.group(0).lstrip("<").startswith("/") else "<think>",
        text,
    )


class _ThinkingTagRewriter:
    """流式 <thinking> → <think> 改写器，处理跨 chunk 被切碎的标签。

    用法：对每段 delta.content 调用 feed()，结束时调用 flush() 取回残留缓冲。
    """

    def __init__(self):
        self._buf = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        data = self._buf + text
        # 先把已完整出现的标签替换掉
        data = _rewrite_thinking_tags(data)
        # 检查末尾是否是「半个标签」，是则留在缓冲里等下一块
        m = _THINKING_PARTIAL_RE.search(data)
        if m and m.start() < len(data):
            self._buf = data[m.start():]
            return data[: m.start()]
        self._buf = ""
        return data

    def flush(self) -> str:
        out = self._buf
        self._buf = ""
        return out


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

        # ---------- 🎛️ 多模型管理接口 ----------
        if scope["path"] == "/api/models":
            await self._handle_models_api(scope, receive, send)
            return

        # ---------- 🧠 情绪 / 欲望系统状态接口（只读） ----------
        # 返回 user_facts 里 desire_* 的最新持久化快照，供 Mini App / 前端面板使用。
        # 已在上方全局拦截中过 API_SECRET 鉴权。纯只读：不推进引擎、不写库。
        if scope["path"] == "/api/desire":
            await self._handle_desire_api(send)
            return

        # ---------- 🎛️ 内置模型管理网页 ----------
        if scope["path"] == "/admin/models":
            await self._handle_admin_page(send)
            return

        # ---------- 📱 情绪 / 欲望 Mini App（静态页面） ----------
        if scope["path"] == "/emotion" or scope["path"] == "/emotion/":
            await self._handle_emotion_page(send)
            return

        # ---------- 📱 Mini App 配置面板（前端直连 Supabase） ----------
        if scope["path"] == "/miniapp" or scope["path"] == "/miniapp/":
            await self._handle_miniapp_page(send)
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
            # 优先返回数据库模型注册表（前端可自由增删改）
            reg = _load_llm_registry()
            reg_models = [m for m in reg.get("models", []) if m.get("enabled", True)]
            if reg_models:
                default_id = reg.get("default", "")
                data = []
                for m in reg_models:
                    mid = m.get("id") or m.get("model")
                    if not mid:
                        continue
                    data.append({
                        "id": mid,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "mcp-gateway",
                        # 附带展示名与是否默认，方便自定义前端渲染；标准客户端会忽略多余字段
                        "label": m.get("label", mid),
                        "is_default": (mid == default_id),
                    })
                await _send_json_resp(send, 200, {"object": "list", "data": data})
                return

            # 回退：注册表为空时沿用环境变量（向后兼容）
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

        # 解析上游配置：优先走多模型注册表（前端传入 model 命中对应上游），
        # 未命中或注册表为空时回退到环境变量（统一用 OPENAI_*，兼容旧 CHAT_*）。
        requested_model = str(req_data.get("model", "")).strip()
        reg_base, reg_key, reg_model, matched = _resolve_model(requested_model)

        if matched and reg_key:
            upstream_base = reg_base
            upstream_key = reg_key
            # 命中注册表：把请求体里的 model 换成该条目的"真实模型名"再转发给上游
            req_data["model"] = reg_model
            _log(f"🎛️ [路由] 前端模型={requested_model or '(空)'} → 上游模型={reg_model} @ {reg_base[:32]}")
        else:
            upstream_base = os.environ.get("OPENAI_BASE_URL", os.environ.get("CHAT_BASE_URL", os.environ.get("DEFAULT_BASE_URL", ""))).strip()
            upstream_key = os.environ.get("OPENAI_API_KEY", os.environ.get("CHAT_API_KEY", os.environ.get("DEFAULT_API_KEY", ""))).strip()
            default_model = os.environ.get("OPENAI_MODEL_NAME", os.environ.get("CHAT_MODEL_NAME", os.environ.get("DEFAULT_MODEL_NAME", "gpt-3.5-turbo")))
            if not req_data.get("model"):
                req_data["model"] = default_model

        if not upstream_key:
            await _send_json_resp(send, 500, {"error": {"message": "Server 未配置模型上游（注册表为空且未设置 OPENAI_API_KEY）"}})
            return

        # 构造上游 URL（智能兼容各家 base_url 写法）
        # 规则：
        #   1) base 已带 /chat/completions → 原样用（用户填了完整端点）
        #   2) base 以版本号段结尾（/v1 /v2 /v3 /v4 或 /paas/v4 等）→ 直接补 /chat/completions
        #   3) 其余（裸域名/根路径）→ 按 OpenAI 习惯补 /v1/chat/completions
        # 这样智谱 https://open.bigmodel.cn/api/paas/v4 → .../v4/chat/completions（不再错拼成 /v4/v1/...）
        base = upstream_base.rstrip("/") or "https://api.openai.com/v1"
        if re.search(r"/chat/completions$", base):
            upstream_url = base
        elif re.search(r"/v\d+[a-zA-Z]*$", base):
            # 结尾是版本段：/v1 /v2 /v4 /v1beta 等 → 直接接 /chat/completions
            upstream_url = f"{base}/chat/completions"
        else:
            upstream_url = f"{base}/v1/chat/completions"

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

        # 🏷️ 是否把 Claude 的 <thinking> 标签改写成 <think>（跨 chunk 状态机）
        rewrite_thinking = _should_rewrite_thinking(req_data.get("model", ""))
        thinking_rewriter = _ThinkingTagRewriter() if rewrite_thinking else None
        if rewrite_thinking:
            _log(f"🏷️ [thinking改写] 已启用 <thinking>→<think> | model={req_data.get('model')}")

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

            # 默认原样透传；启用 thinking 改写时对 data 行做 <thinking>→<think>
            if not rewrite_thinking or not chunk.startswith("data: ") or chunk == "data: [DONE]":
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
                continue

            # ---- 需要改写 thinking 标签的分支 ----
            try:
                dj = json.loads(chunk[6:])
            except Exception:
                # 解析失败：原样透传，不影响其他 SSE 行（如注释/心跳）
                await send({"type": "http.response.body", "body": (chunk + "\n\n").encode("utf-8"), "more_body": True})
                continue

            rewritten = False
            if dj.get("choices"):
                delta = dj["choices"][0].get("delta", {})
                if delta.get("content"):
                    new_content = thinking_rewriter.feed(delta["content"])
                    delta["content"] = new_content
                    collected_content += new_content
                    rewritten = True
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

            out_line = f"data: {json.dumps(dj, ensure_ascii=False)}" if rewritten else chunk
            await send({"type": "http.response.body", "body": (out_line + "\n\n").encode("utf-8"), "more_body": True})

        # 🏷️ flush 改写器残留缓冲（末尾停在半个标签的情况），补一个 content chunk
        if thinking_rewriter is not None:
            tail = thinking_rewriter.flush()
            if tail:
                collected_content += tail
                tail_chunk = {
                    "id": "chatcmpl-tail",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": req_data.get("model"),
                    "choices": [{"index": 0, "delta": {"content": tail}, "finish_reason": None}],
                }
                await send({"type": "http.response.body", "body": f"data: {json.dumps(tail_chunk, ensure_ascii=False)}\n\n".encode("utf-8"), "more_body": True})

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
            # 画像查询：排除系统配置键；desire_ 前缀在 Python 侧精细过滤（见 _is_profile_key）。
            # 加 .order("key") 稳定排序 —— 顺序固定，注入进稳定前缀的内容才不会变，缓存前缀才能命中。
            pr = await asyncio.to_thread(lambda: sb.table("user_facts").select("key, value").neq("key", "sys_config").neq("key", "llm_settings").neq("key", "llm_models").order("key").execute())
            if pr and pr.data:
                rows = [r for r in pr.data if _is_profile_key(r.get("key", ""))]
                user_prof = "\n".join([f"- {r['key']}: {str(r['value'])[:200]}" for r in rows[:60]])
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

        # 🆕 设备状态快照（device_data 最新一条，含更新时间标注；可开关）
        device_snapshot = ""
        if os.environ.get("DEVICE_CONTEXT_ENABLED", "true").strip().lower() not in ("0", "false", "no"):
            try:
                device_snapshot = _fetch_device_snapshot(sb)
            except Exception as e:
                _log(f"⚠️ 设备快照注入失败（跳过）: {e}")

        # ==========================================
        # 📦 缓存友好的两段式拼装（修复缓存命中率≈0 & AI 漏看时间戳）
        #   ① stable_system —— 稳定前缀（人设 + 画像 + 阶段总结），几乎不随请求变化，
        #      放最前面当作可命中 prompt cache 的公共前缀（上游多为前缀匹配缓存，
        #      前缀里一旦混入每请求都变的时间戳，整段缓存即失效 → 命中率归零）。
        #   ② volatile_block —— 易变尾块（实时时间 / 沉默时长 / 渠道 / 按本轮话题检索的
        #      深层记忆 / 设备快照），随每次请求变化，塞到「最后一条 user 之前」，
        #      既不污染缓存前缀，又落在模型注意力最高的末尾位置。
        #   ③ 时间戳放在 volatile_block 的最末行、紧贴用户消息，避免被 AI 漏看。
        # ==========================================
        stable_parts = []
        if persona:
            stable_parts.append(persona)
        stable_parts.append(f"【{user_name}的核心画像】:\n{user_prof}")
        stable_parts.append(f"【近3次阶段总结】:\n{core_summaries}")
        stable_system = "\n\n".join(stable_parts)

        volatile_block = (
            f"--- 以下为按本轮话题检索的背景记忆（请注意这是过去的事，不是现在正在聊的内容） ---\n"
            f"【深层关联记忆】:\n{pinecone_context}\n"
        )
        if device_snapshot:
            volatile_block += f"{device_snapshot}\n"
        volatile_block += (
            f"------------------------------------------------\n"
            f"[实时状态 · 回复前请先读这里]\n"
            f"⏰ 当前时间：{time_str}（北京时间）\n"
            f"🔕 距离上次聊天：{silence_hours}h\n"
            f"📡 当前聊天渠道：{channel_display}"
        )

        # ① 注入稳定前缀到 system：已有 system 就「前置」拼接（保证稳定内容仍在最前，
        #    维持缓存前缀不被前端自带 system 内容顶开），没有就插入到最前。
        has_system = False
        for m in req_data.get("messages", []):
            if m.get("role") == "system":
                existing = str(m.get("content", ""))
                m["content"] = f"{stable_system}\n\n{existing}" if existing else stable_system
                has_system = True
                break
        if not has_system and req_data.get("messages"):
            req_data["messages"].insert(0, {"role": "system", "content": stable_system})

        # 清理：去掉末尾的 assistant 尾巴（防止前端误带）
        while req_data.get("messages") and req_data["messages"][-1].get("role") == "assistant":
            req_data["messages"].pop()

        # ② 把上文历史插到 system 之后、user 之前
        if history_msgs:
            sys_idx = 0
            for i, m in enumerate(req_data["messages"]):
                if m.get("role") == "system":
                    sys_idx = i + 1
                    break
            for j, hm in enumerate(history_msgs):
                req_data["messages"].insert(sys_idx + j, hm)

        # ③ 易变尾块：作为一条 system 消息塞到「最后一条 user 之前」，
        #    确保实时时间/记忆落在缓存前缀之后 + 注意力最高的末尾。
        msgs = req_data.get("messages", [])
        last_user_idx = None
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                last_user_idx = i
                break
        volatile_msg = {"role": "system", "content": volatile_block}
        if last_user_idx is not None:
            msgs.insert(last_user_idx, volatile_msg)
        else:
            msgs.append(volatile_msg)

        _log(f"🧠 [智能体] 注入完成：画像{len(user_prof)}字 + 总结{len(core_summaries)}字 + Pinecone{len(pinecone_context)}字 + 上文{len(history_msgs)}条" + (f" + 设备快照{len(device_snapshot)}字" if device_snapshot else "") + f" ｜ 稳定前缀{len(stable_system)}字 + 易变尾块{len(volatile_block)}字")

    async def _save_conversation(self, sb, user_msg, ai_msg, reasoning, tool_calls):
        """异步把本轮对话存到 Supabase memories 表 + Pinecone"""
        ai_name = os.environ.get("AI_NAME", "助手")
        user_name = os.environ.get("USER_NAME", "用户")
        user_id = os.environ.get("USER_ID", "default")
        chat_tag = os.environ.get("CHAT_TAG", "Web_Chat")
        # ⚠️ timestamptz 列必须写显式带时区 ISO，否则无时区字符串按会话时区(UTC)解释
        # 统一写 UTC 时刻，与旧网关数据兼容
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

        final_save_text = ai_msg or ""
        # 🧠 默认过滤思考过程，避免 <think> 占满字数导致正文被截断。
        #    如需保留旧行为（存 <think> 块），设置环境变量 SAVE_THINKING=true。
        save_thinking = os.environ.get("SAVE_THINKING", "false").strip().lower() in ("1", "true", "yes")
        if save_thinking:
            if reasoning:
                final_save_text = f"<think>\n{reasoning}\n</think>\n\n{final_save_text}"
        else:
            # 剥离正文里可能内联混入的 <think>...</think> 块（reasoning 直接丢弃不拼接）
            final_save_text = re.sub(
                r"<think>.*?</think>", "", final_save_text, flags=re.DOTALL | re.IGNORECASE
            ).strip()
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

    # ------------------------------------------
    # 🎛️ 多模型管理接口 /api/models
    # ------------------------------------------
    async def _handle_models_api(self, scope, receive, send):
        """
        多模型注册表 CRUD（已经过 API_SECRET 鉴权）：
          GET    /api/models              列出全部模型（api_key 脱敏）+ default
          POST   /api/models              新增/更新一个模型（按 id upsert）
                 body: {id,label,base_url,api_key,model,enabled}
          POST   /api/models  {action:"set_default", id:"xxx"}   设默认
          DELETE /api/models?id=xxx       删除一个模型
        说明：删除仅移除注册表里的一条 JSON 项，不触碰其它数据库表。
        """
        method = scope["method"]

        # ---- GET：列出 ----
        if method == "GET":
            reg = _load_llm_registry()
            safe = []
            for m in reg.get("models", []):
                safe.append({
                    "id": m.get("id", ""),
                    "label": m.get("label", m.get("id", "")),
                    "base_url": m.get("base_url", ""),
                    "api_key_masked": _mask_key(m.get("api_key", "")),
                    "has_key": bool(str(m.get("api_key", "")).strip()),
                    "model": m.get("model", ""),
                    "enabled": m.get("enabled", True),
                })
            await _send_json_resp(send, 200, {"models": safe, "default": reg.get("default", "")})
            return

        # ---- 读 body（POST/DELETE 可能带）----
        body = b""
        if method in ("POST", "DELETE", "PUT"):
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body", False):
                    break
        payload = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                await _send_json_resp(send, 400, {"error": "Invalid JSON body"})
                return

        # ---- DELETE：删一个 ----
        if method == "DELETE":
            # id 可来自 body 或 query string
            del_id = str(payload.get("id", "")).strip()
            if not del_id:
                qs = scope.get("query_string", b"").decode("utf-8")
                for part in qs.split("&"):
                    if part.startswith("id="):
                        from urllib.parse import unquote
                        del_id = unquote(part[3:]).strip()
            if not del_id:
                await _send_json_resp(send, 400, {"error": "缺少 id"})
                return
            reg = _load_llm_registry()
            before = len(reg.get("models", []))
            reg["models"] = [m for m in reg.get("models", []) if m.get("id") != del_id]
            if reg.get("default") == del_id:
                reg["default"] = reg["models"][0]["id"] if reg["models"] else ""
            if len(reg["models"]) == before:
                await _send_json_resp(send, 404, {"error": f"未找到模型 id={del_id}"})
                return
            ok = _save_llm_registry(reg)
            await _send_json_resp(send, 200 if ok else 500,
                                  {"ok": ok, "deleted": del_id, "count": len(reg["models"])})
            return

        # ---- POST ----
        if method == "POST":
            action = str(payload.get("action", "")).strip()

            # 设默认
            if action == "set_default":
                did = str(payload.get("id", "")).strip()
                reg = _load_llm_registry()
                if not any(m.get("id") == did for m in reg.get("models", [])):
                    await _send_json_resp(send, 404, {"error": f"未找到模型 id={did}"})
                    return
                reg["default"] = did
                ok = _save_llm_registry(reg)
                await _send_json_resp(send, 200 if ok else 500, {"ok": ok, "default": did})
                return

            # 新增 / 更新（按 id upsert）
            mid = str(payload.get("id", "")).strip()
            if not mid:
                await _send_json_resp(send, 400, {"error": "缺少 id（模型的唯一标识，也是前端下拉框显示的值）"})
                return
            base_url = str(payload.get("base_url", "")).strip()
            real_model = str(payload.get("model", "")).strip() or mid
            new_key = str(payload.get("api_key", "")).strip()

            reg = _load_llm_registry()
            existing = next((m for m in reg["models"] if m.get("id") == mid), None)
            entry = {
                "id": mid,
                "label": str(payload.get("label", "")).strip() or (existing.get("label") if existing else mid),
                "base_url": base_url or (existing.get("base_url") if existing else ""),
                "model": real_model,
                "enabled": bool(payload.get("enabled", existing.get("enabled", True) if existing else True)),
            }
            # api_key：留空表示"沿用旧值"（避免脱敏回传时误清空）
            if new_key:
                entry["api_key"] = new_key
            elif existing:
                entry["api_key"] = existing.get("api_key", "")
            else:
                entry["api_key"] = ""

            if not entry["api_key"]:
                await _send_json_resp(send, 400, {"error": "缺少 api_key"})
                return
            if not entry["base_url"]:
                await _send_json_resp(send, 400, {"error": "缺少 base_url"})
                return

            if existing:
                idx = reg["models"].index(existing)
                reg["models"][idx] = entry
            else:
                reg["models"].append(entry)
            # 第一个加入的模型自动设为默认
            if not reg.get("default"):
                reg["default"] = mid

            ok = _save_llm_registry(reg)
            await _send_json_resp(send, 200 if ok else 500, {
                "ok": ok,
                "saved": {**entry, "api_key": _mask_key(entry["api_key"])},
                "default": reg.get("default", ""),
            })
            return

        await _send_json_resp(send, 405, {"error": f"Method {method} not allowed"})

    async def _handle_admin_page(self, send):
        """返回内置模型管理网页（纯静态 HTML，逻辑走 /api/models）。"""
        html = _ADMIN_MODELS_HTML
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                (b"access-control-allow-origin", b"*")]})
        await send({"type": "http.response.body", "body": html.encode("utf-8")})

    async def _handle_miniapp_page(self, send):
        """返回 Mini App 配置面板（纯静态 HTML，前端直连 Supabase）。
        页面从同目录 miniapp.html 读取；找不到则返回提示。
        """
        try:
            import os as _os
            _here = _os.path.dirname(_os.path.abspath(__file__))
            with open(_os.path.join(_here, "miniapp.html"), "r", encoding="utf-8") as f:
                html = f.read()
            body = html.encode("utf-8")
            status = 200
        except Exception as e:
            body = f"miniapp.html 未找到: {e}".encode("utf-8")
            status = 500
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                (b"access-control-allow-origin", b"*")]})
        await send({"type": "http.response.body", "body": body})

    # ------------------------------------------
    # 🧠 情绪 / 欲望系统状态接口 /api/desire
    # ------------------------------------------
    async def _handle_desire_api(self, send):
        """只读返回「情感 16 维 + 欲望 8 维」最新持久化快照。

        数据源：user_facts 表里的 desire_* 键（desire_bridge 心跳每拍写库）。
        纯只读：不推进引擎、不消费事件、不写库，可放心高频轮询。

        返回结构：
          {
            "ok": true,
            "source": "user_facts",
            "updated_at": <drive_state.snapshot_at 或 null>,
            "emotion": <desire_emotion_state 原样 JSON>,
            "drive_state": <desire_drive_state 原样 JSON>,
            "refractory": {...},
            "last_action": str,
            "action_repeat": int,
            "next_heartbeat_at": float(ms) | null,
            "attachment_baseline": float,
            "events_queue_len": int,
            "derived": {
              "display": {16维 + fatigue},   // 最新 display 快照
              "drive": {8维},                // 最新驱动条
              "scores": {7维召唤力},          // 从 drive 现值近似（无念头加成）
              "intent": null,                // 服务端不推进引擎，intent 由前端本地算或置空
              "sleep": {...},                // 睡眠子状态
              "unanswered_thread": {...} | null,
              "last_interaction_at": str | null,
              "active_whim": {...} | null,
              "time_episode_applied": {...}, // 本 episode 已累积的维度
            },
          }
        """
        sb = _get_supabase()
        if not sb:
            await _send_json_resp(send, 200, {
                "ok": False,
                "error": "网关未配置 Supabase，无法读取情绪/欲望状态",
            })
            return

        def _load(key):
            try:
                r = sb.table("user_facts").select("value").eq("key", key).maybe_single().execute()
                if r.data and r.data.get("value"):
                    return json.loads(r.data["value"])
            except Exception as e:
                _log(f"⚠️ /api/desire 读取 {key} 失败: {e}")
            return None

        emotion = _load("desire_emotion_state")
        drive_state = _load("desire_drive_state")
        refractory = _load("desire_refractory") or {}
        last_action = _load("desire_last_action")
        try:
            action_repeat = int(_load("desire_action_repeat") or 0)
        except (TypeError, ValueError):
            action_repeat = 0
        next_hb = _load("desire_next_heartbeat_at")
        try:
            att_baseline = float(_load("desire_attachment_baseline"))
        except (TypeError, ValueError):
            att_baseline = None
        queue = _load("desire_events_queue") or []

        # ---- 派生视图（纯本地拼装，不调引擎） ----
        derived = {
            "display": None,
            "drive": None,
            "scores": None,
            "intent": None,
            "sleep": None,
            "unanswered_thread": None,
            "last_interaction_at": None,
            "active_whim": None,
            "time_episode_applied": {},
        }
        if isinstance(emotion, dict):
            emo = emotion
            d = emo.get("display") or emo.get("prev_display") or {}
            derived["display"] = d
            derived["sleep"] = emo.get("sleep")
            derived["unanswered_thread"] = emo.get("unanswered_thread")
            derived["last_interaction_at"] = emo.get("last_interaction_at")
            derived["active_whim"] = emo.get("active_whim")
            ep = emo.get("time_episode") or {}
            derived["time_episode_applied"] = ep.get("applied", {})
        if isinstance(drive_state, dict) and isinstance(drive_state.get("drive"), dict):
            drv = drive_state["drive"]
            derived["drive"] = drv
            # 召唤力 = 驱动条现值（念头加成服务端不存，按 0 处理）→ 7 维
            ranked = ["attachment", "curiosity", "reflection", "duty",
                      "social", "libido", "stress"]
            derived["scores"] = {k: drv.get(k, 0.0) for k in ranked}

        updated_at = None
        if isinstance(drive_state, dict):
            updated_at = drive_state.get("snapshot_at")

        await _send_json_resp(send, 200, {
            "ok": True,
            "source": "user_facts",
            "updated_at": updated_at,
            "emotion": emotion,
            "drive_state": drive_state,
            "refractory": refractory,
            "last_action": last_action,
            "action_repeat": action_repeat,
            "next_heartbeat_at": next_hb,
            "attachment_baseline": att_baseline,
            "events_queue_len": len(queue),
            "derived": derived,
        })

    # ------------------------------------------
    # 📱 情绪 / 欲望 Mini App 页面（/emotion）
    # ------------------------------------------
    async def _handle_emotion_page(self, send):
        """返回情绪/欲望 Mini App 静态页面（同目录 emotion_miniapp.html）。"""
        try:
            import os as _os
            _here = _os.path.dirname(_os.path.abspath(__file__))
            with open(_os.path.join(_here, "emotion_miniapp.html"), "r", encoding="utf-8") as f:
                html = f.read()
            body = html.encode("utf-8")
            status = 200
        except Exception as e:
            body = f"emotion_miniapp.html 未找到: {e}".encode("utf-8")
            status = 500
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                (b"access-control-allow-origin", b"*")]})
        await send({"type": "http.response.body", "body": body})


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
            (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
            (b"access-control-allow-headers", b"Content-Type, Authorization, X-Api-Key"),
        ]
    })
    await send({"type": "http.response.body", "body": body})


async def _send_cors_preflight(send):
    await send({
        "type": "http.response.start",
        "status": 204,
        "headers": [
            (b"access-control-allow-origin", b"*"),
            (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
            (b"access-control-allow-headers", b"Content-Type, Authorization, X-Api-Key"),
            (b"access-control-max-age", b"86400"),
        ]
    })
    await send({"type": "http.response.body", "body": b""})

# ==========================================
# 🎛️ 内置模型管理网页（/admin/models）
# ==========================================
# 纯静态单页：读取/新增/编辑/删除模型、设默认、发消息实测。
# 所有写操作都带 API_SECRET（页面里填一次，存 localStorage）。
_ADMIN_MODELS_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>模型管理 · MCP Gateway</title>
<style>
  :root { --bg:#0f1117; --card:#1a1d27; --line:#2a2e3a; --fg:#e6e8ee; --mut:#8a90a2; --acc:#6ea8fe; --ok:#4ade80; --err:#f87171; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif; font-size:14px; }
  header { padding:16px 20px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  header h1 { font-size:17px; margin:0; font-weight:600; }
  .wrap { max-width:960px; margin:0 auto; padding:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:16px; }
  .card h2 { font-size:14px; margin:0 0 12px; color:var(--mut); font-weight:600; letter-spacing:.3px; }
  label { display:block; font-size:12px; color:var(--mut); margin:8px 0 4px; }
  input, textarea, select { width:100%; background:#0f1219; border:1px solid var(--line); color:var(--fg); border-radius:8px; padding:9px 11px; font-size:13px; font-family:inherit; }
  input:focus, textarea:focus, select:focus { outline:none; border-color:var(--acc); }
  .row { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  .row3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; }
  button { cursor:pointer; border:none; border-radius:8px; padding:9px 14px; font-size:13px; font-weight:500; background:var(--acc); color:#0b1220; }
  button.ghost { background:transparent; border:1px solid var(--line); color:var(--fg); }
  button.danger { background:transparent; border:1px solid #5b2330; color:var(--err); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .btns { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:9px 8px; border-bottom:1px solid var(--line); font-size:13px; vertical-align:middle; }
  th { color:var(--mut); font-weight:500; font-size:12px; }
  .tag { display:inline-block; padding:1px 7px; border-radius:999px; font-size:11px; }
  .tag.def { background:rgba(110,168,254,.15); color:var(--acc); }
  .tag.on { background:rgba(74,222,128,.13); color:var(--ok); }
  .tag.off { background:rgba(248,113,113,.13); color:var(--err); }
  .mut { color:var(--mut); }
  .toast { position:fixed; right:16px; bottom:16px; background:var(--card); border:1px solid var(--line); padding:11px 15px; border-radius:10px; font-size:13px; max-width:320px; opacity:0; transform:translateY(8px); transition:.2s; pointer-events:none; }
  .toast.show { opacity:1; transform:none; }
  .toast.ok { border-color:#2b5d3a; } .toast.err { border-color:#5b2330; }
  code { background:#0f1219; padding:1px 6px; border-radius:5px; font-size:12px; }
  #chatOut { white-space:pre-wrap; background:#0f1219; border:1px solid var(--line); border-radius:8px; padding:12px; min-height:64px; font-size:13px; line-height:1.6; }
  .hint { font-size:12px; color:var(--mut); margin-top:6px; line-height:1.5; }
</style>
</head>
<body>
<header>
  <h1>🎛️ 模型管理</h1>
  <span class="mut">MCP Gateway · 多模型注册与切换</span>
  <span style="flex:1"></span>
  <span class="mut" id="baseHint"></span>
</header>
<div class="wrap">

  <div class="card">
    <h2>🔑 访问密钥（API_SECRET）</h2>
    <div class="row">
      <div>
        <input id="secret" type="password" placeholder="若服务端设了 API_SECRET，填这里"/>
        <div class="hint">保存在浏览器本地，用于调用 /api/models。没设 API_SECRET 可留空。</div>
      </div>
      <div style="display:flex;align-items:flex-start;gap:8px">
        <button onclick="saveSecret()">保存密钥</button>
        <button class="ghost" onclick="reload()">刷新列表</button>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>➕ 新增 / 编辑模型</h2>
    <div class="row3">
      <div>
        <label>ID（前端下拉框显示值，唯一）*</label>
        <input id="f_id" placeholder="如 gpt-4o / my-glm"/>
      </div>
      <div>
        <label>显示名 label</label>
        <input id="f_label" placeholder="如 GPT-4o（可留空）"/>
      </div>
      <div>
        <label>真实模型名 model *</label>
        <input id="f_model" placeholder="供应商实际的 model，如 gpt-4o"/>
      </div>
    </div>
    <div class="row">
      <div>
        <label>Base URL *</label>
        <input id="f_base" placeholder="如 https://api.openai.com/v1"/>
      </div>
      <div>
        <label>API Key *（编辑时留空=不改）</label>
        <input id="f_key" type="password" placeholder="sk-..."/>
      </div>
    </div>
    <div class="btns">
      <button onclick="saveModel()">💾 保存模型</button>
      <button class="ghost" onclick="clearForm()">清空表单</button>
    </div>
    <div class="hint">ID 是给前端选的名字；model 是真正发给上游的模型名。两者可相同。base_url 填到 <code>/v1</code> 或不带都行。</div>
  </div>

  <div class="card">
    <h2>📋 已注册模型</h2>
    <table>
      <thead><tr><th>ID / 显示名</th><th>真实模型</th><th>Base URL</th><th>Key</th><th>状态</th><th></th></tr></thead>
      <tbody id="tbody"><tr><td colspan="6" class="mut">加载中…</td></tr></tbody>
    </table>
  </div>

  <div class="card">
    <h2>💬 发消息实测</h2>
    <div class="row">
      <div>
        <label>选择模型</label>
        <select id="testModel"></select>
      </div>
      <div>
        <label>消息</label>
        <input id="testMsg" placeholder="你好" value="你好，用一句话介绍你自己"/>
      </div>
    </div>
    <div class="btns"><button onclick="testChat()" id="testBtn">🚀 发送（流式）</button></div>
    <label>回复</label>
    <div id="chatOut" class="mut">（结果显示在这里）</div>
  </div>

</div>
<div id="toast" class="toast"></div>

<script>
const $ = s => document.querySelector(s);
const ORIGIN = location.origin;
$("#baseHint").textContent = ORIGIN;

function getSecret(){ return localStorage.getItem("mcp_secret") || ""; }
function saveSecret(){ localStorage.setItem("mcp_secret", $("#secret").value.trim()); toast("密钥已保存", "ok"); reload(); }
function headers(json){
  const h = {}; if(json) h["Content-Type"]="application/json";
  const s = getSecret(); if(s){ h["Authorization"]="Bearer "+s; h["X-Api-Key"]=s; }
  return h;
}
function toast(msg, kind){
  const t=$("#toast"); t.textContent=msg; t.className="toast show "+(kind||"");
  setTimeout(()=>{ t.className="toast "+(kind||""); }, 2600);
}

async function reload(){
  try{
    const r = await fetch(ORIGIN+"/api/models", {headers:headers(false)});
    if(r.status===401){ renderErr("鉴权失败：请填写正确的 API_SECRET"); return; }
    const d = await r.json();
    renderTable(d.models||[], d.default||"");
    fillTestSelect(d.models||[], d.default||"");
  }catch(e){ renderErr("加载失败："+e.message); }
}
function renderErr(m){ $("#tbody").innerHTML = '<tr><td colspan="6" class="mut">'+m+'</td></tr>'; }

function renderTable(models, def){
  if(!models.length){ $("#tbody").innerHTML='<tr><td colspan="6" class="mut">还没有模型，用上面表单添加一个吧</td></tr>'; return; }
  $("#tbody").innerHTML = models.map(m=>{
    const isDef = m.id===def;
    const st = m.enabled ? '<span class="tag on">启用</span>' : '<span class="tag off">停用</span>';
    const dt = isDef ? ' <span class="tag def">默认</span>' : '';
    return `<tr>
      <td><b>${esc(m.id)}</b>${dt}<br><span class="mut">${esc(m.label||"")}</span></td>
      <td>${esc(m.model||"")}</td>
      <td class="mut" style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${esc(m.base_url||"")}</td>
      <td class="mut">${esc(m.api_key_masked||(m.has_key?"***":"—"))}</td>
      <td>${st}</td>
      <td style="white-space:nowrap">
        <button class="ghost" onclick='editRow(${JSON.stringify(m).replace(/'/g,"&#39;")})'>编辑</button>
        ${isDef?'':`<button class="ghost" onclick="setDefault('${esc(m.id)}')">设默认</button>`}
        <button class="danger" onclick="delModel('${esc(m.id)}')">删除</button>
      </td></tr>`;
  }).join("");
}
function fillTestSelect(models, def){
  const sel=$("#testModel");
  sel.innerHTML = models.filter(m=>m.enabled).map(m=>`<option value="${esc(m.id)}" ${m.id===def?"selected":""}>${esc(m.label||m.id)}</option>`).join("");
}
function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function editRow(m){
  $("#f_id").value=m.id||""; $("#f_label").value=m.label||"";
  $("#f_model").value=m.model||""; $("#f_base").value=m.base_url||""; $("#f_key").value="";
  toast("已载入到表单，Key 留空表示不修改", "ok");
  window.scrollTo({top:0,behavior:"smooth"});
}
function clearForm(){ ["f_id","f_label","f_model","f_base","f_key"].forEach(i=>$("#"+i).value=""); }

async function saveModel(){
  const body = {
    id: $("#f_id").value.trim(), label: $("#f_label").value.trim(),
    model: $("#f_model").value.trim(), base_url: $("#f_base").value.trim(),
    api_key: $("#f_key").value.trim(), enabled: true,
  };
  if(!body.id){ toast("请填 ID", "err"); return; }
  try{
    const r = await fetch(ORIGIN+"/api/models", {method:"POST", headers:headers(true), body:JSON.stringify(body)});
    const d = await r.json();
    if(r.ok && d.ok){ toast("已保存 "+body.id, "ok"); clearForm(); reload(); }
    else toast("保存失败："+(d.error||JSON.stringify(d)), "err");
  }catch(e){ toast("请求失败："+e.message, "err"); }
}
async function setDefault(id){
  try{
    const r = await fetch(ORIGIN+"/api/models", {method:"POST", headers:headers(true), body:JSON.stringify({action:"set_default", id})});
    const d = await r.json();
    if(r.ok && d.ok){ toast("默认已设为 "+id, "ok"); reload(); } else toast("失败："+(d.error||""), "err");
  }catch(e){ toast("请求失败："+e.message, "err"); }
}
async function delModel(id){
  if(!confirm("确认删除模型 "+id+" ？（只删注册表这一条，不影响其它数据）")) return;
  try{
    const r = await fetch(ORIGIN+"/api/models", {method:"DELETE", headers:headers(true), body:JSON.stringify({id})});
    const d = await r.json();
    if(r.ok && d.ok){ toast("已删除 "+id, "ok"); reload(); } else toast("删除失败："+(d.error||""), "err");
  }catch(e){ toast("请求失败："+e.message, "err"); }
}

async function testChat(){
  const model = $("#testModel").value;
  const msg = $("#testMsg").value.trim() || "你好";
  const out = $("#chatOut"); out.className=""; out.textContent="";
  $("#testBtn").disabled = true;
  try{
    const r = await fetch(ORIGIN+"/v1/chat/completions", {
      method:"POST", headers:headers(true),
      body: JSON.stringify({model, messages:[{role:"user",content:msg}], stream:true}),
    });
    if(!r.ok){ out.textContent = "HTTP "+r.status+" "+(await r.text()); $("#testBtn").disabled=false; return; }
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf="";
    while(true){
      const {value, done} = await reader.read(); if(done) break;
      buf += dec.decode(value, {stream:true});
      let idx;
      while((idx = buf.indexOf("\n\n")) >= 0){
        const chunk = buf.slice(0, idx); buf = buf.slice(idx+2);
        for(const line of chunk.split("\n")){
          const s = line.trim(); if(!s.startsWith("data:")) continue;
          const data = s.slice(5).trim(); if(data==="[DONE]") continue;
          try{ const j=JSON.parse(data); const dc=j.choices&&j.choices[0]&&j.choices[0].delta&&j.choices[0].delta.content; if(dc) out.textContent += dc; }catch(_){}
        }
      }
    }
    if(!out.textContent) out.textContent="（无内容返回）";
  }catch(e){ out.textContent = "请求失败："+e.message; }
  $("#testBtn").disabled = false;
}

$("#secret").value = getSecret();
reload();
</script>
</body>
</html>"""
