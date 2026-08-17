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

# ============================================================
# 环境变量（灰度开关 + 上限）
# ============================================================
TOOL_LOOP_ENABLED = os.environ.get("FREE_ACTIVITY_TOOL_LOOP", "true").strip().lower() in ("1", "true", "yes")
MAX_TOOL_CALLS = int(os.environ.get("FREE_ACTIVITY_TOOL_MAX_CALLS", "5"))

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
]
_OUTGOING_ACTIVITIES = {"想对方了", "分享发现", "偷偷关心"}
_VALID_ACTIVITY_NAMES = {name for name, _ in _FREE_ACTIVITIES}
_OUT_NAMES = "、".join(_OUTGOING_ACTIVITIES)


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
            "bypass_cap=false（默认）：接活赚钱，计入周上限80元，超额部分按50%进加班银行。"
            "适用场景：完成自主任务后领取报酬，如写随笔/观察笔记/短篇(5-10元)、研究话题整理笔记(8-15元)、给小屋做建设(5-12元)。"
            "bypass_cap=true：不计周上限、不进加班银行。适用场景：每周零花钱(source_key=allowance_YYYYW##)、打赏(source_key=tip_<时间戳>)。"
            "source_key 用于幂等防重，相同 source_key 重复调用会被拒绝。",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "金额（CNY）"},
                "source_key": {"type": "string", "description": "唯一标识防重复入账，如 task_essay_20260814_001 或 allowance_2026W33"},
                "reason": {"type": "string", "description": "入账理由"},
                "bypass_cap": {"type": "boolean", "description": "false=接活赚钱(计周上限)，true=零花钱/打赏(不计周上限)"},
            },
            "required": ["amount", "source_key", "reason"],
        },
        "callable": _hs.wallet_earn,
        "fixed_args": {"wallet_id": _hs.DEFAULT_WALLET_ID, "meta": {}},
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
}


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
# 核心：进程内调用单个工具
# ============================================================
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

    # 赚钱系统入口门控：wallet_earn 且 bypass_cap=False（Agent 自主赚钱）时，
    # 若 money_earning_enabled=false 则拒绝。bypass_cap=True（零花钱/打赏）不受影响。
    # 这是最终入口门控，防止仅在前端/暴露层隐藏后被 MCP 直调绕过。
    if name == "wallet_earn" and not _money_earning_enabled():
        bypass_cap = bool(full_args.get("bypass_cap", False))
        if not bypass_cap:
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

    返回 (activity, log_text)；None 表示本轮应跳过（草稿 log 为空）。
    主循环拿到结果后自行写 memories / 外向推送 / desire satisfy。
    """
    # 防重复：构造候选与 avoid_hint
    options = [f"{name}（{desc}）" for name, desc in _FREE_ACTIVITIES if name != avoid]
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

    if activity not in _VALID_ACTIVITY_NAMES:
        activity = random.choice([n for n, _ in _FREE_ACTIVITIES if n != avoid])
        print(f"{log_prefix} 阶段1 未按格式选活动，兜底: {activity}")

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
    stage2_prompt = f"""
你刚才选了「{activity}」这个自由活动。你现在可以真正调用以下工具来执行它（而非只是描述）：
{schema_block}

规则：
- 最多调用 {MAX_TOOL_CALLS} 个工具；不需要工具时返回空数组。
- 参数必须符合上面的类型与枚举。
- source_key 用唯一字符串避免重复入账（如 tip_20260812_001）。
- 不要调用上面没列出的工具。

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
    stage3_prompt = f"""
你刚才选了「{activity}」这个自由活动，并执行了以下操作：
{results_text}

你最初的草稿是：{log_draft or "（空）"}

请基于真实执行结果，生成最终的 log 内容。{log_rule}。
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
