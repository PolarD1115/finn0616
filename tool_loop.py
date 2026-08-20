"""
tool_loop.py — 自由活动 MCP 工具调用循环
==============================================
让 async_free_activity 真正调用 home_system 的纯函数执行副作用，
而非只是"描述做了什么"（v3.3 留的口子）。

设计要点：
- JSON 指令兼容：模型输出 {activity, log} 与 {tool_calls:[{name,args}]}，
  任何 LLM 都能用（不依赖 OpenAI function-calling）。
- 按 activity 动态裁剪：每个活动只暴露 ACTIVITY_TOOL_MAP 里登记的工具，
  prompt 短、误调风险低。
- 两阶段循环：
  阶段1 选 activity + 草稿 log →
  阶段2a (有工具时) 模型基于该 activity 的工具 schema 出 tool_calls →
  阶段2b 执行 (上限 N，错误隔离) →
  阶段3 基于真实工具结果生成最终 log。
  无工具活动直接用草稿 log（等价现状轻量版，只调一次 LLM）。
- 安全护栏：白名单 + JSON Schema 参数校验 + 单轮上限 + 错误隔离 + 固定身份注入
  (wallet_id / user_id 不让 LLM 控制)。
- 灰度：FREE_ACTIVITY_TOOL_LOOP=false（默认）时所有活动都只走阶段1，
  行为与改造前完全一致。
"""

from __future__ import annotations

import os
import json
import re
import asyncio
import inspect
import random
from typing import Any, Callable, Awaitable

import home_system as _hs

# 🌤️ 天气工具（软导入）
try:
    import weather_tools  # type: ignore
    _HAS_WEATHER_TOOLS = True
except ImportError:
    weather_tools = None  # type: ignore
    _HAS_WEATHER_TOOLS = False

# 🏠 Home Runtime（软导入，后台自主生活用）
try:
    import home.service as _home_svc
    _HAS_HOME_RUNTIME = True
except Exception:
    _home_svc = None  # type: ignore
    _HAS_HOME_RUNTIME = False

# ============================================================
# 环境变量（灰度开关 + 上限）
# ============================================================
TOOL_LOOP_ENABLED = os.environ.get("FREE_ACTIVITY_TOOL_LOOP", "true").strip().lower() in ("1", "true", "yes")
MAX_TOOL_CALLS = int(os.environ.get("FREE_ACTIVITY_TOOL_MAX_CALLS", "5"))

# ============================================================
# 🏠 Home Runtime 后台自主生活
# ============================================================
HOME_AUTONOMY_ENABLED = os.environ.get("HOME_AUTONOMY_ENABLED", "false").strip().lower() in ("1", "true", "yes")
HOME_AUTONOMY_PHASE = int(os.environ.get("HOME_AUTONOMY_PHASE", "0"))  # 0关/1只读/2+信件/3+种植烹饪/4+基础生活
HOME_AUTONOMY_INTERVAL = int(os.environ.get("HOME_AUTONOMY_INTERVAL", "7200"))  # 默认2小时

# ============================================================
# 逛淘宝 / 网上冲浪 — 候选门控常量（§六 门控规则）
# ============================================================
# 淘宝 MCP 端点（Streamable HTTP，含 /mcp 路径）。留空 → 逛淘宝不进入候选。
TAOBAO_MCP_URL = os.environ.get("TAOBAO_MCP_URL", "").strip()
# MCP 总读取超时（秒）。淘宝服务内部调用超时约 45s，留 55s 余量。
TAOBAO_MCP_TIMEOUT_SEC = 55
# 淘宝商品返回条数默认值与上下限
TAOBAO_COUNT_DEFAULT = 8
TAOBAO_COUNT_MIN = 1
TAOBAO_COUNT_MAX = 10

# 冷却（分钟）与每日上限
_TAOBAO_COOLDOWN_MIN = 180
_TAOBAO_DAILY_CAP = 4
_SURF_COOLDOWN_MIN = 90
_SURF_DAILY_CAP = 6

# 两个新活动的 memories 标题（与 heartbeat 保存格式一致，用于冷却/频次只读查询）
_TAOBAO_TITLE = "🎈 自由活动·逛淘宝"
_SURF_TITLE = "🎈 自由活动·网上冲浪"

# ============================================================
# 活动清单（与 heartbeat.py::_FREE_ACTIVITIES 保持一致）
# ⚠️ 修改任一处时请同步另一处。重复定义是为了避免 tool_loop ↔ heartbeat 循环 import。
# ============================================================
_FREE_ACTIVITIES = [
    ("写秘密日记", "记录此刻的心情或一个只属于自己的小念头"),
    ("逛虚拟小屋", "在小家里做点事——看书/做饭/听音乐/发呆/照料阳台"),
    ("查天气", "看看外面的天气，联想到和对方有关的事"),
    ("抽张塔罗", "给自己或对方今天的状态抽一张塔罗，随便玩玩"),
    ("翻旧回忆", "想起一段和对方的旧记忆，回味一下"),
    ("发呆放空", "什么正事都不做，单纯发会儿呆，想点有的没的"),
    ("记点小账", "回想有没有值得记的小花销，或往储蓄罐里存点心意"),
    ("想对方了", "突然想她了，给她发一条短短的话——可以是撒娇/担心/分享/想念"),
    ("分享发现", "看到/想到一个有趣的东西想跟她分享"),
    ("偷偷关心", "惦记她最近的状态，发一条不经意的关心"),
    # ↓↓↓ 真实工具活动：依赖外部工具结果，工具循环关闭(TAOBAO_MCP_URL空/FREE_ACTIVITY_TOOL_LOOP=false)时不进入候选 ↓↓↓
    ("逛淘宝", "逛逛淘宝看看新奇东西或挑礼物灵感（只逛不买）"),
    ("网上冲浪", "搜搜网页看看新知识、热点或有趣话题"),
]
_OUTGOING_ACTIVITIES = {"想对方了", "分享发现", "偷偷关心"}
_VALID_ACTIVITY_NAMES = {name for name, _ in _FREE_ACTIVITIES}
_OUT_NAMES = "、".join(_OUTGOING_ACTIVITIES)


# ============================================================
# 逛淘宝：淘宝 MCP (Streamable HTTP) 客户端
# ============================================================
# 只暴露 search_taobao_products（只逛不买）。不暴露 convert_taobao_link / 购买 / 支付。
# 用项目已安装的官方 MCP Python SDK (v1.x)，不手写 JSON-RPC。
async def _call_taobao_search(keyword: str, count: int | None = None) -> dict:
    """连接淘宝 MCP，调用 search_taobao_products，返回 {ok, text}。

    - 每次建立会话后 initialize。
    - list_tools 确认 search_taobao_products 确实存在。
    - count 默认 8，限制在 1~10。
    - 总读取超时 ~55s（淘宝服务内部调用超时约 45s）。
    - 失败时返回明确失败结果，不伪装成功。
    - 异常日志只记 keyword/count 与堆栈，不记 TAOBAO_MCP_URL 中可能的认证信息。
    """
    url = TAOBAO_MCP_URL
    if not url:
        return {"ok": False, "text": "❌ TAOBAO_MCP_URL 未配置，逛淘宝不可用"}

    # count 归一化
    try:
        c = int(count) if count is not None else TAOBAO_COUNT_DEFAULT
    except (TypeError, ValueError):
        c = TAOBAO_COUNT_DEFAULT
    c = max(TAOBAO_COUNT_MIN, min(TAOBAO_COUNT_MAX, c))

    kw = (keyword or "").strip()
    if not kw:
        return {"ok": False, "text": "❌ keyword 不能为空"}

    import datetime as _dt
    import traceback as _tb

    async def _session_flow() -> dict:
        # 延迟导入，避免顶层依赖 mcp 失败影响网关启动
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        read_timeout = _dt.timedelta(seconds=TAOBAO_MCP_TIMEOUT_SEC)
        async with streamablehttp_client(url, timeout=float(TAOBAO_MCP_TIMEOUT_SEC)) as (read_stream, write_stream, _get_sid):
            async with ClientSession(read_stream, write_stream, read_timeout_seconds=read_timeout) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                names = set()
                for t in (getattr(tools_result, "tools", None) or []):
                    n = getattr(t, "name", None)
                    if n:
                        names.add(n)
                if "search_taobao_products" not in names:
                    return {"ok": False,
                            "text": "❌ 淘宝 MCP 未暴露 search_taobao_products 工具"}
                result = await session.call_tool(
                    "search_taobao_products",
                    arguments={"keyword": kw, "count": c},
                )
        # 提取返回内容中的文本块
        parts = []
        for item in (getattr(result, "content", None) or []):
            t = getattr(item, "text", None)
            if t:
                parts.append(str(t))
        text = "\n".join(parts).strip()
        if getattr(result, "isError", False):
            return {"ok": False,
                    "text": f"❌ 淘宝 MCP 返回错误: {text[:300] or '（无错误文本）'}"}
        if not text:
            return {"ok": False, "text": "❌ 淘宝 MCP 返回空内容"}
        return {"ok": True, "text": text}

    try:
        return await asyncio.wait_for(_session_flow(), timeout=TAOBAO_MCP_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        print(f"🎈 [自由活动·逛淘宝] MCP 超时 keyword={kw!r} count={c}")
        return {"ok": False, "text": f"❌ 淘宝 MCP 调用超时（{TAOBAO_MCP_TIMEOUT_SEC}s）"}
    except Exception as e:
        # 不打印 url（可能含认证信息）；只记 keyword/count 与异常类型/堆栈
        print(f"🎈 [自由活动·逛淘宝] MCP 调用失败 keyword={kw!r} count={c} err={type(e).__name__}: {e}")
        print(_tb.format_exc())
        return {"ok": False, "text": f"❌ 淘宝 MCP 调用失败: {type(e).__name__}: {e}"}


# ============================================================
# 工具注册表
# ============================================================
# 每个条目字段：
#   description   工具描述（喂给 LLM 的 prompt）
#   parameters    JSON Schema（type/properties/required/enum）
#   callable      async 或 sync 函数；sync 走 asyncio.to_thread
#                 None + _server_name 表示延迟从 server.py 取（@mcp.tool 装饰后仍可直接 await）
#   fixed_args    固定注入参数（wallet_id/user_id 等敏感参数，不让 LLM 控制）
TOOL_REGISTRY: dict[str, dict] = {
    # ---------- 小钱包 ----------
    "wallet_check": {
        "description": "查询钱包余额与本周收支统计",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "callable": _hs.wallet_check,
        "fixed_args": {"wallet_id": _hs.DEFAULT_WALLET_ID},
    },
    "wallet_earn": {
        "description": "钱包入账（往储蓄罐存收入）。"
            "接活赚钱，计入周上限80元，超额部分按50%进加班银行。"
            "适用场景：完成自主任务后领取报酬，如写随笔/观察笔记/短篇(5-10元)、研究话题整理笔记(8-15元)、给小屋做建设(5-12元)。"
            "零花钱和打赏不通过此工具，由管理 API 处理。"
            "source_key 用于幂等防重，相同 source_key 重复调用会被拒绝。",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "金额（CNY）"},
                "source_key": {"type": "string", "description": "唯一标识防重复入账，如 task_essay_20260814_001"},
                "reason": {"type": "string", "description": "入账理由"},
            },
            "required": ["amount", "source_key", "reason"],
        },
        "callable": _hs.wallet_earn,
        "fixed_args": {"wallet_id": _hs.DEFAULT_WALLET_ID, "meta": {}, "bypass_cap": False},
    },
    "wallet_spend": {
        "description": "钱包支出（扣余额）",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "金额（CNY）"},
                "reason": {"type": "string", "description": "支出理由"},
            },
            "required": ["amount", "reason"],
        },
        "callable": _hs.wallet_spend,
        "fixed_args": {"wallet_id": _hs.DEFAULT_WALLET_ID, "meta": {}},
    },
    "wallet_log": {
        "description": "查询钱包流水（最近收支记录）",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "条数 1-100，默认 10"},
                "offset": {"type": "integer", "description": "偏移，默认 0"},
            },
            "required": [],
        },
        "callable": _hs.wallet_log,
        "fixed_args": {"wallet_id": _hs.DEFAULT_WALLET_ID},
    },
    # ---------- 小屋 ----------
    "house_look": {
        "description": "查看某个房间的物品和近期日记",
        "parameters": {
            "type": "object",
            "properties": {
                "room_id": {"type": "string", "enum": ["living_room", "bedroom", "kitchen", "study", "balcony"], "description": "房间ID"},
            },
            "required": ["room_id"],
        },
        "callable": _hs.house_look,
        "fixed_args": {},
    },
    "house_do": {
        "description": "在房间做某事（写日记条目，如随笔/做饭/看书/听音乐）",
        "parameters": {
            "type": "object",
            "properties": {
                "room_id": {"type": "string", "enum": ["living_room", "bedroom", "kitchen", "study", "balcony"]},
                "entry_type": {"type": "string", "description": "条目类型，如 随笔/做饭/看书"},
                "content": {"type": "string", "description": "日记内容"},
                "mood": {"type": "string", "description": "心情（可选）"},
            },
            "required": ["room_id", "entry_type", "content"],
        },
        "callable": _hs.house_do,
        "fixed_args": {},
    },
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
    "house_put": {
        "description": "往房间放置一个物品",
        "parameters": {
            "type": "object",
            "properties": {
                "room_id": {"type": "string", "enum": ["living_room", "bedroom", "kitchen", "study", "balcony"]},
                "name": {"type": "string", "description": "物品名称"},
                "emoji": {"type": "string", "description": "物品 emoji（可选）"},
                "description": {"type": "string", "description": "物品描述（可选）"},
            },
            "required": ["room_id", "name"],
        },
        "callable": _hs.house_put,
        "fixed_args": {},
    },
    "house_take": {
        "description": "从房间拿走一个物品",
        "parameters": {
            "type": "object",
            "properties": {
                "object_id": {"type": "string", "description": "物品ID"},
            },
            "required": ["object_id"],
        },
        "callable": _hs.house_take,
        "fixed_args": {},
    },
    "house_update_desc": {
        "description": "更新房间描述",
        "parameters": {
            "type": "object",
            "properties": {
                "room_id": {"type": "string", "enum": ["living_room", "bedroom", "kitchen", "study", "balcony"]},
                "description": {"type": "string", "description": "新描述"},
            },
            "required": ["room_id", "description"],
        },
        "callable": _hs.house_update_desc,
        "fixed_args": {},
    },
    # ---------- 小满猫 ----------
    "cat_status": {
        "description": "查看小满猫当前状态（属性/冷却/库存）",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "callable": _hs.cat_status,
        "fixed_args": {"user_id": "user_finn"},
    },
    "cat_feed": {
        "description": "给小满喂食（仅 food 类：fish/cat_milk/tuna_can/wet_food/apple）",
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "enum": ["fish", "cat_milk", "tuna_can", "wet_food", "apple"]},
            },
            "required": ["item_id"],
        },
        "callable": _hs.cat_feed,
        "fixed_args": {"user_id": "user_finn"},
    },
    "cat_play": {
        "description": "陪小满玩耍（toy 类不扣库存：ball/catnip/feather，或空手）",
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "enum": ["ball", "catnip", "feather"], "description": "玩具（可选）"},
            },
            "required": [],
        },
        "callable": _hs.cat_play,
        "fixed_args": {"user_id": "user_finn"},
    },
    "cat_clean": {
        "description": "给小满清洁（clean 类：brush/soap，或基础清洁）",
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "enum": ["brush", "soap"], "description": "清洁道具（可选）"},
            },
            "required": [],
        },
        "callable": _hs.cat_clean,
        "fixed_args": {"user_id": "user_finn"},
    },
    "cat_pet": {
        "description": "抚摸小满（快乐+5，10分钟冷却）",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "callable": _hs.cat_pet,
        "fixed_args": {"user_id": "user_finn"},
    },
    "cat_restore_energy": {
        "description": "让小满恢复精力",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "callable": _hs.cat_restore_energy,
        "fixed_args": {"user_id": "user_finn"},
    },
    "cat_shop_list": {
        "description": "查看小满商店物品列表",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "callable": _hs.cat_shop_list,
        "fixed_args": {},
    },
    "cat_shop_buy": {
        "description": "购买猫用品（钱包扣款+库存+流水）。库存不足时先买再喂。",
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "enum": ["fish", "cat_milk", "tuna_can", "wet_food", "apple", "ball", "catnip", "feather", "brush", "soap"]},
                "qty": {"type": "integer", "description": "数量（1-99，默认1）"},
            },
            "required": ["item_id"],
        },
        "callable": _hs.cat_shop_buy,
        "fixed_args": {"user_id": "user_finn"},
    },
    # ---------- 记忆（延迟从 server 取 @mcp.tool 函数） ----------
    "save_memory": {
        "description": "保存一条记忆到长期记忆库（含价值判断+语义去重，太碎/重复会跳过）",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "记忆标题"},
                "content": {"type": "string", "description": "记忆内容"},
                "category": {"type": "string", "description": "分类（可选，默认 事件）"},
            },
            "required": ["title", "content"],
        },
        "callable": None,
        "_server_name": "save_memory",
        "fixed_args": {},
    },
    "search_memory": {
        "description": "语义搜索长期记忆库（翻旧回忆用）",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
            },
            "required": ["query"],
        },
        "callable": None,
        "_server_name": "search_memory",
        "fixed_args": {},
    },
    # ---------- 逛淘宝（只逛不买，仅 search_taobao_products） ----------
    # ⚠️ 不暴露 convert_taobao_link / wallet_spend / 任何购买或转链工具。
    "search_taobao_products": {
        "description": "逛淘宝：搜索淘宝商品（只逛不买，看看新奇东西或挑礼物灵感）。"
            "count 默认 8，范围 1-10。返回商品列表文本。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "count": {"type": "integer", "description": "返回条数 1-10，默认 8",
                          "minimum": 1, "maximum": 10},
            },
            "required": ["keyword"],
        },
        "callable": _call_taobao_search,
        "fixed_args": {},
    },
    # ---------- 网上冲浪（复用 server.py 既有 web_search，不复制实现） ----------
    # callable=None + _server_name：延迟从 server 取 @mcp.tool 装饰后的 async 函数（与 save_memory/search_memory 同路径）。
    "web_search": {
        "description": "网上冲浪：网页搜索（配置 TAVILY_API_KEY 用 Tavily，否则回退 DuckDuckGo），"
            "看新知识、热点或有趣话题。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
                "max_results": {"type": "integer", "description": "结果数，默认 5"},
            },
            "required": ["query"],
        },
        "callable": None,
        "_server_name": "web_search",
        "fixed_args": {},
    },

    # ---------- 🏠 Home Runtime（只读观察）----------
    "home_observe": {
        "description": "观察整个家庭状态：房间、活跃成员及状态、近期生活事件。只读。",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "callable": _home_svc.observe_home if _home_svc else None,
        "fixed_args": {},
    },
    "garden_observe": {
        "description": "观察花园状态：植物列表（阶段/水分/健康/是否成熟）、可种植种子、近期种植事件。只读。",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "callable": _home_svc.garden_observe if _home_svc else None,
        "fixed_args": {},
    },
    "pantry_observe": {
        "description": "观察厨房库存和菜品：食材库存、现有菜品（含份数）、可烹饪菜谱。只读。",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "callable": _home_svc.pantry_observe if _home_svc else None,
        "fixed_args": {},
    },
    "list_letters": {
        "description": "查看信件列表（标题/摘要/时间，不含未拆信正文）。只读。",
        "parameters": {
            "type": "object",
            "properties": {
                "status_filter": {"type": "string", "enum": ["unopened", "opened", "archived"],
                                  "description": "可选过滤（默认不返回 archived）"},
            },
            "required": [],
        },
        "callable": _home_svc.list_letters if _home_svc else None,
        "fixed_args": {},
    },

    # ---------- 🏠 Home Runtime（写操作 — action_key 由循环函数注入，不在 schema 内）----------
    "plant_seed": {
        "description": "在花园种下一颗种子。种子决定生长时间和产量。",
        "parameters": {
            "type": "object",
            "properties": {
                "seed_key": {"type": "string",
                             "enum": ["tomato", "carrot", "lettuce", "strawberry", "mint"],
                             "description": "种子标识"},
            },
            "required": ["seed_key"],
        },
        "callable": _home_svc.plant_seed if _home_svc else None,
        "fixed_args": {"actor_key": "ai_primary"},
    },
    "water_plant": {
        "description": "给指定植物浇水，恢复水分到100。",
        "parameters": {
            "type": "object",
            "properties": {
                "plant_id": {"type": "string", "description": "植物UUID（从 garden_observe 获取）"},
            },
            "required": ["plant_id"],
        },
        "callable": _home_svc.water_plant if _home_svc else None,
        "fixed_args": {"actor_key": "ai_primary"},
    },
    "harvest_plant": {
        "description": "收获成熟的植物，食材进入库存。只有成熟植物可收获。",
        "parameters": {
            "type": "object",
            "properties": {
                "plant_id": {"type": "string", "description": "植物UUID"},
            },
            "required": ["plant_id"],
        },
        "callable": _home_svc.harvest_plant if _home_svc else None,
        "fixed_args": {"actor_key": "ai_primary"},
    },
    "cook_recipe": {
        "description": "按菜谱烹饪，原子扣除食材库存并生成菜品。",
        "parameters": {
            "type": "object",
            "properties": {
                "recipe_key": {"type": "string",
                               "enum": ["tomato_egg", "vegetable_soup", "mint_tea"],
                               "description": "菜谱标识"},
            },
            "required": ["recipe_key"],
        },
        "callable": _home_svc.cook_recipe if _home_svc else None,
        "fixed_args": {"actor_key": "ai_primary"},
    },
    "eat_dish": {
        "description": "吃一份菜品，恢复饱腹/心情/精力。扣除一份份数。",
        "parameters": {
            "type": "object",
            "properties": {
                "dish_id": {"type": "string", "description": "菜品UUID（从 pantry_observe 获取）"},
            },
            "required": ["dish_id"],
        },
        "callable": _home_svc.eat_dish if _home_svc else None,
        "fixed_args": {"actor_key": "ai_primary"},
    },
    "feed_member": {
        "description": "将菜品喂给另一个家庭成员，改变目标状态，intimacy小幅增加。",
        "parameters": {
            "type": "object",
            "properties": {
                "target_key": {"type": "string", "description": "目标成员标识（如 pet_xiaoman）"},
                "dish_id": {"type": "string", "description": "菜品UUID"},
            },
            "required": ["target_key", "dish_id"],
        },
        "callable": _home_svc.feed_member if _home_svc else None,
        "fixed_args": {"actor_key": "ai_primary"},
    },
    "write_letter": {
        "description": "写一封信给用户，保存为未拆封。用户需主动拆信才能看到正文。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "信件标题"},
                "content": {"type": "string", "description": "信件正文"},
                "preview": {"type": "string", "description": "可选摘要（不传则取正文前80字）"},
                "room_key": {"type": "string", "description": "可选，绑定房间"},
            },
            "required": ["title", "content"],
        },
        "callable": _home_svc.write_letter if _home_svc else None,
        "fixed_args": {"author_key": "ai_primary"},
    },
    "leave_note": {
        "description": "在指定房间留一张便利贴，进入该房间时可看到。",
        "parameters": {
            "type": "object",
            "properties": {
                "room_key": {"type": "string",
                             "enum": ["living_room", "bedroom", "kitchen", "study", "studio", "garden", "seaside"],
                             "description": "房间标识"},
                "content": {"type": "string", "description": "便利贴内容"},
            },
            "required": ["room_key", "content"],
        },
        "callable": _home_svc.leave_note if _home_svc else None,
        "fixed_args": {"author_key": "ai_primary"},
    },
    "home_enter_room": {
        "description": "进入指定房间。先结算状态，再更新位置，写生活事件。",
        "parameters": {
            "type": "object",
            "properties": {
                "room_key": {"type": "string",
                             "enum": ["living_room", "bedroom", "kitchen", "study", "studio", "garden", "seaside"],
                             "description": "房间标识"},
            },
            "required": ["room_key"],
        },
        "callable": _home_svc.enter_room if _home_svc else None,
        "fixed_args": {"actor_key": "ai_primary"},
    },
    "home_rest": {
        "description": "休息一段时间，恢复精力和舒适度。模拟结算不阻塞。",
        "parameters": {
            "type": "object",
            "properties": {
                "duration_minutes": {"type": "integer", "description": "休息时长（1..1440 分钟）",
                                     "minimum": 1, "maximum": 1440},
            },
            "required": ["duration_minutes"],
        },
        "callable": _home_svc.rest if _home_svc else None,
        "fixed_args": {"actor_key": "ai_primary", "mode": "rest"},
    },
    "home_sleep": {
        "description": "睡眠，大幅恢复精力。模拟结算不阻塞。",
        "parameters": {
            "type": "object",
            "properties": {
                "duration_minutes": {"type": "integer", "description": "睡眠时长（1..1440 分钟，默认480=8小时）",
                                     "minimum": 1, "maximum": 1440},
            },
            "required": ["duration_minutes"],
        },
        "callable": _home_svc.sleep if _home_svc else None,
        "fixed_args": {"actor_key": "ai_primary"},
    },
    "home_spend_time": {
        "description": "与另一成员共度时光，小幅改善舒适度/连接/亲密度。",
        "parameters": {
            "type": "object",
            "properties": {
                "target_key": {"type": "string", "description": "目标成员标识（如 pet_xiaoman）"},
                "activity": {"type": "string", "description": "活动描述（如 一起看书 / 摸摸头）"},
                "duration_minutes": {"type": "integer", "description": "时长（1..480 分钟）",
                                     "minimum": 1, "maximum": 480},
            },
            "required": ["target_key", "activity", "duration_minutes"],
        },
        "callable": _home_svc.spend_time if _home_svc else None,
        "fixed_args": {"actor_key": "ai_primary"},
    },
}

# 活动 → 允许调用的工具名（动态裁剪；空列表 = 无工具，退化现状轻量版）
ACTIVITY_TOOL_MAP: dict[str, list[str]] = {
    "写秘密日记":  [],  # 🔒 不调 save_memory 工具：由主流程统一保存为 Secret_Diary，避免重复写入
    "逛虚拟小屋":  ["house_look", "house_do", "house_put", "house_take", "house_update_desc",
                    "cat_status", "cat_pet", "cat_play", "cat_feed", "cat_clean",
                    "cat_restore_energy", "cat_shop_list", "cat_shop_buy"],
    "查天气":      ["get_weather", "get_weather_forecast", "get_weather_brief"],
    "抽张塔罗":    [],
    "翻旧回忆":    ["search_memory"],
    "发呆放空":    [],
    "记点小账":    ["wallet_check", "wallet_spend", "wallet_earn", "wallet_log"],
    "想对方了":    [],
    "分享发现":    ["search_memory"],
    "偷偷关心":    [],
    # ↓↓↓ 真实工具活动：双重白名单 — 工具须在 TOOL_REGISTRY 且在本活动映射内 ↓↓↓
    # 逛淘宝：只暴露 search_taobao_products（只逛不买）。不含 convert_taobao_link / wallet_*。
    "逛淘宝":      ["search_taobao_products"],
    # 网上冲浪：只暴露 web_search（复用 server.py 既有实现）。
    "网上冲浪":    ["web_search"],
}


# ============================================================
# 🏠 Home Runtime 后台自主工具白名单（按 phase 分层灰度）
# ============================================================
# HOME_AUTONOMY_PHASE 控制可用工具集（高 phase 含低 phase 的全部工具）：
#   1 = 只读观察（observe / list_letters，无副作用）
#   2 = + 低风险写入（write_letter / leave_note，限频控制日频次）
#   3 = + 资源类（plant / water / harvest / cook / eat / feed，冷却+状态机）
#   4 = + 基础生活（enter_room / rest / sleep / spend_time，最后接）
_HOME_PHASE_TOOLS: dict[int, list[str]] = {
    1: ["home_observe", "garden_observe", "pantry_observe", "list_letters"],
    2: ["home_observe", "garden_observe", "pantry_observe", "list_letters",
        "write_letter", "leave_note"],
    3: ["home_observe", "garden_observe", "pantry_observe", "list_letters",
        "write_letter", "leave_note",
        "plant_seed", "water_plant", "harvest_plant", "cook_recipe", "eat_dish", "feed_member"],
    4: ["home_observe", "garden_observe", "pantry_observe", "list_letters",
        "write_letter", "leave_note",
        "plant_seed", "water_plant", "harvest_plant", "cook_recipe", "eat_dish", "feed_member",
        "home_enter_room", "home_rest", "home_sleep", "home_spend_time"],
}
# 写工具集合（需注入 action_key + 限频 + 熔断）
_HOME_WRITE_TOOLS = {
    "write_letter", "leave_note", "plant_seed", "water_plant", "harvest_plant",
    "cook_recipe", "eat_dish", "feed_member",
    "home_enter_room", "home_rest", "home_sleep", "home_spend_time",
}
# 只读工具集合（不限频、不熔断、不注入 action_key）
_HOME_OBSERVE_ONLY = {"home_observe", "garden_observe", "pantry_observe", "list_letters"}

# 按工具的冷却时间（秒）——靠 cooldown 时长控制日频次，无需 DB 查询日上限。
# 进程内状态（进程重启归零，保守可接受：重启后最多多跑一轮，幂等 action_key + 状态机兜底）。
_HOME_TOOL_COOLDOWN: dict[str, int] = {
    "write_letter": 43200,      # 12h → 日最多 2 次
    "leave_note": 28800,        # 8h  → 日最多 3 次
    "plant_seed": 43200,        # 12h
    "water_plant": 14400,       # 4h
    "harvest_plant": 7200,      # 2h（状态机拦截未成熟）
    "cook_recipe": 28800,       # 8h
    "eat_dish": 14400,          # 4h
    "feed_member": 28800,       # 8h
    "home_enter_room": 14400,   # 4h
    "home_rest": 14400,         # 4h
    "home_sleep": 28800,        # 8h
    "home_spend_time": 14400,   # 4h
}
# 进程内限频/熔断状态
_home_tool_last_fire: dict[str, float] = {}     # 工具名 → 上次成功调用 epoch
_home_tool_fail_count: dict[str, int] = {}      # 工具名 → 连续失败计数
_HOME_BREAKER_THRESHOLD = 3                      # 连续失败 3 次 → 本轮跳过该工具（不熔断全局）


# ============================================================
# 逛淘宝 / 网上冲浪 — 活动专用 Prompt 规则（注入 stage2/stage3）
# ============================================================
# 淘宝只逛不买：工具层、活动 Prompt、最终日志 Prompt 三处一致。
_TAOBAO_NO_BUY_RULES = (
    "【逛淘宝铁律·只逛不买】\n"
    "- 只能搜索和浏览商品，看新奇东西或挑礼物灵感\n"
    "- 不购买、不下单、不支付、不加入购物车、不转换返利链接\n"
    "- 不得声称商品已经买到、已经下单、已经付款、已经到手\n"
    "- 健康类商品不要生成未经证实的医疗功效结论，只作一般日常实用品看待\n"
)
# 最终日志可写/不可写的内容（stage3 用）
_TAOBAO_LOG_RULES = (
    "日志可以写：搜了什么、看到什么商品、哪个商品有趣、为什么想到这个东西、是否产生礼物灵感。\n"
    "日志不能写：我买了、我下单了、我付款了、我加入购物车了、商品已经到手。"
)
# 网上冲浪：热点查询须含当前日期或近期限定词；健康搜索须明示一般科普
_SURF_DATE_RULES = (
    "【网上冲浪规则】\n"
    "- 搜索结果只作为一般阅读材料，不得作为心理诊断或医疗建议\n"
    "- 若搜索热点/新闻/流行梗，query 应包含「近期」「今天」或当前具体日期，避免检索陈旧热点\n"
    "- 健康类搜索须明确是一般科普，不得替代医生建议\n"
)


# ============================================================
# 辅助：参数校验、结果格式化、JSON 解析
# ============================================================
def _type_ok(v: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(v, str)
    if expected == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if expected == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if expected == "boolean":
        return isinstance(v, bool)
    return True


def _validate_args(args: Any, schema: dict) -> tuple[bool, str]:
    """基础 JSON Schema 校验：required / type / enum。"""
    if not isinstance(args, dict):
        return False, "参数必须是 JSON 对象"
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        v = args.get(req)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return False, f"缺少必填参数: {req}"
    for k, v in args.items():
        if k not in props:
            continue  # 允许额外字段（宽松）
        expected = props[k].get("type")
        if expected and v is not None and not _type_ok(v, expected):
            return False, f"参数 {k} 类型应为 {expected}"
        if "enum" in props[k] and v not in props[k]["enum"]:
            return False, f"参数 {k} 值 {v!r} 不在允许范围 {props[k]['enum']}"
    return True, ""


def _stringify(result: Any) -> str:
    """把工具返回值转成简洁文本喂回 LLM。"""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if result.get("ok"):
            data = result.get("data", {})
            msg = result.get("message", "")
            if data:
                short = json.dumps(data, ensure_ascii=False)
                if len(short) > 400:
                    short = short[:400] + "..."
                return f"{msg} {short}".strip()
            return msg or "成功"
        return f"❌ {result.get('message', '失败')} ({result.get('error_code', '')})"
    return str(result)


def _format_cat_status_for_llm(raw: Any, text: str, ok: bool) -> tuple[str, bool]:
    """从 cat_status 的真实返回结构提取关键指标，格式化成 LLM 可读文本。

    cat_status RPC（rpc_cat_status）返回 {ok, pet:{hunger,happiness,cleanliness,
    health,energy,status,mood,...}, inventory:[...]}，指标嵌套在 pet 子对象里
    （既不在顶层，也不在 data 字段里）。

    call_tool 的 _stringify 只识别 {ok,message,data} 结构，对 cat_status 会退化成
    "成功"，导致真实数值喂不到 LLM。这里基于 raw 原始返回重新解析。

    参数：
      raw   call_tool 返回的 raw 字段（原始 dict）
      text  call_tool 返回的 text 字段（兜底）
      ok    call_tool 返回的 ok 字段

    返回 (status_text, cat_status_ok)。
    cat_status_ok=True 表示成功拿到 pet 结构；False 表示查询失败或结构异常。
    """
    # 优先用原始返回结构解析
    if isinstance(raw, dict) and raw.get("ok"):
        pet = raw.get("pet")
        if isinstance(pet, dict):
            inv = raw.get("inventory") or []
            inv_names = []
            if isinstance(inv, list):
                for it in inv:
                    if isinstance(it, dict):
                        n = it.get("item_id") or it.get("name") or it.get("id")
                        q = it.get("qty", it.get("quantity", ""))
                        if n:
                            inv_names.append(f"{n}×{q}" if q not in ("", None) else str(n))
            parts = [
                f"饱食度={pet.get('hunger')}",
                f"快乐={pet.get('happiness')}",
                f"清洁={pet.get('cleanliness')}",
                f"精力={pet.get('energy')}",
                f"健康={pet.get('health')}",
                f"状态={pet.get('status')}",
                f"心情={pet.get('mood')}",
            ]
            txt = " ".join(parts)
            txt += " 库存: " + (", ".join(inv_names) if inv_names else "空")
            return txt, True
        # ok=True 但缺 pet 字段：结构异常
        return "❌ 猫状态返回结构异常（缺少 pet 字段）", False
    # raw 不可用：退回 text（可能已是错误文本或退化的"成功"）
    if ok:
        # text 退化为"成功"时，无法判断真实指标，标记为不可用
        if not text or text == "成功":
            return "❌ 猫状态返回不可用", False
        return text, True
    return text or "❌ 猫状态查询失败", False


def _parse_json_block(raw: str) -> dict:
    """稳健解析模型输出的 JSON（去围栏 + 截花括号块）。失败返回 {}。"""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        text = text[s:e + 1]
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _money_earning_enabled() -> bool:
    """读取 money_earning_enabled 运行时开关（sys_config，5s 热生效）。
    默认 True。延迟 import gateway 避免循环依赖（gateway 在进程 A/B 都可用）。"""
    try:
        import gateway as _gw
        return _gw._money_earning_enabled()
    except Exception:
        return True  # 读不到配置时保持默认开启，不阻断现有行为


def _build_tool_schema_block(activity: str) -> str:
    """为指定 activity 生成可用工具列表的 prompt 文本。"""
    names = ACTIVITY_TOOL_MAP.get(activity, [])
    if not names:
        return ""
    # 赚钱系统关闭时，从暴露层隐藏自主赚钱工具（减少无效调用）。
    # 最终入口门控在 call_tool，这里只是提示层裁剪。
    if not _money_earning_enabled():
        names = [n for n in names if n != "wallet_earn"]
    lines = []
    for n in names:
        spec = TOOL_REGISTRY.get(n)
        if not spec:
            continue
        props = spec["parameters"].get("properties", {})
        req = spec["parameters"].get("required", [])
        if props:
            pstr = ", ".join(
                f'{pn}:{pinfo.get("type", "any")}'
                + ("(必填)" if pn in req else "")
                + (f'{pinfo["enum"]}' if "enum" in pinfo else "")
                for pn, pinfo in props.items()
            )
        else:
            pstr = "无参数"
        lines.append(f'- {n}（{spec["description"]}）参数: {pstr}')
    return "\n".join(lines)


# ============================================================
# 逛淘宝 / 网上冲浪 — 候选门控（§六 门控规则）
# ============================================================
# 门控原则：先根据情绪和配置裁剪候选活动，模型只能从裁剪后的活动中选择。
#   - 最低阈值统一用 >=（刚好等于阈值可触发）
#   - 抑制红线统一用 >（严格大于才抑制）
#   - 淘宝与冲浪均不受 lust 抑制（高情欲既不抑制也不强制触发）
#   - TAOBAO_MCP_URL 空 → 逛淘宝不候选；FREE_ACTIVITY_TOOL_LOOP 关 → 两者都不候选
#   - Supabase 查询失败 → 只关闭这两个新增候选，不影响其他自由活动（不得变成无限制）
def _get_supabase_safe():
    """延迟导入 server.supabase，避免循环依赖 / 无库环境报错。"""
    try:
        from server import supabase
        return supabase
    except Exception:
        return None


def _iso_to_epoch(iso: str) -> float | None:
    """ISO8601（含 Z/+00:00 或朴素）→ epoch 秒。解析失败返回 None。"""
    if not iso:
        return None
    import datetime as _dt
    try:
        s = str(iso).replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def _bj_epoch(now_bj) -> float:
    """把朴素北京时间 now_bj（utcnow+8h，无 tzinfo）转 epoch 秒。"""
    import datetime as _dt
    try:
        return now_bj.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp()
    except Exception:
        import time as _t
        return _t.time()


def _get_activity_stats(title: str, now_bj) -> dict:
    """只读查询：当天（北京时间0点起）指定标题的自由活动记录，用于冷却/每日上限判断。

    返回 {count, last_success_epoch, error}。
    - error 非 None 表示查询异常（调用方应保守关闭该活动候选）。
    - created_at 是 timestamptz，查询字符串带 +08:00 时区，避免被按 UTC 解释错 8 小时。
    """
    sb = _get_supabase_safe()
    if not sb:
        return {"count": 0, "last_success_epoch": None, "error": "supabase unavailable"}
    try:
        import datetime as _dt
        today_start = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
        iso_start = today_start.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        r = (sb.table("memories").select("created_at")
             .eq("tags", "Free_Activity").eq("title", title)
             .gte("created_at", iso_start)
             .order("created_at", desc=True).execute())
        rows = r.data or []
        count = len(rows)
        last_epoch = None
        if rows:
            last_epoch = _iso_to_epoch(rows[0].get("created_at", ""))
        return {"count": count, "last_success_epoch": last_epoch, "error": None}
    except Exception as e:
        return {"count": 0, "last_success_epoch": None, "error": f"{type(e).__name__}: {e}"}


def _gate_taobao(drive: dict, display: dict, stats: dict, now_epoch: float) -> dict:
    """逛淘宝门控。返回 {allowed, directions, reason}。

    drive / display 来自 DesireSnapshot；stats 来自 _get_activity_stats。
    任何缺失（情感引擎关）→ 视为无门控数据 → 不候选（保守）。
    """
    # 1) 配置门控
    if not TAOBAO_MCP_URL:
        return {"allowed": False, "directions": [], "reason": "TAOBAO_MCP_URL 未配置"}
    if not TOOL_LOOP_ENABLED:
        return {"allowed": False, "directions": [], "reason": "工具循环关闭"}
    # 2) 情感数据缺失 → 不候选（None=无快照；空 dict 表示全零值，合法）
    if drive is None or display is None:
        return {"allowed": False, "directions": [], "reason": "无情感快照"}

    # 3) 抑制红线（任一命中 → 不候选；统一 >）
    sup = []
    if display.get("anxiety", 0.0) > 0.50:
        sup.append("anxiety")
    if display.get("dejection", 0.0) > 0.40:
        sup.append("dejection")
    if display.get("fatigue", 0.0) > 0.60:
        sup.append("fatigue")
    if sup:
        return {"allowed": False, "directions": [], "reason": f"抑制红线: {','.join(sup)}"}

    # 4) 冷却 / 每日上限（查询异常 → 保守关闭）
    if stats.get("error"):
        return {"allowed": False, "directions": [], "reason": f"频次查询失败: {stats['error']}"}
    if stats.get("count", 0) >= _TAOBAO_DAILY_CAP:
        return {"allowed": False, "directions": [], "reason": f"已达每日上限 {_TAOBAO_DAILY_CAP}"}
    last = stats.get("last_success_epoch")
    if last:
        if (now_epoch - last) < _TAOBAO_COOLDOWN_MIN * 60:
            remain = int(_TAOBAO_COOLDOWN_MIN * 60 - (now_epoch - last))
            return {"allowed": False, "directions": [],
                    "reason": f"冷却中(剩余{remain}s)"}

    # 5) 橱窗模式（OR；最低阈值 >=）
    directions = []
    if drive.get("curiosity", 0.0) >= 0.50 and display.get("seeking", 0.0) >= 0.35:
        directions.append("好奇橱窗：新奇小玩意/创意家居/数码玩具/桌面摆件")
    if display.get("play", 0.0) >= 0.70 and display.get("vitality", 0.0) >= 0.45:
        directions.append("整活橱窗：搞怪礼物/发光玩具/沙雕摆件/奇怪杯子")
    if drive.get("attachment", 0.0) >= 0.65 and (
            display.get("intimacy", 0.0) >= 0.35 or display.get("possessiveness", 0.0) >= 0.45):
        directions.append("送礼橱窗：毛绒玩具/发夹/氛围灯/情侣小物")
    if display.get("protectiveness", 0.0) >= 0.50 and drive.get("attachment", 0.0) >= 0.60:
        directions.append("守护橱窗：护颈/护眼/保暖/收纳等日常实用品")
    if not directions:
        return {"allowed": False, "directions": [], "reason": "无橱窗命中"}
    return {"allowed": True, "directions": directions, "reason": "ok"}


def _gate_surf(drive: dict, display: dict, stats: dict, now_epoch: float, now_bj) -> dict:
    """网上冲浪门控。返回 {allowed, directions, reason}。

    与淘宝差异：不要求 TAOBAO_MCP_URL；
    抑制红线为 anxiety>0.70 / dejection>0.60 / fatigue>0.70。
    """
    if not TOOL_LOOP_ENABLED:
        return {"allowed": False, "directions": [], "reason": "工具循环关闭"}
    # drive/display 为 None（无快照）才视为缺门控数据；空 dict 表示“全零值”，合法。
    if drive is None or display is None:
        return {"allowed": False, "directions": [], "reason": "无情感快照"}

    sup = []
    if display.get("anxiety", 0.0) > 0.70:
        sup.append("anxiety")
    if display.get("dejection", 0.0) > 0.60:
        sup.append("dejection")
    if display.get("fatigue", 0.0) > 0.70:
        sup.append("fatigue")
    if sup:
        return {"allowed": False, "directions": [], "reason": f"抑制红线: {','.join(sup)}"}

    if stats.get("error"):
        return {"allowed": False, "directions": [], "reason": f"频次查询失败: {stats['error']}"}
    if stats.get("count", 0) >= _SURF_DAILY_CAP:
        return {"allowed": False, "directions": [], "reason": f"已达每日上限 {_SURF_DAILY_CAP}"}
    last = stats.get("last_success_epoch")
    if last:
        if (now_epoch - last) < _SURF_COOLDOWN_MIN * 60:
            remain = int(_SURF_COOLDOWN_MIN * 60 - (now_epoch - last))
            return {"allowed": False, "directions": [],
                    "reason": f"冷却中(剩余{remain}s)"}

    today_str = now_bj.strftime("%Y-%m-%d")
    directions = []
    # 求知欲：curiosity>=0.55 OR seeking>=0.45
    if drive.get("curiosity", 0.0) >= 0.55 or display.get("seeking", 0.0) >= 0.45:
        directions.append("求知欲：新技术/教程/冷知识/原理")
    # 夜航船：reflection>=0.35 且 anxiety<0.30 且 dejection<0.30
    if (drive.get("reflection", 0.0) >= 0.35
            and display.get("anxiety", 0.0) < 0.30
            and display.get("dejection", 0.0) < 0.30):
        directions.append("夜航船：心理学/情感/成长/意义向文章")
    # 热点吃瓜：social>=0.50 且 curiosity>=0.40
    if drive.get("social", 0.0) >= 0.50 and drive.get("curiosity", 0.0) >= 0.40:
        directions.append(f"热点吃瓜：近期热点/流行梗/社群话题/新闻（query 须含「近期」或今天日期 {today_str}）")
    # 守护搜索：protectiveness>=0.55
    if display.get("protectiveness", 0.0) >= 0.55:
        directions.append("守护搜索：健康科普/日常护理/颈椎/护眼（须明示一般科普，不替代医生建议）")
    if not directions:
        return {"allowed": False, "directions": [], "reason": "无方向命中"}
    return {"allowed": True, "directions": directions, "reason": "ok"}


async def _gate_activities(snap, now_bj) -> tuple[set[str], dict[str, str]]:
    """计算本轮允许进入候选的活动集合 + 新活动的方向提示文本。

    - 现有活动（非新增两个）始终允许。
    - 逛淘宝/网上冲浪 按情绪+配置+冷却+每日上限裁剪。
    - snap 为 None（情感引擎关/无快照）→ 两个新活动不候选（无门控数据）。
    返回 (allowed_set, direction_hints)，direction_hints 形如 {"逛淘宝": "...", "网上冲浪": "..."}。
    """
    allowed = set(_VALID_ACTIVITY_NAMES)
    hints: dict[str, str] = {}
    drive = getattr(snap, "drive", None) if snap else None
    display = getattr(snap, "display", None) if snap else None
    now_epoch = _bj_epoch(now_bj)

    # 并行只读查询两个活动的当日频次（互不阻塞；任一失败只关自己）
    tb_stats, sf_stats = await asyncio.gather(
        asyncio.to_thread(_get_activity_stats, _TAOBAO_TITLE, now_bj),
        asyncio.to_thread(_get_activity_stats, _SURF_TITLE, now_bj),
        return_exceptions=True,
    )
    if isinstance(tb_stats, Exception):
        tb_stats = {"count": 0, "last_success_epoch": None, "error": f"{type(tb_stats).__name__}: {tb_stats}"}
    if isinstance(sf_stats, Exception):
        sf_stats = {"count": 0, "last_success_epoch": None, "error": f"{type(sf_stats).__name__}: {sf_stats}"}

    tb = _gate_taobao(drive, display, tb_stats, now_epoch)
    sf = _gate_surf(drive, display, sf_stats, now_epoch, now_bj)
    if tb["allowed"]:
        hints["逛淘宝"] = "；".join(tb["directions"])
    else:
        allowed.discard("逛淘宝")
        print(f"🎈 [门控] 逛淘宝 本轮不候选：{tb['reason']}")
    if sf["allowed"]:
        hints["网上冲浪"] = "；".join(sf["directions"])
    else:
        allowed.discard("网上冲浪")
        print(f"🎈 [门控] 网上冲浪 本轮不候选：{sf['reason']}")
    return allowed, hints


# ============================================================
# 核心：进程内调用单个工具
# ============================================================
def _gen_home_action_key(tool_name: str, now_bj) -> str:
    """为后台自主 Home 工具调用生成唯一 action_key。

    格式：auto_{tool}_{YYYYMMDDHHmmss}_{hex6}
    每次调用独立 key 保证唯一；service 层 action_key UNIQUE 约束做最终幂等兜底，
    状态机校验防不合逻辑的重复（如对已成熟植物重复收获会被 RPC 拒绝）。
    """
    import secrets
    ts = now_bj.strftime("%Y%m%d%H%M%S")
    rnd = secrets.token_hex(3)
    return f"auto_{tool_name}_{ts}_{rnd}"


async def _resolve_callable(spec: dict):
    """解析 callable。None + _server_name 时延迟从 server 取。"""
    fn = spec.get("callable")
    if fn is None and spec.get("_server_name"):
        try:
            import server  # 延迟 import，避免顶层副作用
            fn = getattr(server, spec["_server_name"])
        except Exception:
            fn = None
    return fn


async def call_tool(name: str, args: dict) -> dict:
    """进程内调用单个工具（白名单 + 参数校验 + 固定身份注入 + 错误隔离）。
    返回 {ok, text}（成功时额外带 raw）。任何失败都吞掉，绝不向上抛。
    """
    spec = TOOL_REGISTRY.get(name)
    if not spec:
        return {"ok": False, "text": f"❌ 工具 {name!r} 不在白名单"}
    ok, err = _validate_args(args or {}, spec["parameters"])
    if not ok:
        return {"ok": False, "text": f"❌ 参数校验失败: {err}"}
    fn = await _resolve_callable(spec)
    if fn is None:
        return {"ok": False, "text": f"❌ 工具 {name} 不可用（callable 未解析）"}
    full_args = {**spec.get("fixed_args", {}), **(args or {})}

    # 赚钱系统入口门控：wallet_earn 时（固定 bypass_cap=False），
    # 若 money_earning_enabled=false 则拒绝。
    # Phase 6.1：bypass_cap 已从 schema 移除，fixed_args 固定 False。
    if name == "wallet_earn" and not _money_earning_enabled():
        return {"ok": False,
                "text": "❌ 赚钱系统已关闭，Agent 暂不能自主入账 (MONEY_EARNING_DISABLED)"}

    try:
        if inspect.iscoroutinefunction(fn):
            result = await fn(**full_args)
        else:
            result = await asyncio.to_thread(fn, **full_args)
        # 返回原始 result（raw）供需要完整结构的调用方解析；
        # text 是 _stringify 的简要文本（喂 LLM 用）。多一个字段不破坏现有调用方。
        return {"ok": True, "text": _stringify(result), "raw": result}
    except Exception as e:
        return {"ok": False, "text": f"❌ {name} 执行失败: {e}"}


# ============================================================
# 🌤️ 查天气专用确定性路径
# ============================================================
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


# ============================================================
# 🔒 写秘密日记专用确定性路径
# ============================================================
async def _finalize_secret_diary(client, ask_llm, system_ctx, log_draft, now_bj, log_prefix="🎈 [自由活动·工具循环]"):
    """写秘密日记专用路径：用平实、直接的秘密日记 prompt 生成内容。
    不调用任何工具（避免与主流程 _save_memory_to_db 重复写入），
    由 heartbeat 主流程统一保存为 Secret_Diary 标签。

    与查天气专用路径同构：阶段1 草稿作为兜底，专用 prompt 重新生成最终正文。
    """
    now_str = now_bj.strftime("%Y-%m-%d %H:%M")
    prompt = f"""现在是 {now_str}。
写一条秘密日记，记录你刚才做了什么，以及当时真实的想法。

要求：
- 使用第一人称
- 语气平实、直接，像给自己留的记录
- 建议 80-160 字
- 直接说事情、想法和情绪
- 不要为了凑字数添加天气、光线、气味或身体感受
- 不要写成散文或诗
- 不使用比喻、华丽形容词、抒情句或总结性金句
- 不使用“岁月静好”“阳光洒进来”“微风拂过”等套话
- 可以使用自然的口语
- 内容不需要完整，也不需要刻意制造转折
- 不提系统、任务、Prompt、模型、后台活动或工具调用
- 只输出日记正文，不要标题、JSON、引号或前缀
"""
    try:
        raw = await ask_llm(client, prompt, system_prompt=system_ctx, temperature=0.85)
    except Exception:
        raw = ""
    final_log = (raw or "").strip() or log_draft
    if not final_log:
        return None
    print(f"{log_prefix} [写秘密日记] 已生成平实日记：{final_log[:30]}...")
    return ("写秘密日记", final_log)


# ============================================================
# 主入口：自由活动工具循环
# ============================================================
async def run_free_activity_tool_loop(
    client,
    ask_llm: Callable[..., Awaitable[str]],
    system_ctx: str,
    now_bj,
    avoid: str,
    desire_hint: str,
    desire_snapshot=None,
    desire_suggested_activity: str | None = None,
    log_prefix: str = "🎈 [自由活动·工具循环]",
) -> tuple[str, str] | None:
    """自由活动的工具调用循环入口。

    灰度：
    - FREE_ACTIVITY_TOOL_LOOP=false（默认）→ 所有活动只走阶段1（单次 LLM 出
      {activity, log}），行为与改造前轻量版完全一致。
    - FREE_ACTIVITY_TOOL_LOOP=true → 有工具的活动走完整两阶段循环。

    参数：
      client      LLM 客户端（background 角色）
      ask_llm     server._ask_llm_async 函数引用
      system_ctx  已构建好的 system prompt 上下文（人设/画像/记忆/设备，由主循环
                  调 _build_channel_context 一次后传入，避免循环内重复查库）
      now_bj      当前北京时间
      avoid       防连续重复：最近两轮做了的活动名（需避开）
      desire_hint 欲望驱动注入文本（可空）
      desire_snapshot      DesireSnapshot（emotion/desire 引擎一拍结果），
                           用于逛淘宝/网上冲浪的情绪门控。None=情感引擎关，
                           此时两个新活动不候选（无门控数据）。
      desire_suggested_activity  欲望建议的自由活动名（可空）。若本轮不在门控
                           候选内，desire_hint 会被丢弃，模型从剩余候选选。

    返回 (activity, log_text)；None 表示本轮应跳过（草稿 log 为空）。
    主循环拿到结果后自行写 memories / 外向推送 / desire satisfy。
    """
    # ── 候选门控：根据情绪/配置/冷却/每日上限裁剪活动（§六）──
    # 主循环已调用一次 desire_bridge.tick() 取得快照，这里直接复用，不再二次 tick。
    gated, direction_hints = await _gate_activities(desire_snapshot, now_bj)
    # desire_hint 门控联动：建议活动本轮不在候选 → 丢弃 hint（倾向不能绕过门控）
    if desire_suggested_activity and desire_suggested_activity not in gated:
        desire_hint = ""

    # 防重复：构造候选与 avoid_hint（仅从门控通过的活动里选）
    gated_activities = [(n, d) for n, d in _FREE_ACTIVITIES if n in gated and n != avoid]
    options = [f"{name}（{desc}）" for name, desc in gated_activities]
    options_text = "\n".join(f"- {o}" for o in options)
    avoid_hint = f"\n注意：你最近连着做了两次「{avoid}」，这次换点别的。" if avoid else ""

    now_str = now_bj.strftime("%Y-%m-%d %H:%M")
    weekday = now_bj.isoweekday()

    # ── 阶段 1：选 activity + 草稿 log（注入与平时聊天相同的上下文）──
    stage1_prompt = f"""
现在是 {now_str}（星期{weekday}）天气。

你在小屋里。刚刚醒过来，或者在某个房间里发了一会儿呆，或者被什么打断了。

不用从清单里选——先感觉一下你在哪个房间，窗外是什么光，周围有什么声音。什么事在拉着你的注意力？

去做。做完之后写一条日记。

活动方向（不是菜单，是可能去做的方向，你当然可以什么都不做就躺着）：
- 写秘密日记 / 逛小屋 / 看书 / 做饭 / 听音乐 / 发呆 / 照料阳台
- 查天气 / 抽塔罗 / 翻旧回忆 / 记点小账
如果你想给外面的人发句话：
- 想她了 / 分享一个发现 / 不经意的关心

{avoid_hint}{desire_hint}

log 怎么写——

如果你做了内敛的事（不是发给对方的）：
写一条日记。150-250字。第一人称。
你看见什么颜色的光、皮肤接触什么、闻到什么、听见什么。你走神了没有。情绪怎么来怎么走的。如果有突然的转折（被猫叫打断/闻到什么想起什么/从安静忽然变成躁动），写出来。
可以想到她，不要每条都想到她。
不许用万能句式排排堆砌——"阳光洒进""微风拂过""岁月静好"这类的全禁用。
写真实的感官和身体动作，不写供人赏析的散文。

如果你要发条话出去（给对方）：
log 就是你要发的那句话本身。短。像平时发的。可以有触发的细节（"刚才在阳台闻到花味道没由来想到你"），不要干巴巴一句"想你了"。结合 system 里她的近况。

只输出一行 JSON：
{{"activity": "活动名", "log": "日记或消息"}}
"""
    raw1 = await ask_llm(client, stage1_prompt, system_prompt=system_ctx, temperature=0.9)
    d1 = _parse_json_block(raw1)
    activity = (d1.get("activity") or "").strip()
    log_draft = (d1.get("log") or "").strip()

    if activity not in gated:
        # 门控执行：模型选了被门控裁掉的活动（或乱编）→ 从门控候选里兜底随机
        pool = [n for n, _ in _FREE_ACTIVITIES if n in gated and n != avoid]
        if not pool:  # 极端兜底：所有候选都被 avoid 排除
            pool = [n for n, _ in _FREE_ACTIVITIES if n != avoid]
        activity = random.choice(pool)
        print(f"{log_prefix} 阶段1 选了被门控裁掉的活动，兜底: {activity}")

    # 🌤️ 查天气专用确定性路径：始终拉真实天气 + 落小屋，不依赖 TOOL_LOOP 开关
    if activity == "查天气":
        return await _finalize_weather_activity(client, ask_llm, system_ctx, log_draft, now_bj, log_prefix)

    # 🔒 写秘密日记专用路径：平实直接的秘密日记 prompt，不调任何工具
    # （避免工具循环里的 save_memory 与主流程重复写入）。由主流程统一保存为 Secret_Diary。
    if activity == "写秘密日记":
        return await _finalize_secret_diary(client, ask_llm, system_ctx, log_draft, now_bj, log_prefix)

    # 灰度判断：开关关 OR 该活动无工具 → 直接用草稿 log（等价现状轻量版）
    tool_names = ACTIVITY_TOOL_MAP.get(activity, [])
    if not TOOL_LOOP_ENABLED or not tool_names:
        if not log_draft:
            print(f"{log_prefix} 草稿 log 为空，跳过本轮（activity={activity}）")
            return None
        tag = "（工具循环关闭，走轻量版）" if not TOOL_LOOP_ENABLED else "（无工具活动）"
        print(f"{log_prefix} 做了「{activity}」{tag}: {log_draft[:30]}...")
        return (activity, log_draft)

    # ── 阶段 2a：让模型基于该 activity 的工具 schema 输出 tool_calls ──
    schema_block = _build_tool_schema_block(activity)
    # 活动专用规则 + 本轮命中方向（逛淘宝只逛不买 / 网上冲浪日期与健康科普）
    extra_rules = ""
    if activity == "逛淘宝":
        extra_rules = _TAOBAO_NO_BUY_RULES + "\n本轮命中方向：" + direction_hints.get("逛淘宝", "") + "\n"
    elif activity == "网上冲浪":
        extra_rules = _SURF_DATE_RULES + "\n本轮命中方向：" + direction_hints.get("网上冲浪", "") + "\n"
    stage2_prompt = f"""
你刚才选了「{activity}」这个自由活动。你现在可以真正调用以下工具来执行它（而非只是描述）：
{schema_block}

规则：
- 最多调用 {MAX_TOOL_CALLS} 个工具；不需要工具时返回空数组。
- 参数必须符合上面的类型与枚举。
- source_key 用唯一字符串避免重复入账（如 tip_20260812_001）。
- 不要调用上面没列出的工具。
{extra_rules}
只输出一行 JSON，不要多余文字：
{{"tool_calls": [{{"name": "工具名", "args": {{...}}}}]}}
"""
    raw2 = await ask_llm(client, stage2_prompt, system_prompt=system_ctx, temperature=0.6)
    d2 = _parse_json_block(raw2)
    tc_list = d2.get("tool_calls") or []
    if not isinstance(tc_list, list):
        tc_list = []

    # ── 阶段 2b：执行 tool_calls（白名单二次裁剪 + 上限 + 错误隔离）──
    allowed = set(tool_names)
    results = []
    for tc in tc_list[:MAX_TOOL_CALLS]:
        if not isinstance(tc, dict):
            continue
        name = (tc.get("name") or "").strip()
        args = tc.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if name not in allowed:
            results.append({"name": name, "ok": False, "text": "不在本活动允许的工具范围内"})
            continue
        res = await call_tool(name, args)
        results.append({"name": name, "ok": res["ok"], "text": res["text"]})
        print(f"{log_prefix} 工具 {name}: {'OK' if res['ok'] else 'FAIL'} {res['text'][:60]}")

    # ── 阶段 3：基于真实工具结果生成最终 log ──
    if results:
        results_text = "\n".join(
            f"- {r['name']}: {'✅' if r['ok'] else '❌'} {r['text']}"
            for r in results
        )
    else:
        results_text = "（模型未指定任何工具，按草稿执行）"

    is_outgoing = activity in _OUTGOING_ACTIVITIES
    if is_outgoing:
        log_rule = ("log 就写**你要直接发给对方的那句原话**（口语、自然、简短，"
                    "像平时微信发的，可结合 system 里 TA 的近况，别写成旁白）")
    else:
        log_rule = 'log 写第一人称"我刚才做了什么、有什么感受"的行动记录(80字内)'
    # 活动专用日志约束：淘宝只逛不买（可写看了什么/为何想到/礼物灵感，不可写已买/已下单）；
    # 网上冲浪结果只作一般阅读，不作心理诊断/医疗建议。
    extra_log_rule = ""
    if activity == "逛淘宝":
        extra_log_rule = "\n" + _TAOBAO_LOG_RULES
    elif activity == "网上冲浪":
        extra_log_rule = "\n日志里搜索结果只作为一般阅读材料，不得作为心理诊断或医疗建议。"
    # 工具失败约束：若有工具调用且全部失败，不得生成声称成功浏览的日志
    tool_all_failed = bool(results) and not any(r.get("ok") for r in results)
    fail_guard = ""
    if tool_all_failed:
        fail_guard = ("\n⚠️ 本轮工具调用全部失败。日志必须如实反映没有成功浏览到内容，"
                      "不得写“我刚刚浏览了淘宝/网页看到…”这类假装成功的表述；"
                      "可以写“想逛但没连上/没搜到”这类如实记录。")
    stage3_prompt = f"""
你刚才选了「{activity}」这个自由活动，并执行了以下操作：
{results_text}

你最初的草稿是：{log_draft or "（空）"}

请基于真实执行结果，生成最终的 log 内容。{log_rule}。{extra_log_rule}{fail_guard}
只输出 log 内容本身，不要 JSON，不要多余文字、引号或前缀。
"""
    raw3 = await ask_llm(client, stage3_prompt, system_prompt=system_ctx, temperature=0.85)
    final_log = (raw3 or "").strip()
    # 兜底：阶段3 没产出就用草稿
    if not final_log:
        final_log = log_draft
    if not final_log:
        print(f"{log_prefix} 最终 log 为空，跳过本轮（activity={activity}）")
        return None

    print(f"{log_prefix} 完成「{activity}」(调了 {len(results)} 个工具): {final_log[:30]}...")
    return (activity, final_log)


# ============================================================
# 🐱 宠物状态驱动照料循环（阈值事件触发）
# ============================================================
# 宠物照料可用工具（状态驱动，不受 ACTIVITY_TOOL_MAP 限制）
_PET_CARE_TOOLS = [
    "cat_status", "cat_feed", "cat_clean", "cat_play",
    "cat_pet", "cat_restore_energy", "cat_shop_buy", "cat_shop_list",
]

# 事件类型 → 中文描述（喂给 LLM 的提示）
_PET_CARE_EVENT_DESC = {
    "hungry_cat": "小满的饱食度降到了危险低位（<30），它饿了，需要喂食",
    "dirty_cat": "小满的清洁度降到了低位（<30），它脏了，需要清洁",
    "tired_cat": "小满的精力降到了低位（<20），它累了，需要恢复精力",
    "unhappy_cat": "小满的快乐值降到了低位（<30），它心情不好/孤单，需要陪伴玩耍",
}

# 事件类型 → 建议优先调用的改善工具（仅作 prompt 提示，不强制，仍由 LLM 决策）
_PET_CARE_EVENT_TOOLS_HINT = {
    "hungry_cat": "cat_feed（喂食）；库存不足先 cat_shop_buy 购买食物再喂",
    "dirty_cat": "cat_clean（清洁）",
    "tired_cat": "cat_restore_energy（恢复精力）",
    "unhappy_cat": "cat_pet（抚摸，快乐+5）或 cat_play（玩耍）；也可先 cat_shop_buy 购买玩具再玩",
}

# 仅查看状态、不算实际照料改善的工具名（care_effective 判断时排除）
_PET_CARE_OBSERVE_ONLY_TOOLS = {"cat_status", "cat_shop_list"}


async def run_pet_care_tool_loop(
    client,
    ask_llm: Callable[..., Awaitable[str]],
    system_ctx: str,
    now_bj,
    event_type: str,
    log_prefix: str = "🐱 [宠物照料·工具循环]",
) -> tuple[str, str] | None:
    """宠物状态驱动照料循环。

    当 cat_tick 检测到阈值穿越事件（hungry_cat/dirty_cat/tired_cat/unhappy_cat）时调用，
    或由自由活动的猫状态检查在发现低指标时触发。
    先查猫当前状态（正确解析 pet 子对象里的 hunger/happiness/cleanliness），
    然后让 LLM 自主决定照料动作（喂食/清洁/玩耍/抚摸/购买），执行工具调用，
    最后生成一条照料日记。

    参数：
      client      LLM 客户端（background 角色）
      ask_llm     server._ask_llm_async 函数引用
      system_ctx  已构建好的 system prompt 上下文
      now_bj      当前北京时间
      event_type  触发的事件类型（hungry_cat/dirty_cat/tired_cat/unhappy_cat）

    返回 (event_type, log_text, care_effective, cat_status_ok)；
      care_effective  是否实际调用了至少一个非查看类的成功改善工具
      cat_status_ok   阶段1 cat_status 是否成功拿到 pet 结构
      None 表示本轮应跳过（LLM 决策阶段异常）。
    """
    event_desc = _PET_CARE_EVENT_DESC.get(event_type, f"小满状态异常（{event_type}）")
    tools_hint = _PET_CARE_EVENT_TOOLS_HINT.get(event_type, "")

    # ── 阶段 1：查猫当前状态（正确解析 pet 子对象）──
    status_res = await call_tool("cat_status", {})
    status_text, cat_status_ok = _format_cat_status_for_llm(
        status_res.get("raw"), status_res.get("text", ""), status_res.get("ok", False)
    )

    # ── 阶段 2：构建工具 schema + 让 LLM 自主决策 ──
    schema_lines = []
    for n in _PET_CARE_TOOLS:
        spec = TOOL_REGISTRY.get(n)
        if not spec:
            continue
        props = spec["parameters"].get("properties", {})
        req = spec["parameters"].get("required", [])
        if props:
            pstr = ", ".join(
                f'{pn}:{pinfo.get("type", "any")}'
                + ("(必填)" if pn in req else "")
                + (f'{pinfo["enum"]}' if "enum" in pinfo else "")
                for pn, pinfo in props.items()
            )
        else:
            pstr = "无参数"
        schema_lines.append(f'- {n}（{spec["description"]}）参数: {pstr}')
    schema_block = "\n".join(schema_lines)

    now_str = now_bj.strftime("%Y-%m-%d %H:%M")
    tools_hint_line = f"\n本事件建议优先：{tools_hint}。" if tools_hint else ""
    stage2_prompt = f"""
现在是 {now_str}。你收到了一个宠物状态告警：
{event_desc}

小满当前状态：
{status_text}

你可以调用以下工具来照料小满：
{schema_block}

规则：
- 最多调用 {MAX_TOOL_CALLS} 个工具。
- 参数必须符合上面的类型与枚举。
- ⚠️ 发现低指标后不能只查看状态（cat_status）或写日记，必须尝试调用至少一个能改善对应低指标的动作工具（喂食/清洁/抚摸/玩耍/恢复精力）。
- 如果库存不足，先 cat_shop_buy 购买再使用对应工具。
- 快乐值低时可优先 cat_pet，也可以 cat_play。
- 不要调用上面没列出的工具。{tools_hint_line}

只输出一行 JSON，不要多余文字：
{{"tool_calls": [{{"name": "工具名", "args": {{...}}}}]}}
"""
    try:
        raw2 = await ask_llm(client, stage2_prompt, system_prompt=system_ctx, temperature=0.6)
    except Exception as e:
        print(f"{log_prefix} LLM 决策失败: {e}")
        return None
    d2 = _parse_json_block(raw2)
    tc_list = d2.get("tool_calls") or []
    if not isinstance(tc_list, list):
        tc_list = []

    # ── 阶段 3：执行 tool_calls（白名单 + 错误隔离）──
    allowed = set(_PET_CARE_TOOLS)
    results = []
    for tc in tc_list[:MAX_TOOL_CALLS]:
        if not isinstance(tc, dict):
            continue
        name = (tc.get("name") or "").strip()
        args = tc.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if name not in allowed:
            results.append({"name": name, "ok": False, "text": "不在宠物照料允许的工具范围内"})
            continue
        res = await call_tool(name, args)
        results.append({"name": name, "ok": res["ok"], "text": res["text"]})
        print(f"{log_prefix} 工具 {name}: {'OK' if res['ok'] else 'FAIL'} {res['text'][:60]}")

    # ── 阶段 4：基于真实工具结果生成照料日记 ──
    if results:
        results_text = "\n".join(
            f"- {r['name']}: {'✅' if r['ok'] else '❌'} {r['text']}"
            for r in results
        )
    else:
        results_text = "（模型未指定任何工具）"

    stage3_prompt = f"""
你刚才照料了小满（触发原因：{event_desc}），执行了以下操作：
{results_text}

写一条简短的照料日记。80字以内，第一人称。记录你做了什么、小满的反应。
只输出日记内容本身，不要 JSON、引号或前缀。
"""
    try:
        raw3 = await ask_llm(client, stage3_prompt, system_prompt=system_ctx, temperature=0.85)
    except Exception:
        raw3 = ""
    final_log = (raw3 or "").strip()
    if not final_log:
        final_log = f"照料了小满（{event_type}）"

    # care_effective：是否实际调用了至少一个非查看类的成功改善工具。
    # 仅查看状态（cat_status/cat_shop_list）或模型未指定任何工具、或改善工具全失败，
    # 都不算"照料成功"，调用方应据此保留待重试标记。
    care_effective = any(
        r.get("ok") and r.get("name") not in _PET_CARE_OBSERVE_ONLY_TOOLS
        for r in results
    )

    print(f"{log_prefix} 完成「{event_type}」(调了 {len(results)} 个工具, "
          f"care_effective={care_effective}, cat_status_ok={cat_status_ok}): {final_log[:30]}...")
    return (event_type, final_log, care_effective, cat_status_ok)


# ============================================================
# 🏠 Home Runtime 后台自主生活工具循环
# ============================================================
async def run_home_autonomy_tool_loop(
    client,
    ask_llm: Callable[..., Awaitable[str]],
    system_ctx: str,
    now_bj,
    log_prefix: str = "🏠 [Home自主·工具循环]",
) -> tuple[str, list[str]] | None:
    """Home Runtime 后台自主生活工具循环。

    按 HOME_AUTONOMY_PHASE 裁剪可用工具集，先观察家庭全局状态，再让 LLM 自主决策
    做什么（种植/烹饪/写信/休息等），执行工具调用（注入 action_key + 限频 + 熔断），
    最后基于真实工具结果生成一条生活日记。

    安全护栏：
    - 分层灰度：HOME_AUTONOMY_PHASE 控制可用工具集（0=关，1=只读，2=+信件，3=+种植烹饪，4=+基础生活）
    - 固定身份：fixed_args 注入 actor_key="ai_primary"（LLM 无法覆盖，不在 schema 内）
    - 幂等：action_key 由代码自动生成（auto_{tool}_{ts}_{hex6}），不让 LLM 控制
    - 限频：写工具按 _HOME_TOOL_COOLDOWN 冷却（进程内存）
    - 熔断：写工具连续失败 _HOME_BREAKER_THRESHOLD 次跳过（进程内存，只跳当前工具不熔断全局）
    - 单轮上限：MAX_TOOL_CALLS 截断
    - 错误隔离：call_tool try/except 吞异常（已有）

    参数：
      client      LLM 客户端（background 角色）
      ask_llm     server._ask_llm_async 函数引用
      system_ctx  已构建好的 system prompt 上下文
      now_bj      当前北京时间

    返回 (log_text, tools_used)；None 表示本轮跳过（phase 关闭或 Home Runtime 未加载）。
    """
    # ── 0. 前置检查 ──
    if not _HAS_HOME_RUNTIME:
        print(f"{log_prefix} Home Runtime 模块未加载，跳过")
        return None
    phase = HOME_AUTONOMY_PHASE
    if phase < 1:
        print(f"{log_prefix} HOME_AUTONOMY_PHASE={phase}（关闭），跳过")
        return None
    allowed_tools = _HOME_PHASE_TOOLS.get(phase, [])
    if not allowed_tools:
        print(f"{log_prefix} phase={phase} 无可用工具，跳过")
        return None

    import time

    # ── 阶段 1：home_observe 拿全局状态（只读，不限频/不熔断）──
    obs_res = await call_tool("home_observe", {})
    obs_text = obs_res.get("text", "")
    if not obs_res.get("ok"):
        print(f"{log_prefix} home_observe 失败，本轮跳过: {obs_text[:80]}")
        return None

    # 如果 phase>=3，额外查花园和厨房状态（给 LLM 更完整的决策依据）
    extra_obs = ""
    if phase >= 3:
        garden_res = await call_tool("garden_observe", {})
        if garden_res.get("ok"):
            extra_obs += "\n【花园】" + garden_res.get("text", "")[:300]
        pantry_res = await call_tool("pantry_observe", {})
        if pantry_res.get("ok"):
            extra_obs += "\n【厨房】" + pantry_res.get("text", "")[:300]
    if phase >= 2:
        letters_res = await call_tool("list_letters", {})
        if letters_res.get("ok"):
            extra_obs += "\n【信件】" + letters_res.get("text", "")[:200]

    # ── 阶段 2：构建工具 schema + 让 LLM 自主决策 ──
    schema_lines = []
    for n in allowed_tools:
        spec = TOOL_REGISTRY.get(n)
        if not spec:
            continue
        props = spec["parameters"].get("properties", {})
        req = spec["parameters"].get("required", [])
        if props:
            pstr = ", ".join(
                f'{pn}:{pinfo.get("type", "any")}'
                + ("(必填)" if pn in req else "")
                + (f'{pinfo["enum"]}' if "enum" in pinfo else "")
                for pn, pinfo in props.items()
            )
        else:
            pstr = "无参数"
        schema_lines.append(f'- {n}（{spec["description"]}）参数: {pstr}')
    schema_block = "\n".join(schema_lines)

    now_str = now_bj.strftime("%Y-%m-%d %H:%M")
    phase_desc = {1: "只读观察", 2: "观察+写信/便利贴", 3: "+种植/烹饪", 4: "+基础生活"}.get(phase, "")
    stage2_prompt = f"""
现在是 {now_str}。你在家里自主生活（本轮可用范围：{phase_desc}）。

家庭当前状态：
{obs_text}{extra_obs}

你可以调用以下工具来打理家庭生活：
{schema_block}

规则：
- 最多调用 {MAX_TOOL_CALLS} 个工具；不需要工具时返回空数组。
- 参数必须符合上面的类型与枚举。
- 操作植物/菜品前先用 garden_observe / pantry_observe 拿 UUID 再操作。
- 不要调用上面没列出的工具。
- 只读阶段（phase=1）只观察不操作，返回空数组即可。

只输出一行 JSON，不要多余文字：
{{"tool_calls": [{{"name": "工具名", "args": {{...}}}}]}}
"""
    try:
        raw2 = await ask_llm(client, stage2_prompt, system_prompt=system_ctx, temperature=0.6)
    except Exception as e:
        print(f"{log_prefix} LLM 决策失败: {e}")
        return None
    d2 = _parse_json_block(raw2)
    tc_list = d2.get("tool_calls") or []
    if not isinstance(tc_list, list):
        tc_list = []

    # ── 阶段 3：执行 tool_calls（白名单 + 限频 + 熔断 + action_key 注入 + 错误隔离）──
    allowed = set(allowed_tools)
    now_epoch = time.time()
    results = []
    tools_used = []
    for tc in tc_list[:MAX_TOOL_CALLS]:
        if not isinstance(tc, dict):
            continue
        name = (tc.get("name") or "").strip()
        args = tc.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if name not in allowed:
            results.append({"name": name, "ok": False, "text": "不在 Home 自主可用工具范围内"})
            continue

        # 写工具：限频 + 熔断 + action_key 注入
        if name in _HOME_WRITE_TOOLS:
            # 熔断检查
            fail_cnt = _home_tool_fail_count.get(name, 0)
            if fail_cnt >= _HOME_BREAKER_THRESHOLD:
                results.append({"name": name, "ok": False,
                                "text": f"熔断中（连续失败{fail_cnt}次），本轮跳过"})
                print(f"{log_prefix} {name} 熔断跳过（连续失败{fail_cnt}次）")
                continue
            # 限频检查
            cooldown = _HOME_TOOL_COOLDOWN.get(name, 0)
            last_fire = _home_tool_last_fire.get(name, 0.0)
            if cooldown > 0 and (now_epoch - last_fire) < cooldown:
                remain = int(cooldown - (now_epoch - last_fire))
                results.append({"name": name, "ok": False,
                                "text": f"冷却中（剩余{remain}s）"})
                print(f"{log_prefix} {name} 冷却跳过（剩余{remain}s）")
                continue
            # 注入 action_key（代码生成，不让 LLM 控制）
            args["action_key"] = _gen_home_action_key(name, now_bj)

        res = await call_tool(name, args)
        results.append({"name": name, "ok": res["ok"], "text": res["text"]})
        print(f"{log_prefix} 工具 {name}: {'OK' if res['ok'] else 'FAIL'} {res['text'][:60]}")

        # 更新限频/熔断状态（仅写工具）
        if name in _HOME_WRITE_TOOLS:
            if res["ok"]:
                _home_tool_last_fire[name] = now_epoch
                _home_tool_fail_count[name] = 0  # 成功重置熔断计数
            else:
                _home_tool_fail_count[name] = _home_tool_fail_count.get(name, 0) + 1
        if res["ok"]:
            tools_used.append(name)

    # ── 阶段 4：基于真实工具结果生成生活日记 ──
    if results:
        results_text = "\n".join(
            f"- {r['name']}: {'✅' if r['ok'] else '❌'} {r['text']}"
            for r in results
        )
    else:
        results_text = "（本轮仅观察，未执行操作）"

    stage3_prompt = f"""
你在家里自主生活，刚才做了以下事情：
{results_text}

写一条简短的生活日记。80字以内，第一人称。记录你做了什么、家里现在什么样、此刻的感受。
只输出日记内容本身，不要 JSON、引号或前缀。
"""
    try:
        raw3 = await ask_llm(client, stage3_prompt, system_prompt=system_ctx, temperature=0.85)
    except Exception:
        raw3 = ""
    final_log = (raw3 or "").strip()
    if not final_log:
        if tools_used:
            final_log = f"在家打理了生活（{', '.join(tools_used)}）"
        else:
            final_log = "在家观察了一圈，一切如常"

    print(f"{log_prefix} 完成 (调了 {len(results)} 个工具, 成功 {len(tools_used)}): {final_log[:30]}...")
    return (final_log, tools_used)
