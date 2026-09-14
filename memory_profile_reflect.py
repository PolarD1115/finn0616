# -*- coding: utf-8 -*-
"""阶段 D2 —— 画像反思周更（只增不减 + 长度保护）。

每周日深夜日记流程中触发一次：读取真画像 + 近 7 天 active 记忆，
用 compression 角色在原文基础上最多新增 5 句；任何删减/缩写/合并已有段落
或新长度 < 原长度 × 0.9 → 拒绝写入。

门控 PROFILE_REFLECT_ENABLED（默认 true）。日志只打计数，不写画像正文。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re

CREATED_BY = "profile_reflect"
MAX_NEW_SENTENCES = 5
LENGTH_KEEP_RATIO = 0.9

# 非真画像 key（与 gateway._SYSTEM_PROFILE_KEYS / desire 运行时态对齐）
_EXCLUDED_EXACT = frozenset({
    "sys_config", "llm_settings", "llm_models", "sys_ai_persona",
})


def profile_reflect_enabled() -> bool:
    return os.environ.get("PROFILE_REFLECT_ENABLED", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def _log(msg: str) -> None:
    print(f"[memory_profile_reflect] {msg}")


def _is_true_profile_key(key: str) -> bool:
    """真画像 key：排除系统配置与 desire 运行时状态。"""
    if not key or not isinstance(key, str):
        return False
    if key in _EXCLUDED_EXACT:
        return False
    if key.startswith("llm_"):
        return False
    if key.startswith("desire_"):
        # 带日期后缀的笔记放行（与 gateway._is_profile_key 一致）
        return bool(re.search(r"_\d{4}_\d{2}_\d{2}$", key))
    return True


def split_paragraphs(text: str) -> list[str]:
    """按空行 / 换行拆段落，去空白，保序去重。"""
    if not isinstance(text, str) or not text.strip():
        return []
    # 先按空行，再按单换行兜底
    chunks = re.split(r"\n\s*\n", text.strip())
    paras = []
    seen = set()
    for ch in chunks:
        for line in ch.split("\n"):
            p = line.strip()
            if not p:
                continue
            if p in seen:
                continue
            seen.add(p)
            paras.append(p)
    return paras


def count_new_sentences(old_text: str, new_text: str) -> int:
    """估算新增句数：新文本相对旧文本多出的句子数。"""
    def _sents(t: str) -> list[str]:
        parts = re.split(r"(?<=[。！？!?；;])\s*", (t or "").strip())
        return [p.strip() for p in parts if p and p.strip()]

    old_set = set(_sents(old_text))
    new_sents = _sents(new_text)
    added = [s for s in new_sents if s not in old_set]
    return len(added)


def validate_profile_growth(old_text: str, new_text: str) -> dict:
    """只增不减 + 长度保护 + 最多 5 句新增。

    返回 {ok, error_code, old_len, new_len, missing_paragraphs, new_sentences}。
    """
    old_text = old_text if isinstance(old_text, str) else ""
    new_text = new_text if isinstance(new_text, str) else ""
    old_len = len(old_text)
    new_len = len(new_text)
    old_paras = split_paragraphs(old_text)
    # 旧段落须作为完整子串保留（允许同一行后续追加新句）
    missing = [p for p in old_paras if p not in new_text]

    new_sent_n = count_new_sentences(old_text, new_text)
    result = {
        "ok": False,
        "error_code": None,
        "old_len": old_len,
        "new_len": new_len,
        "missing_paragraphs": len(missing),
        "new_sentences": new_sent_n,
    }
    if not new_text.strip():
        result["error_code"] = "EMPTY_NEW_PROFILE"
        return result
    if missing:
        result["error_code"] = "PARAGRAPH_REMOVED"
        return result
    if old_len > 0 and new_len < old_len * LENGTH_KEEP_RATIO:
        result["error_code"] = "LENGTH_REGRESSION"
        return result
    if new_sent_n > MAX_NEW_SENTENCES:
        result["error_code"] = "TOO_MANY_NEW_SENTENCES"
        return result
    if new_text.strip() == old_text.strip():
        result["error_code"] = "NO_CHANGE"
        return result
    result["ok"] = True
    return result


def build_reflect_prompt(profile_map: dict, memories: list[str],
                         user_name: str = "用户") -> str:
    """构造只增不减反思 Prompt（不含密钥/内部 ID）。"""
    lines = []
    for k in sorted(profile_map.keys()):
        v = str(profile_map.get(k) or "").strip()
        if v:
            lines.append(f"[{k}]\n{v}")
    profile_block = "\n\n".join(lines) if lines else "（暂无画像）"
    mem_block = "\n".join(f"- {m}" for m in memories[:40]) if memories else "（近 7 日无分层记忆）"
    return (
        f"你是用户画像维护器。请基于近 7 天经历，在【现有画像】原文基础上只做增补。\n"
        f"硬性规则：\n"
        f"1. 不得删除、不得缩写、不得合并已有段落；已有每一段必须原样保留；\n"
        f"2. 每次最多新增 {MAX_NEW_SENTENCES} 句新认知；\n"
        f"3. 新内容必须有经历依据；没有新认知时返回原样；\n"
        f"4. 只输出 JSON 对象，不要 Markdown：\n"
        f'{{"updates":[{{"key":"已有key或profile_<topic>_<hash6>","value":"完整新值（含原段落+新增）"}}]}}\n'
        f"5. updates 可为空数组；不得输出要删除的 key。\n\n"
        f"【现有画像 · {user_name}】\n{profile_block}\n\n"
        f"【近 7 天 active 记忆（long_term/moment/current）】\n{mem_block}\n"
    )


def _parse_updates(raw: str) -> tuple[list | None, str | None]:
    if not isinstance(raw, str) or not raw.strip():
        return None, "EMPTY_RESPONSE"
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        obj = json.loads(text)
    except Exception:
        # 尝试截取第一个 JSON 对象
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None, "JSON_PARSE_ERROR"
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return None, "JSON_PARSE_ERROR"
    if not isinstance(obj, dict):
        return None, "JSON_PARSE_ERROR"
    updates = obj.get("updates")
    if updates is None:
        updates = []
    if not isinstance(updates, list):
        return None, "INVALID_UPDATES"
    cleaned = []
    for u in updates:
        if not isinstance(u, dict):
            continue
        key = u.get("key")
        value = u.get("value")
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        key = key.strip()
        if key in _EXCLUDED_EXACT or key.startswith("llm_"):
            continue
        cleaned.append({"key": key, "value": value.strip()})
    return cleaned, None


def suggest_new_key(topic: str, value: str) -> str:
    """新 key：profile_<topic>_<hash6>。"""
    topic_s = re.sub(r"[^a-zA-Z0-9_]+", "_", (topic or "note").strip())[:40] or "note"
    h = hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:6]
    return f"profile_{topic_s}_{h}"


async def fetch_true_profile(supabase_client) -> dict:
    """读取真画像 dict[key]=value。"""
    if supabase_client is None:
        return {}
    try:
        res = await asyncio.to_thread(
            lambda: supabase_client.table("user_facts")
            .select("key, value").execute())
        rows = getattr(res, "data", None) or []
        out = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            k = r.get("key")
            if not _is_true_profile_key(k):
                continue
            v = r.get("value")
            if isinstance(v, str) and v.strip():
                out[str(k)] = v.strip()
        return out
    except Exception as e:  # noqa: BLE001
        _log(f"读取画像失败: {type(e).__name__}")
        return {}


async def fetch_recent_active_memories(supabase_service, user_id: str,
                                       days: int = 7) -> list[str]:
    """近 N 天 active 记忆正文（排除 core / superseded）。"""
    if supabase_service is None or not user_id:
        return []
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=days)).isoformat()
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items")
            .select("content,memory_type,status,created_at")
            .eq("user_id", user_id)
            .eq("status", "active")
            .in_("memory_type", ["long_term", "moment", "current"])
            .gte("created_at", since)
            .order("created_at", desc=True)
            .limit(50)
            .execute())
        rows = getattr(res, "data", None) or []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            c = str(r.get("content") or "").strip()
            if c:
                out.append(c[:300])
        return out
    except Exception as e:  # noqa: BLE001
        _log(f"读取记忆失败: {type(e).__name__}")
        return []


async def upsert_profile_updates(supabase_client, updates: list,
                                 existing_keys: set) -> dict:
    """按 key upsert；不删 key。返回 {written, skipped, new_keys}。"""
    stats = {"written": 0, "skipped": 0, "new_keys": 0, "error_code": None}
    if supabase_client is None:
        stats["error_code"] = "SERVICE_UNAVAILABLE"
        return stats
    for u in updates:
        key = u["key"]
        value = u["value"]
        if key not in existing_keys and not key.startswith("profile_"):
            # 新 key 规范化
            key = suggest_new_key(key, value)
            u = {"key": key, "value": value}
        is_new = key not in existing_keys
        try:
            await asyncio.to_thread(
                lambda k=key, v=value: supabase_client.table("user_facts")
                .upsert({"key": k, "value": v, "confidence": 1.0},
                        on_conflict="key").execute())
            stats["written"] += 1
            if is_new:
                stats["new_keys"] += 1
                existing_keys.add(key)
        except Exception as e:  # noqa: BLE001
            _log(f"upsert 失败: {type(e).__name__}")
            stats["skipped"] += 1
            stats["error_code"] = "UPSERT_FAILED"
            break
    return stats


async def run_profile_reflect(*, supabase_client=None, supabase_service=None,
                              user_id: str | None = None,
                              llm_call=None,
                              user_name: str | None = None) -> dict:
    """周画像反思主入口。"""
    result = {
        "ok": False, "error_code": None,
        "profile_keys": 0, "memory_count": 0,
        "updates_proposed": 0, "updates_written": 0,
        "rejected": 0, "length_checks": [],
    }
    if not profile_reflect_enabled():
        result["error_code"] = "DISABLED"
        return result

    if supabase_client is None or supabase_service is None:
        try:
            import server
            supabase_client = supabase_client or server.supabase
            supabase_service = supabase_service or server.supabase_service
            user_id = user_id or server._resolve_pinecone_user_id()
        except Exception:
            pass
    if user_id is None:
        user_id = os.environ.get("USER_ID") or os.environ.get("MEM0_USER_ID") or "default"
    user_name = user_name or os.environ.get("USER_NAME", "用户")

    profile = await fetch_true_profile(supabase_client)
    memories = await fetch_recent_active_memories(supabase_service, user_id, days=7)
    result["profile_keys"] = len(profile)
    result["memory_count"] = len(memories)

    if llm_call is None:
        import memory_extractor as mx
        llm_call = mx.make_compression_llm_call()

    prompt = build_reflect_prompt(profile, memories, user_name=user_name)
    try:
        raw = await asyncio.to_thread(llm_call, prompt)
    except Exception as e:  # noqa: BLE001
        _log(f"LLM 失败: {type(e).__name__}")
        result["error_code"] = "LLM_ERROR"
        return result

    updates, parse_err = _parse_updates(raw if isinstance(raw, str) else "")
    if parse_err:
        result["error_code"] = parse_err
        return result
    result["updates_proposed"] = len(updates or [])

    accepted = []
    for u in updates or []:
        key = u["key"]
        old_val = profile.get(key, "")
        # 新 key：旧值为空，只需长度与句数基本检查
        check = validate_profile_growth(old_val, u["value"])
        result["length_checks"].append({
            "key": key,
            "ok": check["ok"],
            "error_code": check["error_code"],
            "old_len": check["old_len"],
            "new_len": check["new_len"],
            "new_sentences": check["new_sentences"],
        })
        # 脱敏日志：只打 key/长度/结果，不打正文
        _log(f"校验 key={key} ok={check['ok']} code={check['error_code']} "
             f"old_len={check['old_len']} new_len={check['new_len']} "
             f"new_sent={check['new_sentences']}")
        if check["ok"]:
            accepted.append(u)
        elif check["error_code"] == "NO_CHANGE":
            continue
        else:
            result["rejected"] += 1

    if not accepted:
        result["ok"] = True
        result["error_code"] = result["error_code"] or "NO_ACCEPTED_UPDATES"
        _log(f"无写入 proposed={result['updates_proposed']} rejected={result['rejected']}")
        return result

    write_stats = await upsert_profile_updates(
        supabase_client, accepted, set(profile.keys()))
    result["updates_written"] = write_stats["written"]
    result["error_code"] = write_stats.get("error_code")
    result["ok"] = write_stats.get("error_code") is None
    _log(f"完成 keys={result['profile_keys']} mem={result['memory_count']} "
         f"proposed={result['updates_proposed']} written={result['updates_written']} "
         f"rejected={result['rejected']} new_keys={write_stats.get('new_keys', 0)}")
    return result
