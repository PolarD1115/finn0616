# -*- coding: utf-8 -*-
"""阶段 C2 —— memory_events 原始事件安全清理（preview→commit 两步人工确认）。

职责与边界：
- 只删 memory_events 中「已被提取消费」的事件：
  processing_status IN ('processed','failed') 且 created_at < 阈值
  （MEMORY_CLEANUP_OLDER_THAN_DAYS 默认 7 天，可被 preview 参数覆盖，范围 1~90）；
- 绝不删：pending / processing 状态的事件；任何 memory_items（用 superseded/
  invalid_at 收束，永不物理删）；任何 memories 表数据；
- 两步确认：preview 只读（COUNT + 按 status/channel 分组 + 最旧/最新时间 +
  一次性 cleanup_token），commit 校验 token + COUNT 一致性核对（与 preview
  差异 > 20% 视为漂移：中止、消费 token、要求重新 preview）后才 DELETE；
  token 一次性消费防重放；
- 首次引入删除：只有显式调用本接口才会删除，绝不做成后台自动循环删除；
- 删除用 service_role 客户端（server.supabase_service），不新建客户端；
- 日志只打计数与脱敏错误码，绝不打正文 / user_id / 密钥。
"""

import asyncio
import datetime
import secrets
import time

DEFAULT_OLDER_THAN_DAYS = 7
OLDER_THAN_DAYS_MIN = 1
OLDER_THAN_DAYS_MAX = 90
_TOKEN_TTL_SECONDS = 900          # 15 分钟（代码常量，与 memory_preview 同风格）
_CACHE_MAX_ENTRIES = 5
_DRIFT_RATIO = 0.2                # COUNT 一致性容差（±20%）
_DELETE_SCAN_CAP = 20000          # 分组计数的单次拉取上限（仅 channel/status 两列）
_DELETABLE_STATUSES = ("processed", "failed")

CODE_PREVIEW_READY = "CLEANUP_PREVIEW_READY"
CODE_CLEANUP_COMPLETED = "CLEANUP_COMPLETED"
CODE_NOTHING_TO_DELETE = "CLEANUP_NOTHING_TO_DELETE"
CODE_INVALID_REQUEST = "INVALID_CLEANUP_REQUEST"
CODE_TOKEN_NOT_FOUND = "CLEANUP_TOKEN_NOT_FOUND_OR_EXPIRED"
CODE_TOKEN_USED = "CLEANUP_TOKEN_ALREADY_USED"
CODE_COUNT_DRIFT = "CLEANUP_COUNT_DRIFT"
CODE_QUERY_FAILED = "CLEANUP_QUERY_FAILED"
CODE_DELETE_FAILED = "CLEANUP_DELETE_FAILED"
CODE_SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

# ── 一次性 token 进程内缓存（与 memory_preview 同款机制；仅单进程部署有效）──
_token_cache = {}       # token -> entry（未消费）
_used_tokens = {}       # token -> expires_at（已消费墓碑，用于区分"已用"与"不存在"）
_inflight = set()       # 正在执行 commit 的 token（防并发双击）
_TOKEN_USED = object()


def _purge_caches(now=None):
    now = time.time() if now is None else now
    for t in [t for t, e in _token_cache.items() if e.get("expires_at", 0) <= now]:
        _token_cache.pop(t, None)
    for t in [t for t, exp in _used_tokens.items() if exp <= now]:
        _used_tokens.pop(t, None)
    while len(_token_cache) > _CACHE_MAX_ENTRIES:
        oldest = min(_token_cache.items(), key=lambda kv: kv[1].get("created_at", 0))[0]
        _token_cache.pop(oldest, None)


def _store_entry(entry):
    """缓存 preview 上下文，返回不透明随机 token（不含 user_id/计数/时间原文）。"""
    _purge_caches()
    token = secrets.token_urlsafe(32)
    _token_cache[token] = entry
    while len(_token_cache) > _CACHE_MAX_ENTRIES:
        oldest = min(_token_cache.items(), key=lambda kv: kv[1].get("created_at", 0))[0]
        _token_cache.pop(oldest, None)
    return token


def _peek_entry(token):
    """返回 entry / _TOKEN_USED（已消费）/ None（不存在或过期）。"""
    if not isinstance(token, str) or not token:
        return None
    _purge_caches()
    if token in _used_tokens:
        return _TOKEN_USED
    return _token_cache.get(token)


def _consume_entry(token):
    """消费 token（pop + 写墓碑）。preview 后决定不执行的场景也会消费，
    防止拿过期上下文反复重试。"""
    entry = _token_cache.pop(token, None)
    if entry is not None:
        _used_tokens[token] = entry.get(
            "expires_at", time.time() + _TOKEN_TTL_SECONDS)
    return entry


def _deletable_rows_query(sb, threshold_iso):
    """白名单查询构造：仅 memory_events 的 processed/failed 且早于阈值。
    pending/processing 永远不在条件内；任何其他表不会被本模块触碰。"""
    return (sb.table("memory_events")
            .in_("processing_status", list(_DELETABLE_STATUSES))
            .lt("created_at", threshold_iso))


async def run_preview(supabase_service, older_than_days=DEFAULT_OLDER_THAN_DAYS):
    """只读预览：统计将被删除的事件并签发一次性 cleanup_token。零删除、零写入。

    返回 {"ok", "code", "stats", "cleanup_token"?, "expires_in_seconds"?}。"""
    stats = {}
    if supabase_service is None:
        return {"ok": False, "code": CODE_SERVICE_UNAVAILABLE, "stats": stats}
    if (isinstance(older_than_days, bool)
            or not isinstance(older_than_days, int)
            or not (OLDER_THAN_DAYS_MIN <= older_than_days <= OLDER_THAN_DAYS_MAX)):
        return {"ok": False, "code": CODE_INVALID_REQUEST, "stats": stats}

    now = datetime.datetime.now(datetime.timezone.utc)
    threshold_iso = (now - datetime.timedelta(days=older_than_days)).isoformat()
    try:
        # 总数（count 精确）+ 分组原始行（仅两列，封顶 _DELETE_SCAN_CAP）
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_events")
            .select("channel,processing_status", count="exact")
            .in_("processing_status", list(_DELETABLE_STATUSES))
            .lt("created_at", threshold_iso)
            .limit(_DELETE_SCAN_CAP).execute())
        rows = [r for r in (getattr(res, "data", None) or []) if isinstance(r, dict)]
        total = int(getattr(res, "count", 0) or 0)
        by_status = {"processed": 0, "failed": 0}
        by_channel = {}
        for r in rows:
            st = str(r.get("processing_status", "unknown"))
            ch = str(r.get("channel", "unknown"))
            by_status[st] = by_status.get(st, 0) + 1
            by_channel[ch] = by_channel.get(ch, 0) + 1
        # 最旧/最新 created_at（两条 1 行查询，精确于分组行的封顶截断）
        oldest_res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_events")
            .select("created_at")
            .in_("processing_status", list(_DELETABLE_STATUSES))
            .lt("created_at", threshold_iso)
            .order("created_at").limit(1).execute())
        newest_res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_events")
            .select("created_at")
            .in_("processing_status", list(_DELETABLE_STATUSES))
            .lt("created_at", threshold_iso)
            .order("created_at", desc=True).limit(1).execute())
        oldest_rows = getattr(oldest_res, "data", None) or []
        newest_rows = getattr(newest_res, "data", None) or []
        oldest_at = oldest_rows[0].get("created_at") if oldest_rows else None
        newest_at = newest_rows[0].get("created_at") if newest_rows else None
    except Exception as e:  # noqa: BLE001 —— 只记异常类型，不外泄数据库异常原文
        print(f"⚠️ [事件清理] preview 查询失败: {type(e).__name__}")
        return {"ok": False, "code": CODE_QUERY_FAILED, "stats": stats}

    stats.update({"total": total,
                  "by_status": by_status,
                  "by_channel": by_channel,
                  "oldest_created_at": oldest_at,
                  "newest_created_at": newest_at,
                  "threshold_iso": threshold_iso,
                  "older_than_days": older_than_days,
                  "writes_executed": False})
    print(f"🧹 [事件清理] preview: total={total} "
          f"processed={by_status.get('processed', 0)} "
          f"failed={by_status.get('failed', 0)} "
          f"older_than_days={older_than_days}")
    if total <= 0:
        return {"ok": True, "code": CODE_NOTHING_TO_DELETE, "stats": stats}

    entry = {"threshold_iso": threshold_iso,
             "older_than_days": older_than_days,
             "preview_count": total,
             "created_at": time.time(),
             "expires_at": time.time() + _TOKEN_TTL_SECONDS}
    token = _store_entry(entry)
    return {"ok": True, "code": CODE_PREVIEW_READY, "stats": stats,
            "cleanup_token": token, "expires_in_seconds": _TOKEN_TTL_SECONDS}


async def run_commit(supabase_service, cleanup_token):
    """两步确认第二步：校验 token + COUNT 一致性核对后执行删除。

    删除范围（白名单，硬编码）：memory_events 中 processing_status IN
    ('processed','failed') 且 created_at < preview 时的阈值。pending/processing
    不删；memory_items / memories 永不触碰。token 一次性消费（漂移/失败亦消费，
    需重新 preview）。"""
    stats = {}
    if supabase_service is None:
        return {"ok": False, "code": CODE_SERVICE_UNAVAILABLE, "stats": stats}
    peeked = _peek_entry(cleanup_token)
    if peeked is _TOKEN_USED:
        return {"ok": False, "code": CODE_TOKEN_USED, "stats": stats}
    entry = peeked
    if not isinstance(entry, dict):
        return {"ok": False, "code": CODE_TOKEN_NOT_FOUND, "stats": stats}
    if cleanup_token in _inflight:
        return {"ok": False, "code": CODE_TOKEN_USED, "stats": stats}
    _inflight.add(cleanup_token)
    try:
        threshold_iso = entry.get("threshold_iso")
        preview_count = int(entry.get("preview_count") or 0)

        # 1. COUNT 一致性核对：防 preview 与 commit 之间数据漂移
        try:
            current = await asyncio.to_thread(
                lambda: supabase_service.table("memory_events")
                .select("id", count="exact")
                .in_("processing_status", list(_DELETABLE_STATUSES))
                .lt("created_at", threshold_iso)
                .limit(1).execute())
            current_count = int(getattr(current, "count", 0) or 0)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ [事件清理] commit 计数失败: {type(e).__name__}")
            _consume_entry(cleanup_token)
            return {"ok": False, "code": CODE_QUERY_FAILED, "stats": stats}

        drift = abs(current_count - preview_count)
        if preview_count > 0 and drift > max(1, int(preview_count * _DRIFT_RATIO)):
            _consume_entry(cleanup_token)
            print(f"⚠️ [事件清理] COUNT 漂移 preview={preview_count} "
                  f"current={current_count}，已中止（token 已失效，请重新 preview）")
            return {"ok": False, "code": CODE_COUNT_DRIFT,
                    "stats": {"preview_count": preview_count,
                              "current_count": current_count}}
        if current_count <= 0:
            _consume_entry(cleanup_token)
            stats["deleted"] = 0
            return {"ok": True, "code": CODE_NOTHING_TO_DELETE, "stats": stats}

        # 2. 执行删除（条件与 preview 完全同款；pending/processing 天然不在条件内）
        try:
            del_res = await asyncio.to_thread(
                lambda: supabase_service.table("memory_events")
                .delete(count="exact")
                .in_("processing_status", list(_DELETABLE_STATUSES))
                .lt("created_at", threshold_iso)
                .execute())
            deleted = getattr(del_res, "count", None)
            if deleted is None:
                deleted = len(getattr(del_res, "data", None) or [])
            deleted = int(deleted or 0)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ [事件清理] 删除失败: {type(e).__name__}")
            _consume_entry(cleanup_token)
            return {"ok": False, "code": CODE_DELETE_FAILED, "stats": stats}

        _consume_entry(cleanup_token)
        stats.update({"deleted": deleted,
                      "preview_count": preview_count,
                      "current_count": current_count,
                      "threshold_iso": threshold_iso,
                      "older_than_days": entry.get("older_than_days")})
        print(f"🧹 [事件清理] 已删除 {deleted} 条已消费事件"
              f"（阈值之前：{threshold_iso}）")
        return {"ok": True, "code": CODE_CLEANUP_COMPLETED, "stats": stats}
    finally:
        _inflight.discard(cleanup_token)
