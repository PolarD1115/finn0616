# -*- coding: utf-8 -*-
"""阶段 D1 —— 换窗备忘 memo（交接纸条）。

职责：
- 长沉默 / session 断开后，本轮有效对话落库时后台生成 1 条 memo；
- 读取：volatile 区块注入最新 1 条 active memo；
- 去重：同 subject_key 或同 source_event_ids 的旧 active → superseded；
- 门控 MEMORY_MEMO_ENABLED（默认 true）；关闭时不写不读。

memo 只操作 memory_type='memo'，不影响 core/current/long_term/moment。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re
import threading
import uuid

CREATED_BY = "session_memo"
SOURCE = "session_memo"
DEFAULT_SILENCE_HOURS = 6.0
DEFAULT_EXPIRES_DAYS = 7
MEMO_SUBJECT_KEY = "session_handoff_memo"


def memo_enabled() -> bool:
    return os.environ.get("MEMORY_MEMO_ENABLED", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def memo_silence_hours() -> float:
    raw = os.environ.get("MEMORY_MEMO_SILENCE_HOURS", str(DEFAULT_SILENCE_HOURS))
    try:
        v = float(raw)
        return v if v >= 0 else DEFAULT_SILENCE_HOURS
    except (TypeError, ValueError):
        return DEFAULT_SILENCE_HOURS


def _log(msg: str) -> None:
    print(f"[memory_memo] {msg}")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def should_generate_memo(*, silence_hours: float | None = None,
                         session_id=None,
                         session_changed: bool = False) -> bool:
    """写入时机判定：沉默超时，或调用方显式标记 session 变化。

    注意：Web 渠道 session_id 恒为 None（诚实写空），不能仅凭 None 触发，
    否则每轮对话都会写 memo；None 仅表示「无可靠会话标识」，须配合沉默阈值。
    """
    if not memo_enabled():
        return False
    if session_changed:
        return True
    if silence_hours is None:
        return False
    try:
        sh = float(silence_hours)
    except (TypeError, ValueError):
        return False
    return sh > memo_silence_hours()


def format_memo_block(memo_row: dict) -> str:
    """把 memo 行格式化为 volatile 注入段。"""
    content = ""
    if isinstance(memo_row, dict):
        content = str(memo_row.get("content") or "").strip()
    if not content:
        return ""
    # 若模型已按结构化输出，原样包标题；否则整段作为「上次聊到」
    if "上次聊到" in content or "未完成" in content:
        body = content
    else:
        body = f"上次聊到：{content}"
    return f"【上次交接备忘】\n{body}"


async def fetch_latest_active_memo(supabase_service, user_id: str) -> dict | None:
    """只读：最新 1 条 active memo（未过期优先；失败返回 None）。"""
    if not memo_enabled() or supabase_service is None or not user_id:
        return None
    try:
        now_iso = _utcnow().isoformat()
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items")
            .select("id,content,subject_key,expires_at,created_at,source,status,memory_type")
            .eq("user_id", user_id)
            .eq("memory_type", "memo")
            .eq("status", "active")
            .order("created_at", desc=True)
            .limit(5)
            .execute())
        rows = getattr(res, "data", None) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            exp = row.get("expires_at")
            if exp:
                try:
                    exp_dt = datetime.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=datetime.timezone.utc)
                    if exp_dt <= _utcnow():
                        continue
                except Exception:
                    pass
            if str(row.get("content") or "").strip():
                return row
        return None
    except Exception as e:  # noqa: BLE001
        _log(f"读取 memo 失败: {type(e).__name__}")
        return None


def build_memo_prompt(user_msg: str, ai_msg: str, *,
                      user_name: str = "用户", ai_name: str = "助手") -> str:
    return (
        f"你是会话交接备忘生成器。根据本轮对话，写一条给下次新窗口用的交接纸条。\n"
        f"身份：对话中的「对方/用户」是「{user_name}」本人；你是「{ai_name}」。\n"
        f"禁止把「{user_name}」写成用户身边的第三人，禁止把 {ai_name} 写成「用户」。\n"
        f"要求：只输出纯文本，按下面四行格式（每行一句，简洁）：\n"
        f"上次聊到：…\n未完成：…\n对方状态：…\n建议：…\n"
        f"「对方」=「{user_name}」。不要 Markdown，不要 JSON，不要角色前缀。\n\n"
        f"【本轮对话】\n{user_name}：{(user_msg or '')[:800]}\n"
        f"{ai_name}：{(ai_msg or '')[:800]}\n"
    )


def _parse_memo_content(raw: str) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    # 去掉可能的代码围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    # 尝试 JSON
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                parts = []
                mapping = (("上次聊到", "topic"), ("未完成", "todo"),
                           ("对方状态", "mood"), ("建议", "suggest"))
                for label, key in mapping:
                    val = obj.get(label) or obj.get(key) or ""
                    if val:
                        parts.append(f"{label}：{val}")
                if parts:
                    return "\n".join(parts)
        except Exception:
            pass
    # 确保至少有一行有用内容
    if len(text) < 4:
        return None
    return text[:500]


async def _supersede_old_memos(supabase_service, user_id: str, *,
                               subject_key: str | None,
                               source_event_ids: list | None,
                               new_id: str | None) -> int:
    """把同主题 / 同源 active memo 标为 superseded（不物理删除）。"""
    if supabase_service is None:
        return 0
    superseded = 0
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items")
            .select("id,subject_key,source_event_ids")
            .eq("user_id", user_id)
            .eq("memory_type", "memo")
            .eq("status", "active")
            .execute())
        rows = getattr(res, "data", None) or []
    except Exception as e:  # noqa: BLE001
        _log(f"supersede 查询失败: {type(e).__name__}")
        return 0

    src_set = set(str(x) for x in (source_event_ids or []) if x)
    for row in rows:
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        if not rid or (new_id and str(rid) == str(new_id)):
            continue
        match = False
        if subject_key and row.get("subject_key") == subject_key:
            match = True
        if src_set:
            old_ids = row.get("source_event_ids") or []
            if isinstance(old_ids, list) and src_set.intersection(str(x) for x in old_ids):
                match = True
        if not match:
            continue
        payload = {
            "status": "superseded",
            "superseded_by": new_id,
            "updated_at": _utcnow().isoformat(),
        }
        try:
            await asyncio.to_thread(
                lambda i=rid, p=payload: supabase_service.table("memory_items")
                .update(p).eq("id", i).eq("status", "active").execute())
            superseded += 1
        except Exception as e:  # noqa: BLE001
            _log(f"supersede 更新失败: {type(e).__name__}")
    return superseded


async def generate_and_store_memo(*, user_msg: str, ai_msg: str,
                                  silence_hours: float | None = None,
                                  session_id=None,
                                  session_changed: bool = False,
                                  source_event_ids: list | None = None,
                                  llm_call=None,
                                  supabase_service=None,
                                  user_id: str | None = None,
                                  user_name: str | None = None,
                                  ai_name: str | None = None) -> dict:
    """生成并写入 memo（可注入依赖供测试）。"""
    result = {"ok": False, "error_code": None, "inserted": False, "superseded": 0}
    if not should_generate_memo(silence_hours=silence_hours, session_id=session_id,
                                session_changed=session_changed):
        result["error_code"] = "SKIPPED"
        return result
    if not (user_msg or "").strip() and not (ai_msg or "").strip():
        result["error_code"] = "EMPTY_DIALOGUE"
        return result

    if user_id is None:
        try:
            import server
            user_id = server._resolve_pinecone_user_id()
        except Exception:
            user_id = "default"
    user_name = user_name or os.environ.get("USER_NAME", "用户")
    ai_name = ai_name or os.environ.get("AI_NAME", "助手")

    if llm_call is None:
        import memory_extractor as mx
        llm_call = mx.make_compression_llm_call()
    if supabase_service is None:
        try:
            import server
            supabase_service = server.supabase_service
        except Exception:
            supabase_service = None
    if supabase_service is None:
        result["error_code"] = "SERVICE_UNAVAILABLE"
        return result

    prompt = build_memo_prompt(user_msg, ai_msg, user_name=user_name, ai_name=ai_name)
    try:
        raw = await asyncio.to_thread(llm_call, prompt)
    except Exception as e:  # noqa: BLE001
        _log(f"LLM 失败: {type(e).__name__}")
        result["error_code"] = "LLM_ERROR"
        return result

    content = _parse_memo_content(raw if isinstance(raw, str) else "")
    if not content:
        result["error_code"] = "EMPTY_MEMO"
        return result

    now = _utcnow()
    expires = now + datetime.timedelta(days=DEFAULT_EXPIRES_DAYS)
    new_id = str(uuid.uuid4())
    row = {
        "id": new_id,
        "user_id": user_id,
        "memory_type": "memo",
        "content": content,
        "content_hash": _sha256(content),
        "status": "active",
        "importance": 5,
        "confidence": 0.8,
        "source": SOURCE,
        "source_event_ids": list(source_event_ids or []),
        "source_batch_id": None,
        "subject_key": MEMO_SUBJECT_KEY,
        "valid_at": now.isoformat(),
        "invalid_at": None,
        "expires_at": expires.isoformat(),
        "last_confirmed_at": None,
        "superseded_by": None,
        "metadata": {"kind": "session_handoff"},
        "created_by": CREATED_BY,
    }

    # 先插入新条，再用新 id 标记旧条 superseded
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items").insert(row).execute())
        if not (getattr(res, "data", None) or []):
            raise RuntimeError("empty insert")
    except Exception as e:  # noqa: BLE001
        _log(f"写入失败: {type(e).__name__}")
        result["error_code"] = "INSERT_FAILED"
        return result

    superseded = await _supersede_old_memos(
        supabase_service, user_id,
        subject_key=MEMO_SUBJECT_KEY,
        source_event_ids=source_event_ids,
        new_id=new_id)
    result.update({"ok": True, "inserted": True, "superseded": superseded})
    _log(f"memo 已写入 expires_days={DEFAULT_EXPIRES_DAYS} superseded={superseded}")
    return result


def schedule_memo_generation(**kwargs) -> None:
    """异步调度 memo 生成（吞异常）。"""
    async def _job():
        try:
            await generate_and_store_memo(**kwargs)
        except Exception as e:  # noqa: BLE001
            _log(f"调度任务失败: {type(e).__name__}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_job())
    except RuntimeError:
        def _runner():
            try:
                asyncio.run(_job())
            except Exception as e:  # noqa: BLE001
                _log(f"线程任务失败: {type(e).__name__}")
        threading.Thread(target=_runner, daemon=True).start()


async def inject_memo_text(supabase_service, user_id: str) -> str:
    """供渠道上下文调用：返回格式化 memo 段或空串。"""
    if not memo_enabled():
        return ""
    row = await fetch_latest_active_memo(supabase_service, user_id)
    if not row:
        return ""
    return format_memo_block(row)
