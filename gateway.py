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
import threading
import requests

# ---------- 日志静音：屏蔽 httpx 的 "HTTP Request: ..." 请求噪音 ----------
# supabase-py 底层 (postgrest→httpx) 在 INFO 级别逐条打印数据库请求日志
#   HTTP Request: GET https://xxx.supabase.co/rest/v1/... "HTTP/2 200 OK"
# 把 httpx 日志级别抬到 WARNING，只保留网关自己的活动日志（宠物 tick / 聊天注入等）。
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

# ==========================================
# 全局连接（延迟初始化，避免启动时无 Supabase 就崩）
# ==========================================
_supabase_client = None
_system_logs_buffer = []   # 简易日志缓存（用于 /api/logs）
_MAX_LOGS = 200
_injected_prompts_buffer = []   # 最近注入的 volatile_block 快照（供 /api/prompts，只留最新5条）
_MAX_INJECTED_PROMPTS = 5


def _log(msg: str):
    """统一的日志打印 + 内存缓存（供 /api/logs 查询）"""
    line = f"[{datetime.datetime.utcnow().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _system_logs_buffer.append(line)
    if len(_system_logs_buffer) > _MAX_LOGS:
        del _system_logs_buffer[: len(_system_logs_buffer) - _MAX_LOGS]


def _capture_injected_prompt(rec: dict):
    """记录最近一次注入的 volatile_block 快照（只留最新 N 条，供 /api/prompts 调试面板查看）。"""
    try:
        _injected_prompts_buffer.append(rec)
        if len(_injected_prompts_buffer) > _MAX_INJECTED_PROMPTS:
            del _injected_prompts_buffer[: len(_injected_prompts_buffer) - _MAX_INJECTED_PROMPTS]
    except Exception:
        pass


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

# 三种模型身份角色（见下 _ROLES 文档与 resolve_llm_role）。
# chat         —— 实时聊天（网页 /v1/chat/completions、TG 轮询、QQ NapCat 的即时回复）
# compression  —— 阶段总结、历史压缩、日记压缩等压缩类任务
# background   —— 自由活动、主动问候、后台思考等后台活动
# 同一模型可同时承担多个角色；被禁用的模型不能被分配角色。
_LLM_ROLES = ("chat", "compression", "background")


def _normalize_registry(reg: dict) -> dict:
    """把任意历史版本的注册表归一化为 v2 结构 {schema_version, models, default, roles}。

    兼容：
      - 旧版只有 {models, default}（无 roles）→ roles.chat_default=default，roles.chat=[default]
      - v3.9 临时字段 assignments:{background: id} → 迁入 roles.background
      - 已有 roles（v2）→ 原样保留，但补全缺失字段
    幂等：对已是 v2 的结构再跑一次结果不变。
    """
    models = reg.get("models") if isinstance(reg.get("models"), list) else []
    default = str(reg.get("default", "") or "").strip()
    roles = reg.get("roles") if isinstance(reg.get("roles"), dict) else {}

    # 归一化每个模型项的 thinking 字段为 auto/on/off（向后兼容旧数据）
    # 归一化 extra_keys：同一 base_url 下的额外密钥列表（多 key 轮询）
    for _m in models:
        if isinstance(_m, dict):
            _m["thinking"] = _normalize_thinking(_m.get("thinking"))
            ek = _m.get("extra_keys")
            if isinstance(ek, list):
                _m["extra_keys"] = [str(k).strip() for k in ek if str(k).strip()]
            else:
                _m["extra_keys"] = []

    # 旧 assignments 字段迁移到 roles（仅当 roles.background 未设置时）
    assignments = reg.get("assignments") if isinstance(reg.get("assignments"), dict) else {}
    for r in _LLM_ROLES:
        if r not in roles or roles[r] in (None, "", []):
            av = assignments.get(r)
            if av:
                roles[r] = av

    # chat_default 缺失时回退到 default / 第一个启用模型
    if not roles.get("chat_default"):
        roles["chat_default"] = default
    if not isinstance(roles.get("chat"), list):
        # 旧版无 chat 列表：用 chat_default（若仍是有效模型）作为唯一聊天模型
        cd = roles.get("chat_default")
        roles["chat"] = [cd] if cd else []
    # compression / background 支持多值（端点轮询池）；旧的单值字符串兼容为单元素列表
    for r in ("compression", "background"):
        v = roles.get(r)
        if isinstance(v, list):
            roles[r] = [str(x).strip() for x in v if str(x).strip()]
        elif isinstance(v, str) and v.strip():
            roles[r] = [v.strip()]
        else:
            roles[r] = []

    # 补 chat_default 进 chat 列表（保证默认聊天模型在列表里）
    cd = roles.get("chat_default")
    if cd and cd not in roles["chat"]:
        roles["chat"] = [cd] + [x for x in roles["chat"] if x != cd]

    # default 字段始终与 chat_default 保持一致（向后兼容前端 /v1/models 与 /admin/models）
    default = roles.get("chat_default") or default
    return {
        "schema_version": 2,
        "models": models,
        "default": default,
        "roles": {
            "chat": roles["chat"],
            "chat_default": roles.get("chat_default") or default,
            "compression": roles.get("compression") or [],
            "background": roles.get("background") or [],
        },
    }


def _load_llm_registry():
    """从 Supabase 读取模型注册表（归一化为 v2 结构）。
    无表/无数据/未配库时返回空结构。
    """
    sb = _get_supabase()
    if not sb:
        return _normalize_registry({"models": [], "default": ""})
    try:
        res = sb.table("user_facts").select("value").eq("key", _LLM_REGISTRY_KEY).execute()
        if res.data and res.data[0].get("value"):
            reg = json.loads(res.data[0]["value"])
            if isinstance(reg, dict) and isinstance(reg.get("models"), list):
                return _normalize_registry(reg)
    except Exception as e:
        _log(f"⚠️ 读取模型注册表失败: {e}")
    return _normalize_registry({"models": [], "default": ""})


def _save_llm_registry(reg: dict) -> bool:
    """把模型注册表写回 Supabase（upsert），持久化 v2 结构（含 roles + schema_version）。"""
    sb = _get_supabase()
    if not sb:
        return False
    try:
        norm = _normalize_registry(reg)
        payload = {
            "key": _LLM_REGISTRY_KEY,
            "value": json.dumps(norm, ensure_ascii=False),
        }
        sb.table("user_facts").upsert(payload).execute()
        return True
    except Exception as e:
        _log(f"❌ 保存模型注册表失败: {e}")
        return False


def _resolve_model(model_id: str):
    """按前端传入的 model(id) 解析出真实上游三元组。
    返回 (base_url, api_key, real_model, matched)；matched=False 表示未命中注册表。
    （仅用于网页 /v1/chat/completions 的请求路由，按前端指定 model id 命中）
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


def _find_enabled_model(reg: dict, model_id: str):
    """在注册表里按 id 找一个 *已启用* 的模型条目，找不到返回 None。"""
    if not model_id:
        return None
    for m in reg.get("models", []):
        if m.get("id") == model_id and m.get("enabled", True):
            return m
    return None


# 思考开关合法取值
_THINKING_VALUES = ("auto", "on", "off")


def _normalize_thinking(val) -> str:
    """把 thinking 字段归一化为 auto/on/off 三选一，非法/缺失降级为 auto。"""
    v = str(val or "").strip().lower()
    return v if v in _THINKING_VALUES else "auto"


def _thinking_params(model_name: str, setting: str) -> dict:
    """按上游真实模型名 + 思考设置（auto/on/off）返回要注入请求体的参数字典。

    auto 或未知模型一律返回 {}（不传参，走模型默认）。
    厂商映射（基于 2025-2026 官方文档查证）：
      - DeepSeek V4 / GLM 4.5+ / Kimi K2.x：{"thinking":{"type":"enabled"/"disabled"}}
      - Kimi K3（思考常开，用 effort 间接控制）：{"reasoning_effort":"max"/"low"}
      - Qwen3 / QwQ：{"enable_thinking":true/false}
      - OpenAI o1/o3/o4/gpt-5 推理系：{"reasoning_effort":"medium"/"minimal"}
      - 其他/未知：不传（避免上游报未知参数错）
    """
    s = _normalize_thinking(setting)
    if s == "auto":
        return {}
    m = (model_name or "").lower()
    on = s == "on"
    # DeepSeek V4 / GLM 4.5+ / Kimi K2.x —— 同款 thinking.type 格式
    if ("deepseek" in m or "v4-flash" in m or "v4-pro" in m
            or "glm" in m
            or "kimi-k2" in m):
        return {"thinking": {"type": "enabled" if on else "disabled"}}
    # Kimi K3（思考常开，只能调 effort）
    if "kimi-k3" in m or m.endswith("k3"):
        return {"reasoning_effort": "max" if on else "low"}
    # Qwen3 / QwQ
    if "qwen3" in m or "qwq" in m:
        return {"enable_thinking": bool(on)}
    # OpenAI o 系推理模型
    import re as _re
    if _re.match(r"\b(o1|o3|o4|gpt-5)\b", m):
        return {"reasoning_effort": "medium" if on else "minimal"}
    # 未知模型：不传参，避免报错
    return {}


def _has_legacy_llm_settings() -> bool:
    """检查 user_facts 是否存在旧版 llm_settings（控制台"待迁移模型"提示用）。"""
    sb = _get_supabase()
    if not sb:
        return False
    try:
        r = sb.table("user_facts").select("value").eq("key", "llm_settings").maybe_single().execute()
        if r and r.data and r.data.get("value"):
            ls = json.loads(r.data["value"])
            return isinstance(ls, dict) and bool(ls.get("key"))
    except Exception:
        pass
    return False


def _model_role_usage(reg: dict, model_id: str) -> list:
    """返回该 model_id 当前承担的角色列表（用于删除/禁用前的占用检查）。"""
    roles = reg.get("roles", {})
    used = []
    if model_id in (roles.get("chat") or []):
        used.append("chat")
    if model_id == roles.get("chat_default"):
        used.append("chat_default")
    if model_id in (roles.get("compression") or []):
        used.append("compression")
    if model_id in (roles.get("background") or []):
        used.append("background")
    return used


def _migrate_llm_settings_to_registry(reg: dict) -> tuple:
    """把旧 llm_settings 幂等迁移为新注册表的一个模型条目。
    返回 (new_reg, migrated:bool, reason:str)。
    - 若 llm_settings 无效 → 原样返回 reg, False, "llm_settings 无效"
    - 若注册表已有同 base_url+model 的条目 → 视为已迁移，原样返回
    - 否则新增一条 id="legacy_main_chat" 的模型，并把 chat_default 指向它（仅当无 default 时）
    """
    sb = _get_supabase()
    if not sb:
        return reg, False, "无 Supabase"
    try:
        r = sb.table("user_facts").select("value").eq("key", "llm_settings").maybe_single().execute()
        if not (r and r.data and r.data.get("value")):
            return reg, False, "llm_settings 不存在"
        ls = json.loads(r.data["value"])
        if not (isinstance(ls, dict) and ls.get("key")):
            return reg, False, "llm_settings 无 key 字段"
    except Exception as e:
        return reg, False, f"读取失败: {e}"

    models = list(reg.get("models", []))
    ls_model = str(ls.get("model", "")).strip() or "main_chat"
    ls_base = str(ls.get("url", "")).strip()
    ls_key = str(ls.get("key", "")).strip()

    # 幂等：已有同 base_url + model 的条目则不重复生成
    for m in models:
        if m.get("base_url", "").strip() == ls_base and m.get("model", "").strip() == ls_model:
            return reg, False, "已存在等价条目，跳过"

    new_id = "legacy_main_chat"
    # 避免与已有 id 冲突
    existing_ids = {m.get("id") for m in models}
    if new_id in existing_ids:
        new_id = "legacy_main_chat_2"
    entry = {
        "id": new_id,
        "label": f"旧主聊天模型({ls_model})",
        "base_url": ls_base,
        "api_key": ls_key,
        "model": ls_model,
        "enabled": True,
    }
    models.append(entry)
    new_reg = dict(reg)
    new_reg["models"] = models
    roles = dict(reg.get("roles", {}))
    if not roles.get("chat_default"):
        roles["chat_default"] = new_id
        roles["chat"] = [new_id] + [x for x in roles.get("chat", []) if x != new_id]
    new_reg["roles"] = roles
    new_reg["default"] = roles.get("chat_default") or reg.get("default", "")
    return new_reg, True, f"已迁移为 {new_id}"


def resolve_llm_role(role: str):
    """统一的角色解析函数 —— 所有聊天/压缩/后台活动调用点都走这里，不要在各文件重复解析 JSON。

    role ∈ {"chat","compression","background"}
    返回 dict：
      {
        "api_key": str, "base_url": str, "model": str,
        "registry_id": str | "",   # 命中的注册表模型 id（空=非注册表来源）
        "source": str,             # "registry" / "llm_settings" / "env" / "default"
        "fallback": bool,          # True=未命中理想来源，走了回退
        "enabled": bool,           # 该角色当前是否可用（有可用 key+model）
      }

    回退顺序（逐级尝试，记入 source/fallback）：
      1. llm_models.roles[role] 命中已启用注册表模型          → source="registry"
      2. 默认聊天模型 / 兼容映射（chat_default）              → source="registry", fallback=True
      3. 旧 user_facts.llm_settings（key/url/model）          → source="llm_settings"
      4. 对应环境变量（CHAT_* / COMPRESS_* / BACKGROUND_*；OPENAI_* 兼容）→ source="env"
      5. 原有默认值                                          → source="default"
    """
    role = (role or "chat").strip()
    if role not in _LLM_ROLES:
        role = "chat"

    reg = _load_llm_registry()
    roles = reg.get("roles", {})

    # 1) 注册表角色命中
    if role == "chat":
        cand_ids = [rid for rid in roles.get("chat", []) if rid]
        cd = roles.get("chat_default")
        if cd and cd not in cand_ids:
            cand_ids = [cd] + cand_ids
    else:
        # compression / background 现为列表（端点池）；取全部候选，命中第一个即返回
        _v = roles.get(role, [])
        if isinstance(_v, list):
            cand_ids = [str(x).strip() for x in _v if str(x).strip()]
        elif _v:
            cand_ids = [str(_v).strip()]
        else:
            cand_ids = []

    for rid in cand_ids:
        m = _find_enabled_model(reg, rid)
        if m:
            return {
                "api_key": str(m.get("api_key", "")).strip(),
                "base_url": str(m.get("base_url", "")).strip(),
                "model": str(m.get("model", "")).strip() or m.get("id", ""),
                "registry_id": m.get("id", ""),
                "source": "registry",
                "fallback": False,
                "enabled": True,
            }

    # 2) 注册表默认聊天模型回退
    cd = roles.get("chat_default") or reg.get("default")
    m = _find_enabled_model(reg, cd) if cd else None
    if m:
        return {
            "api_key": str(m.get("api_key", "")).strip(),
            "base_url": str(m.get("base_url", "")).strip(),
            "model": str(m.get("model", "")).strip() or m.get("id", ""),
            "registry_id": m.get("id", ""),
            "source": "registry",
            "fallback": True,
            "enabled": True,
        }

    # 3) 旧 llm_settings（db）
    sb = _get_supabase()
    if sb:
        try:
            r = sb.table("user_facts").select("value").eq("key", "llm_settings").maybe_single().execute()
            if r and r.data and r.data.get("value"):
                ls = json.loads(r.data["value"])
                if isinstance(ls, dict) and ls.get("key"):
                    return {
                        "api_key": str(ls.get("key", "")).strip(),
                        "base_url": str(ls.get("url", "")).strip(),
                        "model": str(ls.get("model", "")).strip(),
                        "registry_id": "",
                        "source": "llm_settings",
                        "fallback": True,
                        "enabled": True,
                    }
        except Exception as e:
            _log(f"⚠️ resolve_llm_role 读取 llm_settings 失败: {e}")

    # 4) 环境变量（按角色拆分，向后兼容 OPENAI_* / CHAT_*）
    if role == "compression":
        env_prefix = "COMPRESS"
        fb_prefix = "CHAT"
    elif role == "background":
        env_prefix = "BACKGROUND"
        fb_prefix = "CHAT"
    else:
        env_prefix = "CHAT"
        fb_prefix = "OPENAI"

    def _env(*names):
        for n in names:
            v = os.environ.get(n, "").strip()
            if v:
                return v
        return ""

    api_key = _env(f"{env_prefix}_API_KEY", f"{fb_prefix}_API_KEY", "OPENAI_API_KEY", "DEFAULT_API_KEY")
    base_url = _env(f"{env_prefix}_BASE_URL", f"{fb_prefix}_BASE_URL", "OPENAI_BASE_URL", "DEFAULT_BASE_URL")
    model = _env(f"{env_prefix}_MODEL_NAME", f"{fb_prefix}_MODEL_NAME", "OPENAI_MODEL_NAME", "DEFAULT_MODEL_NAME") or "gpt-3.5-turbo"

    src = "env" if api_key else "default"
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "registry_id": "",
        "source": src,
        "fallback": True,
        "enabled": bool(api_key),
    }


# ==========================================
# 🔁 多端点轮询 + 故障转移（chat / compression / background）
# ==========================================
# 注册表 roles.<role> 为模型 id 列表（端点池），每条模型自带 base_url+api_key。
# 调用层（server.ask_role）按 round-robin 取下一个健康端点：
#   - 连接错误 / 5xx / 429 / 401 / 403 / 安全过滤拦截 → 标记冷却，换下一个
#   - 冷却时长线性增长（60s × 连续失败次数，封顶 30 分钟），成功即清零
# 这样不同 key 分摊流量（防单 key 触发安全过滤），某个 key 断了即时切换（防断连）。
_EP_HEALTH = {}          # model_id -> {"fails":int, "cooldown_until":float, "last_ts":float}
_EP_CURSOR = {}           # role -> int（round-robin 游标）
_EP_LOCK = threading.Lock()
_EP_COOLDOWN_BASE = 60.0  # 单次失败基础冷却秒数
_EP_COOLDOWN_MAX = 1800.0  # 冷却封顶 30 分钟
_SSRF_SAFE_HOSTS = set()  # 已确认安全的主机缓存，避免每次建客户端都做 DNS 解析

# Gemini / Claude 安全过滤错误关键词（出现在异常 message 里 → 端点级故障，换 key 重试）
_SAFETY_ERROR_KEYWORDS = (
    "content_filter", "content policy", "content_policy", "usage policy",
    "sensitive word", "sensitive content", "prohibited use",
    "could not be submitted", "may violate", "not allowed",
    "violat", "safety", "refusal", "blocked",
    "敏感词", "敏感内容", "违反", "安全策略", "内容政策", "不允许", "违规",
)

# 文本级拒答开头短语（仅当整段回复 < 400 字且短语出现在开头 80 字内才判定为拒答，
# 避免把"先道歉后正常作答"的长回答误判为拒答）。英文 + 中文。
_REFUSAL_PHRASES = (
    # —— 英文 ——
    "i can't help", "i cannot help", "i can't assist", "i cannot assist",
    "i'm unable to", "i am unable to", "i'm not able to", "i am not able to",
    "i can't create", "i cannot create", "i can't generate", "i cannot generate",
    "i can't provide", "i cannot provide", "i can't write", "i cannot write",
    "i won't help", "i will not help", "i won't provide", "i won't generate",
    "i'm sorry, but i can't", "i'm sorry, but i cannot", "i'm sorry, but i am unable",
    "i'm sorry, i can't", "i'm sorry, i cannot", "sorry, i can't", "sorry, i cannot",
    "sorry, i'm unable", "i'm unable to fulfill", "i cannot fulfill",
    "i don't think i can", "i do not think i can",
    "the prompt could not be submitted", "sensitive words that violate",
    "violates google", "prohibited use policy", "generative ai prohibited",
    "against our content policy", "violates our usage policy", "violates our content policy",
    "as an ai", "as a language model", "as an ai language model",
    # —— 中文 ——
    "抱歉，我不能", "对不起，我不能", "抱歉，我无法", "对不起，我无法",
    "抱歉，我帮不了", "对不起，我帮不了", "抱歉，我不能帮", "对不起，我不能帮",
    "我无法协助", "我不能协助", "我无法提供", "我不能提供",
    "我无法生成", "我不能生成", "我无法创建", "我不能创建",
    "我无法回答", "我不能回答", "我无法完成", "我不能完成",
    "作为ai", "作为一个ai", "作为人工智能", "作为一个人工智能",
    "这违反了", "违反了使用政策", "违反了内容政策", "违反政策", "违反了政策",
    "涉及敏感", "包含敏感词", "包含敏感内容", "敏感内容",
    "为了您的安全", "为了安全起见", "出于安全",
)


def _ep_health(mid: str) -> dict:
    """返回某端点的健康记录（无记录或非 registry 端点返回零值）。"""
    if not mid:
        return {"fails": 0, "cooldown_until": 0.0, "last_ts": 0.0}
    return _EP_HEALTH.get(mid, {"fails": 0, "cooldown_until": 0.0, "last_ts": 0.0})


def _ep_is_down(mid: str) -> bool:
    """该端点当前是否处于冷却期。"""
    if not mid:
        return False
    return time.time() < _ep_health(mid)["cooldown_until"]


def _ep_mark_fail(mid: str):
    """标记端点失败：连续失败计数 +1，冷却时长线性增长（封顶 30 分钟）。"""
    if not mid:
        return
    with _EP_LOCK:
        h = _EP_HEALTH.setdefault(mid, {"fails": 0, "cooldown_until": 0.0, "last_ts": 0.0})
        h["fails"] += 1
        h["cooldown_until"] = time.time() + min(_EP_COOLDOWN_BASE * h["fails"], _EP_COOLDOWN_MAX)
        h["last_ts"] = time.time()


def _ep_mark_ok(mid: str):
    """标记端点成功：清零失败计数与冷却。"""
    if not mid:
        return
    with _EP_LOCK:
        _EP_HEALTH[mid] = {"fails": 0, "cooldown_until": 0.0, "last_ts": time.time()}


def _assert_safe_base_url(base_url: str) -> bool:
    """SSRF 防护：校验 base_url 的协议与主机是否安全可请求。缓存已知安全主机。"""
    if not base_url:
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(base_url).hostname or "").lower()
    except Exception:
        return False
    if host and host in _SSRF_SAFE_HOSTS:
        return True
    if _check_ssrf(base_url):
        return False
    if host:
        with _EP_LOCK:
            _SSRF_SAFE_HOSTS.add(host)
    return True


def _build_openai_client_from_ep(ep: dict):
    """按端点 dict 构造 OpenAI 客户端：补 /v1、SSRF 校验。返回 client 或 None。"""
    try:
        from openai import OpenAI
        base_url = (ep.get("base_url") or "").strip() or None
        if base_url:
            if not _assert_safe_base_url(base_url):
                _log(f"⚠️ 端点 base_url {base_url} 被 SSRF 防护拦截，跳过该端点")
                return None
            base = base_url.rstrip("/")
            # 裸域名（不以 /vN 结尾、非完整 /chat/completions）自动补 /v1，
            # 与 _handle_chat / 旧 _role_client 保持一致。
            if not re.search(r"/chat/completions$", base) and not re.search(r"/v\d+[a-zA-Z]*$", base):
                base_url = f"{base}/v1"
        return OpenAI(api_key=ep["api_key"], base_url=base_url, timeout=60.0)
    except Exception as e:
        _log(f"⚠️ 构造 OpenAI 客户端失败: {e}")
        return None


def resolve_llm_pool(role: str) -> list:
    """返回该角色的有序端点池（每项含 api_key/base_url/model/registry_id/source/fallback/enabled
    + ep_key/ep_index/label）。

    多 key 展开：一个模型条目有 api_key + extra_keys[] 时，按 key 顺序展开为多个端点，
    每个端点共享同一 base_url/model 但用不同 key。ep_key = "model_id#key序号"，
    健康跟踪按 ep_key 粒度（单个 key 挂了只冷却那一个）。

    回退链与 resolve_llm_role 一致；注册表命中时返回多条（端点池），非注册表回退时长度为 1（或 0）。
    """
    role = (role or "chat").strip()
    if role not in _LLM_ROLES:
        role = "chat"
    reg = _load_llm_registry()
    roles = reg.get("roles", {})

    if role == "chat":
        cand_ids = [rid for rid in (roles.get("chat") or []) if rid]
        cd = roles.get("chat_default")
        if cd and cd not in cand_ids:
            cand_ids = [cd] + cand_ids
    else:
        _v = roles.get(role, [])
        if isinstance(_v, list):
            cand_ids = [str(x).strip() for x in _v if str(x).strip()]
        elif _v:
            cand_ids = [str(_v).strip()]
        else:
            cand_ids = []

    def _expand_model(m, fallback=False):
        """把一个注册表模型条目展开为端点列表（主 key + extra_keys）。"""
        rid = m.get("id", "")
        model = str(m.get("model", "")).strip() or rid
        base_url = str(m.get("base_url", "")).strip()
        label = m.get("label", rid)
        # 收集该模型的所有 key：主 key 在前，extra_keys 在后
        keys = [str(m.get("api_key", "")).strip()]
        keys += [str(k).strip() for k in (m.get("extra_keys") or []) if str(k).strip()]
        eps = []
        for ki, k in enumerate(keys):
            if not k:
                continue
            eps.append({
                "api_key": k,
                "base_url": base_url,
                "model": model,
                "registry_id": rid,
                "source": "registry",
                "fallback": fallback,
                "enabled": True,
                "ep_key": f"{rid}#{ki+1}",       # 健康跟踪键（模型#key序号）
                "ep_index": ki + 1,               # 该模型内的 key 序号（1 起）
                "label": label,
            })
        return eps

    pool = []
    for rid in cand_ids:
        m = _find_enabled_model(reg, rid)
        if m:
            pool.extend(_expand_model(m))
    if pool:
        return pool

    # 2) 默认聊天模型回退（注册表）
    cd = roles.get("chat_default") or reg.get("default")
    m = _find_enabled_model(reg, cd) if cd else None
    if m:
        return _expand_model(m, fallback=True)

    # 3-5) 复用 resolve_llm_role 的 llm_settings / env / default 回退（单元素池，无 ep_key）
    r = resolve_llm_role(role)
    if r.get("enabled") and r.get("api_key"):
        r["ep_key"] = ""
        r["ep_index"] = 1
        r["label"] = ""
        return [r]
    return []


def pick_role_endpoint(role: str) -> dict | None:
    """round-robin 挑下一个健康端点（跳过冷却中的）；全部冷却时返回最早到期的（兜底，避免饿死）。
    健康跟踪按 ep_key（模型#key序号）粒度：单个 key 挂了只冷却那一个。
    """
    pool = resolve_llm_pool(role)
    if not pool:
        return None
    n = len(pool)
    with _EP_LOCK:
        start = _EP_CURSOR.get(role, 0) % n
        _EP_CURSOR[role] = (start + 1) % n
    order = [(start + i) % n for i in range(n)]
    healthy = [pool[i] for i in order
               if not (pool[i].get("ep_key") and _ep_is_down(pool[i]["ep_key"]))]
    if healthy:
        return healthy[0]
    # 全冷却：返回最早到期的
    return min(pool, key=lambda e: _ep_health(e.get("ep_key", ""))["cooldown_until"])


def _extract_response_text(resp) -> str:
    """从 OpenAI/Gemini/Claude 响应对象里尽量取出文本（跨 SDK 形态兜底）。"""
    try:
        if getattr(resp, "choices", None):
            return (resp.choices[0].message.content or "")
    except Exception:
        pass
    try:
        cands = getattr(resp, "candidates", None)
        if cands:
            c0 = cands[0]
            content = getattr(c0, "content", None)
            parts = getattr(content, "parts", None) if content else None
            if parts:
                return "".join(getattr(p, "text", "") or "" for p in parts)
    except Exception:
        pass
    try:
        contents = getattr(resp, "content", None)
        if isinstance(contents, list):
            return "".join(getattr(b, "text", "") or "" for b in contents
                           if getattr(b, "type", "") == "text")
    except Exception:
        pass
    return ""


def _looks_like_refusal_text(text: str) -> bool:
    """判断一段文本是否是纯拒答/安全过滤回复。
    规则：整段 < 400 字 且 拒答短语出现在前 80 字内 —— 这样能放过"先道歉后正常作答"的长回答。
    """
    if not text:
        return False
    t = text.strip()
    if len(t) >= 400:
        return False
    head = t[:120].lower()
    for p in _REFUSAL_PHRASES:
        idx = head.find(p)
        if idx >= 0 and idx < 80:
            return True
    return False


def _is_refusal_response(resp) -> bool:
    """检测一次 200 响应是否属于安全过滤/拒答（应触发端点转移）。
    三层：API 级 block 字段 → finish_reason 枚举 → 文本拒答短语。
    """
    if resp is None:
        return False
    try:
        # 1) OpenAI content_filter / Gemini 经适配器的 finish_reason
        try:
            ch = resp.choices[0]
            fr = str(getattr(ch, "finish_reason", "") or "").lower()
            if fr in ("content_filter", "safety", "blocked", "recitation",
                      "blocklist", "prohibited_content"):
                return True
        except Exception:
            pass
        # 2) Gemini 原生 prompt_feedback.block_reason
        fb = getattr(resp, "prompt_feedback", None)
        if fb is not None and getattr(fb, "block_reason", None):
            return True
        # 3) Claude refusal stop_reason / content block
        try:
            stop = str(getattr(resp, "stop_reason", "") or getattr(resp, "stop", "") or "").lower()
            if stop == "refusal":
                return True
            contents = getattr(resp, "content", None)
            if isinstance(contents, list):
                for blk in contents:
                    if getattr(blk, "type", "") == "refusal":
                        return True
        except Exception:
            pass
        # 4) 文本级拒答（最兜底）
        return _looks_like_refusal_text(_extract_response_text(resp))
    except Exception:
        return False


def _classify_llm_error(e) -> tuple:
    """分类一次 LLM 异常。返回 (reason:str, mark_fail:bool)。
    - 安全过滤 / 连接超时 / 401 / 403 / 429 / 5xx → mark_fail=True（端点级，换下个）
    - 400 非安全类（多为我们请求体问题）→ mark_fail=False（重试但不惩罚，避免把全池标冷却）
    """
    msg = str(e).lower()
    if any(k in msg for k in _SAFETY_ERROR_KEYWORDS):
        return ("safety_filter", True)
    sc = getattr(e, "status_code", None)
    if sc is None:
        return ("connection", True)
    if sc in (401, 403, 429) or sc >= 500:
        return (f"http_{sc}", True)
    if sc == 400:
        return ("bad_request", False)
    return (f"http_{sc}", True)


def _role_client(role: str):
    """构造一个 OpenAI 客户端用于给定角色（供 server._get_llm_client 复用）。
    返回 (client, model_name)；client 可能为 None（角色未配置）。
    多端点池时按 round-robin 挑下一个健康端点（跳过冷却中的）。
    """
    ep = pick_role_endpoint(role)
    if not ep or not ep.get("api_key"):
        return (None, (ep or {}).get("model", ""))
    client = _build_openai_client_from_ep(ep)
    if client is None:
        return (None, ep.get("model", ""))
    client.custom_model_name = ep["model"]
    client.role_source = ep["source"]        # 便于日志/调试
    client.ep_id = ep.get("ep_key", "")      # 健康跟踪键（模型#key序号），调用方上报健康用
    client.ep_role = role
    return (client, ep["model"])


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
# 🎛️ 运行时配置系统（sys_config）+ 全渠道门控开关
# ==========================================
# 设计：
#   - 所有可热生效的开关持久化在 user_facts.sys_config（一个 JSON 字符串）。
#   - 各进程（A 消息进程 / B 后台进程 / WS 处理器 / 网关请求线程）通过
#     _get_runtime_config() 读取，带 5 秒 TTL 内存缓存 —— 既避免每条消息都打 DB，
#     又能在数秒内把控制台修改热同步到所有进程（与 heartbeat.async_env_sync 10s 对齐）。
#   - 数据库优先于环境变量：sys_config 里有就用它，没有回退环境变量默认值。
#   - 修复 v3.8 遗留：情感/欲望状态已迁到 desire_state 表，/api/desire 必须从该表读。
#
# 开关作用范围（全部跨 Web/TG/QQ 三渠道一致）：
#   telegram_enabled              TG 轮询门控（进程 A）
#   qq_enabled                    QQ NapCat 消息处理门控（WS 处理器）
#   emotion_enabled               情感/欲望引擎总开关（心跳 tick + 事件入队）
#   desire_driven                 DESIRE_DRIVEN 是否覆盖行为（DB 覆盖同名环境变量）
#   chat_history_write_enabled    聊天记录写入门控（memories 流水 + Pinecone）
#   vector_memory_injection_enabled 向量记忆检索注入门控（_inject_context / _build_channel_context）

_SYS_CONFIG_KEY = "sys_config"
_runtime_config_cache = {"data": None, "ts": 0.0}
_RUNTIME_CONFIG_TTL = 5.0  # 秒

# ==========================================
# 📦 Prompt Cache 友好：stable_system 组件 TTL 缓存
#   core_summaries / user_prof 每轮都查 DB，一旦内容变化（哪怕一个字符），
#   整个 stable_system 前缀就变 → 上游 prompt cache 严格前缀匹配 → 命中率归零。
#   用 TTL 缓存让它们在一个窗口内保持字节不变，窗口内连续对话才能命中缓存。
# ==========================================
_stable_prefix_cache: dict[str, dict] = {}  # key -> {"value": str, "ts": float}
_STABLE_PREFIX_TTL = max(0, int(os.environ.get("STABLE_PREFIX_TTL", "300")))      # 默认 5 分钟
_CORE_SUMMARIES_TTL = max(0, int(os.environ.get("CORE_SUMMARIES_TTL", str(_STABLE_PREFIX_TTL))))  # 默认同上，可单独调长


def _stable_cached(key: str, ttl: int) -> str | None:
    """读 TTL 缓存；未过期返回缓存值，过期/不存在返回 None。"""
    if ttl <= 0:
        return None
    entry = _stable_prefix_cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl:
        return entry["value"]
    return None


def _stable_set(key: str, value: str) -> None:
    _stable_prefix_cache[key] = {"value": value, "ts": time.time()}


# ==========================================
# 📦 静态常驻提示词文件（prompts/*.md）
#   世界书 / 回复规则等静态常驻内容从 rikkahub 客户端迁到网关，
#   进 stable_system 前缀 → 享受 TTL 缓存前缀命中 + 不干扰自适应注入
#   （_client_msg_count 回归真实历史条数）。启动时读一次，进程级常量。
# ==========================================
_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def _load_prompt_file(name: str) -> str:
    """读取 prompts/ 下的静态提示词文件；不存在或读取失败返回空串（优雅降级）。"""
    try:
        p = os.path.join(_PROMPTS_DIR, name)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception as _e:
        _log(f"⚠️ 加载提示词文件 {name} 失败: {_e}")
    return ""


_WORLD_BOOK_TEXT = _load_prompt_file("world_book.md")     # 世界书（表情包规则等），注入 persona 之后
_REPLY_RULES_TEXT = _load_prompt_file("reply_rules.md")   # 回复规则（看时间戳/thinking 要求等），注入 stable 最末紧邻 volatile 时间戳


def _log_cache_usage(model: str, usage: dict | None) -> None:
    """
    解析上游 usage 里的 prompt cache 字段并打日志。
    覆盖三家厂商的不同字段命名：
      - DeepSeek: prompt_cache_hit_tokens / prompt_cache_miss_tokens
      - GLM/OpenAI: prompt_tokens_details.cached_tokens
      - Anthropic(中转): cache_read_input_tokens / cache_creation_input_tokens
    """
    if not isinstance(usage, dict) or not usage:
        return
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    cached_tokens = None
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cached_tokens = prompt_details.get("cached_tokens")
    cache_read = usage.get("cache_read_input_tokens")
    cache_creation = usage.get("cache_creation_input_tokens")

    # 没有任何缓存字段就不记，减少噪音
    if all(v is None for v in (hit, miss, cached_tokens, cache_read, cache_creation)):
        return

    prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0

    # 计算命中率百分比（按厂商口径分别算）
    rate_str = ""
    if hit is not None or miss is not None:
        h, m = hit or 0, miss or 0
        total = h + m
        rate_str = f" hit_rate={h / total * 100:.1f}%" if total else ""
    elif cached_tokens is not None and prompt_tokens:
        rate_str = f" cached_rate={cached_tokens / prompt_tokens * 100:.1f}%"
    elif cache_read is not None or cache_creation is not None:
        r, c = cache_read or 0, cache_creation or 0
        total = r + c
        rate_str = f" hit_rate={r / total * 100:.1f}%" if total else ""

    _log(
        f"📊 [Cache] model={model} prompt={prompt_tokens} completion={completion_tokens}"
        f" ds_hit={hit} ds_miss={miss} cached={cached_tokens}"
        f" cc_read={cache_read} cc_creation={cache_creation}{rate_str}"
    )


def _apply_claude_cache_control(req_data: dict) -> None:
    """
    给 Claude（中转站）的 system 消息加 cache_control: {type: ephemeral} 标记。
    Anthropic 需要手动标记才会缓存；DeepSeek/Kimi/GLM 是自动缓存，不需要此标记。
    ⚠️ 依赖中转站透传 cache_control 字段——部分中转站会剥离未知字段导致无效，需实测。

    环境变量 CLAUDE_CACHE_CONTROL：
      - auto（默认）：仅当 model 名含 'claude' 时启用
      - true / 1 / yes：强制对所有模型启用
      - false / 0 / no：强制关闭
    """
    _mode = os.environ.get("CLAUDE_CACHE_CONTROL", "auto").strip().lower()
    if _mode in ("false", "0", "no", "off"):
        return
    model_name = str(req_data.get("model", "")).lower()
    if _mode in ("auto",) and "claude" not in model_name:
        return
    if _mode not in ("auto", "true", "1", "yes", "on"):
        return

    messages = req_data.get("messages")
    if not isinstance(messages, list):
        return

    _MARKER = {"type": "ephemeral"}
    _applied = False

    # 给第一条 system 消息加缓存标记（把字符串 content 转成 content-block 数组格式）
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content")
            if isinstance(content, str):
                m["content"] = [{"type": "text", "text": content, "cache_control": _MARKER}]
                _applied = True
            elif isinstance(content, list) and content:
                # 已是数组格式：给最后一个 block 加标记（避免重复标记）
                last_block = content[-1]
                if isinstance(last_block, dict) and "cache_control" not in last_block:
                    last_block["cache_control"] = _MARKER
                    _applied = True
            break

    # 给 tools 定义也加缓存标记（Anthropic 建议 tools 也标记）
    tools = req_data.get("tools")
    if isinstance(tools, list) and tools:
        last_tool = tools[-1]
        if isinstance(last_tool, dict) and "cache_control" not in last_tool:
            last_tool["cache_control"] = _MARKER
            _applied = True

    if _applied:
        _log(f"🏷️ [Cache] 已为 model={model_name} 添加 Claude cache_control(ephemeral) 标记")


def _default_runtime_config() -> dict:
    """默认值：未配置 sys_config 或某键缺失时的回退（与现有行为一致）。"""
    return {
        "telegram_enabled": True,
        "qq_enabled": True,
        "emotion_enabled": True,
        "desire_driven": os.environ.get("DESIRE_DRIVEN", "false").strip().lower() in ("1", "true", "yes", "on"),
        "chat_history_write_enabled": True,
        "vector_memory_injection_enabled": True,
        # 设备状态快照注入：仅前台聊天门控（后台自主活动仍走 DEVICE_CONTEXT_ENABLED 环境变量）
        "device_context_enabled": True,
        # Agent 赚钱系统：false 时禁止 Agent 自主入账（wallet_earn bypass_cap=False），
        # 但不关闭钱包（余额查询/消费/猫用品购买/零花钱/打赏不受影响）。
        "money_earning_enabled": True,
        # Home Runtime 上下文注入：true 时在聊天上下文中注入家庭房间/成员/事件摘要
        "home_context_enabled": True,
    }


def _load_sys_config_raw() -> dict:
    """直接读 sys_config JSON（无缓存），失败返回空 dict。"""
    sb = _get_supabase()
    if not sb:
        return {}
    try:
        r = sb.table("user_facts").select("value").eq("key", _SYS_CONFIG_KEY).maybe_single().execute()
        if r and r.data and r.data.get("value"):
            conf = json.loads(r.data["value"])
            if isinstance(conf, dict):
                return conf
    except Exception as e:
        _log(f"⚠️ 读取 sys_config 失败: {e}")
    return {}


def _get_runtime_config() -> dict:
    """读取运行时配置（带 TTL 缓存）。返回合并了默认值的完整 dict。"""
    now = time.time()
    if _runtime_config_cache["data"] is not None and (now - _runtime_config_cache["ts"]) < _RUNTIME_CONFIG_TTL:
        return _runtime_config_cache["data"]
    raw = _load_sys_config_raw()
    merged = _default_runtime_config()
    # 数据库覆盖默认值（仅当显式存在且是合法 bool 值时）
    for k in list(merged.keys()):
        if k in raw and raw[k] is not None:
            v = raw[k]
            if isinstance(v, bool):
                merged[k] = v
            elif isinstance(v, str):
                merged[k] = v.strip().lower() in ("1", "true", "yes", "on")
    _runtime_config_cache["data"] = merged
    _runtime_config_cache["ts"] = now
    return merged


def _invalidate_runtime_config():
    """强制刷新缓存（PATCH 配置后调用，保证下次读取拿到新值）。"""
    _runtime_config_cache["data"] = None
    _runtime_config_cache["ts"] = 0.0


def _tg_enabled() -> bool:
    """Telegram 渠道是否启用（轮询门控）。"""
    return _get_runtime_config().get("telegram_enabled", True)


def _qq_enabled() -> bool:
    """QQ 渠道是否启用（消息处理门控）。"""
    return _get_runtime_config().get("qq_enabled", True)


def _emotion_enabled() -> bool:
    """情感/欲望引擎总开关（关闭后停止 tick + 停止事件入队）。"""
    return _get_runtime_config().get("emotion_enabled", True)


def _desire_driven_enabled() -> bool:
    """DESIRE_DRIVEN 是否覆盖行为（数据库 sys_config 优先于环境变量）。"""
    return _get_runtime_config().get("desire_driven", False)


def _chat_write_enabled() -> bool:
    """聊天记录写入门控（memories 流水 + Pinecone 写入）。"""
    return _get_runtime_config().get("chat_history_write_enabled", True)


def _vector_injection_enabled() -> bool:
    """向量记忆检索注入门控（构建 prompt 时跳过 Pinecone 检索）。"""
    return _get_runtime_config().get("vector_memory_injection_enabled", True)


def _device_context_enabled() -> bool:
    """设备状态快照注入门控（仅前台聊天生效，不影响后台自主活动）。

    前台渠道（网页 _inject_context / TG / QQ 的 _build_channel_context）读取此开关；
    后台自主活动（主动问候 / 自由活动）调用 _build_channel_context 时不传 inject_device，
    仍沿用 DEVICE_CONTEXT_ENABLED 环境变量，不受此开关影响。
    """
    return _get_runtime_config().get("device_context_enabled", True)


def _money_earning_enabled() -> bool:
    """Agent 赚钱系统门控（sys_config 运行时开关，5s 热生效）。

    True（默认）：允许 Agent 通过 wallet_earn（bypass_cap=False）自主赚钱。
    False：禁止 Agent 自主入账，但不关闭钱包——
      wallet_check / wallet_log / wallet_spend / cat_shop_buy /
      手动零花钱（bypass_cap=True）/ 手动打赏（bypass_cap=True）均不受影响。
    门控必须放在实际入账调用入口，tool_loop.call_tool 与 server.py 的 wallet_earn
    都会调用本函数，防止仅前端隐藏后被 MCP 直调绕过。
    """
    return _get_runtime_config().get("money_earning_enabled", True)


def _home_context_enabled() -> bool:
    """Home Runtime 上下文注入门控（sys_config 运行时开关，5s 热生效）。

    True（默认）：在聊天上下文 volatile_block 中注入家庭房间/成员/事件摘要。
    False：不注入 Home Runtime 上下文（聊天仍正常工作）。
    """
    return _get_runtime_config().get("home_context_enabled", True)


def _gw_home_context_safe() -> str:
    """安全构建 Home Runtime 上下文文本。失败时返回空字符串。"""
    try:
        from home import context as _home_ctx
        return _home_ctx.build_home_context()
    except Exception:
        return ""


def _extract_user_side_from_history(content, user_name):
    """🔒 第1阶段（目标B/C）：从 memories 历史条目中只提取「用户侧」内容。

    背景（第0阶段审计确认）：memories 聊天流水混合存储了用户与 AI 双方原文，
    旧 AI 回复一旦进入上下文，模型会把它当作自己的续写范例（模仿/重复根因）。
    本函数按各渠道写入端（gateway._save_conversation / heartbeat._handle_merged /
    napcat._handle_merged）的确定性格式提取用户侧，无法安全判断角色时返回 None，
    调用方应整条跳过——宁可少注入，也绝不注入 AI 原文。

    支持的写入格式：
      1. Web user 条目:  f"{user_name}：{msg}"              → 返回用户消息
      2. Web AI   条目:  f"我({ai_name})：{reply}"           → 返回 None（明确排除）
      3. TG/QQ 混合条目: "用户: {text}\\n回复: {reply}" / "{nick}: {text}\\n回复: {reply}"
                          → 只保留 "回复:" 之前的用户部分，并剥掉 "{角色}: " 前缀
      4. 其他未知格式    → 返回 None（不做猜测）
    """
    if not isinstance(content, str):
        return None
    c = content.strip()
    if not c:
        return None
    # 1. Web user 条目（中文冒号），如 "小满：今天好累"
    if user_name and c.startswith(user_name):
        if "：" in c:
            return c.split("：", 1)[-1].strip() or None
        return None
    # 2. Web AI 条目，如 "我(Finn)：好的亲爱的……" —— 旧 AI 回复不得进入上下文
    if c.startswith("我("):
        return None
    # 3. TG/QQ 混合条目：以行首 "回复:" / "回复：" 分隔，前半段是用户侧
    parts = re.split(r"\n\s*回复[:：]", c, maxsplit=1)
    if len(parts) == 2:
        user_part = parts[0].strip()
        if not user_part:
            return None
        # 剥掉第一行的 "{用户: " / "{昵称}: " 角色前缀（取第一个冒号之后的内容）
        for sep in ("：", ":"):
            if sep in user_part:
                user_part = user_part.split(sep, 1)[1].strip()
                break
        return user_part or None
    # 4. 其余格式无法安全判断角色（如 TG 兜底 "[未回复：AI 服务未配置]"、
    #    Core_Cognition 总结、自由活动日志等）——整条跳过，不做猜测
    return None


def _config_source_of(key: str) -> str:
    """判断某个开关当前生效来源：'database' / 'env' / 'default'。"""
    raw = _load_sys_config_raw()
    if key in raw and raw[key] is not None:
        return "database"
    if key == "desire_driven" and os.environ.get("DESIRE_DRIVEN", "").strip():
        return "env"
    return "default"


# ==========================================
# 🗂️ 记忆分类映射（记忆库页 6 个页签的服务端权威实现）
# ==========================================
_CORE_TAGS = {"Core_Cognition", "Core_Cognition_Weekly", "Core_Cognition_Monthly", "Core_Cognition_Yearly"}
_QQ_TAGS = {"QQ_MSG", "QQ_Chat", "QQ_Group"}
_TG_TAGS = {"TG_MSG"}

# 渠道页签 → 用于 memories.in_("tags", [...]) 的 SQL 过滤
MEM_CATEGORY_TAGS = {
    "core": ["Core_Cognition", "Core_Cognition_Weekly", "Core_Cognition_Monthly", "Core_Cognition_Yearly"],
    "web": ["Web_Chat"],
    "qq": ["QQ_MSG", "QQ_Chat", "QQ_Group"],
    "tg": ["TG_MSG"],
    "free": ["Free_Activity"],
    "secret_diary": ["Secret_Diary"],
}


def _memory_category(tags: str, content: str = "") -> str:
    """把一条 memory 的 tags 映射到页签之一：core/web/qq/tg/free/secret_diary/other。

    规则（与控制台前端、记忆库页完全一致，单一真相源）：
      - 核心认知：tags ∈ Core_Cognition 系列
      - 网页对话：tags == Web_Chat
      - QQ 对话：tags ∈ QQ_MSG/QQ_Chat/QQ_Group
      - TG 对话：tags == TG_MSG
      - 自由活动：tags == Free_Activity
      - 秘密日记：tags == Secret_Diary（仅「写秘密日记」活动产生，独立面板展示）
      - 其他/历史：Archived_Chat、Desire_Trace、Heartbeat、逗号分隔的旧多词标签、
                   空标签、created_at 为空的历史数据 —— 统一归入 other
    """
    t = str(tags or "").strip()
    if t in _CORE_TAGS:
        return "core"
    if t == "Web_Chat":
        return "web"
    if t in _QQ_TAGS:
        return "qq"
    if t in _TG_TAGS:
        return "tg"
    if t == "Free_Activity":
        return "free"
    if t == "Secret_Diary":
        return "secret_diary"
    return "other"


def _category_tag_filter(category: str):
    """返回该页签对应的 tags 白名单（用于服务端精确分页查询）。
    other 页签无法用单次 in_ 查询表达（它是"不在以上分类"的补集），
    返回 None 表示调用方需用 Python 侧过滤或反向查询。
    """
    if category in MEM_CATEGORY_TAGS:
        return MEM_CATEGORY_TAGS[category]
    return None  # other


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
    判断一个 user_facts 的 key 是否属于「用户画像」（应注入 prompt / 应在画像页展示）。
    这是画像过滤的单一真相源：控制台画像页、_inject_context、_build_channel_context 都复用它。

    系统配置键（必须隐藏）：
      sys_config / llm_settings / llm_models / sys_ai_persona —— 结构化系统配置，非画像。
    desire 系列的处理：
    - 运行时状态（desire_drive_state / desire_emotion_state / desire_last_tick_at
      / desire_next_heartbeat_at 等）——每拍都在变，是引擎内部状态，绝不能进画像，
      否则既是噪音、又会因为时间戳每拍变化而击穿缓存前缀 → 排除。
    - 人写的笔记（如 desire_system_tech_debt_2026_08_05）——带日期后缀，属于真画像 → 放行。

    识别方式：系统配置键直接排除；desire_ 开头且**结尾带 _YYYY_MM_DD 日期后缀**的视为笔记放行，
    其余 desire_ 开头一律当运行时状态排除。非系统键且非 desire_ 开头的照常放行。
    """
    if not key:
        return False
    if key in _SYSTEM_PROFILE_KEYS:
        return False
    if key.startswith("desire_"):
        # 结尾是 _2026_08_05 这种日期后缀 → 笔记，放行
        return bool(re.search(r"_\d{4}_\d{2}_\d{2}$", key))
    return True


# 系统配置键白名单：绝不作为用户画像展示/注入（单一真相源，与 _inject_context 的 neq 对齐）
_SYSTEM_PROFILE_KEYS = {"sys_config", "llm_settings", "llm_models", "sys_ai_persona"}


def _fetch_device_snapshot(sb):
    """
    拉取 device_data 最新一条，渲染成可注入 prompt 的文本块。
    只注入最新一条，并标注数据更新时间（设备时间 + 距今多久前）。
    失败/无数据时返回空串，由调用方优雅降级。
    """
    top_apps = int(os.environ.get("DEVICE_CONTEXT_TOP_APPS", "5") or "5")
    max_notifs = int(os.environ.get("DEVICE_CONTEXT_MAX_NOTIFS", "3") or "3")

    # 取最近若干条：device_data 混有「完整快照」与「轻量事件行」(screen_on/off 等)，
    # 单取最新一条常命中空事件行 → 快照几乎为空。这里取最近 8 条，从中挑出最新的
    # 「富数据行」作为快照主体，并单独抓最新一条 device_event 补进来。
    res = sb.table("device_data").select("*").order("id", desc=True).limit(8).execute()
    try:
        rows = res.data or []
    except Exception:
        rows = (res or {}).get("data", []) if isinstance(res, dict) else []
    if not rows:
        return ""

    # 富数据行判定：任一字段有值即算（app_usage/health_data/notifications/位置/前台应用）
    def _has_payload(r):
        return bool(
            r.get("app_usage") or r.get("health_data") or r.get("notifications")
            or (r.get("location_address") and str(r.get("location_address")).strip())
            or (r.get("foreground_app") and str(r.get("foreground_app")).strip())
        )

    row = next((r for r in rows if _has_payload(r)), rows[0])
    # 最新一条非空 device_event（可能来自比 row 更新的事件行，如刚发生的 screen_off）
    latest_event_row = next((r for r in rows if r.get("device_event")), None)

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

    # 设备事件（取最新一条事件行，可能比快照主体更新，如刚发生的 screen_off）
    _evt = (latest_event_row or {}).get("device_event") if latest_event_row else None
    if _evt:
        lines.append(f"⚡ 设备事件：{_evt}")

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


# 🆕 天气工具（软导入）：关键词注入 + 可选 tool loop
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


def _extract_message_text(content) -> str:
    """从 OpenAI 兼容的 message content 中提取纯文本。
    - 字符串 content 原样返回；
    - 数组 content 只提取 type=text/input_text 的文本 part，按顺序用换行合并；
    - 图片/音频/视频/文件/Base64/URL 等媒体 part 全部忽略；
    - 未知 part 类型忽略，不执行 str() 或 json.dumps()；
    - 非法结构（None/数字/布尔/dict 等）返回空字符串；
    - 不修改输入。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type", "")
        if ptype in ("text", "input_text"):
            text_val = part.get("text", "")
            if isinstance(text_val, str):
                parts.append(text_val)
        # 其他 type（image_url/input_image/input_audio/audio/video_url/input_video/file/input_file 等）忽略
    return "\n".join(parts)


def _sanitize_outgoing_messages(messages):
    """清洗发送给上游模型的消息列表，移除会导致 400 的空消息。
    - 不修改输入列表（返回新列表）；
    - 删除 content 为空/纯空白的 user/assistant 消息（除非含 tool_calls）；
    - 保留带 tool_calls 的 assistant 消息（即使 content 为空）；
    - 保留带 tool_call_id 的 tool 消息（即使 content 为空）；
    - 不删除 system 消息；
    - 对数组 content，删除空白 text part，保留媒体 part；
    - 如果数组清洗后全部为空且无 tool_calls，删除该消息。
    """
    if not isinstance(messages, list):
        return messages
    import copy
    result = []
    for m in messages:
        if not isinstance(m, dict):
            result.append(m)
            continue
        role = m.get("role", "")
        content = m.get("content", "")
        has_tool_calls = bool(m.get("tool_calls"))
        has_tool_call_id = bool(m.get("tool_call_id"))
        # system 消息不删除
        if role == "system":
            result.append(m)
            continue
        # 带 tool_calls 的 assistant 消息保留
        if role == "assistant" and has_tool_calls:
            result.append(m)
            continue
        # 带 tool_call_id 的 tool 消息保留
        if role == "tool" and has_tool_call_id:
            result.append(m)
            continue
        # 字符串 content
        if isinstance(content, str):
            if content.strip():
                result.append(m)
            # else: 空字符串/纯空白 → 删除
            continue
        # 数组 content（多模态）
        if isinstance(content, list):
            cleaned_parts = []
            for part in content:
                if not isinstance(part, dict):
                    cleaned_parts.append(part)
                    continue
                ptype = part.get("type", "")
                if ptype in ("text", "input_text"):
                    text_val = part.get("text", "")
                    if isinstance(text_val, str) and text_val.strip():
                        cleaned_parts.append(part)
                    # else: 空 text part → 删除
                else:
                    # 非文本 part（image_url/file/audio/video 等）保留
                    cleaned_parts.append(part)
            if cleaned_parts:
                m_copy = dict(m)
                m_copy["content"] = cleaned_parts
                result.append(m_copy)
            # else: 数组全空 → 删除
            continue
        # content 为 None 且无 tool_calls/tool_call_id → 删除
        # content 为其他类型（数字/布尔等）→ 保留（不常见但安全）
        if content is not None and not isinstance(content, (str, list)):
            result.append(m)
        # None content 无结构 → 删除
    return result


def _strip_incoming_reasoning(messages):
    """从 incoming messages 中移除客户端回传的历史 reasoning_content 字段。
    最小逻辑：遍历 list，对每个 dict message 删除 reasoning_content，其他字段原样保留。
    不记录 reasoning 正文，只记录移除了多少个字段。"""
    if not isinstance(messages, list):
        return messages
    removed = 0
    for m in messages:
        if isinstance(m, dict) and "reasoning_content" in m:
            del m["reasoning_content"]
            removed += 1
    if removed:
        _log(f"🧹 已移除 {removed} 个历史 reasoning_content 字段")
    return messages


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

        # ---------- 📝 注入提示词快照（最近5条，调试用） ----------
        if scope["path"] == "/api/prompts":
            await self._handle_prompts_api(send)
            return

        # ---------- 🎛️ 多模型管理接口 ----------
        if scope["path"] == "/api/models":
            await self._handle_models_api(scope, receive, send)
            return

        # ---------- 🧠 情绪 / 欲望系统状态接口（只读） ----------
        # 返回 desire_state 表里 desire_* 的最新持久化快照，供 Mini App / 前端面板使用。
        # 已在上方全局拦截中过 API_SECRET 鉴权。纯只读：不推进引擎、不写库。
        if scope["path"] == "/api/desire":
            await self._handle_desire_api(send)
            return

        # ---------- 🖥️ 桌面控制台（电脑端网关管理控制台 HTML） ----------
        if scope["path"] == "/console" or scope["path"] == "/console/":
            await self._handle_console_page(send)
            return

        # ---------- ⚙️ 管理配置（运行时开关） ----------
        if scope["path"] == "/api/admin/config":
            await self._handle_admin_config(scope, receive, send)
            return

        # ---------- 📊 系统状态（TG/QQ 连接、进程、最近错误） ----------
        if scope["path"] == "/api/admin/status":
            await self._handle_admin_status(send)
            return

        # ---------- 🧪 模型连接测试 ----------
        if scope["path"] == "/api/models/test":
            await self._handle_models_test(scope, receive, send)
            return

        # ---------- 📚 记忆库 CRUD（服务端分页 + 分类查询） ----------
        if scope["path"] == "/api/memories":
            await self._handle_memories_api(scope, receive, send, mem_id=None)
            return
        if scope["path"].startswith("/api/memories/"):
            mid = scope["path"][len("/api/memories/"):]
            await self._handle_memories_api(scope, receive, send, mem_id=mid)
            return

        # ---------- 👤 用户画像 CRUD（user_facts，系统键过滤） ----------
        if scope["path"] == "/api/profile":
            await self._handle_profile_api(scope, receive, send, key=None)
            return
        if scope["path"].startswith("/api/profile/"):
            pkey = scope["path"][len("/api/profile/"):]
            await self._handle_profile_api(scope, receive, send, key=pkey)
            return

        # ---------- 🐱 Tick 日志查询 ----------
        if scope["path"] == "/api/ticks":
            await self._handle_ticks_api(scope, receive, send)
            return

        # ---------- 💰 钱包后端代理 API（Phase 6.1：前端不再直调 Supabase RPC） ----------
        if scope["path"] == "/api/wallet" and scope["method"] == "GET":
            await self._handle_wallet_api(scope, send, action="check")
            return
        if scope["path"] == "/api/wallet/log" and scope["method"] == "GET":
            await self._handle_wallet_api(scope, send, action="log")
            return
        if scope["path"] == "/api/wallet/allowance" and scope["method"] == "POST":
            await self._handle_wallet_api(scope, send, action="allowance", receive=receive)
            return
        if scope["path"] == "/api/wallet/tip" and scope["method"] == "POST":
            await self._handle_wallet_api(scope, send, action="tip", receive=receive)
            return
        if scope["path"] == "/api/wallet/spend" and scope["method"] == "POST":
            await self._handle_wallet_api(scope, send, action="spend", receive=receive)
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

        # ---------- 🧪 手动事实提取预览（第10阶段：只读、零写入；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/memory-extraction-preview":
            await self._handle_memory_extraction_preview(scope, receive, send)
            return

        # ---------- ✍️ 手动候选确认提交（第17阶段：两步人工确认第二步；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/memory-extraction-commit":
            await self._handle_memory_extraction_commit(scope, receive, send)
            return

        # ---------- 🗂️ pending_review 人工审批（第19阶段：只读列表 + 单条 approve/reject；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/memory-review":
            await self._handle_memory_review(scope, receive, send)
            return
        if scope["path"] == "/api/memory-review/decision":
            await self._handle_memory_review_decision(scope, receive, send)
            return

        # ---------- 🔎 active 记忆只读召回预览（第21阶段：只读、零写入、不接正式上下文；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/memory-recall-preview":
            await self._handle_memory_recall_preview(scope, receive, send)
            return

        # ---------- 📐 生产 embedding 维度安全诊断（第28阶段：固定合成探针、最多一次 provider 调用、零数据库/Pinecone/LLM 副作用；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/embedding-dimension-preview":
            await self._handle_embedding_dimension_preview(scope, receive, send)
            return

        # ---------- 🧬 active 记忆向量手动回填（第31阶段：一次一条、条件 UPDATE 原子写三列、无 Pinecone/LLM/调度；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/memory-embedding-backfill":
            await self._handle_memory_embedding_backfill(scope, receive, send)
            return

        # ---------- 🧪 memory_items 向量 RPC 自匹配只读预览（第33阶段：自 content 重嵌入 → service_role 只读 RPC → Top1 内部 ID 核对；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/memory-vector-selftest-preview":
            await self._handle_memory_vector_selftest(scope, receive, send)
            return

        # ---------- 🔍 用户查询向量召回只读预览（第35阶段：query 重嵌入 → service_role 只读 RPC → active-only 二次过滤；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/memory-vector-recall-preview":
            await self._handle_memory_vector_recall(scope, receive, send)
            return

        # ---------- 🔀 lexical+vector 混合召回只读预览（第37阶段：一次 embedding + 一次 RPC 取同批 active 候选 → deterministic_lexical_v1 词面二次排序 → 内部 ID 合并去重 → RRF 融合；手动、零写入、不接正式上下文；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/memory-hybrid-recall-preview":
            await self._handle_memory_hybrid_recall(scope, receive, send)
            return

        # ---------- 🏠 Home 聚合只读视图（C6：后端读取+安全投影，GET 零写副作用；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/home/state":
            await self._handle_home_state_api(scope, send)
            return

        # ---------- 📋 行动日志分页查询（C6：只读、tools_used 二次白名单投影；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/activity-logs":
            await self._handle_activity_logs_api(scope, send)
            return

        # ---------- 🔐 秘密日记统一索引（C6：仅元数据不含正文；受 /api/* 统一鉴权） ----------
        if scope["path"] == "/api/secret-diaries":
            await self._handle_secret_diaries_api(scope, send)
            return
        if scope["path"].startswith("/api/secret-diaries/"):
            diary_ref = scope["path"][len("/api/secret-diaries/"):]
            await self._handle_secret_diary_body_api(scope, send, reference=diary_ref)
            return

        # ---------- ✉️ 信件拆阅入口（C9：POST 拆信有副作用 / GET 已拆信零副作用阅读；受 /api/* 统一鉴权） ----------
        if scope["path"].startswith("/api/home/letters/"):
            letter_rest = scope["path"][len("/api/home/letters/"):]
            if letter_rest.endswith("/open"):
                await self._handle_letter_open_api(scope, send, raw_key=letter_rest[:-len("/open")])
            else:
                await self._handle_letter_read_api(scope, send, raw_key=letter_rest)
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

        # 🧹 清洗 incoming reasoning_content：移除客户端回传的历史推理字段，
        #    防止旧 reasoning 被原样转发给上游模型。不影响上游新产生的 reasoning 流式返回。
        _strip_incoming_reasoning(req_data.get("messages"))

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
        # 🧠 思考开关：按模型注册表 thinking 配置注入厂商对应参数
        # raw HTTP 下这些字段直接作为 JSON 顶层 key 塞进 req_data（等价于 SDK 的 extra_body）。
        # 流式转发与 tool loop 都从 req_data 取参，此处注入一次即覆盖两条路径。
        # ==========================================
        try:
            _reg = _load_llm_registry()
            _entry = _find_enabled_model(_reg, requested_model) if matched else None
            if _entry:
                _tp = _thinking_params(_entry.get("model", ""), _entry.get("thinking", "auto"))
                if _tp:
                    req_data.update(_tp)
                    _log(f"🧠 [thinking] model={_entry.get('model')} setting={_entry.get('thinking')} → {_tp}")
        except Exception as _te:
            _log(f"⚠️ [thinking] 注入失败（已降级不传参）: {_te}")

        # ==========================================
        # 🧠 智能体模式：注入上文/人设/记忆（仅当配了 Supabase 时启用）
        # ==========================================
        sb = _get_supabase()
        user_msg = ""
        for m in reversed(req_data.get("messages", [])):
            if m.get("role") == "user":
                user_msg = _extract_message_text(m.get("content", ""))
                break

        if sb and user_msg:
            try:
                await self._inject_context(req_data, sb, user_msg)
            except Exception as e:
                _log(f"⚠️ 上文注入失败（已降级为透传）: {e}")
        else:
            if sb:
                _log("➡️ [透传] 无 user 消息或无 Supabase，直接转发")

        # 🌤️ 可选天气 tool loop（默认关 WEATHER_TOOL_LOOP=false）：开启时注入 schema + 走本地 function-call 循环
        _weather_loop = (
            _HAS_WEATHER_TOOLS and weather_tools is not None and weather_tools.enabled()
            and os.environ.get("WEATHER_TOOL_LOOP", "false").strip().lower() in ("1", "true", "yes")
        )
        # 🛡️ 请求已带客户端工具（非天气工具）时不接管 tool loop——网关无法本地执行这些工具，
        # 强行接管会把客户端工具调用误判为 Unknown tool 耗尽轮次，最终给出误导性报错。
        # 交还客户端自行执行 function-call，天气能力仍由 WEATHER_KEYWORD_INJECT 关键词注入保障。
        if _weather_loop and isinstance(req_data.get("tools"), list):
            _client_tools = [
                t.get("function", {}).get("name", "")
                for t in req_data["tools"]
                if isinstance(t, dict) and t.get("type") == "function"
                and t.get("function", {}).get("name", "") not in weather_tools.TOOL_NAMES
            ]
            if _client_tools:
                _log(f"➡️ [Weather] 请求已带客户端工具 {_client_tools}，跳过网关 tool loop（交还客户端执行）")
                _weather_loop = False
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

        # 强制流式（便于边透传边收集）
        req_data["stream"] = True
        # 让上游在流末尾返回 usage（含 cached_tokens / prompt_cache_hit_tokens 等），
        # 不支持的上游（Kimi/GLM 等）会自动忽略此字段，不报错。
        req_data.setdefault("stream_options", {})["include_usage"] = True
        if req_data.get("tools"):
            req_data["tool_choice"] = "auto"

        # 🏷️ Claude cache_control（opt-in）：给 system 消息加 ephemeral 缓存标记
        _apply_claude_cache_control(req_data)

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
                # 🧹 发送前清洗空消息，防止上游 400（不修改原始 req_data 的其他字段）
                req_data["messages"] = _sanitize_outgoing_messages(req_data.get("messages", []))
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
        collected_usage = None  # 上游返回的 usage（含缓存命中字段）

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
                        if dj.get("usage"):
                            collected_usage = dj["usage"]
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
            if dj.get("usage"):
                collected_usage = dj["usage"]
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
        # 📊 Prompt Cache 命中观测（流结束后打日志，不阻塞响应）
        # ==========================================
        _log_cache_usage(req_data.get("model", ""), collected_usage)

        # ==========================================
        # 💾 异步双写：把本轮对话存到 Supabase + Pinecone（不阻塞响应）
        # ==========================================
        if sb and user_msg and (collected_content or tool_calls_dict):
            asyncio.create_task(self._save_conversation(sb, user_msg, collected_content, collected_reasoning, tool_calls_dict))

        # 💗 欲望驱动：网页用户消息分类 + AI 回复事件入队（同 TG/QQ，吞异常）。
        #    放在流式响应结束后、不阻塞首字；仅在情感引擎总开关开启且有实际回复时入队。
        if user_msg and (collected_content or tool_calls_dict) and _emotion_enabled():
            asyncio.create_task(self._record_desire_events(user_msg, channel="Web"))

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

        # 自适应注入共用：客户端自带非 system 消息条数（user/assistant 白名单）
        _client_msg_count = sum(
            1 for m in req_data.get("messages", [])
            if m.get("role") in ("user", "assistant")
        )

        # 阶段总结自适应注入（与 INJECT_DB_HISTORY 同模式）
        # 📦 客户端已自带上下文（非 system 消息 >1）时跳过，维持 stable_system 前缀稳定。
        #    由环境变量 INJECT_CORE_SUMMARIES 控制：auto=自适应（默认）、always=总是注入、never=从不注入。
        _inject_core_summaries_mode = os.environ.get("INJECT_CORE_SUMMARIES", "auto").strip().lower()
        _skip_core_summaries = (
            _inject_core_summaries_mode == "never"
            or (_inject_core_summaries_mode == "auto" and _client_msg_count > 1)
        )
        core_summaries = "无长期记忆"
        if not _skip_core_summaries:
            # 阶段总结（带 TTL 缓存 —— 内容不变才能维持 prompt cache 前缀稳定）
            core_summaries = _stable_cached("core_summaries", _CORE_SUMMARIES_TTL)
            if core_summaries is not None:
                _log(f"📦 [Cache] core_summaries 命中 TTL 缓存（{_CORE_SUMMARIES_TTL}s）")
            else:
                core_summaries = "无长期记忆"
                try:
                    sr = await asyncio.to_thread(lambda: sb.table("memories").select("content").eq("tags", "Core_Cognition").order("created_at", desc=True).limit(3).execute())
                    if sr and sr.data:
                        core_summaries = "\n".join([f"- {s['content']}" for s in sr.data])
                except Exception:
                    pass
                _stable_set("core_summaries", core_summaries)
        else:
            if _inject_core_summaries_mode == "auto" and _client_msg_count > 1:
                _log(f"📦 [Cache] 客户端已带 {_client_msg_count} 条历史消息，跳过阶段总结注入（维持缓存前缀稳定）")

        # 用户画像（带 TTL 缓存 —— 同理）
        user_prof = _stable_cached("user_prof", _STABLE_PREFIX_TTL)
        if user_prof is not None:
            _log(f"📦 [Cache] user_prof 命中 TTL 缓存（{_STABLE_PREFIX_TTL}s）")
        else:
            user_prof = "暂无"
            try:
                # 画像查询：排除系统配置键；desire_ 前缀在 Python 侧精细过滤（见 _is_profile_key）。
                # 加 .order("key") 稳定排序 —— 顺序固定，注入进稳定前缀的内容才不会变，缓存前缀才能命中。
                pr = await asyncio.to_thread(lambda: sb.table("user_facts").select("key, value").neq("key", "sys_config").neq("key", "llm_settings").neq("key", "llm_models").order("key").execute())
                if pr and pr.data:
                    rows = [r for r in pr.data if _is_profile_key(r.get("key", ""))]
                    profile_lines = []
                    for r in rows[:30]:
                        val = str(r.get("value", "")).strip()
                        if val:
                            profile_lines.append(f"• {val[:150]}")
                    user_prof = "\n".join(profile_lines) if profile_lines else "暂无"
            except Exception:
                pass
            _stable_set("user_prof", user_prof)

        # Pinecone 向量记忆（可选）
        # 🚫 向量记忆注入门控：vector_memory_injection_enabled=false 时跳过 Pinecone 检索
        #    （不影响普通画像注入、Core_Cognition 注入、Pinecone 数据本身）。
        pinecone_context = "无相关深层记忆"
        shared_context = ""  # Phase 6：shared_experience 共同经历短摘要
        mc = _get_pinecone_memory()
        if mc and current_query.strip() and _vector_injection_enabled():
            try:
                import server as _srv_uid
                def _s():
                    return mc.search(query=str(current_query), user_id=_srv_uid._resolve_pinecone_user_id(), limit=5, source="web_user")
                mr = await asyncio.to_thread(_s)
                if mr:
                    rl = mr.get("results", mr) if isinstance(mr, dict) else mr
                    if isinstance(rl, list) and rl:
                        from shared_experience import partition_recall, render_shared_context
                        _regular, _shared = partition_recall(rl)
                        if _regular:
                            pinecone_context = "\n".join([f"- {m.get('memory', str(m))}" if isinstance(m, dict) else f"- {str(m)}" for m in _regular])
                        shared_context = render_shared_context(_shared)
                    else:
                        _log("🧠 Pinecone 召回 0 条")
                else:
                    _log("🧠 Pinecone 召回 0 条")
            except Exception as e:
                _log(f"Pinecone 检索失败（跳过）: {e}")

        # 最近对话历史（按 tag 拉，转成 user/assistant 交替）
        # 📦 自适应注入：客户端已自带历史（非 system 消息 >1）时跳过 DB 历史，
        #    避免每轮 DB 窗口滚动导致前缀移位、破坏 prompt cache 命中。
        #    由环境变量 INJECT_DB_HISTORY 控制：auto=自适应（默认）、always=总是注入、never=从不注入。
        _inject_db_history_mode = os.environ.get("INJECT_DB_HISTORY", "auto").strip().lower()
        history_msgs = []
        _skip_db_history = (
            _inject_db_history_mode == "never"
            or (_inject_db_history_mode == "auto" and _client_msg_count > 1)
        )
        if _skip_db_history:
            if _inject_db_history_mode == "auto" and _client_msg_count > 1:
                _log(f"📦 [Cache] 客户端已带 {_client_msg_count} 条历史消息，跳过 DB 历史注入（维持缓存前缀稳定）")
        else:
            try:
                _TAGS = [chat_tag, "TG_MSG", "QQ_Chat", "QQ_Group", "Email_Process"]
                hr = await asyncio.to_thread(lambda: sb.table("memories").select("content, tags").in_("tags", _TAGS).order("created_at", desc=True).limit(20).execute())
                if hr and hr.data:
                    rows = list(reversed(hr.data))[-10:]
                    for row in rows:
                        c = str(row.get("content", "")).strip()
                        if not c:
                            continue
                        # 🔒 第1阶段（目标B）：数据库兜底历史只注入「用户侧」内容。
                        #    原实现把旧 AI 回复（"我(AI名)：..."）转成 {"role": "assistant"}
                        #    消息重放给模型，是模仿/重复的直接根因；现在旧 AI 回复与
                        #    无法判断角色的条目一律跳过，不再进入 messages。
                        user_side = _extract_user_side_from_history(c, user_name)
                        if user_side:
                            history_msgs.append({"role": "user", "content": user_side[:500]})
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

        # 🆕 设备状态快照（device_data 最新一条，含更新时间标注）
        #    前台开关门控：device_context_enabled=false 时跳过（仅影响前台聊天）。
        device_snapshot = ""
        if _device_context_enabled():
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
        # 角色层（人设/表情包/回复规则）→ stable_system（可缓存）
        # 审计层（画像/阶段总结）→ volatile_block（不污染人设）
        character_parts = []
        if persona:
            character_parts.append(persona)
        if _WORLD_BOOK_TEXT:
            character_parts.append(_WORLD_BOOK_TEXT)
        if _REPLY_RULES_TEXT:
            character_parts.append(_REPLY_RULES_TEXT)
        stable_system = "\n\n".join(character_parts)

        # 原来拼接 stable_parts 的位置改成 volatile
        volatile_block = (
            f"关于{user_name}：\n{user_prof}\n"
            f"【近3次阶段总结】:\n{core_summaries}\n"
            f"[注：以下是历史参考片段，仅作事实核对，与当前对话无关时忽略。]\n"
            f"【深层关联记忆】:\n{pinecone_context}\n"
        )
        if shared_context:
            volatile_block += f"{shared_context}\n"
        if device_snapshot:
            volatile_block += f"{device_snapshot}\n"
        # 🏠 Home Runtime 上下文（只读，受运行时门控，放在 volatile 区域不破坏缓存前缀）
        if _home_context_enabled():
            try:
                _home_ctx_text = await asyncio.to_thread(_gw_home_context_safe)
                if _home_ctx_text:
                    volatile_block += f"{_home_ctx_text}\n"
            except Exception:
                pass
        volatile_block += (
            f"------------------------------------------------\n"
            f"[实时状态 · 回复前请先读这里]\n"
            f"⏰ 当前时间：{time_str}（北京时间）\n"
            f"🔕 距离上次聊天：{silence_hours}h\n"
            f"📡 当前聊天渠道：{channel_display}"
        )

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

        # 📅 日程注入：查询 [now-7d, now+7d] 的日历事件，注入 volatile_block
        if os.environ.get("CALENDAR_INJECT", "true").strip().lower() == "true":
            try:
                from server import fetch_schedule_for_injection
                _sched = await asyncio.wait_for(fetch_schedule_for_injection(), timeout=8)
                if _sched:
                    volatile_block += f"\n{_sched}"
                    _log(f"📅 [Calendar] 日程已注入上下文")
            except Exception as e:
                _log(f"⚠️ [Calendar] 日程注入失败: {e}")

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

        _summ_tag = "跳过" if _skip_core_summaries else f"{len(core_summaries)}字"
        _log(f"🧠 [智能体] 注入完成：画像{len(user_prof)}字 + 总结{_summ_tag} + Pinecone{len(pinecone_context)}字 + 上文{len(history_msgs)}条" + (f" + 设备快照{len(device_snapshot)}字" if device_snapshot else "") + f" ｜ 稳定前缀{len(stable_system)}字 + 易变尾块{len(volatile_block)}字")

        # 📝 记录本轮注入的 volatile_block 快照（供 /api/prompts 调试面板查看，只留最新5条）
        try:
            _capture_injected_prompt({
                "ts": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
                "channel": channel_display,
                "model": str(req_data.get("model", "")),
                "user_preview": (current_query or "")[:300],
                "volatile_block": volatile_block,
                "stats": {
                    "volatile_total": len(volatile_block),
                    "pinecone": len(pinecone_context or ""),
                    "device_snapshot": len(device_snapshot or ""),
                    "history_msgs": len(history_msgs),
                },
            })
        except Exception:
            pass

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
        # 循环耗尽 / 遇到无法执行的工具时的中性兜底文案（避免误导性的"天气工具调用次数达上限"）
        _TOOL_LOOP_FALLBACK_TEXT = "（工具调用未能在限定轮次内完成，请稍后重试或换种方式提问）"
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
        loop_usage = None  # 收集最后一轮的 usage（含缓存命中字段）

        for round_i in range(max_rounds):
            payload = copy.deepcopy(base_payload)
            payload["messages"] = _sanitize_outgoing_messages(messages)
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

            if data.get("usage"):
                loop_usage = data["usage"]

            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []

            if message.get("reasoning_content"):
                collected_reasoning += str(message.get("reasoning_content") or "")

            if tool_calls:
                # 🛡️ 防御：模型调用了网关无法本地执行的工具（客户端 web_search / mcp__* 或幻觉出的工具名）。
                # 继续执行只会得到 Unknown tool 反馈空耗轮次，提前退出并把已收集内容交还客户端。
                _unknown = [
                    (tc.get("function") or {}).get("name", "?")
                    for tc in tool_calls
                    if not weather_tools.is_weather_tool_call(tc)
                ]
                if _unknown:
                    _log(f"⚠️ [WeatherLoop] 检测到网关无法执行的工具 {_unknown}，退出循环交还客户端")
                    final_text = collected_content or _TOOL_LOOP_FALLBACK_TEXT
                    break

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
            final_text = collected_content or _TOOL_LOOP_FALLBACK_TEXT

        await self._sse_final_text(send, req_data, final_text)

        _log_cache_usage(req_data.get("model", ""), loop_usage)

        if sb and user_msg and (collected_content or tool_calls_dict):
            asyncio.create_task(
                self._save_conversation(sb, user_msg, collected_content, collected_reasoning, tool_calls_dict)
            )

        # 💗 欲望驱动：网页用户消息分类 + AI 回复事件入队（同普通流式路径，吞异常）。
        #    仅在正常结束（有最终文本或工具调用）且情感引擎开启时入队；
        #    上游失败的 3 个 return 分支不入队（无成功回复）。
        if user_msg and (collected_content or tool_calls_dict) and _emotion_enabled():
            asyncio.create_task(self._record_desire_events(user_msg, channel="Web"))

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

    async def _record_desire_events(self, user_msg, channel: str = "Web"):
        """网页聊天：用户消息分类 + AI 回复事件入队（同 TG/QQ，吞异常不影响聊天）。

        顺序：先 record_user_message（含 LLM 分类 + msg_user 事件），
        再 record_assistant_message（msg_assistant 事件），保证事件队列里
        msg_user 在 msg_assistant 之前——与 heartbeat(TG)/napcat(QQ) 完全一致。
        任何异常吞掉、只打日志，绝不影响已发出的聊天响应。
        """
        try:
            import desire_bridge
            await desire_bridge.record_user_message(user_msg, channel=channel)
            await desire_bridge.record_assistant_message()
        except Exception as e:
            _log(f"💗 [欲望驱动] [{channel}] Web 事件入队跳过：{e}")

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
        # 🚫 聊天记录写入门控：chat_history_write_enabled=false 时跳过对话流水写入
        #    （不影响手动保存记忆、已有记忆读取、必要的系统状态记录）。
        if not _chat_write_enabled():
            _log(f"🔇 [聊天写入已关闭] 跳过本轮 memories 流水写入（{chat_tag}）")
        else:
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

            # 2. 写入 Pinecone（可选）—— 同属"聊天记录写入"，受同一开关控制
            #    v2: 仅写 user 消息，不再拼接 assistant 回复，避免旧 AI 回复污染召回。
            mc = _get_pinecone_memory()
            if mc and mc.index and user_msg:
                try:
                    import server as _srv_uid2
                    def _add_m():
                        return mc.add(
                            [{"role": "user", "content": user_msg}],
                            user_id=_srv_uid2._resolve_pinecone_user_id(),
                            metadata={
                                "schema_version": "v2",
                                "source_role": "user",
                                "memory_type": "chat_user_raw",
                                "channel": "web",
                                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            }
                        )
                    _vec_ok = await asyncio.to_thread(_add_m)
                    if _vec_ok:
                        _log("🧠 Pinecone 已写入")
                    else:
                        _log("⚠️ Pinecone 写入返回 False（嵌入失败或索引不可用），本次向量未真正落库 —— 检查 DOUBAO_API_KEY/DOUBAO_EMBEDDING_EP")
                except Exception as e:
                    _log(f"Pinecone 写入失败: {e}")
            elif mc and not mc.index:
                _log("🔇 Pinecone 未配置（PINECONE_API_KEY 缺失），跳过向量写入")

            # 2.5 🔒 第3阶段（memory_events 双写）：Web 原始事件账本（只写不读）
            #    - 受与 memories/Pinecone 相同的 chat_history_write_enabled 门控（隐私开关语义一致：
            #      用户关闭聊天记录写入时，原始事件同样不落库）；
            #    - 独立 try 块：任何失败只记日志，绝不影响上方 memories/Pinecone 写入与主聊天响应；
            #    - user + assistant 两条事件一次批量 insert（同一请求原子落库，不存在半轮事件）；
            #    - 本函数每轮请求恰好被调度一次（流式 / 天气工具循环两条路径互斥汇聚于此），
            #      单次调用内只 insert 一次，天然幂等；不做 select-then-insert 预查询——
            #      表无 source_event_id 唯一约束，查询也做不到强幂等，而查询失败时跳过插入
            #      反而会丢事件（丢事件的代价高于极小概率的重复）；
            #    - occurred_at 与 memories.created_at 使用同一 now_str（保存时刻），保证跨表
            #      时间线可对账；它与"用户消息到达时刻"存在流式回复时长的偏差，已知限制。
            try:
                import uuid as _uuid
                import hashlib as _hashlib
                import server as _srv_ev
                _ev_service = _srv_ev.supabase_service
                if not _ev_service:
                    _log("🔇 [事件账本] service_role 客户端不可用（SUPABASE_SERVICE_KEY 未配置），跳过 memory_events 写入")
                else:
                    # 请求级 ID：uuid4 由服务端生成，仅用于本轮事件归属与日志关联，日志只取前 8 位
                    _ev_request_id = str(_uuid.uuid4())
                    # 统一用户隔离 ID：复用全项目唯一解析规则（USER_ID → MEM0_USER_ID → default）
                    _ev_uid = _srv_ev._resolve_pinecone_user_id()
                    _ev_rows = []
                    if user_msg and user_msg.strip():
                        _ev_rows.append({
                            "user_id": _ev_uid,
                            "session_id": None,  # Web 请求当前无可靠会话标识，诚实写空
                            "channel": "web",
                            "role": "user",
                            "content": user_msg,
                            "content_hash": _hashlib.sha256(user_msg.encode("utf-8")).hexdigest(),
                            "occurred_at": now_str,
                            "source_event_id": f"{_ev_request_id}:user",
                            "processing_status": "pending",
                            "attempt_count": 0,
                            "metadata": {"request_id": _ev_request_id},
                            "created_by": "gateway",
                        })
                    if final_save_text and final_save_text.strip():
                        _ev_rows.append({
                            "user_id": _ev_uid,
                            "session_id": None,
                            "channel": "web",
                            "role": "assistant",
                            # final_save_text 默认已剥离 <think>（SAVE_THINKING=false）；
                            # 工具调用无正文时为脱敏的系统描述，不含工具参数与结果原文
                            "content": final_save_text,
                            "content_hash": _hashlib.sha256(final_save_text.encode("utf-8")).hexdigest(),
                            "occurred_at": now_str,
                            "source_event_id": f"{_ev_request_id}:assistant",
                            "processing_status": "pending",
                            "attempt_count": 0,
                            "metadata": {"request_id": _ev_request_id},
                            "created_by": "gateway",
                        })
                    if _ev_rows:
                        def _insert_events():
                            _ev_service.table("memory_events").insert(_ev_rows).execute()
                        await asyncio.to_thread(_insert_events)
                        _log(f"🧾 [事件账本] Web 原始事件已写入 {len(_ev_rows)} 条（请求 {_ev_request_id[:8]}）")
            except Exception as e:
                _log(f"⚠️ [事件账本] memory_events 写入失败（不影响主流程）: {e}")

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

    async def _handle_prompts_api(self, send):
        """返回最近注入的 volatile_block 快照（只读，最新在前，最多5条）。"""
        try:
            items = list(reversed(_injected_prompts_buffer))  # 最新在前
            await _send_json_resp(send, 200, {"items": items, "total": len(items)})
        except Exception as e:
            await _send_json_resp(send, 500, {"error": str(e)})

    # ------------------------------------------
    # 🧪 手动事实提取预览 /api/memory-extraction-preview（第10阶段）
    #    只读、零写入：不写 memory_items、不更新 memory_events、不触碰 Pinecone。
    #    位于 /api/* 统一鉴权之下（API_SECRET）；仅支持 POST；compression ≤1 次。
    # ------------------------------------------
    async def _handle_memory_extraction_preview(self, scope, receive, send):
        method = scope.get("method", "")
        if method != "POST":
            # OPTIONS 已由全局 CORS 分支处理；其余方法一律 405
            await _send_json_resp(send, 405, {"error": f"Method {method} not allowed"})
            return

        # 读取小型 JSON 请求体（沿用项目 while-receive 聚合模式）
        body = b""
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            await _send_json_resp(send, 400, {"error": "Invalid JSON body"})
            return
        if not isinstance(payload, dict):
            await _send_json_resp(send, 400, {"error": "body 必须是 JSON 对象"})
            return

        # 显式确认：防止误触发（请求体不接受 user_id/event ID/Prompt/模型参数）
        if payload.get("confirm") != "PREVIEW_ONLY":
            await _send_json_resp(send, 400, {"error": "confirm 必须为 \"PREVIEW_ONLY\""})
            return
        limit_raw = payload.get("limit", 10)
        if isinstance(limit_raw, bool) or not isinstance(limit_raw, int) \
                or not (2 <= limit_raw <= 10):
            await _send_json_resp(send, 400, {"error": "limit 必须为 2～10 的整数"})
            return

        try:
            import memory_preview
            import server as _srv_preview
            result = await memory_preview.run_preview(
                _srv_preview.supabase_service, limit=limit_raw)
            await _send_json_resp(send, 200, result)
        except Exception as e:
            # 零写入保证：任何异常都不落库，只返回脱敏错误
            _log(f"⚠️ [提取预览] 接口异常: {type(e).__name__}")
            await _send_json_resp(send, 500, {"ok": False, "code": "INTERNAL_ERROR",
                                              "candidates": []})

    # ------------------------------------------
    # ✍️ 手动候选确认提交 /api/memory-extraction-commit（第17阶段）
    #    两步人工确认的第二步：本接口不接收任何候选内容——正文/类型/时间/来源
    #    全部取自 preview_token 对应的服务端缓存；请求体走严格字段白名单，
    #    出现 content/status/user_id 等任何额外候选数据字段一律 400 不写库。
    #    位于 /api/* 统一鉴权之下（API_SECRET）；仅 POST；强制 status=pending_review；
    #    失败不消费 token、不更新事件；全部成功才消费 token 并更新整批事件。
    # ------------------------------------------
    async def _handle_memory_extraction_commit(self, scope, receive, send):
        method = scope.get("method", "")
        if method != "POST":
            # OPTIONS 已由全局 CORS 分支处理；其余方法一律 405
            await _send_json_resp(send, 405, {"ok": False, "code": "METHOD_NOT_ALLOWED",
                                              "stats": {}})
            return

        # 读取小型 JSON 请求体（沿用项目 while-receive 聚合模式）
        body = b""
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        _invalid = {"ok": False, "code": "INVALID_SELECTION", "stats": {}}
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            await _send_json_resp(send, 400, _invalid)
            return
        if not isinstance(payload, dict):
            await _send_json_resp(send, 400, _invalid)
            return

        # 严格字段白名单：除以下 4 个字段外，出现任何其他字段（content/status/
        # user_id/source_event_ids/importance/metadata/模型参数等）→ 400，不执行写入
        allowed_fields = {"confirm", "preview_token",
                          "selected_preview_indexes", "reviewed_all"}
        if set(payload.keys()) - allowed_fields:
            await _send_json_resp(send, 400, _invalid)
            return
        # 显式确认（必须完全匹配）
        if payload.get("confirm") != "WRITE_PENDING_REVIEW":
            await _send_json_resp(send, 400, {"ok": False, "code": "INVALID_CONFIRMATION",
                                              "stats": {}})
            return
        token = payload.get("preview_token")
        if not isinstance(token, str) or not token:
            await _send_json_resp(send, 400, _invalid)
            return
        indexes = payload.get("selected_preview_indexes")
        if (not isinstance(indexes, list) or not indexes
                or any(isinstance(i, bool) or not isinstance(i, int) for i in indexes)
                or len(set(indexes)) != len(indexes)):
            await _send_json_resp(send, 400, _invalid)
            return
        # reviewed_all 必须严格为 true：用户已审核整批候选，
        # 未选中者视为本轮人工不采纳（不写入），本批事件不再自动提取
        if payload.get("reviewed_all") is not True:
            await _send_json_resp(send, 400, _invalid)
            return

        try:
            import memory_preview
            import server as _srv_commit
            result = await memory_preview.run_commit(
                _srv_commit.supabase_service, token, indexes,
                reviewed_all=payload.get("reviewed_all"))
        except Exception as e:
            # 任何异常都不落库、不消费 token，只返回脱敏错误（不含异常原文）
            _log(f"⚠️ [记忆人工提交] 接口异常: {type(e).__name__}")
            await _send_json_resp(send, 500, {"ok": False, "code": "INTERNAL_ERROR",
                                              "stats": {}})
            return

        error_status = {
            "INVALID_SELECTION": 400,
            "PREVIEW_TOKEN_NOT_FOUND_OR_EXPIRED": 404,
            "PREVIEW_TOKEN_ALREADY_USED": 409,
            "DEDUP_CHECK_FAILED": 500,
            "MEMORY_ITEM_INSERT_FAILED": 500,
            "EVENT_STATUS_UPDATE_FAILED": 500,
            "SERVICE_UNAVAILABLE": 503,
        }
        status = 200 if result.get("ok") else error_status.get(result.get("code"), 500)
        await _send_json_resp(send, status, result)

    # ------------------------------------------
    # 🗂️ pending_review 人工审批列表 /api/memory-review（第19阶段）
    #    只读列出 status=pending_review 候选（最旧优先，limit 1~20），生成短时
    #    review_session_token；候选定位全走服务端缓存，客户端不见数据库 ID。
    #    位于 /api/* 统一鉴权之下（API_SECRET）；仅 GET；零写入、不查 active/rejected。
    # ------------------------------------------
    async def _handle_memory_review(self, scope, receive, send):
        method = scope.get("method", "")
        if method != "GET":
            # OPTIONS 已由全局 CORS 分支处理；其余方法一律 405（不查库）
            await _send_json_resp(send, 405, {"ok": False, "code": "METHOD_NOT_ALLOWED",
                                              "stats": {}})
            return

        # 解析 query string（沿用项目手写解析模式）
        qs = scope.get("query_string", b"").decode("utf-8")
        params = {}
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = v
        limit_raw = params.get("limit", "20")
        _invalid = {"ok": False, "code": "INVALID_REVIEW_REQUEST", "stats": {}}
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            await _send_json_resp(send, 400, _invalid)
            return
        if not (1 <= limit <= 20):
            await _send_json_resp(send, 400, _invalid)
            return

        try:
            import memory_review
            import server as _srv_review
            result = await memory_review.run_list(_srv_review.supabase_service, limit=limit)
        except Exception as e:
            # 只读保证：任何异常都不写库，只返回脱敏错误
            _log(f"⚠️ [记忆审核列表] 接口异常: {type(e).__name__}")
            await _send_json_resp(send, 500, {"ok": False, "code": "INTERNAL_ERROR",
                                              "stats": {}})
            return

        status = 200 if result.get("ok") else 500
        await _send_json_resp(send, status, result)

    # ------------------------------------------
    # 🗂️ pending_review 人工单条决策 /api/memory-review/decision（第19阶段）
    #    approve 仅 pending_review→active；reject 仅 pending_review→rejected（不删除）。
    #    请求体严格 4 字段白名单：客户端不提交候选正文、不提交 status/user_id 等
    #    任何字段；候选定位全部取自 review_session_token 对应服务端缓存。
    #    位于 /api/* 统一鉴权之下（API_SECRET）；仅 POST；逐条显式决策，无批量。
    # ------------------------------------------
    async def _handle_memory_review_decision(self, scope, receive, send):
        method = scope.get("method", "")
        if method != "POST":
            # OPTIONS 已由全局 CORS 分支处理；其余方法一律 405（不查库、不更新）
            await _send_json_resp(send, 405, {"ok": False, "code": "METHOD_NOT_ALLOWED",
                                              "stats": {}})
            return

        # 读取小型 JSON 请求体（沿用项目 while-receive 聚合模式）
        body = b""
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        _invalid = {"ok": False, "code": "INVALID_REVIEW_REQUEST", "stats": {}}
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            await _send_json_resp(send, 400, _invalid)
            return
        if not isinstance(payload, dict):
            await _send_json_resp(send, 400, _invalid)
            return

        # 严格字段白名单：除以下 4 个字段外，出现任何其他字段（item ID/content/
        # status/user_id/importance/confidence/subject_key/memory_type/valid_at/
        # expires_at/metadata/active/reason/comment 等）→ 400，不执行更新
        allowed_fields = {"confirm", "review_session_token", "review_index", "decision"}
        if set(payload.keys()) - allowed_fields:
            await _send_json_resp(send, 400, _invalid)
            return
        # 显式确认（必须完全匹配）
        if payload.get("confirm") != "DECIDE_MEMORY_REVIEW":
            await _send_json_resp(send, 400, {"ok": False, "code": "INVALID_CONFIRMATION",
                                              "stats": {}})
            return
        token = payload.get("review_session_token")
        if not isinstance(token, str) or not token:
            await _send_json_resp(send, 400, _invalid)
            return
        index = payload.get("review_index")
        if isinstance(index, bool) or not isinstance(index, int):
            await _send_json_resp(send, 400, _invalid)
            return
        decision = payload.get("decision")
        if decision not in ("approve", "reject"):
            await _send_json_resp(send, 400, {"ok": False, "code": "INVALID_DECISION",
                                              "stats": {}})
            return

        try:
            import memory_review
            import server as _srv_review
            result = await memory_review.run_decision(
                _srv_review.supabase_service, token, index, decision)
        except Exception as e:
            # 任何异常都不写库、不消费 index，只返回脱敏错误（不含异常原文）
            _log(f"⚠️ [记忆审核决策] 接口异常: {type(e).__name__}")
            await _send_json_resp(send, 500, {"ok": False, "code": "INTERNAL_ERROR",
                                              "stats": {}})
            return

        error_status = {
            "INVALID_REVIEW_REQUEST": 400,
            "INVALID_DECISION": 400,
            "REVIEW_SESSION_NOT_FOUND_OR_EXPIRED": 404,
            "REVIEW_INDEX_NOT_FOUND": 404,
            "REVIEW_INDEX_ALREADY_DECIDED": 409,
            "REVIEW_ITEM_STATE_CHANGED": 409,
            "ACTIVE_SUBJECT_CONFLICT": 409,
            "ACTIVE_EXACT_DUPLICATE": 409,
            "REVIEW_QUERY_FAILED": 500,
            "REVIEW_UPDATE_FAILED": 500,
        }
        status = 200 if result.get("ok") else error_status.get(result.get("code"), 500)
        await _send_json_resp(send, status, result)

    # ------------------------------------------
    # 🔎 active 记忆只读召回预览 /api/memory-recall-preview（第21阶段）
    #    用户手动提交一条查询，服务端只读查询同用户 status=active 的
    #    memory_items，内存排除已过期条目，确定性词面相关性排序返回预览。
    #    位于 /api/* 统一鉴权之下（API_SECRET）；仅 POST；零写入、不接正式
    #    上下文、无 LLM/Pinecone/embedding；user_id 由服务端统一解析，
    #    请求体严格 3 字段白名单（confirm/query/top_k），其余字段一律 400。
    # ------------------------------------------
    async def _handle_memory_recall_preview(self, scope, receive, send):
        method = scope.get("method", "")
        if method != "POST":
            # OPTIONS 已由全局 CORS 分支处理；其余方法一律 405（不查库）
            await _send_json_resp(send, 405, {"ok": False, "code": "METHOD_NOT_ALLOWED",
                                              "stats": {}})
            return

        # 读取小型 JSON 请求体（沿用项目 while-receive 聚合模式）
        body = b""
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        _invalid = {"ok": False, "code": "INVALID_RECALL_REQUEST", "stats": {}}
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            await _send_json_resp(send, 400, _invalid)
            return
        if not isinstance(payload, dict):
            await _send_json_resp(send, 400, _invalid)
            return

        # 严格字段白名单：出现任何其他字段（user_id/status/memory_type/item ID/
        # namespace/threshold/provider/model/include_pending/include_rejected/
        # include_expired/write_back/update_recall_count 等）→ 400，不查库
        allowed_fields = {"confirm", "query", "top_k"}
        if set(payload.keys()) - allowed_fields:
            await _send_json_resp(send, 400, _invalid)
            return
        # 显式确认（必须完全匹配）
        if payload.get("confirm") != "RECALL_PREVIEW_ONLY":
            await _send_json_resp(send, 400, {"ok": False, "code": "INVALID_CONFIRMATION",
                                              "stats": {}})
            return
        # query：字符串、trim 后非空、≤500 字符
        query = payload.get("query")
        if (not isinstance(query, str) or not query.strip()
                or len(query.strip()) > 500):
            await _send_json_resp(send, 400, _invalid)
            return
        # top_k：整数且非 bool，缺省 5，范围 1~10
        top_k = payload.get("top_k", 5)
        if (isinstance(top_k, bool) or not isinstance(top_k, int)
                or not (1 <= top_k <= 10)):
            await _send_json_resp(send, 400, _invalid)
            return

        try:
            import memory_recall
            import server as _srv_recall
            # user_id 由服务端统一解析规则给出，客户端无任何提交入口
            result = await memory_recall.run_recall(
                _srv_recall.supabase_service,
                _srv_recall._resolve_pinecone_user_id(),
                query.strip(), top_k)
        except Exception as e:
            # 只读保证：任何异常都不写库，只返回脱敏错误（不含异常原文）
            _log(f"⚠️ [记忆召回预览] 接口异常: {type(e).__name__}")
            await _send_json_resp(send, 500, {"ok": False, "code": "INTERNAL_ERROR",
                                              "stats": {}})
            return

        error_status = {
            "INVALID_RECALL_REQUEST": 400,
            "RECALL_QUERY_FAILED": 500,
            "RECALL_SERVICE_UNAVAILABLE": 503,
        }
        status = 200 if result.get("ok") else error_status.get(result.get("code"), 500)
        await _send_json_resp(send, status, result)

    # ------------------------------------------
    # 📐 生产 embedding 维度安全诊断 /api/embedding-dimension-preview（第28阶段）
    #    唯一目标：确认生产运行时 _get_embedding() 的实际输出维度
    #    （EMBEDDING_DIMENSION_NOT_CONFIRMED 是向量召回设计的唯一阻塞项）。
    #    固定合成探针、最多一次 provider 调用；只返回维度与 finite 检查结果，
    #    不返回向量/模型名/provider/URL/Key/环境变量/异常原文；
    #    零数据库 / Pinecone / LLM 副作用；不接正式上下文；手动调用，无自动调度。
    # ------------------------------------------
    async def _handle_embedding_dimension_preview(self, scope, receive, send):
        method = scope.get("method", "")
        if method != "POST":
            # OPTIONS 已由全局 CORS 分支处理；其余方法一律 405（不触碰 provider）
            await _send_json_resp(send, 405, {
                "ok": False, "code": "METHOD_NOT_ALLOWED",
                "diagnostics": {"dimension": None, "all_values_numeric": None,
                                "all_values_finite": None,
                                "hnsw_vector_dimension_supported": None},
                "execution": {"provider_calls": 0, "database_reads": 0,
                              "database_writes": 0, "pinecone_touched": False,
                              "llm_touched": False}})
            return

        # 读取小型 JSON 请求体（沿用项目 while-receive 聚合模式）
        body = b""
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        _invalid = {
            "ok": False, "code": "INVALID_DIAGNOSTIC_REQUEST",
            "diagnostics": {"dimension": None, "all_values_numeric": None,
                            "all_values_finite": None,
                            "hnsw_vector_dimension_supported": None},
            "execution": {"provider_calls": 0, "database_reads": 0,
                          "database_writes": 0, "pinecone_touched": False,
                          "llm_touched": False}}
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            await _send_json_resp(send, 400, _invalid)
            return
        if not isinstance(payload, dict):
            await _send_json_resp(send, 400, _invalid)
            return

        # 严格字段白名单：除 confirm 外出现任何字段（text/input/model/provider/
        # api_key/endpoint/dimensions/user_id/write/backfill/item_id 等）→ 400，
        # 绝不调用 _get_embedding()
        allowed_fields = {"confirm"}
        if set(payload.keys()) - allowed_fields:
            await _send_json_resp(send, 400, _invalid)
            return
        # 显式确认（必须完全匹配）
        if payload.get("confirm") != "PROBE_EMBEDDING_DIMENSION":
            await _send_json_resp(send, 400, {
                "ok": False, "code": "INVALID_CONFIRMATION",
                "diagnostics": {"dimension": None, "all_values_numeric": None,
                                "all_values_finite": None,
                                "hnsw_vector_dimension_supported": None},
                "execution": {"provider_calls": 0, "database_reads": 0,
                              "database_writes": 0, "pinecone_touched": False,
                              "llm_touched": False}})
            return

        # 惰性导入（沿用项目 handler 内按需 import 惯例）：
        # embedding_diagnostics 为零依赖纯函数模块；server 仅取 _get_embedding，
        # 不创建第二个 embedding 客户端、不绕过既有路径
        try:
            import embedding_diagnostics as _ed
            import server as _srv_embed
            result, log_line = _ed.run_dimension_probe(_srv_embed._get_embedding)
        except Exception as e:
            # 模块内部已全捕获；此处仅防御 import 等意外，异常只记类型不记原文
            _log(f"⚠️ embedding维度诊断失败：code=INTERNAL_ERROR "
                 f"stage=handler exception_type={type(e).__name__}")
            await _send_json_resp(send, 500, {
                "ok": False, "code": "INTERNAL_ERROR",
                "diagnostics": {"dimension": None, "all_values_numeric": None,
                                "all_values_finite": None,
                                "hnsw_vector_dimension_supported": None},
                "execution": {"provider_calls": 0, "database_reads": 0,
                              "database_writes": 0, "pinecone_touched": False,
                              "llm_touched": False}})
            return

        _log(log_line)
        status = _ed.HTTP_STATUS_BY_CODE.get(result.get("code"), 500)
        await _send_json_resp(send, status, result)

    # ------------------------------------------
    # 🧬 active 记忆向量手动回填 /api/memory-embedding-backfill（第31阶段）
    #    手动、一次一条、受 API_SECRET 保护：服务端强制选定最旧 active 且
    #    embedding IS NULL 的一条，用其事实化 content 恰调用一次现有
    #    _get_embedding，校验 1024/finite/非零后，以单条条件 UPDATE 原子写入
    #    embedding/embedding_model/embedded_at 三列。
    #    客户端仅提交 {"confirm": "..."}；item_id/正文/向量/模型/user_id 等
    #    任何额外字段一律拒绝；不改 status/content/updated_at；无 Pinecone/
    #    LLM/自动调度；不接正式上下文；幂等依据 embedding IS NULL。
    # ------------------------------------------
    async def _handle_memory_embedding_backfill(self, scope, receive, send):
        method = scope.get("method", "")
        if method != "POST":
            # OPTIONS 已由全局 CORS 分支处理；其余方法一律 405（不查库、不调 provider）
            await _send_json_resp(send, 405, {
                "ok": False, "code": "METHOD_NOT_ALLOWED",
                "stats": {"selected": 0, "updated": 0},
                "execution": {"provider_calls": 0, "database_reads": 0,
                              "database_writes": 0, "pinecone_touched": False,
                              "llm_touched": False}})
            return

        # 读取小型 JSON 请求体（沿用项目 while-receive 聚合模式）
        body = b""
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break

        _invalid = {
            "ok": False, "code": "INVALID_BACKFILL_REQUEST",
            "stats": {"selected": 0, "updated": 0},
            "execution": {"provider_calls": 0, "database_reads": 0,
                          "database_writes": 0, "pinecone_touched": False,
                          "llm_touched": False}}
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            await _send_json_resp(send, 400, _invalid)
            return
        if not isinstance(payload, dict):
            await _send_json_resp(send, 400, _invalid)
            return

        # 严格字段白名单：只允许 confirm。客户端提交 item_id/user_id/content/
        # text/vector/embedding/model/provider/dimensions/limit/status/force/
        # overwrite/write_back/batch 等任何额外字段 → 400，
        # 绝不查询数据库、绝不调用 provider、绝不 UPDATE
        allowed_fields = {"confirm"}
        if set(payload.keys()) - allowed_fields:
            await _send_json_resp(send, 400, _invalid)
            return
        # 显式确认（必须完全匹配，与 memory_embedding.CONFIRM_TOKEN 一致）
        if payload.get("confirm") != "BACKFILL_ONE_ACTIVE_MEMORY":
            await _send_json_resp(send, 400, {
                "ok": False, "code": "INVALID_CONFIRMATION",
                "stats": {"selected": 0, "updated": 0},
                "execution": {"provider_calls": 0, "database_reads": 0,
                              "database_writes": 0, "pinecone_touched": False,
                              "llm_touched": False}})
            return

        # 惰性导入（沿用项目 handler 内按需 import 惯例）：
        # server 仅取 service_role 客户端、_get_embedding 与服务端 user_id 解析；
        # 模型标识在 handler 内只读现有环境变量 DOUBAO_EMBEDDING_EP 后传入模块，
        # 仅用于写入 embedding_model 列，不打印、不返回；不新建 embedding 客户端
        try:
            import memory_embedding as _me
            import server as _srv_bf
            model_id = os.environ.get("DOUBAO_EMBEDDING_EP", "").strip()
            result, log_line = await _me.run_backfill(
                _srv_bf.supabase_service,
                _srv_bf._resolve_pinecone_user_id(),
                _srv_bf._get_embedding,
                model_id)
        except Exception as e:
            # 模块内部已全捕获；此处仅防御 import 等意外，异常只记类型不记原文
            _log(f"⚠️ active记忆向量回填失败：stage=handler "
                 f"error=INTERNAL_ERROR exception_type={type(e).__name__}")
            await _send_json_resp(send, 500, {
                "ok": False, "code": "INTERNAL_ERROR",
                "stats": {"selected": 0, "updated": 0},
                "execution": {"provider_calls": 0, "database_reads": 0,
                              "database_writes": 0, "pinecone_touched": False,
                              "llm_touched": False}})
            return

        _log(log_line)
        status = _me.HTTP_STATUS_BY_CODE.get(result.get("code"), 500)
        await _send_json_resp(send, status, result)

    # ------------------------------------------
    # 🧪 memory_items 向量 RPC 自匹配只读预览 /api/memory-vector-selftest-preview（第33阶段）
    #    手动、只读、受 API_SECRET 保护：服务端强制选定最旧 active 且 embedding
    #    非空的一条，核对其 embedding_model 与当前配置一致后，用其事实化 content
    #    恰调用一次现有 _get_embedding 生成查询向量，经 service_role 客户端只读
    #    调用第30阶段 match_memory_items（match_count 固定 5），核对 Top1 内部
    #    ID 与相似度（≥0.99）。
    #    客户端仅提交 {"confirm": "..."}；item_id/正文/向量/模型/user_id 等
    #    任何额外字段一律拒绝；响应只含脱敏统计（不含 ID/正文/user_id/模型名/
    #    向量/RPC 原始行/hash/来源）；零写入、无 Pinecone、无 LLM、无自动调度；
    #    不接正式上下文；自匹配成立不代表同义召回成立。
    # ------------------------------------------
    async def _handle_memory_vector_selftest(self, scope, receive, send):
        def _safe_body(code):
            """错误/诊断路径统一安全骨架（不含任何敏感值）。"""
            return {"ok": False, "code": code,
                    "stats": {"selected": 0, "rpc_returned": 0,
                              "top1_match": False, "top1_similarity": None,
                              "dimension": None},
                    "retrieval": {"method": "pgvector_cosine_selftest_v1",
                                  "active_only": True,
                                  "expired_excluded": True,
                                  "user_scoped": True,
                                  "writes_executed": False},
                    "execution": {"provider_calls": 0, "database_reads": 0,
                                  "database_writes": 0,
                                  "pinecone_touched": False,
                                  "llm_touched": False}}

        method = scope.get("method", "")
        if method != "POST":
            # OPTIONS 已由全局 CORS 分支处理；其余方法一律 405（不查库、不调 provider）
            await _send_json_resp(send, 405, _safe_body("METHOD_NOT_ALLOWED"))
            return

        # 读取小型 JSON 请求体（沿用项目 while-receive 聚合模式）
        body = b""
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            await _send_json_resp(send, 400, _safe_body("INVALID_SELFTEST_REQUEST"))
            return
        if not isinstance(payload, dict):
            await _send_json_resp(send, 400, _safe_body("INVALID_SELFTEST_REQUEST"))
            return

        # 严格字段白名单：只允许 confirm。客户端提交 query/content/item_id/
        # user_id/vector/embedding/model/provider/top_k/threshold/status/
        # include_pending/write/update/backfill 等任何额外字段 → 400，
        # 绝不查询数据库、绝不调用 provider、绝不调用 RPC
        allowed_fields = {"confirm"}
        if set(payload.keys()) - allowed_fields:
            await _send_json_resp(send, 400, _safe_body("INVALID_SELFTEST_REQUEST"))
            return
        # 显式确认（必须完全匹配，与 memory_vector_selftest.CONFIRM_TOKEN 一致）
        if payload.get("confirm") != "VECTOR_SELFTEST_PREVIEW_ONLY":
            await _send_json_resp(send, 400, _safe_body("INVALID_CONFIRMATION"))
            return

        # 惰性导入（沿用项目 handler 内按需 import 惯例）：
        # server 仅取 service_role 客户端、_get_embedding 与服务端 user_id 解析；
        # 模型标识在 handler 内只读现有环境变量 DOUBAO_EMBEDDING_EP 后传入模块，
        # 仅用于与库内 embedding_model 比对，不打印、不返回；不新建 embedding 客户端
        try:
            import memory_vector_selftest as _mvs
            import server as _srv_st

            def _rpc_caller(params):
                # 只读 RPC：第30阶段 service_role-only 的 active-only 余弦召回；
                # 每请求至多被 selftest 模块调用一次
                return _srv_st.supabase_service.rpc(_mvs.RPC_NAME, params).execute()

            model_id = os.environ.get("DOUBAO_EMBEDDING_EP", "").strip()
            result, log_line = await _mvs.run_selftest(
                _srv_st.supabase_service,
                _srv_st._resolve_pinecone_user_id(),
                _srv_st._get_embedding,
                model_id,
                _rpc_caller)
        except Exception as e:
            # 模块内部已全捕获；此处仅防御 import 等意外，异常只记类型不记原文
            _log(f"⚠️ 向量自匹配预览失败：stage=handler "
                 f"error=INTERNAL_ERROR exception_type={type(e).__name__}")
            await _send_json_resp(send, 500, _safe_body("INTERNAL_ERROR"))
            return

        _log(log_line)
        status = _mvs.HTTP_STATUS_BY_CODE.get(result.get("code"), 500)
        await _send_json_resp(send, status, result)

    # ------------------------------------------
    # 🔍 用户查询向量召回只读预览 /api/memory-vector-recall-preview（第35阶段）
    #    手动、只读、受 API_SECRET 保护：服务端解析 user_id 后，用现有
    #    _get_embedding 对用户查询文本恰嵌入一次（1024/finite/非零校验），
    #    经 service_role 客户端只读调用第30阶段 match_memory_items 恰一次
    #    （match_count 服务端固定 10，客户端 top_k 只截断预览列表），模块内
    #    二次过滤（active-only / 过期 / 时间可解析 / 内部 ID 去重）后按
    #    similarity 降序返回脱敏候选。
    #    请求体白名单仅 confirm/query/top_k；user_id/status/memory_type/
    #    threshold/provider/model 等任何额外字段一律 400 且零调用；
    #    不设相似度硬阈值（threshold_applied=false），低相似度候选照常返回；
    #    响应不含内部 ID/user_id/向量/模型名/provider/异常原文；零写入、
    #    无 Pinecone、无 LLM、无自动调度；不接正式上下文；不接词面召回。
    # ------------------------------------------
    async def _handle_memory_vector_recall(self, scope, receive, send):
        def _safe_body(code):
            """错误/诊断路径统一安全骨架（不含任何敏感值）。"""
            return {"ok": False, "code": code,
                    "stats": {"query_embedded": False, "dimension": None,
                              "rpc_returned": 0, "returned": 0,
                              "status_filtered": 0, "expired_filtered": 0,
                              "invalid_time_filtered": 0,
                              "duplicate_filtered": 0},
                    "retrieval": {"method": "pgvector_cosine_vector_recall_v1",
                                  "active_only": True,
                                  "expired_excluded": True,
                                  "user_scoped": True,
                                  "threshold_applied": False,
                                  "writes_executed": False},
                    "items": []}

        method = scope.get("method", "")
        if method != "POST":
            # OPTIONS 已由全局 CORS 分支处理；其余方法一律 405（不查库、不调 provider）
            await _send_json_resp(send, 405, _safe_body("METHOD_NOT_ALLOWED"))
            return

        # 读取小型 JSON 请求体（沿用项目 while-receive 聚合模式）
        body = b""
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            await _send_json_resp(send, 400, _safe_body("INVALID_VECTOR_RECALL_REQUEST"))
            return
        if not isinstance(payload, dict):
            await _send_json_resp(send, 400, _safe_body("INVALID_VECTOR_RECALL_REQUEST"))
            return

        # 严格字段白名单：只允许 confirm/query/top_k。客户端提交 user_id/
        # status/memory_type/threshold/provider/model/namespace/include_*/
        # write_back/item_id/vector/embedding/force/batch 等任何额外字段 → 400，
        # 绝不查询数据库、绝不调用 provider、绝不调用 RPC
        allowed_fields = {"confirm", "query", "top_k"}
        if set(payload.keys()) - allowed_fields:
            await _send_json_resp(send, 400, _safe_body("INVALID_VECTOR_RECALL_REQUEST"))
            return
        # 显式确认（必须完全匹配，与 memory_vector_recall.CONFIRM_TOKEN 一致）
        if payload.get("confirm") != "VECTOR_RECALL_PREVIEW_ONLY":
            await _send_json_resp(send, 400, _safe_body("INVALID_CONFIRMATION"))
            return

        # 惰性导入（沿用项目 handler 内按需 import 惯例）：
        # server 仅取 service_role 客户端、_get_embedding 与服务端 user_id 解析；
        # 模块常量用于请求校验（与模块防御性复验同一来源，避免漂移）；
        # 不新建 embedding 客户端、不读任何环境变量
        try:
            import memory_vector_recall as _mvr
            import server as _srv_st
        except Exception as e:
            _log(f"⚠️ 向量召回预览失败：stage=handler_import "
                 f"error=INTERNAL_ERROR exception_type={type(e).__name__}")
            await _send_json_resp(send, 500, _safe_body("INTERNAL_ERROR"))
            return

        # query 校验：字符串、trim 后非空、≤ 模块定义上限
        query = payload.get("query")
        if not isinstance(query, str):
            await _send_json_resp(send, 400, _safe_body("INVALID_VECTOR_RECALL_REQUEST"))
            return
        query_text = query.strip()
        if not query_text or len(query_text) > _mvr.QUERY_MAX_LENGTH:
            await _send_json_resp(send, 400, _safe_body("INVALID_VECTOR_RECALL_REQUEST"))
            return

        # top_k 校验：整数且非 bool；缺省 5；范围 1~模块定义上限
        top_k = payload.get("top_k", _mvr.DEFAULT_TOP_K)
        if (isinstance(top_k, bool) or not isinstance(top_k, int)
                or not (_mvr.TOP_K_MIN <= top_k <= _mvr.TOP_K_MAX)):
            await _send_json_resp(send, 400, _safe_body("INVALID_VECTOR_RECALL_REQUEST"))
            return

        try:
            def _rpc_caller(params):
                # 只读 RPC：第30阶段 service_role-only 的 active-only 余弦召回；
                # 每请求至多被 recall 模块调用一次
                return _srv_st.supabase_service.rpc(_mvr.RPC_NAME, params).execute()

            result, log_line = await _mvr.run_recall(
                query_text,
                _srv_st._resolve_pinecone_user_id(),
                _srv_st._get_embedding,
                _rpc_caller,
                top_k)
        except Exception as e:
            # 模块内部已全捕获；此处仅防御意外，异常只记类型不记原文
            _log(f"⚠️ 向量召回预览失败：stage=handler "
                 f"error=INTERNAL_ERROR exception_type={type(e).__name__}")
            await _send_json_resp(send, 500, _safe_body("INTERNAL_ERROR"))
            return

        _log(log_line)
        status = _mvr.HTTP_STATUS_BY_CODE.get(result.get("code"), 500)
        await _send_json_resp(send, status, result)

    # ------------------------------------------
    # 🔀 lexical + vector 混合召回只读预览（第37阶段）
    #    一次 embedding + 一次 service_role 只读 RPC 取得 active 向量候选，
    #    对同批候选以 deterministic_lexical_v1 词面二次打分（本阶段 lexical
    #    是对 vector 候选的二次排序，不是全量 active lexical 检索），服务端
    #    内部 memory_item_id 去重合并，RRF（rrf_k=60，排名融合参数而非阈值）
    #    融合排序后返回脱敏候选。
    #    请求体白名单仅 confirm/query/top_k；user_id/status/memory_type/
    #    threshold/weight/rrf_k/item_id/vector/embedding 等任何额外字段一律
    #    400 且零调用；不设相似度阈值（threshold_applied=false）；无固定分数
    #    权重；零写入、不更新召回统计；无 Pinecone、无 LLM、无自动调度；
    #    不接正式上下文；不修改词面算法与向量 RPC（算法本体经模块 import
    #    复用）。
    # ------------------------------------------
    async def _handle_memory_hybrid_recall(self, scope, receive, send):
        def _safe_body(code):
            """错误/诊断路径统一安全骨架（不含任何敏感值）。"""
            return {"ok": False, "code": code,
                    "stats": {"query_embedded": False, "dimension": None,
                              "vector_candidates": 0, "lexical_candidates": 0,
                              "merged_candidates": 0, "returned": 0,
                              "threshold_applied": False},
                    "retrieval": {"method": "rrf_hybrid_preview_v1",
                                  "vector_method":
                                      "pgvector_cosine_vector_recall_v1",
                                  "lexical_method": "deterministic_lexical_v1",
                                  "active_only": True,
                                  "expired_excluded": True,
                                  "user_scoped": True,
                                  "threshold_applied": False,
                                  "writes_executed": False},
                    "items": []}

        method = scope.get("method", "")
        if method != "POST":
            # OPTIONS 已由全局 CORS 分支处理；其余方法一律 405（不查库、不调 provider）
            await _send_json_resp(send, 405, _safe_body("METHOD_NOT_ALLOWED"))
            return

        # 读取小型 JSON 请求体（沿用项目 while-receive 聚合模式）
        body = b""
        while True:
            msg = await receive()
            if msg.get("type") != "http.request":
                break
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            await _send_json_resp(send, 400, _safe_body("INVALID_HYBRID_RECALL_REQUEST"))
            return
        if not isinstance(payload, dict):
            await _send_json_resp(send, 400, _safe_body("INVALID_HYBRID_RECALL_REQUEST"))
            return

        # 严格字段白名单：只允许 confirm/query/top_k。客户端提交 user_id/
        # status/memory_type/threshold/provider/model/lexical_weight/
        # vector_weight/rrf_k/item_id/vector/embedding/write_back 等任何额外
        # 字段 → 400，绝不调用 provider、绝不调用 RPC、绝不做词面打分
        allowed_fields = {"confirm", "query", "top_k"}
        if set(payload.keys()) - allowed_fields:
            await _send_json_resp(send, 400, _safe_body("INVALID_HYBRID_RECALL_REQUEST"))
            return
        # 显式确认（必须完全匹配，与 memory_hybrid_recall.CONFIRM_TOKEN 一致）
        if payload.get("confirm") != "HYBRID_RECALL_PREVIEW_ONLY":
            await _send_json_resp(send, 400, _safe_body("INVALID_CONFIRMATION"))
            return

        # 惰性导入（沿用项目 handler 内按需 import 惯例）：
        # server 仅取 service_role 客户端、_get_embedding 与服务端 user_id
        # 解析；模块常量用于请求校验（与模块防御性复验同一来源，避免漂移）；
        # 不新建 embedding 客户端、不读任何环境变量
        try:
            import memory_hybrid_recall as _mhr
            import server as _srv_st
        except Exception as e:
            _log(f"⚠️ 混合召回预览失败：stage=handler_import "
                 f"error=INTERNAL_ERROR exception_type={type(e).__name__}")
            await _send_json_resp(send, 500, _safe_body("INTERNAL_ERROR"))
            return

        # query 校验：字符串、trim 后非空、≤ 模块定义上限
        query = payload.get("query")
        if not isinstance(query, str):
            await _send_json_resp(send, 400, _safe_body("INVALID_HYBRID_RECALL_REQUEST"))
            return
        query_text = query.strip()
        if not query_text or len(query_text) > _mhr.QUERY_MAX_LENGTH:
            await _send_json_resp(send, 400, _safe_body("INVALID_HYBRID_RECALL_REQUEST"))
            return

        # top_k 校验：整数且非 bool；缺省 5；范围 1~模块定义上限
        top_k = payload.get("top_k", _mhr.DEFAULT_TOP_K)
        if (isinstance(top_k, bool) or not isinstance(top_k, int)
                or not (_mhr.TOP_K_MIN <= top_k <= _mhr.TOP_K_MAX)):
            await _send_json_resp(send, 400, _safe_body("INVALID_HYBRID_RECALL_REQUEST"))
            return

        try:
            def _rpc_caller(params):
                # 只读 RPC：第30阶段 service_role-only 的 active-only 余弦召回；
                # 每请求至多被混合召回模块调用一次
                return _srv_st.supabase_service.rpc(_mhr.RPC_NAME, params).execute()

            result, log_line = await _mhr.run_hybrid_recall(
                query_text,
                _srv_st._resolve_pinecone_user_id(),
                _srv_st._get_embedding,
                _rpc_caller,
                top_k)
        except Exception as e:
            # 模块内部已全捕获；此处仅防御意外，异常只记类型不记原文
            _log(f"⚠️ 混合召回预览失败：stage=handler "
                 f"error=INTERNAL_ERROR exception_type={type(e).__name__}")
            await _send_json_resp(send, 500, _safe_body("INTERNAL_ERROR"))
            return

        _log(log_line)
        status = _mhr.HTTP_STATUS_BY_CODE.get(result.get("code"), 500)
        await _send_json_resp(send, status, result)

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
            roles = reg.get("roles", {})
            safe = []
            for m in reg.get("models", []):
                mid = m.get("id", "")
                safe.append({
                    "id": mid,
                    "label": m.get("label", m.get("id", "")),
                    "base_url": m.get("base_url", ""),
                    "api_key_masked": _mask_key(m.get("api_key", "")),
                    "has_key": bool(str(m.get("api_key", "")).strip()),
                    "model": m.get("model", ""),
                    "enabled": m.get("enabled", True),
                    "thinking": m.get("thinking", "auto"),
                    # 额外密钥（脱敏）：同一 base_url 下的多 key 轮询
                    "extra_keys_masked": [_mask_key(k) for k in (m.get("extra_keys") or [])],
                    "extra_keys_count": len(m.get("extra_keys") or []),
                    # 当前承担的身份（便于前端展示"当前身份"列）
                    "roles": [r for r, v in {
                        "chat": mid in (roles.get("chat") or []),
                        "chat_default": mid == roles.get("chat_default"),
                        "compression": mid == roles.get("compression"),
                        "background": mid == roles.get("background"),
                    }.items() if v],
                })
            await _send_json_resp(send, 200, {
                "models": safe,
                "default": reg.get("default", ""),
                "roles": roles,
                "schema_version": reg.get("schema_version", 1),
                # 旧配置来源提示（控制台"待迁移模型"展示）
                "legacy_llm_settings": _has_legacy_llm_settings(),
            })
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
            # 角色占用检查：禁止删除仍被角色使用的模型，要求用户先重新分配角色
            used = _model_role_usage(reg, del_id)
            if used:
                await _send_json_resp(send, 409, {
                    "error": f"模型 {del_id} 仍被角色 {', '.join(used)} 使用，请先重新分配角色再删除",
                    "used_by": used,
                })
                return
            before = len(reg.get("models", []))
            reg["models"] = [m for m in reg.get("models", []) if m.get("id") != del_id]
            # 清理 roles 里对该 id 的引用
            roles = reg.get("roles", {})
            roles["chat"] = [x for x in (roles.get("chat") or []) if x != del_id]
            if roles.get("chat_default") == del_id:
                roles["chat_default"] = roles["chat"][0] if roles["chat"] else ""
            if roles.get("compression") == del_id:
                roles["compression"] = ""
            if roles.get("background") == del_id:
                roles["background"] = ""
            reg["roles"] = roles
            reg["default"] = roles.get("chat_default") or ""
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

            # 设默认聊天模型（同时更新 roles.chat_default）
            if action == "set_default":
                did = str(payload.get("id", "")).strip()
                reg = _load_llm_registry()
                if not any(m.get("id") == did and m.get("enabled", True) for m in reg.get("models", [])):
                    await _send_json_resp(send, 404, {"error": f"未找到已启用的模型 id={did}"})
                    return
                reg["default"] = did
                roles = reg.get("roles", {})
                roles["chat_default"] = did
                if did not in (roles.get("chat") or []):
                    roles["chat"] = [did] + [x for x in (roles.get("chat") or []) if x != did]
                reg["roles"] = roles
                ok = _save_llm_registry(reg)
                await _send_json_resp(send, 200 if ok else 500, {"ok": ok, "default": did})
                return

            # 设置模型身份（chat 多选 / compression 单选 / background 单选）
            if action == "set_role":
                role = str(payload.get("role", "")).strip()
                if role not in _LLM_ROLES:
                    await _send_json_resp(send, 400, {"error": f"role 必须是 {','.join(_LLM_ROLES)}"})
                    return
                reg = _load_llm_registry()
                roles = dict(reg.get("roles", {}))
                enabled_ids = {m.get("id") for m in reg.get("models", []) if m.get("enabled", True)}
                if role == "chat":
                    ids = payload.get("ids", [])
                    if not isinstance(ids, list):
                        await _send_json_resp(send, 400, {"error": "ids 必须是数组"})
                        return
                    ids = [str(x).strip() for x in ids if str(x).strip()]
                    # 校验引用的模型 ID 是否真实存在且已启用
                    bad = [i for i in ids if i not in enabled_ids]
                    if bad:
                        await _send_json_resp(send, 400, {"error": f"以下模型不存在或未启用: {', '.join(bad)}"})
                        return
                    if not ids:
                        await _send_json_resp(send, 400, {"error": "至少需要一个可用的聊天模型"})
                        return
                    roles["chat"] = ids
                    if roles.get("chat_default") not in ids:
                        roles["chat_default"] = ids[0]
                else:
                    # compression / background：多选（端点轮询池）；兼容旧的单选 id
                    ids = payload.get("ids", None)
                    if ids is None:
                        mid = str(payload.get("id", "")).strip()
                        ids = [mid] if mid else []
                    if not isinstance(ids, list):
                        await _send_json_resp(send, 400, {"error": "ids 必须是数组"})
                        return
                    ids = [str(x).strip() for x in ids if str(x).strip()]
                    bad = [i for i in ids if i not in enabled_ids]
                    if bad:
                        await _send_json_resp(send, 400, {"error": f"以下模型不存在或未启用: {', '.join(bad)}"})
                        return
                    # 允许空（清空该角色池，回退到默认聊天模型）
                    roles[role] = ids
                reg["roles"] = roles
                reg["default"] = roles.get("chat_default") or reg.get("default", "")
                ok = _save_llm_registry(reg)
                await _send_json_resp(send, 200 if ok else 500, {"ok": ok, "roles": roles})
                return

            # 旧 llm_settings → 新注册表幂等迁移
            if action == "migrate_llm_settings":
                reg = _load_llm_registry()
                new_reg, migrated, reason = _migrate_llm_settings_to_registry(reg)
                if migrated:
                    ok = _save_llm_registry(new_reg)
                    await _send_json_resp(send, 200 if ok else 500, {"ok": ok, "migrated": True, "reason": reason})
                else:
                    await _send_json_resp(send, 200, {"ok": True, "migrated": False, "reason": reason})
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
            # extra_keys：同一 base_url 下的额外密钥列表（每行一个 key）
            raw_ek = payload.get("extra_keys")
            if raw_ek is not None:
                if isinstance(raw_ek, list):
                    extra_keys = [str(k).strip() for k in raw_ek if str(k).strip()]
                elif isinstance(raw_ek, str):
                    # 前端可能用换行分隔的文本框传
                    extra_keys = [k.strip() for k in raw_ek.splitlines() if k.strip()]
                else:
                    extra_keys = []
            else:
                extra_keys = (existing.get("extra_keys", []) if existing else [])
            entry = {
                "id": mid,
                "label": str(payload.get("label", "")).strip() or (existing.get("label") if existing else mid),
                "base_url": base_url or (existing.get("base_url") if existing else ""),
                "model": real_model,
                "enabled": bool(payload.get("enabled", existing.get("enabled", True) if existing else True)),
                "thinking": _normalize_thinking(payload.get("thinking", existing.get("thinking", "auto") if existing else "auto")),
                "extra_keys": extra_keys,
            }
            # 禁用模型时检查角色占用：禁止禁用仍被角色使用的模型
            if existing and existing.get("enabled", True) and not entry["enabled"]:
                used = _model_role_usage(reg, mid)
                if used:
                    await _send_json_resp(send, 409, {
                        "error": f"模型 {mid} 仍被角色 {', '.join(used)} 使用，请先重新分配角色再禁用",
                        "used_by": used,
                    })
                    return
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
                "saved": {**entry, "api_key": _mask_key(entry["api_key"]),
                          "extra_keys_masked": [_mask_key(k) for k in entry.get("extra_keys", [])]},
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
        """返回移动端网关管理 Mini App。

        管理功能走网关 /api/*；状态面板按需直连 Supabase。
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

        数据源：desire_state 表里的 desire_* 键（desire_bridge 心跳每拍写库）。
        ⚠️ v3.8 起情感/欲望运行态已从 user_facts 迁出到独立的 desire_state 表
        （kv 结构 + updated_at），此处必须从 desire_state 读，否则全部返回 null。
        纯只读：不推进引擎、不消费事件、不写库，可放心高频轮询。

        返回结构：
          {
            "ok": true,
            "source": "desire_state",
            "updated_at": <drive_state.snapshot_at 或 null>,
            "emotion": <desire_emotion_state 原样 JSON>,
            "drive_state": <desire_drive_state 原样 JSON>,
            "refractory": {...},
            "last_action": str,
            "action_repeat": int,
            "next_heartbeat_at": float(ms) | null,
            "attachment_baseline": float,
            "events_queue_len": int,
            "config": {emotion_enabled, desire_driven, sources},  // 控制台开关状态
            "last_updated": str | null,  // desire_state.updated_at 最大值
            "derived": { ... },
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
                r = sb.table("desire_state").select("value, updated_at").eq("key", key).maybe_single().execute()
                if r and r.data and r.data.get("value"):
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

        # 取 desire_state.updated_at 的最大值作为"最后更新时间"（判断数据是否陈旧）
        last_updated = None
        try:
            lu = sb.table("desire_state").select("updated_at").order("updated_at", desc=True).limit(1).execute()
            if lu and lu.data:
                last_updated = lu.data[0].get("updated_at")
        except Exception:
            pass

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
            "source": "desire_state",
            "updated_at": updated_at,
            "emotion": emotion,
            "drive_state": drive_state,
            "refractory": refractory,
            "last_action": last_action,
            "action_repeat": action_repeat,
            "next_heartbeat_at": next_hb,
            "attachment_baseline": att_baseline,
            "events_queue_len": len(queue),
            "last_updated": last_updated,
            "config": {
                "emotion_enabled": _emotion_enabled(),
                "desire_driven": _desire_driven_enabled(),
                "emotion_source": _config_source_of("emotion_enabled"),
                "desire_driven_source": _config_source_of("desire_driven"),
            },
            "derived": derived,
        })

    # ------------------------------------------
    # 📱 情绪 / 欲望 Mini App 页面（/emotion）
    # ------------------------------------------

    # ------------------------------------------
    # 🖥️ 桌面控制台页面 /console
    # ------------------------------------------
    async def _handle_console_page(self, send):
        """返回电脑端网关管理控制台（同目录 console.html）。
        浏览器 → gateway 管理 API → Supabase；API_SECRET 存浏览器 localStorage。
        """
        try:
            import os as _os
            _here = _os.path.dirname(_os.path.abspath(__file__))
            with open(_os.path.join(_here, "console.html"), "r", encoding="utf-8") as f:
                html = f.read()
            body = html.encode("utf-8")
            status = 200
        except Exception as e:
            body = f"console.html 未找到: {e}".encode("utf-8")
            status = 500
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                (b"access-control-allow-origin", b"*")]})
        await send({"type": "http.response.body", "body": body})

    # ------------------------------------------
    # ⚙️ 管理配置 /api/admin/config  (GET / PATCH)
    # ------------------------------------------
    async def _handle_admin_config(self, scope, receive, send):
        """读取/更新运行时开关（telegram_enabled / qq_enabled / emotion_enabled /
        desire_driven / chat_history_write_enabled / vector_memory_injection_enabled）。

        GET  → 返回所有开关当前生效值 + 来源（database/env/default）+ 是否热生效
        PATCH → 仅接受白名单字段，写回 user_facts.sys_config，刷新缓存
        """
        method = scope["method"]
        if method == "GET":
            cfg = _get_runtime_config()
            out = {}
            for k in cfg:
                out[k] = {
                    "value": cfg[k],
                    "source": _config_source_of(k),
                    "hot_effective": True,  # 所有开关均为运行时读取，5s 内热生效
                    "needs_restart": False,
                }
            out["_raw"] = _load_sys_config_raw()
            out["_schema_version"] = 1
            await _send_json_resp(send, 200, {"ok": True, "config": out})
            return

        if method == "PATCH":
            body = b""
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body", False):
                    break
            try:
                patch = json.loads(body.decode("utf-8"))
            except Exception:
                await _send_json_resp(send, 400, {"error": "Invalid JSON body"})
                return
            if not isinstance(patch, dict):
                await _send_json_resp(send, 400, {"error": "body 必须是 JSON 对象"})
                return

            allowed = {"telegram_enabled", "qq_enabled", "emotion_enabled",
                       "desire_driven", "chat_history_write_enabled",
                       "vector_memory_injection_enabled", "device_context_enabled",
                       "money_earning_enabled"}
            bad = [k for k in patch if k not in allowed]
            if bad:
                await _send_json_resp(send, 400, {"error": f"不允许的字段: {', '.join(bad)}", "allowed": sorted(allowed)})
                return

            normalized = {}
            for k, v in patch.items():
                if isinstance(v, bool):
                    normalized[k] = v
                elif isinstance(v, str):
                    normalized[k] = v.strip().lower() in ("1", "true", "yes", "on")
                else:
                    await _send_json_resp(send, 400, {"error": f"{k} 必须是布尔值"})
                    return

            sb = _get_supabase()
            if not sb:
                await _send_json_resp(send, 500, {"error": "网关未配置 Supabase，无法持久化配置"})
                return
            cur = _load_sys_config_raw()
            cur.update(normalized)
            try:
                sb.table("user_facts").upsert({
                    "key": _SYS_CONFIG_KEY,
                    "value": json.dumps(cur, ensure_ascii=False),
                }).execute()
            except Exception as e:
                await _send_json_resp(send, 500, {"error": f"写入 sys_config 失败: {e}"})
                return
            _invalidate_runtime_config()
            cfg = _get_runtime_config()
            await _send_json_resp(send, 200, {"ok": True, "updated": normalized, "config": cfg})
            return

        await _send_json_resp(send, 405, {"error": f"Method {method} not allowed"})

    # ------------------------------------------
    # 📊 系统状态 /api/admin/status (GET)
    # ------------------------------------------
    async def _handle_admin_status(self, send):
        """返回系统运行状态：TG/QQ 配置 + 启用 + 连接状态、Supabase、模型角色、最近日志。"""
        cfg = _get_runtime_config()
        tg = {
            "enabled": cfg.get("telegram_enabled", True),
            "enabled_source": _config_source_of("telegram_enabled"),
            "token_configured": bool(os.environ.get("TG_BOT_TOKEN", "").strip()),
            "chat_id_configured": bool(os.environ.get("TG_CHAT_ID", "").strip()),
        }
        qq = {"enabled": cfg.get("qq_enabled", True), "enabled_source": _config_source_of("qq_enabled")}
        try:
            import napcat as _nc
            qq["connected"] = bool(getattr(_nc, "_napcat_connected", False))
            qq["status_message"] = getattr(_nc, "_napcat_status_message", "未知")
            qq["last_connected_at"] = getattr(_nc, "_napcat_last_connected_at", None)
            qq["ws_url_configured"] = bool(os.environ.get("NAPCAT_WS_URL", "").strip())
            qq["bot_qq_configured"] = bool(os.environ.get("NAPCAT_BOT_QQ", "").strip())
            try:
                qq["napcat_status"] = _nc.get_napcat_status()
            except Exception:
                pass
        except Exception as e:
            qq["error"] = str(e)

        roles_status = {}
        try:
            for role in _LLM_ROLES:
                r = resolve_llm_role(role)
                roles_status[role] = {
                    "model": r["model"],
                    "base_url": r["base_url"][:40] + "…" if len(r["base_url"]) > 40 else r["base_url"],
                    "registry_id": r["registry_id"],
                    "source": r["source"],
                    "fallback": r["fallback"],
                    "enabled": r["enabled"],
                    "has_key": bool(r["api_key"]),
                }
                # 多端点池 + 每个端点的健康状态（冷却/失败计数）
                # 按 ep_key（模型#key序号）粒度跟踪健康：单个 key 挂了只冷却那一个
                pool = resolve_llm_pool(role)
                ep_list = []
                for ep in pool:
                    ek = ep.get("ep_key", "")
                    h = _ep_health(ek)
                    ep_list.append({
                        "model": ep.get("model", ""),
                        "registry_id": ep.get("registry_id", ""),
                        "label": ep.get("label", ""),
                        "ep_index": ep.get("ep_index", 1),  # 该模型内的 key 序号（1 起）
                        "ep_key": ek,                       # 健康跟踪键
                        "source": ep.get("source", ""),
                        "fallback": ep.get("fallback", False),
                        "fails": h["fails"],
                        "cooldown_remaining": max(0, int(h["cooldown_until"] - time.time())),
                        "down": _ep_is_down(ek),
                    })
                roles_status[role]["pool"] = ep_list
                roles_status[role]["pool_size"] = len(ep_list)
        except Exception as e:
            roles_status["error"] = str(e)

        sb_status = {
            "configured": bool(os.environ.get("SUPABASE_URL", "").strip() and os.environ.get("SUPABASE_KEY", "").strip()),
            "url": os.environ.get("SUPABASE_URL", "")[:40] + "…" if len(os.environ.get("SUPABASE_URL", "")) > 40 else os.environ.get("SUPABASE_URL", ""),
        }

        import os as _os
        process = {
            "role": _os.environ.get("GATEWAY_ROLE", "single"),
            "chat_write_enabled": cfg.get("chat_history_write_enabled", True),
            "vector_injection_enabled": cfg.get("vector_memory_injection_enabled", True),
            "emotion_enabled": cfg.get("emotion_enabled", True),
            "desire_driven": cfg.get("desire_driven", False),
            "money_earning_enabled": cfg.get("money_earning_enabled", True),
        }
        recent_logs = _system_logs_buffer[-30:]

        await _send_json_resp(send, 200, {
            "ok": True,
            "telegram": tg,
            "qq": qq,
            "supabase": sb_status,
            "model_roles": roles_status,
            "process": process,
            "config_sources": {k: _config_source_of(k) for k in (
                "telegram_enabled", "qq_enabled", "emotion_enabled", "desire_driven",
                "chat_history_write_enabled", "vector_memory_injection_enabled",
                "device_context_enabled", "money_earning_enabled")},
            "recent_logs": recent_logs,
        })

    # ------------------------------------------
    # 🧪 模型连接测试 /api/models/test (POST)
    # ------------------------------------------
    async def _handle_models_test(self, scope, receive, send):
        """测试一个模型的连通性。body: {id?, base_url, api_key, model}
        若传 id 则用注册表里已存的配置（api_key 留空=沿用旧值）；否则用传入的临时配置。
        返回 {ok, http_status, latency_ms, model, error?}。
        🛡 SSRF 防护：经 _ssrf_safe_post 校验目标主机并禁止重定向。
        """
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            await _send_json_resp(send, 400, {"error": "Invalid JSON body"})
            return

        mid = str(payload.get("id", "")).strip()
        base_url = str(payload.get("base_url", "")).strip()
        api_key = str(payload.get("api_key", "")).strip()
        model = str(payload.get("model", "")).strip()

        if mid:
            reg = _load_llm_registry()
            entry = next((m for m in reg.get("models", []) if m.get("id") == mid), None)
            if entry:
                base_url = base_url or str(entry.get("base_url", "")).strip()
                api_key = api_key or str(entry.get("api_key", "")).strip()
                model = model or str(entry.get("model", "")).strip() or mid
            else:
                await _send_json_resp(send, 404, {"error": f"未找到模型 id={mid}"})
                return
        if not api_key or not base_url or not model:
            await _send_json_resp(send, 400, {"error": "缺少 base_url / api_key / model"})
            return

        base = base_url.rstrip("/")
        if re.search(r"/chat/completions$", base):
            url = base
        elif re.search(r"/v\d+[a-zA-Z]*$", base):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"

        test_prompt = [{"role": "user", "content": "ping，请回复 pong"}]
        t0 = time.time()
        http_status = 0
        err = ""
        resp_body = ""
        try:
            def _do():
                r = _ssrf_safe_post(url, {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }, {"model": model, "messages": test_prompt, "max_tokens": 20, "stream": False}, 30)
                return r.status_code, r.text[:300]
            http_status, resp_body = await asyncio.to_thread(_do)
        except ValueError as e:
            err = str(e)[:300]   # SSRF 拦截
        except requests.exceptions.Timeout:
            err = "请求超时（30s）"
        except Exception as e:
            err = str(e)[:300]
        latency_ms = int((time.time() - t0) * 1000)

        await _send_json_resp(send, 200, {
            "ok": http_status == 200,
            "http_status": http_status,
            "latency_ms": latency_ms,
            "model": model,
            "url": url,
            "error": err or (resp_body if http_status != 200 else ""),
            "response_preview": (resp_body[:200] if http_status == 200 else ""),
        })

    # ------------------------------------------
    # 📚 记忆库 CRUD /api/memories
    # ------------------------------------------
    async def _handle_memories_api(self, scope, receive, send, mem_id=None):
        """服务端分页 + 分类查询 + 编辑 + 删除。
        GET /api/memories?category=core&page=1&size=20&q=关键词
        PATCH /api/memories/:id  {title,content,category,mood,tags,importance}
        DELETE /api/memories/:id
        """
        method = scope["method"]
        sb = _get_supabase()
        if not sb:
            await _send_json_resp(send, 200, {"ok": False, "error": "未配置 Supabase"})
            return

        if method == "GET" and not mem_id:
            qs = scope.get("query_string", b"").decode("utf-8")
            params = {}
            for part in qs.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    from urllib.parse import unquote
                    params[k] = unquote(v)
            category = params.get("category", "core").strip() or "core"
            page = max(1, int(params.get("page", "1") or "1"))
            size = min(100, max(1, int(params.get("size", "20") or "20")))
            q = params.get("q", "").strip()
            tag_filter = _category_tag_filter(category)

            def _query():
                tbl = sb.table("memories").select("id,title,content,category,mood,tags,importance,created_at")
                if tag_filter:
                    tbl = tbl.in_("tags", tag_filter)
                if q:
                    tbl = tbl.or_(f"title.ilike.%{q}%,content.ilike.%{q}%")
                tbl = tbl.order("created_at", desc=True).limit(size).offset((page - 1) * size)
                return tbl.execute()

            def _count():
                tbl = sb.table("memories").select("id", count="exact")
                if tag_filter:
                    tbl = tbl.in_("tags", tag_filter)
                if q:
                    tbl = tbl.or_(f"title.ilike.%{q}%,content.ilike.%{q}%")
                return tbl.execute()

            try:
                res = await asyncio.to_thread(_query)
                cnt = await asyncio.to_thread(_count)
            except Exception as e:
                await _send_json_resp(send, 500, {"error": f"查询失败: {e}"})
                return
            rows = res.data or []
            if tag_filter is None:
                rows = [r for r in rows if _memory_category(r.get("tags", ""), r.get("content", "")) == category]
            total = getattr(cnt, "count", len(rows)) if cnt else len(rows)
            await _send_json_resp(send, 200, {
                "ok": True, "items": rows, "total": total,
                "page": page, "size": size, "category": category,
                "has_more": (page * size) < total,
            })
            return

        if method == "PATCH" and mem_id:
            body = b""
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body", False):
                    break
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                await _send_json_resp(send, 400, {"error": "Invalid JSON body"})
                return
            allowed = {"title", "content", "category", "mood", "tags", "importance"}
            patch = {}
            for k in allowed:
                if k in payload:
                    if k == "importance":
                        try:
                            iv = int(payload[k])
                        except (TypeError, ValueError):
                            await _send_json_resp(send, 400, {"error": "importance 必须是整数"})
                            return
                        if iv < 0 or iv > 10:
                            await _send_json_resp(send, 400, {"error": "importance 范围 0-10"})
                            return
                        patch[k] = iv
                    else:
                        patch[k] = str(payload[k])[:5000]
            if not patch:
                await _send_json_resp(send, 400, {"error": "无有效更新字段"})
                return
            try:
                res = await asyncio.to_thread(lambda: sb.table("memories").update(patch).eq("id", int(mem_id)).execute())
                if res and res.data:
                    await _send_json_resp(send, 200, {"ok": True, "updated": res.data[0]})
                else:
                    await _send_json_resp(send, 404, {"error": f"未找到 id={mem_id}"})
            except (TypeError, ValueError):
                await _send_json_resp(send, 400, {"error": "id 必须是整数"})
            except Exception as e:
                await _send_json_resp(send, 500, {"error": f"更新失败: {e}"})
            return

        if method == "DELETE" and mem_id:
            try:
                chk = await asyncio.to_thread(lambda: sb.table("memories").select("id,title,tags,created_at").eq("id", int(mem_id)).maybe_single().execute())
                if not (chk and chk.data):
                    await _send_json_resp(send, 404, {"error": f"未找到 id={mem_id}"})
                    return
                summary = chk.data
                await asyncio.to_thread(lambda: sb.table("memories").delete().eq("id", int(mem_id)).execute())
                await _send_json_resp(send, 200, {"ok": True, "deleted": int(mem_id), "summary": summary})
            except (TypeError, ValueError):
                await _send_json_resp(send, 400, {"error": "id 必须是整数"})
            except Exception as e:
                await _send_json_resp(send, 500, {"error": f"删除失败: {e}"})
            return

        await _send_json_resp(send, 405, {"error": f"Method {method} not allowed"})

    # ------------------------------------------
    # 👤 用户画像 CRUD /api/profile
    # ------------------------------------------
    async def _handle_profile_api(self, scope, receive, send, key=None):
        """用户画像 user_facts 表的 CRUD。系统键过滤复用后端 _is_profile_key。

        GET /api/profile?q=关键词  → 列出画像（已过滤系统键），支持搜索
        POST /api/profile {key,value,confidence}  → 新增/覆盖（重复键明确提示覆盖）
        DELETE /api/profile/:key  → 按主键精确删除
        """
        method = scope["method"]
        sb = _get_supabase()
        if not sb:
            await _send_json_resp(send, 200, {"ok": False, "error": "未配置 Supabase"})
            return

        if method == "GET":
            qs = scope.get("query_string", b"").decode("utf-8")
            q = ""
            for part in qs.split("&"):
                if part.startswith("q="):
                    from urllib.parse import unquote
                    q = unquote(part[2:]).strip()
            try:
                res = await asyncio.to_thread(lambda: sb.table("user_facts").select("key,value,confidence").order("key").execute())
                rows = res.data or []
                items = [r for r in rows if _is_profile_key(r.get("key", ""))]
                if q:
                    ql = q.lower()
                    items = [r for r in items if ql in str(r.get("key", "")).lower() or ql in str(r.get("value", "")).lower()]
                await _send_json_resp(send, 200, {"ok": True, "items": items, "total": len(items)})
            except Exception as e:
                await _send_json_resp(send, 500, {"error": f"查询失败: {e}"})
            return

        if method == "POST":
            body = b""
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body", False):
                    break
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                await _send_json_resp(send, 400, {"error": "Invalid JSON body"})
                return
            pkey = str(payload.get("key", "")).strip()
            pval = str(payload.get("value", "")).strip()
            if not pkey:
                await _send_json_resp(send, 400, {"error": "缺少 key"})
                return
            if not _is_profile_key(pkey):
                await _send_json_resp(send, 400, {"error": f"'{pkey}' 是系统配置键，不能作为用户画像"})
                return
            conf = payload.get("confidence", 1.0)
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                await _send_json_resp(send, 400, {"error": "confidence 必须是数字"})
                return
            if conf < 0 or conf > 1:
                await _send_json_resp(send, 400, {"error": "confidence 范围 0-1"})
                return
            existed = False
            try:
                chk = await asyncio.to_thread(lambda: sb.table("user_facts").select("key").eq("key", pkey).maybe_single().execute())
                existed = bool(chk and chk.data)
            except Exception:
                pass
            try:
                await asyncio.to_thread(lambda: sb.table("user_facts").upsert({
                    "key": pkey, "value": pval[:10000], "confidence": conf,
                }).execute())
            except Exception as e:
                await _send_json_resp(send, 500, {"error": f"写入失败: {e}"})
                return
            await _send_json_resp(send, 200, {
                "ok": True, "key": pkey, "overwritten": existed,
                "message": ("已覆盖已有键" if existed else "新增成功"),
            })
            return

        if method == "DELETE" and key:
            try:
                from urllib.parse import unquote
                dkey = unquote(key)
                chk = await asyncio.to_thread(lambda: sb.table("user_facts").select("key,value").eq("key", dkey).maybe_single().execute())
                if not (chk and chk.data):
                    await _send_json_resp(send, 404, {"error": f"未找到 key={dkey}"})
                    return
                if not _is_profile_key(dkey):
                    await _send_json_resp(send, 400, {"error": f"'{dkey}' 是系统配置键，不允许删除"})
                    return
                summary = {"key": dkey, "value_preview": str(chk.data.get("value", ""))[:120]}
                await asyncio.to_thread(lambda: sb.table("user_facts").delete().eq("key", dkey).execute())
                await _send_json_resp(send, 200, {"ok": True, "deleted": dkey, "summary": summary})
            except Exception as e:
                await _send_json_resp(send, 500, {"error": f"删除失败: {e}"})
            return

        await _send_json_resp(send, 405, {"error": f"Method {method} not allowed"})

    # ------------------------------------------
    # 🐱 Tick 日志查询 /api/ticks
    # ------------------------------------------
    async def _handle_ticks_api(self, scope, receive, send):
        """GET /api/ticks?page=1&size=20&event=hungry_cat  → 分页查询 tick 日志"""
        method = scope["method"]
        sb = _get_supabase()
        if not sb:
            await _send_json_resp(send, 200, {"ok": False, "error": "未配置 Supabase"})
            return
        if method != "GET":
            await _send_json_resp(send, 405, {"error": f"Method {method} not allowed"})
            return
        qs = scope.get("query_string", b"").decode("utf-8")
        params = {}
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                from urllib.parse import unquote
                params[k] = unquote(v)
        page = max(1, int(params.get("page", "1") or "1"))
        size = min(100, max(1, int(params.get("size", "20") or "20")))
        event = params.get("event", "").strip()

        def _query():
            tbl = sb.table("pet_tick_log").select(
                "id,user_id,pet_id,ticked_at,hours_elapsed,"
                "hunger_before,hunger_after,hunger_delta,"
                "happiness_before,happiness_after,happiness_delta,"
                "cleanliness_before,cleanliness_after,cleanliness_delta,"
                "energy_before,energy_after,energy_delta,"
                "status_before,status_after,threshold_event,skipped,skipped_reason"
            )
            if event:
                tbl = tbl.eq("threshold_event", event)
            tbl = tbl.order("ticked_at", desc=True).limit(size).offset((page - 1) * size)
            return tbl.execute()

        def _count():
            tbl = sb.table("pet_tick_log").select("id", count="exact")
            if event:
                tbl = tbl.eq("threshold_event", event)
            return tbl.execute()

        try:
            res = await asyncio.to_thread(_query)
            cnt = await asyncio.to_thread(_count)
        except Exception as e:
            await _send_json_resp(send, 500, {"error": f"查询失败: {e}"})
            return
        rows = res.data or []
        total = getattr(cnt, "count", len(rows)) if cnt else len(rows)
        await _send_json_resp(send, 200, {
            "ok": True, "items": rows, "total": total,
            "page": page, "size": size,
            "has_more": (page * size) < total,
        })

    async def _handle_wallet_api(self, scope, send, action: str, receive=None):
        """钱包后端代理 API。所有写操作由后端固定 wallet_id/user_id/bypass_cap。
        前端不再直调 Supabase RPC。受 API_SECRET 保护。"""
        import home_system as _hs
        import datetime as _dt
        import uuid as _uuid

        async def _read_body():
            """读取 ASGI 请求体。"""
            body = b""
            if receive:
                while True:
                    msg = await receive()
                    body += msg.get("body", b"")
                    if not msg.get("more_body", False):
                        break
            return body

        async def _ok(data):
            await _send_json_resp(send, 200, data)

        async def _err(code, msg, status=400):
            await _send_json_resp(send, status, {"ok": False, "error_code": code, "message": msg})

        try:
            if action == "check":
                result = await asyncio.to_thread(_hs.wallet_check)
                await _ok(result)

            elif action == "log":
                # 解析分页参数
                query_params = scope.get("query_string", b"").decode("utf-8", errors="ignore")
                limit = 20
                offset = 0
                for pair in query_params.split("&"):
                    if pair.startswith("limit="):
                        try:
                            limit = max(1, min(100, int(pair.split("=")[1])))
                        except Exception:
                            pass
                    elif pair.startswith("offset="):
                        try:
                            offset = max(0, int(pair.split("=")[1]))
                        except Exception:
                            pass
                result = await asyncio.to_thread(_hs.wallet_log, limit, offset)
                await _ok(result)

            elif action in ("allowance", "tip", "spend"):
                # 读取 POST body
                body = await _read_body()
                try:
                    data = json.loads(body) if body else {}
                except Exception:
                    await _err("INVALID_JSON", "请求体不是合法 JSON")
                    return

                if action == "allowance":
                    amount = data.get("amount", 25)
                    try:
                        amount = float(amount)
                    except (TypeError, ValueError):
                        await _err("INVALID_AMOUNT", "金额无效")
                        return
                    if amount <= 0 or amount > 200:
                        await _err("INVALID_AMOUNT", "金额须为 0..200")
                        return
                    # 后端生成 source_key（按周幂等）
                    bj_now = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=8)
                    iso_week = bj_now.strftime("%G-W%V")
                    source_key = f"allowance_{iso_week}"
                    reason = f"每周零花钱 {iso_week}"
                    result = await asyncio.to_thread(
                        _hs.wallet_earn, _hs.DEFAULT_WALLET_ID, amount, source_key, reason, True
                    )
                    await _ok(result)

                elif action == "tip":
                    amount = data.get("amount", 10)
                    try:
                        amount = float(amount)
                    except (TypeError, ValueError):
                        await _err("INVALID_AMOUNT", "金额无效")
                        return
                    if amount <= 0 or amount > 200:
                        await _err("INVALID_AMOUNT", "金额须为 0..200")
                        return
                    reason = data.get("reason", "打赏")
                    if len(reason) > 200:
                        reason = reason[:200]
                    source_key = f"tip_{_uuid.uuid4().hex[:12]}"
                    result = await asyncio.to_thread(
                        _hs.wallet_earn, _hs.DEFAULT_WALLET_ID, amount, source_key, reason, True
                    )
                    await _ok(result)

                elif action == "spend":
                    amount = data.get("amount", 0)
                    try:
                        amount = float(amount)
                    except (TypeError, ValueError):
                        await _err("INVALID_AMOUNT", "金额无效")
                        return
                    if amount <= 0 or amount > 1000:
                        await _err("INVALID_AMOUNT", "金额须为 0..1000")
                        return
                    reason = data.get("reason", "支出")
                    if len(reason) > 200:
                        reason = reason[:200]
                    result = await asyncio.to_thread(
                        _hs.wallet_spend, _hs.DEFAULT_WALLET_ID, amount, reason
                    )
                    await _ok(result)

            else:
                await _err("UNKNOWN_ACTION", "未知操作", 404)

        except Exception as e:
            await _err("RPC_ERROR", "操作失败", 500)

    # ------------------------------------------
    # 🏠 C6: Home 聚合只读视图 /api/home/state
    # ------------------------------------------
    async def _handle_home_state_api(self, scope, send):
        """GET /api/home/state — Home 前端聚合只读视图。

        - 受 /api/* 全局 API_SECRET 鉴权；仅 GET；
        - 数据全部由后端读取（浏览器不接触 Supabase），home_service 层完成安全投影：
          不含内部 UUID、秘密日记正文、未拆信/便利贴全文与任何工具参数；
        - 单区块失败不伪造为空：该区块置 null 并在 data.errors 标记，其余区块照常返回。
        """
        if scope["method"] != "GET":
            await _send_json_resp(send, 405, {"ok": False, "error_code": "METHOD_NOT_ALLOWED",
                                              "message": "仅支持 GET"})
            return
        try:
            import home.service as _home_service
            result = await asyncio.to_thread(_home_service.home_state_overview)
        except Exception as e:
            _log(f"⚠️ [Home聚合] 接口异常: {type(e).__name__}")
            await _send_json_resp(send, 500, {"ok": False, "error_code": "INTERNAL_ERROR",
                                              "message": "服务暂时不可用"})
            return
        if not result.get("ok"):
            await _send_json_resp(send, 500, {"ok": False,
                                              "error_code": result.get("error_code") or "INTERNAL_ERROR",
                                              "message": result.get("message") or "服务暂时不可用"})
            return
        await _send_json_resp(send, 200, {"ok": True, "data": result.get("data", {})})

    # ------------------------------------------
    # 📋 C6: 行动日志分页查询 /api/activity-logs
    # ------------------------------------------
    async def _handle_activity_logs_api(self, scope, send):
        """GET /api/activity-logs — 行动日志分页查询（只读、白名单投影）。

        参数：page(≥1) size(1..100) source(all|unified_autonomy|free_activity|home_autonomy)
              status(all|running|succeeded|observed|partial|failed|skipped) activity_id(≤200字)
        """
        if scope["method"] != "GET":
            await _send_json_resp(send, 405, {"ok": False, "error_code": "METHOD_NOT_ALLOWED",
                                              "message": "仅支持 GET"})
            return
        qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
        params = {}
        from urllib.parse import unquote as _unquote
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = _unquote(v)
        try:
            page = int(params.get("page", "1"))
            size = int(params.get("size", "20"))
        except (TypeError, ValueError):
            await _send_json_resp(send, 400, {"ok": False, "error_code": "INVALID_REQUEST",
                                              "message": "page/size 须为整数"})
            return
        source = (params.get("source", "") or "").strip()
        status = (params.get("status", "") or "").strip()
        activity_id = (params.get("activity_id", "") or "").strip()
        if len(activity_id) > 200:
            await _send_json_resp(send, 400, {"ok": False, "error_code": "INVALID_REQUEST",
                                              "message": "activity_id 过长"})
            return
        try:
            import home.activity_log as _alog
            result = await asyncio.to_thread(
                _alog.query_activity_logs, page, size, source, status, activity_id)
        except Exception as e:
            _log(f"⚠️ [行动日志] 接口异常: {type(e).__name__}")
            await _send_json_resp(send, 500, {"ok": False, "error_code": "INTERNAL_ERROR",
                                              "message": "服务暂时不可用"})
            return
        if not result.get("ok"):
            code = result.get("error_code") or "INTERNAL_ERROR"
            if code.startswith("INVALID"):
                await _send_json_resp(send, 400, {"ok": False, "error_code": code,
                                                  "message": "请求参数无效"})
            else:
                await _send_json_resp(send, 500, {"ok": False, "error_code": code,
                                                  "message": "服务暂时不可用"})
            return
        await _send_json_resp(send, 200, {
            "ok": True, "items": result.get("items", []),
            "total": result.get("total", 0), "page": result.get("page", page),
            "size": result.get("size", size), "has_more": bool(result.get("has_more", False)),
        })

    # ------------------------------------------
    # 🔐 C6: 秘密日记统一索引 /api/secret-diaries
    # ------------------------------------------
    async def _handle_secret_diaries_api(self, scope, send):
        """GET /api/secret-diaries — 新旧秘密日记统一索引（仅元数据，绝不含正文）。"""
        if scope["method"] != "GET":
            await _send_json_resp(send, 405, {"ok": False, "error_code": "METHOD_NOT_ALLOWED",
                                              "message": "仅支持 GET"})
            return
        qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
        params = {}
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = v
        try:
            page = int(params.get("page", "1"))
            size = int(params.get("size", "20"))
        except (TypeError, ValueError):
            await _send_json_resp(send, 400, {"ok": False, "error_code": "INVALID_REQUEST",
                                              "message": "page/size 须为整数"})
            return
        page = max(1, page)
        size = min(100, max(1, size))
        offset = (page - 1) * size
        try:
            import home.service as _home_service
            result = await asyncio.to_thread(
                _home_service.list_private_diary_index, size, offset, True)
        except Exception as e:
            _log(f"⚠️ [秘密日记索引] 接口异常: {type(e).__name__}")
            await _send_json_resp(send, 500, {"ok": False, "error_code": "INTERNAL_ERROR",
                                              "message": "服务暂时不可用"})
            return
        if not result.get("ok"):
            await _send_json_resp(send, 500, {"ok": False,
                                              "error_code": result.get("error_code") or "INTERNAL_ERROR",
                                              "message": "服务暂时不可用"})
            return
        data = result.get("data", {})
        await _send_json_resp(send, 200, {
            "ok": True, "items": data.get("items", []),
            "total": data.get("total", 0), "page": page, "size": size,
            "has_more": bool(data.get("has_more", False)),
        })

    # ------------------------------------------
    # 🔓 C6: 秘密日记受保护正文读取 /api/secret-diaries/:reference
    # ------------------------------------------
    async def _handle_secret_diary_body_api(self, scope, send, reference: str = ""):
        """GET /api/secret-diaries/:reference — 秘密日记正文受保护读取。

        - reference 前端经 encodeURIComponent 编码，此处 unquote 还原；
        - 仅此受保护接口返回正文；非法格式 400；不存在 404（不区分"存在但禁止"）；
          数据库异常 500（不回显异常原文）；
        - 日志只记录异常类型，不记录完整 reference/title/正文。
        """
        if scope["method"] != "GET":
            await _send_json_resp(send, 405, {"ok": False, "error_code": "METHOD_NOT_ALLOWED",
                                              "message": "仅支持 GET"})
            return
        from urllib.parse import unquote as _unquote
        ref = _unquote(reference or "").strip()
        if not ref or len(ref) > 300:
            await _send_json_resp(send, 400, {"ok": False, "error_code": "INVALID_REFERENCE",
                                              "message": "reference 非法"})
            return
        try:
            import home.service as _home_service
            result = await asyncio.to_thread(_home_service.read_private_diary_body, ref)
        except Exception as e:
            _log(f"⚠️ [秘密日记正文] 接口异常: {type(e).__name__}")
            await _send_json_resp(send, 500, {"ok": False, "error_code": "INTERNAL_ERROR",
                                              "message": "服务暂时不可用"})
            return
        if not result.get("ok"):
            code = result.get("error_code") or "INTERNAL_ERROR"
            if code == "INVALID_REFERENCE":
                status_code, message = 400, "reference 非法"
            elif code == "NOT_FOUND_OR_FORBIDDEN":
                status_code, message = 404, "日记不存在"
            else:
                status_code, message = 500, "服务暂时不可用"
            await _send_json_resp(send, status_code, {"ok": False, "error_code": code, "message": message})
            return
        await _send_json_resp(send, 200, {"ok": True, "item": result.get("data", {})})

    # ------------------------------------------
    # ✉️ C9: 信件拆阅受保护入口 /api/home/letters/:letter_key(/open)
    # ------------------------------------------
    async def _handle_letter_open_api(self, scope, send, raw_key: str = ""):
        """POST /api/home/letters/:letter_key/open — 拆信（有副作用）。

        - action_key 由后端 service 层生成，前端/模型不参与；
        - 已拆信（含已拆后归档）不重复执行状态转换，改走零副作用只读返回
          already_opened=true；归档且从未拆开的信 409 LETTER_ARCHIVED；
          不存在 404（不区分"存在但禁止"）；
        - 响应只含 letter_key/title/content/status/created_at/opened_at，
          不含 action_key/event_id/内部 UUID/数据库原始 result；
        - 日志只记录异常类型，不记录完整 letter_key/标题/正文。
        """
        if scope["method"] != "POST":
            await _send_json_resp(send, 405, {"ok": False, "error_code": "METHOD_NOT_ALLOWED",
                                              "message": "仅支持 POST"})
            return
        key = _normalize_letter_key(raw_key)
        if key is None:
            await _send_json_resp(send, 400, {"ok": False, "error_code": "INVALID_LETTER_KEY",
                                              "message": "letter_key 非法"})
            return
        try:
            import home.service as _home_service
            result = await asyncio.to_thread(_home_service.request_open_letter, key)
        except Exception as e:
            _log(f"⚠️ [拆信] 接口异常: {type(e).__name__}")
            await _send_json_resp(send, 500, {"ok": False, "error_code": "INTERNAL_ERROR",
                                              "message": "服务暂时不可用"})
            return
        if not result.get("ok"):
            code = result.get("error_code") or "INTERNAL_ERROR"
            if code == "NOT_FOUND_OR_FORBIDDEN":
                status_code, message = 404, "信件不存在或无法访问"
            elif code == "LETTER_ARCHIVED":
                status_code, message = 409, "这封信已归档"
            elif code == "LETTER_UNOPENED":
                status_code, message = 409, "这封信还没有拆开"
            else:
                status_code, message = 500, "服务暂时不可用"
            await _send_json_resp(send, status_code, {"ok": False, "error_code": code, "message": message})
            return
        data = result.get("data") or {}
        await _send_json_resp(send, 200, {"ok": True,
                                          "already_opened": bool(data.get("already_opened")),
                                          "item": data.get("item") or {}})

    async def _handle_letter_read_api(self, scope, send, raw_key: str = ""):
        """GET /api/home/letters/:letter_key — 已拆信再次阅读（零副作用）。

        - service_role 直接 SELECT，不调用拆信 RPC，不写 home_action_runs/home_events，
          多次读取均成功且不改变任何状态；
        - unopened（含归档未拆）409 LETTER_UNOPENED，不返回正文、不自动拆信；
        - 响应投影与拆信接口一致；日志只记录异常类型，不记录 key/正文。
        """
        if scope["method"] != "GET":
            await _send_json_resp(send, 405, {"ok": False, "error_code": "METHOD_NOT_ALLOWED",
                                              "message": "仅支持 GET"})
            return
        key = _normalize_letter_key(raw_key)
        if key is None:
            await _send_json_resp(send, 400, {"ok": False, "error_code": "INVALID_LETTER_KEY",
                                              "message": "letter_key 非法"})
            return
        try:
            import home.service as _home_service
            result = await asyncio.to_thread(_home_service.read_opened_letter, key)
        except Exception as e:
            _log(f"⚠️ [信件阅读] 接口异常: {type(e).__name__}")
            await _send_json_resp(send, 500, {"ok": False, "error_code": "INTERNAL_ERROR",
                                              "message": "服务暂时不可用"})
            return
        if not result.get("ok"):
            code = result.get("error_code") or "INTERNAL_ERROR"
            if code == "NOT_FOUND_OR_FORBIDDEN":
                status_code, message = 404, "信件不存在或无法访问"
            elif code == "LETTER_UNOPENED":
                status_code, message = 409, "这封信还没有拆开"
            else:
                status_code, message = 500, "服务暂时不可用"
            await _send_json_resp(send, status_code, {"ok": False, "error_code": code, "message": message})
            return
        await _send_json_resp(send, 200, {"ok": True, "item": result.get("data") or {}})

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

def _normalize_letter_key(raw_key: str):
    """C9：校验并规范化 letter_key 路径参数。合法返回 key 字符串，非法返回 None。

    - unquote 还原前端 encodeURIComponent 编码；拒绝空值/超长(>200)/路径分隔符/
      控制字符/%残留（畸形编码如 %ZZ 经 unquote 后仍含 %，一并拒绝）；
    - 值只进入 Supabase 查询构造器参数绑定，不拼 SQL；
    - 校验先行：非法 key 在任何数据库调用之前就被拒绝。
    """
    from urllib.parse import unquote as _unquote
    try:
        key = _unquote(raw_key or "")
    except Exception:
        return None
    key = key.strip()
    if not key or len(key) > 200:
        return None
    if "/" in key or "\\" in key or "%" in key:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in key):
        return None
    return key


async def _check_api_secret(scope, send):
    """校验 API_SECRET。返回 True=通过，False=已拒绝(已发送 401)"""
    api_secret = os.environ.get("API_SECRET", "").strip()
    if not api_secret:
        # Phase 5.1 安全修复：API_SECRET 为空时拒绝受保护入口，不再放行
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"application/json"), (b"access-control-allow-origin", b"*")]})
        await send({"type": "http.response.body", "body": b'{"error":"Service unavailable: API_SECRET not configured"}'})
        return False
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


def _check_ssrf(url: str) -> str:
    """SSRF 防护：校验一个即将被服务端请求的 URL 是否安全。
    返回错误字符串（不安全）或空字符串（通过）。

    规则：
      - 仅允许 http/https 协议
      - 解析主机名 → IP，阻断私网/环回/链路本地/保留地址
      - 阻断已知云元数据端点（AWS/GCP/阿里云 ECS）
    用于 /api/models/test 测试模型连接前的目标校验。
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
    except Exception:
        return "URL 解析失败"
    if p.scheme not in ("http", "https"):
        return f"不允许的协议: {p.scheme}"
    host = (p.hostname or "").lower()
    if not host:
        return "缺少主机名"
    # 已知云元数据端点
    meta_hosts = {"metadata", "metadata.google.internal", "metadata.aws.internal",
                  "169.254.169.254", "169.254.170.2", "100.100.100.200"}
    if host in meta_hosts:
        return "云元数据端点禁止访问"
    # 如果 host 本身就是 IP 字面量，直接校验
    try:
        ipo = ipaddress.ip_address(host)
        if ipo.is_private or ipo.is_loopback or ipo.is_link_local or ipo.is_reserved:
            return f"目标 IP {host} 属于内网/环回/链路本地，禁止访问"
    except ValueError:
        pass
    # 解析主机名到 IP，逐个阻断内网地址
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return "主机名解析失败"
    ips = set()
    for info in infos:
        try:
            ips.add(info[4][0])
        except Exception:
            continue
    for ip in ips:
        try:
            ipo = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if ipo.is_private or ipo.is_loopback or ipo.is_link_local or ipo.is_reserved:
            return f"目标主机 {host} 解析到内网地址 {ip}，禁止访问"
    return ""


def _ssrf_safe_post(url: str, headers: dict, json_body: dict, timeout: float = 30.0):
    """SSRF 安全的 POST：先 _check_ssrf 校验，通过后发起请求并禁止重定向。
    抛出 ValueError 表示被 SSRF 防护拦截；其余异常由调用方处理。
    """
    err = _check_ssrf(url)
    if err:
        raise ValueError(err)
    # allow_redirects=False：防止通过 3xx 重定向绕过 SSRF 防护访问内网
    return requests.post(url, headers=headers, json=json_body, timeout=timeout, allow_redirects=False)

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
