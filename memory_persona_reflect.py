# -*- coding: utf-8 -*-
"""阶段 D2b —— AI 人格反思周更（只增不减 + 长度保护）。

每周日在 D2 画像反思之后触发：读取 sys_ai_persona（fallback AI_PERSONA）
+ 近 7 天关于 AI 自己的 active 记忆，用 compression 角色在原人设基础上
最多新增 5 句；复用 D2 的 validate_profile_growth。

门控 PERSONA_REFLECT_ENABLED（默认 true）。日志只打计数，不写人设/记忆正文。
不修改环境变量 AI_PERSONA；反思结果只写入 user_facts.sys_ai_persona。
"""

from __future__ import annotations

import asyncio
import datetime
import os
import re

from memory_profile_reflect import (
    MAX_NEW_SENTENCES,
    count_new_sentences,
    split_paragraphs,
    validate_profile_growth,
)

PERSONA_KEY = "sys_ai_persona"
CREATED_BY = "persona_reflect"


def persona_reflect_enabled() -> bool:
    return os.environ.get("PERSONA_REFLECT_ENABLED", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def _log(msg: str) -> None:
    print(f"[memory_persona_reflect] {msg}")


def _strip_fences(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    return text


async def fetch_current_persona(supabase_service) -> str:
    """读取当前 AI 人设。

    优先 user_facts.sys_ai_persona；fallback 到环境变量 AI_PERSONA；
    都没有返回空串。失败返回空串，不抛异常。
    """
    if supabase_service is not None:
        try:
            res = await asyncio.to_thread(
                lambda: supabase_service.table("user_facts")
                .select("key, value")
                .eq("key", PERSONA_KEY)
                .execute())
            rows = getattr(res, "data", None) or []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                v = r.get("value")
                if isinstance(v, str) and v.strip():
                    return v.strip()
        except Exception as e:  # noqa: BLE001
            _log(f"读取人设失败: {type(e).__name__}")
    env_v = os.environ.get("AI_PERSONA", "")
    if isinstance(env_v, str) and env_v.strip():
        return env_v.strip()
    return ""


async def fetch_persona_memories(supabase_service, user_id: str,
                                 days: int = 7) -> list[str]:
    """读取近 N 天关于 AI 自己的 active 记忆。

    查询条件：status='active' + created_at >= days 天前；
    memory_type IN ('moment', 'shared_experience')，另可选含「我」的 current；
    排除 source='private_diary'；
    按 importance DESC, created_at DESC 排序，limit 20。
    返回 content 列表。失败返回 []。
    """
    if supabase_service is None or not user_id:
        return []
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=days)).isoformat()
    try:
        # 多取一些再在 Python 侧过滤 current，保证最终 ≤20
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items")
            .select("content,memory_type,status,source,importance,created_at")
            .eq("user_id", user_id)
            .eq("status", "active")
            .in_("memory_type", ["moment", "shared_experience", "current"])
            .gte("created_at", since)
            .neq("source", "private_diary")
            .order("importance", desc=True)
            .order("created_at", desc=True)
            .limit(40)
            .execute())
        rows = getattr(res, "data", None) or []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            mt = r.get("memory_type")
            c = str(r.get("content") or "").strip()
            if not c:
                continue
            if mt in ("moment", "shared_experience"):
                out.append(c[:300])
            elif mt == "current" and "我" in c:
                out.append(c[:300])
            if len(out) >= 20:
                break
        return out
    except Exception as e:  # noqa: BLE001
        _log(f"读取人格记忆失败: {type(e).__name__}")
        return []


def build_persona_reflect_prompt(old_persona: str, memories: list[str],
                                 ai_name: str, user_name: str) -> str:
    """构造反思 Prompt。

    要求模型：在原人设基础上补充新的自我认知；
    不得删除、不得缩写、不得合并已有段落；
    每次最多新增 5 句；用第一人称（我 = ai_name）；
    不得改变人设的核心性格定义，只能补充新的经历感悟。
    """
    persona_block = (old_persona or "").strip() or "（暂无人设）"
    mem_block = ("\n".join(f"- {m}" for m in memories[:40])
                 if memories else "（近 7 日无相关记忆）")
    name = (ai_name or "AI").strip() or "AI"
    uname = (user_name or "用户").strip() or "用户"
    return (
        f"你是【{name}】的自我认知维护器。请基于近 7 天经历，"
        f"在【现有人设】原文基础上只做增补。\n"
        f"硬性规则：\n"
        f"1. 不得删除、不得缩写、不得合并已有段落；已有每一段必须原样保留；\n"
        f"2. 每次最多新增 {MAX_NEW_SENTENCES} 句新的自我认知/经历感悟；\n"
        f"3. 用第一人称书写（我 = {name}；对方 = {uname}）；\n"
        f"4. 不得改变人设的核心性格定义，只能补充新的经历感悟；\n"
        f"5. 新内容必须有经历依据；没有新认知时原样返回现有人设；\n"
        f"6. 只输出完整新人设纯文本，不要 Markdown，不要 JSON，不要解释。\n\n"
        f"【现有人设 · {name}】\n{persona_block}\n\n"
        f"【近 7 天关于我的 active 记忆（moment/shared_experience/含我的 current）】\n"
        f"{mem_block}\n"
    )


async def upsert_persona(supabase_service, new_persona: str) -> str | None:
    """单 key upsert user_facts.sys_ai_persona。成功返回 None，失败返回 error_code。"""
    if supabase_service is None:
        return "SERVICE_UNAVAILABLE"
    value = (new_persona or "").strip()
    if not value:
        return "EMPTY_NEW_PROFILE"
    try:
        await asyncio.to_thread(
            lambda: supabase_service.table("user_facts")
            .upsert(
                {"key": PERSONA_KEY, "value": value, "confidence": 1.0},
                on_conflict="key",
            ).execute())
        return None
    except Exception as e:  # noqa: BLE001
        _log(f"upsert 失败: {type(e).__name__}")
        return "UPSERT_FAILED"


async def run_persona_reflect(supabase_service, llm_call, user_id: str,
                              ai_name: str, user_name: str) -> dict:
    """主入口。永不抛未处理异常。

    返回 {ok, error_code, old_len, new_len, new_sentences}（脱敏，不含正文）。
    """
    result = {
        "ok": False,
        "error_code": None,
        "old_len": 0,
        "new_len": 0,
        "new_sentences": 0,
    }
    try:
        if not persona_reflect_enabled():
            result["error_code"] = "DISABLED"
            return result

        old_persona = await fetch_current_persona(supabase_service)
        memories = await fetch_persona_memories(supabase_service, user_id, days=7)
        result["old_len"] = len(old_persona)

        if llm_call is None:
            result["error_code"] = "LLM_UNAVAILABLE"
            return result

        prompt = build_persona_reflect_prompt(
            old_persona, memories, ai_name, user_name)
        try:
            raw = await asyncio.to_thread(llm_call, prompt)
        except Exception as e:  # noqa: BLE001
            _log(f"LLM 失败: {type(e).__name__}")
            result["error_code"] = "LLM_ERROR"
            return result

        new_persona = _strip_fences(raw if isinstance(raw, str) else "")
        if not new_persona.strip():
            result["error_code"] = "EMPTY_RESPONSE"
            _log(f"空返回 old_len={result['old_len']}")
            return result

        check = validate_profile_growth(old_persona, new_persona)
        result["old_len"] = check["old_len"]
        result["new_len"] = check["new_len"]
        result["new_sentences"] = check["new_sentences"]
        _log(f"校验 ok={check['ok']} code={check['error_code']} "
             f"old_len={check['old_len']} new_len={check['new_len']} "
             f"new_sent={check['new_sentences']}")

        if not check["ok"]:
            result["error_code"] = check["error_code"]
            return result

        write_err = await upsert_persona(supabase_service, new_persona)
        if write_err:
            result["error_code"] = write_err
            return result

        result["ok"] = True
        result["error_code"] = None
        _log(f"完成 old_len={result['old_len']} new_len={result['new_len']} "
             f"new_sent={result['new_sentences']}")
        return result
    except Exception as e:  # noqa: BLE001
        _log(f"未预期失败: {type(e).__name__}")
        result["error_code"] = "UNEXPECTED"
        return result


# 供测试确认复用的符号仍可从本模块路径触及
__all__ = [
    "PERSONA_KEY",
    "persona_reflect_enabled",
    "fetch_current_persona",
    "fetch_persona_memories",
    "build_persona_reflect_prompt",
    "run_persona_reflect",
    "split_paragraphs",
    "count_new_sentences",
    "validate_profile_growth",
]
