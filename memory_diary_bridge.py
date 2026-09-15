# -*- coding: utf-8 -*-
"""阶段 D3 —— 行动日志 / 秘密日记 → 分层记忆（memory_items）旁路桥接。

职责：
- finalize_activity_log 成功后，把活动摘要转为临时 memory_event 结构，
  复用 memory_extractor.extract_memory_candidates 提取，写入 moment/long_term；
- write_private_diary 成功后，同样异步提取，强制写入 moment，
  source='private_diary'（供 search_memory 隐私过滤）；
- 秘密日记原文不进 memories / Pinecone / 日志正文；仅进 memory_items；
- 门控 DIARY_MEMORY_BRIDGE_ENABLED（默认 true）；关闭时零行为。

不修改 A5 worker / 提取器核心逻辑；不物理删除 memory_items。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import os
import threading
import uuid

logger = logging.getLogger(__name__)

SOURCE_ACTIVITY = "activity_log"
SOURCE_PRIVATE_DIARY = "private_diary"
CREATED_BY = "memory_diary_bridge"

# 秘密日记活动由私密日记桥单独处理，避免 activity_log finalize 双提取
_SECRET_DIARY_ACTIVITY_IDS = frozenset({"free:secret_diary"})
_SECRET_DIARY_ACTIVITY_NAMES = frozenset({"写秘密日记"})

# 仅成功类终态触发提取（failed/skipped/running 不触发）
_BRIDGE_OK_STATUSES = frozenset({"succeeded", "observed", "partial"})


def _env_identity_names():
    """从环境变量读取 AI_NAME / USER_NAME（日记桥侧允许读非密钥配置）。"""
    ai = (os.environ.get("AI_NAME") or "").strip() or "助手"
    user = (os.environ.get("USER_NAME") or "").strip() or "用户"
    return ai, user


def diary_bridge_enabled() -> bool:
    """DIARY_MEMORY_BRIDGE_ENABLED 门控（默认 true）。"""
    return os.environ.get("DIARY_MEMORY_BRIDGE_ENABLED", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def _log(msg: str) -> None:
    print(f"[memory_diary_bridge] {msg}")


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_temp_event(*, content: str, user_id: str, channel: str,
                     occurred_at: str | None = None,
                     event_id: str | None = None) -> dict:
    """构造临时 memory_event 结构（逻辑 role=event）。

    提取器仅接受 user/assistant，调用方须经 to_extractable_events 适配后再提取。
    不写库、不进 memory_events 表。
    """
    text = (content or "").strip()
    return {
        "id": event_id or str(uuid.uuid4()),
        "user_id": user_id,
        "session_id": None,
        "channel": channel,
        "role": "event",
        "content": text,
        "content_hash": _sha256(text) if text else "",
        "occurred_at": occurred_at or _utcnow_iso(),
        "processing_status": "pending",
        "attempt_count": 0,
        "metadata": {"bridge": True, "logical_role": "event"},
        "created_by": CREATED_BY,
    }


def to_extractable_events(events: list) -> list:
    """把逻辑 role=event 适配为提取器可消费的 role=user（不改 A4 白名单）。"""
    out = []
    for e in events:
        if not isinstance(e, dict):
            continue
        row = dict(e)
        if row.get("role") == "event":
            row["role"] = "user"
        out.append(row)
    return out


def build_activity_events(activity_name: str = "", activity_id: str = "",
                          thought_summary: str = "", result_summary: str = "",
                          user_id: str = "default",
                          finished_at: str | None = None,
                          user_name: str | None = None,
                          ai_name: str | None = None) -> list:
    """活动日志 → 临时 event 列表（供提取）。"""
    if ai_name is None or user_name is None:
        env_ai, env_user = _env_identity_names()
        ai_name = ai_name if ai_name is not None else env_ai
        user_name = user_name if user_name is not None else env_user
    import memory_extractor as mx
    ai_name, user_name = mx.resolve_identity_names(ai_name, user_name)
    parts = []
    name = (activity_name or activity_id or "活动").strip()
    parts.append(
        f"【身份说明】作者是 AI「{ai_name}」，用户本人是「{user_name}」。"
        f"文中的「{user_name}」即用户本人，禁止写成用户身边的第三人。"
    )
    parts.append(f"AI「{ai_name}」完成了活动「{name}」。")
    thought = (thought_summary or "").strip()
    result = (result_summary or "").strip()
    if thought:
        parts.append(f"当时想法摘要：{thought}")
    if result:
        parts.append(f"活动结果：{result}")
    content = "\n".join(parts).strip()
    # 仅有身份说明 + 空活动名时不提取
    if not thought and not result:
        return []
    return [build_temp_event(
        content=content, user_id=user_id, channel=SOURCE_ACTIVITY,
        occurred_at=finished_at)]


def build_private_diary_events(title: str = "", content: str = "",
                               mood: str = "", user_id: str = "default",
                               created_at: str | None = None,
                               user_name: str | None = None,
                               ai_name: str | None = None) -> list:
    """秘密日记 → 临时 event 列表（供提取；正文不入日志）。"""
    if ai_name is None or user_name is None:
        env_ai, env_user = _env_identity_names()
        ai_name = ai_name if ai_name is not None else env_ai
        user_name = user_name if user_name is not None else env_user
    import memory_extractor as mx
    ai_name, user_name = mx.resolve_identity_names(ai_name, user_name)
    body = (content or "").strip()
    if not body:
        return []
    title_s = (title or "秘密日记").strip()
    mood_s = (mood or "").strip()
    head = f"（私密日记·{title_s}"
    if mood_s:
        head += f"·心情{mood_s}"
    head += "）"
    # 秘密日记由 AI 撰写；仍映射为 user 角色以便证据索引，但必须先钉死身份，
    # 避免 compression 把 AI 当成「用户」、把真人名当成互动对象。
    identity = (
        f"【身份说明】本文是 AI「{ai_name}」撰写的关于用户「{user_name}」的私密日记。"
        f"日记作者是 {ai_name}，不是用户；文中出现的「{user_name}」及其小名/昵称/全名"
        f"一律指用户本人。禁止写成「用户身边有一位名叫{user_name}…」或把角色对调。"
    )
    text = f"{head}\n{identity}\n{body}"
    return [build_temp_event(
        content=text, user_id=user_id, channel=SOURCE_PRIVATE_DIARY,
        occurred_at=created_at)]


def _postprocess_candidates(candidates: list, *, source: str,
                            force_moment: bool = False) -> list:
    """覆盖 source / metadata /（可选）强制 moment；不改 content。"""
    out = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        row = dict(c)
        row["source"] = source
        meta = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
        meta["bridge"] = CREATED_BY
        if source == SOURCE_PRIVATE_DIARY:
            meta["privacy"] = "private_diary"
            meta["private"] = True
        row["metadata"] = meta
        if force_moment:
            row["memory_type"] = "moment"
        elif row.get("memory_type") not in ("moment", "long_term", "current", "memo", "core"):
            row["memory_type"] = "moment"
        # 活动日志：仅保留 moment / long_term（丢弃 core 等不当类型）
        if source == SOURCE_ACTIVITY and row.get("memory_type") not in ("moment", "long_term"):
            row["memory_type"] = "moment"
        row["status"] = "active"
        row["created_by"] = CREATED_BY
        out.append(row)
    return out


def is_private_diary_memory_item(item: dict) -> bool:
    """判断 memory_items 行是否来自秘密日记（供 search_memory 过滤）。"""
    if not isinstance(item, dict):
        return False
    if item.get("source") == SOURCE_PRIVATE_DIARY:
        return True
    meta = item.get("metadata")
    if isinstance(meta, dict):
        if meta.get("privacy") == "private_diary" or meta.get("private") is True:
            return True
    return False


async def _write_items(supabase_service, candidates: list) -> dict:
    """写入 memory_items（active）；content_hash 精确去重；永不物理删除。"""
    from memory_preview import _DEDUP_STATUSES, _MEMORY_ITEM_FIELDS

    stats = {"inserted": 0, "duplicate_skipped": 0, "error_code": None}
    if not candidates:
        return stats
    if supabase_service is None:
        stats["error_code"] = "SERVICE_UNAVAILABLE"
        return stats

    user_id = candidates[0].get("user_id")
    hashes = sorted({str(c.get("content_hash")) for c in candidates if c.get("content_hash")})
    existing = set()
    if hashes:
        try:
            res = await asyncio.to_thread(
                lambda: supabase_service.table("memory_items")
                .select("content_hash")
                .eq("user_id", user_id)
                .in_("content_hash", hashes)
                .in_("status", list(_DEDUP_STATUSES))
                .execute())
            existing = {str(r.get("content_hash"))
                        for r in (getattr(res, "data", None) or [])
                        if isinstance(r, dict)}
        except Exception as e:  # noqa: BLE001
            _log(f"去重查询失败: {type(e).__name__}")
            stats["error_code"] = "DEDUP_FAILED"
            return stats

    for cand in candidates:
        h = str(cand.get("content_hash") or "")
        if h and h in existing:
            stats["duplicate_skipped"] += 1
            continue
        row = {k: cand.get(k) for k in _MEMORY_ITEM_FIELDS}
        row["status"] = "active"
        row["created_by"] = CREATED_BY
        row["superseded_by"] = cand.get("superseded_by")
        try:
            res = await asyncio.to_thread(
                lambda r=row: supabase_service.table("memory_items").insert(r).execute())
            if not (getattr(res, "data", None) or []):
                raise RuntimeError("empty insert")
            stats["inserted"] += 1
            if h:
                existing.add(h)
        except Exception as e:  # noqa: BLE001
            _log(f"写入失败: {type(e).__name__}")
            stats["error_code"] = "INSERT_FAILED"
            break
    return stats


async def extract_and_store(events: list, *, source: str,
                            force_moment: bool = False,
                            llm_call=None, supabase_service=None,
                            user_id: str | None = None,
                            user_name: str | None = None,
                            ai_name: str | None = None) -> dict:
    """提取 + 写入主入口（可注入 llm_call / supabase 供测试）。"""
    result = {"ok": False, "inserted": 0, "duplicate_skipped": 0,
              "candidates": 0, "error_code": None}
    if not diary_bridge_enabled():
        result["error_code"] = "DISABLED"
        return result
    extractable = to_extractable_events(events)
    if not extractable:
        result["error_code"] = "EMPTY_EVENTS"
        return result

    import memory_extractor as mx
    if ai_name is None or user_name is None:
        env_ai, env_user = _env_identity_names()
        ai_name = ai_name if ai_name is not None else env_ai
        user_name = user_name if user_name is not None else env_user
    ai_name, user_name = mx.resolve_identity_names(ai_name, user_name)
    if llm_call is None:
        llm_call = mx.make_compression_llm_call()
    if supabase_service is None:
        try:
            import server
            supabase_service = server.supabase_service
        except Exception:
            supabase_service = None
    if user_id is None:
        try:
            import server
            user_id = server._resolve_pinecone_user_id()
        except Exception:
            user_id = str(extractable[0].get("user_id") or "default")

    try:
        extraction = await mx.extract_memory_candidates(
            extractable, llm_call, user_id=user_id,
            ai_name=ai_name, user_name=user_name)
    except Exception as e:  # noqa: BLE001
        _log(f"提取异常: {type(e).__name__}")
        result["error_code"] = "EXTRACT_EXCEPTION"
        return result

    if not extraction.get("ok"):
        result["error_code"] = extraction.get("error_code") or "EXTRACT_FAILED"
        _log(f"提取未产出 source={source} code={result['error_code']}")
        return result

    cands = _postprocess_candidates(
        extraction.get("candidates") or [],
        source=source, force_moment=force_moment)
    result["candidates"] = len(cands)
    write_stats = await _write_items(supabase_service, cands)
    result["inserted"] = write_stats["inserted"]
    result["duplicate_skipped"] = write_stats["duplicate_skipped"]
    result["error_code"] = write_stats.get("error_code")
    result["ok"] = write_stats.get("error_code") is None
    _log(f"完成 source={source} candidates={result['candidates']} "
         f"inserted={result['inserted']} skipped={result['duplicate_skipped']}")
    return result


async def bridge_activity_log(*, activity_key: str = "", activity_id: str = "",
                              activity_name: str = "", status: str = "",
                              thought_summary: str = "", result_summary: str = "",
                              finished_at: str | None = None,
                              llm_call=None, supabase_service=None,
                              user_id: str | None = None) -> dict:
    """活动日志成功 finalize 后的桥接入口。"""
    if not diary_bridge_enabled():
        return {"ok": False, "error_code": "DISABLED"}
    if status not in _BRIDGE_OK_STATUSES:
        return {"ok": False, "error_code": "STATUS_SKIPPED"}
    aid = (activity_id or "").strip()
    aname = (activity_name or "").strip()
    if aid in _SECRET_DIARY_ACTIVITY_IDS or aname in _SECRET_DIARY_ACTIVITY_NAMES:
        return {"ok": False, "error_code": "SECRET_DIARY_SKIPPED"}

    if user_id is None:
        try:
            import server
            user_id = server._resolve_pinecone_user_id()
        except Exception:
            user_id = "default"

    events = build_activity_events(
        activity_name=aname, activity_id=aid,
        thought_summary=thought_summary, result_summary=result_summary,
        user_id=user_id, finished_at=finished_at)
    if not events:
        return {"ok": False, "error_code": "EMPTY_CONTENT"}
    return await extract_and_store(
        events, source=SOURCE_ACTIVITY, force_moment=False,
        llm_call=llm_call, supabase_service=supabase_service, user_id=user_id)


async def bridge_private_diary(*, title: str = "", content: str = "",
                               mood: str = "", action_key: str = "",
                               llm_call=None, supabase_service=None,
                               user_id: str | None = None) -> dict:
    """秘密日记写入成功后的桥接入口（正文不入日志）。"""
    if not diary_bridge_enabled():
        return {"ok": False, "error_code": "DISABLED"}
    if not (content or "").strip():
        return {"ok": False, "error_code": "EMPTY_CONTENT"}

    if user_id is None:
        try:
            import server
            user_id = server._resolve_pinecone_user_id()
        except Exception:
            user_id = "default"

    events = build_private_diary_events(
        title=title, content=content, mood=mood, user_id=user_id)
    # 日志只打计数，绝不打正文 / action_key 全文
    _log(f"私密日记桥接触发 title_len={len(title or '')} "
         f"content_len={len(content or '')} action_prefix={(action_key or '')[:8]}")
    return await extract_and_store(
        events, source=SOURCE_PRIVATE_DIARY, force_moment=True,
        llm_call=llm_call, supabase_service=supabase_service, user_id=user_id)


def schedule_bridge(coro) -> None:
    """从同步/线程上下文安全调度异步桥接（失败只记日志）。"""
    def _runner():
        try:
            asyncio.run(coro)
        except Exception as e:  # noqa: BLE001
            _log(f"后台桥接失败: {type(e).__name__}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        threading.Thread(target=_runner, daemon=True).start()
