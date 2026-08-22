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
import socket
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_CITY = os.environ.get("WEATHER_DEFAULT_CITY", "Beijing").strip() or "Beijing"
TIMEOUT_SEC = float(os.environ.get("WEATHER_TIMEOUT_SEC", "12") or "12")
USER_AGENT = "weather-tools/1.2.0 (mcp-gateway)"

ALLOWED_HOST = "wttr.in"


# ---------------------------------------------------------------------------
# 安全 URL 校验（防 SSRF / DNS rebinding）
# ---------------------------------------------------------------------------
def _is_safe_host(host: str) -> bool:
    """校验目标主机是否合法，只允许 wttr.in。"""
    return host.lower() == ALLOWED_HOST


def _check_url_safe(url: str) -> None:
    """对请求 URL 做协议、域名和解析后 IP 的边界校验。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Only HTTPS is allowed; got scheme={parsed.scheme}")
    host = parsed.hostname or ""
    if not _is_safe_host(host):
        raise ValueError(f"Disallowed host: {host}")
    # 解析 IP，阻断私网 / 环回 / 链路本地地址（防 DNS rebinding）
    try:
        resolved = socket.getaddrinfo(host, None, socket.AF_INET)
        for _, _, _, _, addr in resolved:
            ip = addr[0]
            if ip.startswith("127.") or ip.startswith("10.") or ip.startswith("192.168."):
                raise ValueError(f"Private IP detected for host {host}: {ip}")
            if ip.startswith("169.254.") or ip.startswith("0.") or ip.startswith("172."):
                # 172.16/12 private range simplification
                parts = ip.split(".")
                if len(parts) >= 2 and parts[0] == "172" and 16 <= int(parts[1]) <= 31:
                    raise ValueError(f"Private IP detected for host {host}: {ip}")
    except socket.gaierror:
        # 无法解析时继续，实际请求会失败
        pass


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
                        "description": "留空=用户当前定位，填写城市名可查指定城市（默认韶关）",
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
    _check_url_safe(url)
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
