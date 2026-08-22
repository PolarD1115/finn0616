# 天气工具缝合任务（喂给 Kimi-k2.6 的分步执行 prompt）

> 这是一份**自包含**的分步执行指令。你（Kimi-k2.6）按 Step 1→6 顺序执行，每步做完用「验证」自检后再做下一步。所有路径以 Windows 绝对路径为准。项目根 = `C:\Users\钟梓昕\Desktop\rikkahub\新网关`。
>
> 背景：把基于 wttr.in 的天气工具缝合进新网关。要求：① 后台"查天气"活动能调真实天气工具；② 聊天命中天气关键词时网关主动拉天气注入 prompt（保流式）；③ 注册为 MCP 工具供客户端调用；④ 默认定位=用户昕的最新 GPS（device_data 表 lat/lon），无 GPS 回退 `WEATHER_DEFAULT_CITY=韶关`；⑤ 可传 city 查指定城市；⑥ 虚拟小屋 weather 字段与用户定位一致。
>
 wttr.in 支持坐标查法：`https://wttr.in/24.8,113.6?format=j1`。

---

## Step 1：新建 `新网关\weather_tools.py`（完整文件，直接 Write）

把下面**整段**写进 `C:\Users\钟梓昕\Desktop\rikkahub\新网关\weather_tools.py`（新文件）。这是移植+GPS 适配后的自包含版本，**不 import server、不调任何外部 helper**，GPS 查询用传入的 sb 客户端直接查 device_data 表。

```python
"""
网关内置天气工具（wttr.in，无需 API Key）
================================================
- 数据源: https://wttr.in
- 默认定位：用户最新 GPS（device_data 表 lat/lon）→ wttr.in/{lat},{lon}
- 回退：WEATHER_DEFAULT_CITY 环境变量（=韶关）
- 可选 city 入参查指定城市
- 供 gateway 关键词注入 / tool_loop 查天气活动 / server MCP 工具调用

环境变量：
  WEATHER_TOOLS_ENABLED=true|false   默认 true
  WEATHER_DEFAULT_CITY=韶关           GPS 缺失回退城市
  WEATHER_TIMEOUT_SEC=12             请求超时
  WEATHER_TOOL_MAX_ROUNDS=3          网页聊天 tool loop 上限
  WEATHER_TOOL_LOOP=false            网页聊天 tool loop 开关
  WEATHER_KEYWORD_INJECT=true        关键词自动注入开关
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_CITY = os.environ.get("WEATHER_DEFAULT_CITY", "Beijing").strip() or "Beijing"
TIMEOUT_SEC = float(os.environ.get("WEATHER_TIMEOUT_SEC", "12") or "12")
USER_AGENT = "weather-tools/1.2.0 (mcp-gateway)"


# ---------------------------------------------------------------------------
# OpenAI Chat Completions tools schema
# ---------------------------------------------------------------------------
OPENAI_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "查询指定城市的当前详细天气信息，包括温度、体感温度、湿度、风速风向、"
                "能见度、气压、紫外线指数、云量、降水量等。"
                "用户提到外面、出门、冷热、穿什么、带不带伞、多少度时优先调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "留空=用户当前定位（自动取最新GPS），填写城市名可查指定城市",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather_brief",
            "description": "查询指定城市的简要天气信息，返回一行简短描述。适合只需快速一句话天气时。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": f"留空=用户当前定位，填写城市名可查指定城市（默认 {DEFAULT_CITY}）",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather_forecast",
            "description": (
                "查询指定城市未来1-3天的天气预报，包含每日最高最低温度、降雨降雪概率、"
                "日出日落、月相以及逐小时天气详情。用户问明天/后天/这几天天气时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "留空=用户当前定位，填写城市名可查指定城市",
                    },
                    "days": {
                        "type": "integer",
                        "description": "预报天数，1-3，默认3",
                        "minimum": 1,
                        "maximum": 3,
                    },
                },
                "required": [],
            },
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in OPENAI_TOOLS}


def enabled() -> bool:
    return os.environ.get("WEATHER_TOOLS_ENABLED", "true").strip().lower() == "true"


def _http_get_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        raw = resp.read()
        charset = "utf-8"
        try:
            charset = resp.headers.get_content_charset() or "utf-8"
        except Exception:
            pass
        return raw.decode(charset, errors="replace")


def _http_get_json(url: str) -> Dict[str, Any]:
    return json.loads(_http_get_text(url))


# ---------------------------------------------------------------------------
# 定位解析（GPS 优先，回退城市；自包含，不 import server）
# ---------------------------------------------------------------------------
def _fetch_latest_gps(sb) -> Optional[Tuple[float, float]]:
    """直接用 sb 客户端查 device_data 最新一条定位。不依赖任何外部 helper。"""
    if sb is None:
        return None
    try:
        res = sb.table("device_data").select(
            "location_latitude, location_longitude, timestamp"
        ).order("timestamp", desc=True).limit(1).execute()
        if res and res.data:
            row = res.data[0]
            lat = row.get("location_latitude")
            lon = row.get("location_longitude")
            if lat is not None and lon is not None:
                try:
                    return (float(lat), float(lon))
                except Exception:
                    return None
    except Exception:
        return None
    return None


def _resolve_location(city: Optional[str], sb=None) -> Tuple[str, str]:
    """
    返回 (kind, value)：
      ("city", "韶关")          — 城市名查法
      ("coords", "24.8,113.6")  — GPS 坐标查法
    优先级：显式 city > sb 的 device_data GPS > WEATHER_DEFAULT_CITY。
    """
    c = (city or "").strip()
    if c:
        return ("city", c)
    gps = _fetch_latest_gps(sb)
    if gps:
        return ("coords", f"{gps[0]},{gps[1]}")
    return ("city", DEFAULT_CITY)


def _build_url(loc_value: str, fmt: str = "j1") -> str:
    return f"https://wttr.in/{urllib.parse.quote(loc_value, safe=',')}?format={fmt}"


def get_weather(city: Optional[str] = None, sb=None) -> Dict[str, Any]:
    kind, value = _resolve_location(city, sb)
    url = _build_url(value, "j1")
    try:
        data = _http_get_json(url)
    except Exception as e:
        return {"success": False, "error": f"Failed to fetch weather: {e}", "city": value, "location_source": kind}

    current_list = data.get("current_condition") or []
    if not current_list:
        return {"success": False, "error": "No current_condition in response", "city": value, "location_source": kind}
    current = current_list[0]

    def _desc(obj: Dict[str, Any]) -> str:
        try:
            return (obj.get("weatherDesc") or [{}])[0].get("value") or ""
        except Exception:
            return ""

    # GPS 坐标查询时，从 nearestArea 取最近区域名做显示
    display_city = value
    try:
        na = data.get("nearestArea") or []
        if na and isinstance(na, list) and isinstance(na[0], dict):
            an = na[0].get("areaName") or []
            if isinstance(an, list) and an and isinstance(an[0], dict):
                display_city = an[0].get("value", value) or value
    except Exception:
        display_city = value

    return {
        "success": True,
        "city": display_city,
        "location_source": kind,
        "description": _desc(current),
        "temperature": f"{current.get('temp_C')}°C",
        "feels_like": f"{current.get('FeelsLikeC')}°C",
        "humidity": f"{current.get('humidity')}%",
        "wind_speed": f"{current.get('windspeedKmph')} km/h",
        "wind_direction": current.get("winddir16Point") or "",
        "visibility": f"{current.get('visibility')} km",
        "pressure": f"{current.get('pressure')} hPa",
        "uv_index": current.get("uvIndex"),
        "cloud_cover": f"{current.get('cloudcover')}%",
        "precipitation": f"{current.get('precipMM')} mm",
    }


def get_weather_brief(city: Optional[str] = None, sb=None) -> Dict[str, Any]:
    kind, value = _resolve_location(city, sb)
    url = _build_url(value, "%C+%t+%h+%w")
    try:
        body = _http_get_text(url)
    except Exception as e:
        return {"success": False, "error": f"Failed to fetch weather: {e}", "city": value, "location_source": kind}
    return {"success": True, "city": value, "location_source": kind, "brief": (body or "").strip()}


def get_weather_forecast(city: Optional[str] = None, days: Optional[int] = None, sb=None) -> Dict[str, Any]:
    kind, value = _resolve_location(city, sb)
    n = 3 if days is None else int(days)
    if n > 3:
        n = 3
    if n < 1:
        n = 1
    url = _build_url(value, "j1")
    try:
        data = _http_get_json(url)
    except Exception as e:
        return {"success": False, "error": f"Failed to fetch weather forecast: {e}", "city": value, "location_source": kind}

    weather_list = data.get("weather") or []
    forecasts: List[Dict[str, Any]] = []
    for i in range(min(n, len(weather_list))):
        day = weather_list[i]
        astro = None
        if day.get("astronomy"):
            astro = day["astronomy"][0]
        hourly_temps = []
        for h in day.get("hourly") or []:
            desc = ""
            try:
                desc = (h.get("weatherDesc") or [{}])[0].get("value") or ""
            except Exception:
                pass
            hourly_temps.append({
                "time": h.get("time"),
                "temp": f"{h.get('tempC')}°C",
                "description": desc,
                "chance_of_rain": f"{h.get('chanceofrain')}%",
                "humidity": f"{h.get('humidity')}%",
                "wind_speed": f"{h.get('windspeedKmph')} km/h",
            })
        hourly_list = day.get("hourly") or []
        midday = hourly_list[4] if len(hourly_list) > 4 else (hourly_list[0] if hourly_list else None)
        midday_desc = ""
        if midday:
            try:
                midday_desc = (midday.get("weatherDesc") or [{}])[0].get("value") or ""
            except Exception:
                midday_desc = ""
        forecasts.append({
            "date": day.get("date"),
            "max_temp": f"{day.get('maxtempC')}°C",
            "min_temp": f"{day.get('mintempC')}°C",
            "avg_temp": f"{day.get('avgtempC')}°C",
            "total_snow": f"{day.get('totalSnow_cm')} cm",
            "sun_hour": day.get("sunHour"),
            "uv_index": day.get("uvIndex"),
            "chance_of_rain": f"{midday.get('chanceofrain')}%" if midday else "N/A",
            "chance_of_snow": f"{midday.get('chanceofsnow')}%" if midday else "N/A",
            "description": midday_desc,
            "astronomy": ({
                "sunrise": astro.get("sunrise"),
                "sunset": astro.get("sunset"),
                "moonrise": astro.get("moonrise"),
                "moonset": astro.get("moonset"),
                "moon_phase": astro.get("moon_phase"),
            } if astro else None),
            "hourly": hourly_temps,
        })
    return {"success": True, "city": value, "location_source": kind, "forecast_days": n, "forecasts": forecasts}


def execute_tool(name: str, arguments: Any, sb=None) -> Dict[str, Any]:
    """执行单个工具。arguments 可以是 dict 或 JSON 字符串。"""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except Exception:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    if name == "get_weather":
        return get_weather(arguments.get("city"), sb)
    if name == "get_weather_brief":
        return get_weather_brief(arguments.get("city"), sb)
    if name == "get_weather_forecast":
        return get_weather_forecast(arguments.get("city"), arguments.get("days"), sb)
    return {"success": False, "error": f"Unknown tool: {name}"}


def execute_tool_json(name: str, arguments: Any, sb=None) -> str:
    try:
        result = execute_tool(name, arguments, sb)
    except Exception as e:
        result = {"success": False, "error": str(e)}
    return json.dumps(result, ensure_ascii=False)


def merge_tools_into_request(req_data: Dict[str, Any]) -> bool:
    """把天气 tools 合并进 OpenAI chat.completions 请求。返回是否注入了任意内置天气 tool。"""
    if not enabled():
        return False
    existing = req_data.get("tools")
    if not isinstance(existing, list):
        existing = []
    existing_names = set()
    for t in existing:
        try:
            if t.get("type") == "function":
                existing_names.add(t["function"]["name"])
        except Exception:
            pass
    added = False
    merged = list(existing)
    for tool in OPENAI_TOOLS:
        name = tool["function"]["name"]
        if name not in existing_names:
            merged.append(tool)
            added = True
    if merged:
        req_data["tools"] = merged
        if "tool_choice" not in req_data:
            req_data["tool_choice"] = "auto"
    return added


def is_weather_tool_call(tool_call: Dict[str, Any]) -> bool:
    try:
        return (tool_call.get("function") or {}).get("name") in TOOL_NAMES
    except Exception:
        return False


def run_tool_calls(tool_calls: List[Dict[str, Any]], sb=None) -> List[Dict[str, Any]]:
    """执行一批 tool_calls，返回 role=tool 的 messages 列表。未知工具返回 error JSON，不抛异常。"""
    out: List[Dict[str, Any]] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name") or "unknown"
        args = fn.get("arguments") or "{}"
        content = execute_tool_json(name, args, sb)
        msg: Dict[str, Any] = {
            "role": "tool",
            "tool_call_id": tc.get("id") or name,
            "content": content,
        }
        msg["name"] = name
        out.append(msg)
    return out


def extract_tool_calls_from_message(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    tcs = message.get("tool_calls")
    if isinstance(tcs, list) and tcs:
        return tcs
    return []


def max_tool_rounds() -> int:
    try:
        n = int(os.environ.get("WEATHER_TOOL_MAX_ROUNDS", "3") or "3")
    except Exception:
        n = 3
    return max(1, min(n, 6))


def brief_text(w: Dict[str, Any]) -> str:
    """把 get_weather 结果压成一行简短文本，供注入 prompt / house_do weather 字段。"""
    if not w or not w.get("success"):
        return "天气未知"
    return (
        f"{w.get('city','?')} {w.get('description','')} "
        f"{w.get('temperature','')} 体感{w.get('feels_like','')} "
        f"湿度{w.get('humidity','')} {w.get('wind_direction','')}{w.get('wind_speed','')}"
    ).strip()
```

**验证**：`python -c "import weather_tools; print(weather_tools.get_weather())"` → 应返回 `success:True`（无 sb 时回退 `WEATHER_DEFAULT_CITY`，见 Step 6 先设环境变量）。

---

## Step 2：改 `新网关\server.py`（注册 2 个 MCP 工具）

### 2a. 顶部软导入 weather_tools
在 `server.py` 顶部 import 区（`from mcp.server.fastmcp import FastMCP` 附近）加：
```python
# 🌤️ 天气工具（软导入：漏传 weather_tools.py 时降级，不影响启动）
try:
    import weather_tools  # type: ignore
    _HAS_WEATHER_TOOLS = True
except ImportError:
    weather_tools = None  # type: ignore
    _HAS_WEATHER_TOOLS = False
    print("[Weather] 未找到 weather_tools.py，天气 MCP 工具已降级关闭")
```

### 2b. 在 `where_is_user` 函数**之后**（搜 `async def where_is_user` 定位，它结尾后）插入两个 MCP 工具
```python
@mcp.tool()
@mcp_error_handler
async def query_weather(city: str = ""):
    """【查天气】查当前详细天气。city 留空=用户当前定位（自动取最新GPS），填城市名可查指定城市。"""
    if not _HAS_WEATHER_TOOLS or weather_tools is None:
        return "❌ weather_tools 未部署到容器"
    try:
        r = await asyncio.to_thread(weather_tools.get_weather, city or None, supabase)
    except Exception as e:
        return f"❌ 天气查询失败: {e}"
    if not r.get("success"):
        return f"❌ {r.get('error', '查询失败')}"
    return (
        f"🌤️ {r.get('city','?')}（{r.get('location_source','?')}）\n"
        f"天气: {r.get('description','')}\n"
        f"温度: {r.get('temperature','')} 体感{r.get('feels_like','')}\n"
        f"湿度: {r.get('humidity','')} 气压: {r.get('pressure','')}\n"
        f"风: {r.get('wind_direction','')}{r.get('wind_speed','')}\n"
        f"能见度: {r.get('visibility','')} 紫外线: {r.get('uv_index','?')}\n"
        f"云量: {r.get('cloud_cover','')} 降水: {r.get('precipitation','')}"
    )


@mcp.tool()
@mcp_error_handler
async def query_weather_forecast(city: str = "", days: int = 3):
    """【查天气预报】查未来1-3天天气。city 留空=用户当前定位。"""
    if not _HAS_WEATHER_TOOLS or weather_tools is None:
        return "❌ weather_tools 未部署到容器"
    try:
        r = await asyncio.to_thread(weather_tools.get_weather_forecast, city or None, days, supabase)
    except Exception as e:
        return f"❌ 天气预报失败: {e}"
    if not r.get("success"):
        return f"❌ {r.get('error', '查询失败')}"
    lines = [f"📅 {r.get('city','?')} 未来{r.get('forecast_days','?')}天预报"]
    for f in r.get("forecasts", []):
        ast = f.get("astronomy") or {}
        lines.append(
            f"\n• {f.get('date','')} {f.get('description','')} "
            f"{f.get('min_temp','')}~{f.get('max_temp','')} "
            f"降雨{f.get('chance_of_rain','')} 日出{ast.get('sunrise','?')} 日落{ast.get('sunset','?')}"
        )
    return "\n".join(lines)
```
> 注意：这两个 wrapper **纯转发**给 `weather_tools.get_weather` / `get_weather_forecast`，GPS 解析全在 weather_tools 内部，server.py 不单独调 `_get_latest_gps_record`。

**验证**：`python -c "import server"` 无报错。

---

## Step 3：改 `新网关\tool_loop.py`（注册工具 + 查天气专用路径）

### 3a. 顶部软导入 weather_tools
在 `tool_loop.py` 顶部 import 区加：
```python
# 🌤️ 天气工具（软导入）
try:
    import weather_tools  # type: ignore
    _HAS_WEATHER_TOOLS = True
except ImportError:
    weather_tools = None  # type: ignore
    _HAS_WEATHER_TOOLS = False
```

### 3b. 在 `TOOL_REGISTRY` 字典里（搜 `"house_do": {` 附近，紧接 `house_do` 条目之后）追加三个天气工具条目
```python
    # ---------- 天气 ----------
    "get_weather": {
        "description": "查当前详细天气（温度/体感/湿度/风/降水等）。city 留空=用户当前定位（自动取最新GPS）",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "留空=用户当前定位，填城市名可查指定城市"},
            },
            "required": [],
        },
        "callable": weather_tools.get_weather if weather_tools else None,
        "fixed_args": {},
    },
    "get_weather_brief": {
        "description": "查一行简短天气描述。city 留空=用户当前定位",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "留空=用户当前定位，填城市名可查指定城市"},
            },
            "required": [],
        },
        "callable": weather_tools.get_weather_brief if weather_tools else None,
        "fixed_args": {},
    },
    "get_weather_forecast": {
        "description": "查未来1-3天天气预报。city 留空=用户当前定位",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "留空=用户当前定位，填城市名可查指定城市"},
                "days": {"type": "integer", "description": "预报天数1-3", "minimum": 1, "maximum": 3},
            },
            "required": [],
        },
        "callable": weather_tools.get_weather_forecast if weather_tools else None,
        "fixed_args": {},
    },
```
> 注意：`call_tool` 已自动把 sync 函数走 `asyncio.to_thread`（见 `call_tool` 里 `inspect.iscoroutinefunction` 分支），无需额外处理。但天气工具需要 sb 做 GPS——`call_tool` 走的 `fn(**full_args)` 不会传 sb，所以查天气**真实 GPS 数据走 3c 的专用路径**，不靠模型 function-call。

### 3c. `ACTIVITY_TOOL_MAP["查天气"]` 由 `[]` 改为有工具
搜 `"查天气":      [],` 替换为：
```python
    "查天气":      ["get_weather", "get_weather_forecast", "get_weather_brief"],
```

### 3d. 新增查天气专用确定性函数 + 分支
在 `tool_loop.py` 的 `run_free_activity_tool_loop` 函数**之前**（即 `async def run_free_activity_tool_loop` 这一行的上方）新增这个独立函数：
```python
async def _finalize_weather_activity(client, ask_llm, system_ctx, log_draft, now_bj, log_prefix="🎈 [自由活动·工具循环]"):
    """查天气专用确定性路径：拉真实天气（用户GPS）→ 注入 → 生成log → 落虚拟小屋。
    不依赖 FREE_ACTIVITY_TOOL_LOOP 开关，保证查天气后台活动始终用真实天气。"""
    if not _HAS_WEATHER_TOOLS or weather_tools is None:
        # 退化：用草稿
        return ("查天气", log_draft) if log_draft else None

    import server as _srv
    sb = getattr(_srv, "supabase", None)

    weather_data = None
    try:
        weather_data = await asyncio.wait_for(
            asyncio.to_thread(weather_tools.get_weather, None, sb), timeout=8
        )
    except Exception as e:
        print(f"{log_prefix} [查天气] 拉取失败: {e}")

    wbrief = weather_tools.brief_text(weather_data) if weather_tools else "天气未知"
    if weather_data and weather_data.get("success"):
        weather_hint = f"窗外真实天气（用户当前定位）：{wbrief}"
    else:
        weather_hint = "（天气拉取失败，凭想象写）"

    now_str = now_bj.strftime("%Y-%m-%d %H:%M")
    prompt = f"""
现在是 {now_str}。你刚走到阳台看了一眼外面。
{weather_hint}

写一条日记。150-250字，第一人称。你看见什么光、皮肤感觉、空气味道、听见什么。
可以想到她，但不要硬凑。禁用"阳光洒进""微风拂过"这类套话，写真实感官。
只输出日记内容本身，不要 JSON、引号或前缀。
"""
    try:
        raw = await ask_llm(client, prompt, system_prompt=system_ctx, temperature=0.85)
    except Exception:
        raw = ""
    final_log = (raw or "").strip() or log_draft
    if not final_log:
        return None

    # 落虚拟小屋：weather 用真实天气（来自用户GPS），保证与用户定位一致
    try:
        _hs.house_do(room_id="balcony", entry_type="看天气",
                     content=final_log, weather=wbrief, mood="惬意")
        print(f"{log_prefix} [查天气] 已落小屋阳台·看天气（weather={wbrief[:30]}）")
    except Exception as e:
        print(f"{log_prefix} [查天气] 落小屋失败: {e}")

    return ("查天气", final_log)
```

然后在 `run_free_activity_tool_loop` 函数体内，**阶段1解析出 activity 之后、灰度闸门之前**插入分支。
精确锚点：找到这两行——
```python
    if activity not in _VALID_ACTIVITY_NAMES:
        activity = random.choice([n for n, _ in _FREE_ACTIVITIES if n != avoid])
        print(f"{log_prefix} 阶段1 未按格式选活动，兜底: {activity}")
```
在这三行**之后**、`# 灰度判断` 注释那行**之前**，插入：
```python
    # 🌤️ 查天气专用确定性路径：始终拉真实天气 + 落小屋，不依赖 TOOL_LOOP 开关
    if activity == "查天气":
        return await _finalize_weather_activity(client, ask_llm, system_ctx, log_draft, now_bj, log_prefix)
```

**验证**：`python -c "import tool_loop; print('ok')"` 无报错；`grep` 确认 `ACTIVITY_TOOL_MAP` 里查天气不再是空列表。

---

## Step 4：改 `新网关\gateway.py`（关键词自动注入 + 可选 tool loop）

### 4a. 顶部软导入 weather_tools
在 `gateway.py` 顶部软导入区（`_HAS_EVENTIDE` 那块之后）加：
```python
# 🆕 天气工具（橘瓣插件移植）：关键词注入 + 可选 tool loop
# 软导入：漏传 weather_tools.py 时网关仍可启动
try:
    import weather_tools  # type: ignore
    _HAS_WEATHER_TOOLS = True
except ImportError:
    weather_tools = None  # type: ignore
    _HAS_WEATHER_TOOLS = False
    print("[Weather] 未找到 weather_tools.py，天气关键词注入已降级关闭")

# 天气关键词（子串匹配；"好热啊"/"好冷啊"可命中）
_WEATHER_KEYWORDS = ("天气", "几度", "下雨", "下雪", "出门", "带伞", "穿什么",
                     "冷不冷", "热不热", "气温", "多少度", "会不会下雨", "好热", "好冷")


def _weather_keyword_hit(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k in text or k in t for k in _WEATHER_KEYWORDS)
```

### 4b. 关键词自动注入到 `_inject_context` 的 volatile_block
在 `gateway.py` 搜 `_inject_context`，找到 volatile_block 组装结尾，精确锚点为：
```python
            f"📡 当前聊天渠道：{channel_display}"
        )
```
在这段 `)` **之后**（即 volatile_block 拼完实时状态后）、`# ① 注入稳定前缀到 system` 这行**之前**，插入：
```python
        # 🌤️ 关键词命中：主动拉真实天气（用户GPS）注入 volatile_block，保流式零中断
        if (_HAS_WEATHER_TOOLS and weather_tools is not None
                and os.environ.get("WEATHER_KEYWORD_INJECT", "true").strip().lower() == "true"
                and _weather_keyword_hit(current_query) and sb):
            try:
                _w = await asyncio.wait_for(
                    asyncio.to_thread(weather_tools.get_weather, None, sb), timeout=6
                )
                if _w.get("success"):
                    volatile_block += (
                        f"\n🌤️ 实时天气（用户当前定位）: "
                        f"{_w.get('city','?')} {_w.get('description','')} "
                        f"{_w.get('temperature','')} 体感{_w.get('feels_like','')} "
                        f"湿度{_w.get('humidity','')} "
                        f"{_w.get('wind_direction','')}{_w.get('wind_speed','')}"
                    )
                    _log(f"🌤️ [Weather] 关键词命中，已注入天气（{_w.get('city','?')}）")
            except Exception as e:
                _log(f"⚠️ [Weather] 关键词天气注入失败: {e}")
```

### 4c. 可选 tool loop（默认关）—— 在 `_handle_chat` 注入 schema + 分流
在 `gateway.py` 搜 `_handle_chat`，找到 `_inject_context` 调用之后、`# 强制流式` 之前的位置。
精确锚点：
```python
        else:
            if sb:
                _log("➡️ [透传] 无 user 消息或无 Supabase，直接转发")

        # 强制流式（便于边透传边收集）
        req_data["stream"] = True
```
在 `➡️ [透传]` 块**之后**、`# 强制流式` **之前**，插入：
```python
        # 🌤️ 可选天气 tool loop（默认关 WEATHER_TOOL_LOOP=false）：开启时注入 schema + 走本地 function-call 循环
        _weather_loop = (
            _HAS_WEATHER_TOOLS and weather_tools is not None and weather_tools.enabled()
            and os.environ.get("WEATHER_TOOL_LOOP", "false").strip().lower() in ("1", "true", "yes")
        )
        if _weather_loop:
            try:
                weather_tools.merge_tools_into_request(req_data)
            except Exception as e:
                _log(f"⚠️ [Weather] tools 注入失败: {e}")
                _weather_loop = False
            if _weather_loop and req_data.get("tools"):
                await self._handle_chat_with_tool_loop(scope, send, req_data, upstream_url, upstream_key, sb, user_msg)
                return
        # 关闭时：不注入 tools，纯流式透传（天气已由关键词注入到 volatile_block）
```

### 4d. 新增三个 tool loop 方法到 `HostFixMiddleware` 类
在 `gateway.py` 搜 `async def _save_conversation`，在它**之前**（仍在 `HostFixMiddleware` 类内）插入下面三个方法。
> 注意：新网关 `_handle_chat` 用 `requests`（不是 httpx），所以这里用 `requests` + `asyncio.to_thread`，**不要**用补丁里的 httpx 版本。

```python
    # ------------------------------------------
    # 🌤️ 天气 tools 循环（OpenAI function calling，可选）
    # ------------------------------------------

    async def _handle_chat_with_tool_loop(self, scope, send, req_data, upstream_url, upstream_key, sb, user_msg):
        """
        OpenAI tools 循环：模型 tool_call → 网关本地执行 weather_tools → 回灌 role=tool → 再请求，
        直到模型给出最终文本，再以 SSE 形式回给客户端。兼容现有流式前端。
        """
        import copy
        if not _HAS_WEATHER_TOOLS or weather_tools is None:
            await self._sse_plain_error(send, req_data, "[Weather] weather_tools 未加载")
            return

        max_rounds = weather_tools.max_tool_rounds()
        client_headers = {
            k.decode("utf-8", "ignore").lower(): v.decode("utf-8", "ignore")
            for k, v in scope.get("headers", [])
        }
        client_ua = client_headers.get("user-agent", "")
        fwd_headers = {
            "Authorization": f"Bearer {upstream_key}",
            "Content-Type": "application/json",
            "User-Agent": client_ua or "Mozilla/5.0 (compatible; mcp-gateway-weather/1.2)",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

        messages = list(req_data.get("messages") or [])
        base_payload = copy.deepcopy(req_data)
        base_payload.pop("stream", None)

        collected_content = ""
        collected_reasoning = ""
        tool_calls_dict = {}
        final_text = ""

        for round_i in range(max_rounds):
            payload = copy.deepcopy(base_payload)
            payload["messages"] = messages
            payload["stream"] = False
            if payload.get("tools") and "tool_choice" not in payload:
                payload["tool_choice"] = "auto"

            _log(f"🌤️ [WeatherLoop] round={round_i + 1}/{max_rounds} messages={len(messages)}")

            try:
                def _do_post():
                    return requests.post(upstream_url, headers=fwd_headers, json=payload, timeout=120)
                resp = await asyncio.to_thread(_do_post)
                status = resp.status_code
                text = resp.text
            except Exception as e:
                _log(f"❌ [WeatherLoop] 上游请求失败: {e}")
                await self._sse_plain_error(send, req_data, f"[连接错误] {e}")
                return

            if status != 200:
                _log(f"❌ [WeatherLoop] 上游 HTTP {status}: {text[:300]}")
                await self._sse_plain_error(send, req_data, f"[上游错误 HTTP {status}] {text[:200]}")
                return

            try:
                data = json.loads(text)
            except Exception:
                await self._sse_plain_error(send, req_data, "[上游错误] 非 JSON 响应")
                return

            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []

            if message.get("reasoning_content"):
                collected_reasoning += str(message.get("reasoning_content") or "")

            if tool_calls:
                assistant_msg = {"role": "assistant", "tool_calls": tool_calls}
                if content:
                    assistant_msg["content"] = content
                messages.append(assistant_msg)
                for tc in tool_calls:
                    tool_calls_dict[len(tool_calls_dict)] = tc
                try:
                    tool_msgs = weather_tools.run_tool_calls(tool_calls, sb)
                except Exception as e:
                    tool_msgs = [{
                        "role": "tool",
                        "tool_call_id": (tool_calls[0].get("id") if tool_calls else "err"),
                        "content": json.dumps({"success": False, "error": str(e)}, ensure_ascii=False),
                    }]
                for tm in tool_msgs:
                    messages.append(tm)
                    _log(f"🌤️ [WeatherLoop] executed {tm.get('name') or tm.get('tool_call_id')}")
                if round_i >= max_rounds - 1:
                    base_payload["tool_choice"] = "none"
                continue

            final_text = content or ""
            collected_content = final_text
            break
        else:
            final_text = collected_content or "（天气工具调用次数已达上限，请稍后再试）"

        await self._sse_final_text(send, req_data, final_text)

        if sb and user_msg and (collected_content or tool_calls_dict):
            asyncio.create_task(
                self._save_conversation(sb, user_msg, collected_content, collected_reasoning, tool_calls_dict)
            )

    async def _sse_final_text(self, send, req_data, text: str):
        """把最终文本包装成 OpenAI SSE，兼容现有流式客户端。"""
        model = req_data.get("model", "unknown")
        created = int(time.time())
        chunk_id = f"chatcmpl-weather-{created}"

        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"cache-control", b"no-cache, no-transform"),
                (b"connection", b"keep-alive"),
                (b"access-control-allow-origin", b"*"),
                (b"x-accel-buffering", b"no"),
            ],
        })

        def _chunk(delta, finish_reason=None):
            return (
                "data: " + json.dumps({
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                }, ensure_ascii=False) + "\n\n"
            ).encode("utf-8")

        await send({"type": "http.response.body", "body": _chunk({"role": "assistant"}), "more_body": True})
        if text:
            step = 400
            for i in range(0, len(text), step):
                await send({"type": "http.response.body", "body": _chunk({"content": text[i:i + step]}), "more_body": True})
        await send({"type": "http.response.body", "body": _chunk({}, "stop") + b"data: [DONE]\n\n", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _sse_plain_error(self, send, req_data, err: str):
        await self._sse_final_text(send, req_data, f"\n\n{err}")

```

### 4e.（可选）QQ/TG 渠道也加关键词注入
在 `server.py` 搜 `_build_channel_context`，在 `volatile_parts` 拼装结尾（`f"📡 当前聊天渠道：{_channel_display_name(channel_tag)}"` 那项**之后**）追加：
```python
    # 🌤️ 关键词命中：QQ/TG 渠道也注入真实天气
    try:
        import weather_tools as _wt
        if _wt.enabled() and _query_weather_hit(query):
            _w = await asyncio.to_thread(_wt.get_weather, None, supabase)
            if _w.get("success"):
                volatile_parts.append(
                    f"🌤️ 实时天气（用户当前定位）: {_w.get('city','?')} {_w.get('description','')} "
                    f"{_w.get('temperature','')} 体感{_w.get('feels_like','')} 湿度{_w.get('humidity','')}"
                )
    except Exception:
        pass
```
并在 `server.py` 加一个轻量 helper（放在 `_build_channel_context` 之前）：
```python
def _query_weather_hit(text: str) -> bool:
    if not text:
        return False
    for k in ("天气","几度","下雨","下雪","出门","带伞","穿什么","冷不冷","热不热","气温","多少度","好热","好冷"):
        if k in text:
            return True
    return False
```
> 注：`_build_channel_context` 是 async，上面用 `await asyncio.to_thread` 即可；若该函数是同步的就用纯 `weather_tools.get_weather(None, supabase)` 包 try。先读一眼函数签名确认 async/sync。

**验证**：`python -c "import gateway, server, tool_loop"` 三个都无报错。

---

## Step 5：更新 `新网关\VARIABLES.md`
在文件末尾追加：
```markdown
## 天气工具（wttr.in，无需 API Key）
| 变量 | 默认 | 说明 |
|---|---|---|
| WEATHER_TOOLS_ENABLED | true | 天气功能总开关 |
| WEATHER_DEFAULT_CITY | Beijing（建议设韶关） | GPS 缺失时回退城市 |
| WEATHER_TIMEOUT_SEC | 12 | wttr.in 请求超时秒 |
| WEATHER_KEYWORD_INJECT | true | 聊天命中天气关键词时主动注入（保流式） |
| WEATHER_TOOL_LOOP | false | 网页聊天天气 tool loop 开关（开则模型可 function-call 天气，轮次内非真流式） |
| WEATHER_TOOL_MAX_ROUNDS | 3 | tool loop 最大轮次 |
| FREE_ACTIVITY_TOOL_LOOP | false | 后台活动工具循环总开关（不影响查天气专用路径） |

定位策略：默认取 device_data 表最新 lat/lon → wttr.in/{lat},{lon}；无 GPS 回退 WEATHER_DEFAULT_CITY；city 入参可查任意城市。虚拟小屋"查天气"活动落 house_do 时 weather 字段取自用户 GPS 天气，与用户定位一致。
```

---

## Step 6：自测（严格按序）

1. **先验网络可达**：
   ```
   curl -4 "https://wttr.in/韶关?format=j1"
   ```
   返回 JSON（含 `current_condition`）才算通；不通就先解决网络（wttr.in 偶发 IPv6 问题，`-4` 强制 IPv4）。通了再往下。

2. **确认环境变量**：`WEATHER_DEFAULT_CITY` 已设成 `韶关`（不是默认 Beijing）。本地可在 `.env` 加 `WEATHER_DEFAULT_CITY=韶关`。

3. **单测 weather_tools**（无 sb，应回退韶关）：
   ```
   python -c "import weather_tools; import json; print(json.dumps(weather_tools.get_weather(), ensure_ascii=False, indent=2))"
   ```
   应见 `"success": true`、`"city"` 含韶关。

4. **单测指定城市**：
   ```
   python -c "import weather_tools; print(weather_tools.get_weather('北京')['city'])"
   ```

5. **import 无环检查**：
   ```
   python -c "import weather_tools, tool_loop, server, gateway; print('all ok')"
   ```
   无报错即过。

6. **查天气专用路径（需 supabase 配置）**：启动后台活动或直接调 `tool_loop._finalize_weather_activity`，确认日志含 `[查天气] 已落小屋阳台·看天气` 且 `weather=` 非空。

7. **关键词注入**：发一条含"今天好热"的网页聊天，看 gateway 日志是否出现 `🌤️ [Weather] 关键词命中，已注入天气`。

> 全部通过即缝合完成。任何一步报错就停下修该步，不要往下堆积错误。
