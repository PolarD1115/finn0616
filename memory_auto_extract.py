# -*- coding: utf-8 -*-
"""阶段 A5 —— memory_events 全自动提取 worker（分批消化 pending 积压）。

职责与边界：
- 由 heartbeat.async_memory_extraction_worker 按 MEMORY_EXTRACTION_INTERVAL 周期
  调用 run_auto_extraction：把最旧的 pending 原始事件分批交给
  memory_extractor.extract_memory_candidates 提取为分层记忆，写入 memory_items；
- 高置信（confidence >= auto_active_threshold）直接插入 status='active'；
  低置信插入 status='pending_review' 交人工审核；
- 写入前沿用 memory_preview._run_commit_locked 同款跨批精确去重思路：
  user_id + content_hash 批量查询（只看 pending_review/active），冲突候选跳过
  不插入并计 duplicate_skipped；重复事件同样标 processed（该事实已存在，
  事件视为已消费，与人工 commit 语义一致）；
- 事件状态推进：pending → processing（原子条件 UPDATE 认领，防多进程/重入并发）
  → processed（提取成功）/ failed（提取失败：attempt_count+1、last_error=脱敏
  error_code、processed_at=NULL）。失败与 processed 的事件都永不物理删除；
- 写入/去重阶段失败时把已认领事件释放回 pending（不标 failed：事件尚未真正
  消费，靠写入前去重实现幂等重试，事件不丢）；
- 本模块只 INSERT memory_items，不改 memory_preview 的手动 commit；
  日志只打计数与 error_code，绝不打正文 / user_id / 密钥。
"""

import asyncio
import datetime
import os

import memory_extractor as mx
from memory_preview import _DEDUP_STATUSES, _MEMORY_ITEM_FIELDS

# 错误代码（脱敏；进 stats.error_code / 日志，不含正文与异常原文）
CODE_SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
CODE_PROCESSING_IN_PROGRESS = "PROCESSING_IN_PROGRESS"
CODE_CLAIM_MISMATCH = "EVENT_CLAIM_MISMATCH"
CODE_DEDUP_FAILED = "DEDUP_CHECK_FAILED"
CODE_INSERT_FAILED = "MEMORY_ITEM_INSERT_FAILED"
CODE_RELEASE_FAILED = "EVENT_RELEASE_FAILED"
CODE_UNEXPECTED = "UNEXPECTED_ERROR"

DEFAULT_BATCH_LIMIT = 20
DEFAULT_AUTO_ACTIVE_THRESHOLD = 0.75

ACTIVE_STATUS = "active"
REVIEW_STATUS = "pending_review"


def _log(msg):
    print(f"[memory_auto_extract] {msg}")


class _WriteStageError(Exception):
    """写入阶段（去重/插入）失败：携带脱敏错误码，触发事件释放回 pending。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _memory_item_row(cand, status, batch_id=None):
    """把已验证候选转为 memory_items 插入行。

    字段集与 memory_preview._MEMORY_ITEM_FIELDS 同款（id/created_at/updated_at
    走数据库默认）；status 由置信度分层决定，created_by 固定为自动提取器标识，
    与人工 commit（memory_extractor）可区分来源。"""
    row = {
        "user_id": cand.get("user_id"),
        "memory_type": cand.get("memory_type"),
        "content": cand.get("content"),
        "content_hash": cand.get("content_hash"),
        "status": status,
        "importance": cand.get("importance"),
        "confidence": cand.get("confidence"),
        "source": cand.get("source"),
        "source_event_ids": list(cand.get("source_event_ids") or []),
        "source_batch_id": batch_id or cand.get("source_batch_id"),
        "subject_key": cand.get("subject_key"),
        "valid_at": cand.get("valid_at"),
        "invalid_at": cand.get("invalid_at"),
        "expires_at": cand.get("expires_at"),
        "last_confirmed_at": cand.get("last_confirmed_at"),
        "superseded_by": cand.get("superseded_by"),
        "metadata": cand.get("metadata") if isinstance(cand.get("metadata"), dict) else {},
        "created_by": "memory_auto_extract",
    }
    return {k: row[k] for k in _MEMORY_ITEM_FIELDS}


async def _write_memory_items(supabase_service, user_id, candidates,
                              auto_active_threshold, batch_id):
    """跨批精确去重 + 逐条写入。返回 {"active", "pending_review", "duplicate_skipped"}。

    去重/插入失败抛 _WriteStageError：已插入条目保留（幂等重试由去重保证），
    调用方负责把已认领事件释放回 pending。"""
    counts = {"active": 0, "pending_review": 0, "duplicate_skipped": 0}

    # 1. 跨批去重：user_id + content_hash 一次批量查询（只看 pending_review/active，
    #    与 _run_commit_locked 同款：不查 superseded/expired/rejected，不做语义近似）
    hashes = sorted({str(c.get("content_hash")) for c in candidates
                     if c.get("content_hash")})
    existing = set()
    if hashes:
        try:
            res = await asyncio.to_thread(
                lambda: supabase_service.table("memory_items")
                .select("content_hash,status")
                .eq("user_id", user_id)
                .in_("content_hash", hashes)
                .in_("status", list(_DEDUP_STATUSES))
                .execute())
            existing = {str(r.get("content_hash"))
                        for r in (getattr(res, "data", None) or [])
                        if isinstance(r, dict)}
        except Exception as e:  # noqa: BLE001 —— 只记异常类型，不外泄数据库异常原文
            _log(f"去重查询失败: {type(e).__name__}")
            raise _WriteStageError(CODE_DEDUP_FAILED)

    to_insert = [c for c in candidates
                 if str(c.get("content_hash")) not in existing]
    counts["duplicate_skipped"] = len(candidates) - len(to_insert)

    # 2. 逐条插入（任一失败立即停止：已成功项保留，剩余事件释放回 pending 重试）
    for cand in to_insert:
        try:
            conf = float(cand.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        status = ACTIVE_STATUS if conf >= auto_active_threshold else REVIEW_STATUS
        row = _memory_item_row(cand, status, batch_id)
        try:
            res = await asyncio.to_thread(
                lambda r=row: supabase_service.table("memory_items")
                .insert(r).execute())
            if not (getattr(res, "data", None) or []):
                raise RuntimeError("empty insert result")
        except Exception as e:  # noqa: BLE001
            _log(f"memory_items 写入失败: {type(e).__name__}")
            raise _WriteStageError(CODE_INSERT_FAILED)
        counts[status] += 1
    return counts


async def _finalize_events(supabase_service, claimed_events, *, ok, error_code,
                           batch_id):
    """按提取结果原子推进本批事件状态。

    按缓存原始 attempt_count 分组（同值一组 → 单条语句原子更新，避免先读后盲写）；
    条件含 processing_status='processing'（只动本流程认领的行，不覆盖其他流程）。
    返回行数是否与预期一致。"""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    groups = {}
    for e in claimed_events:
        try:
            orig = int(e.get("attempt_count") or 0)
        except (TypeError, ValueError):
            orig = 0
        groups.setdefault(orig, []).append(str(e.get("id")))
    total = sum(len(v) for v in groups.values())
    updated = 0
    try:
        for orig, ids in sorted(groups.items()):
            if ok:
                payload = {"processing_status": "processed",
                           "processed_at": now_iso,
                           "batch_id": batch_id,
                           "last_error": None,
                           "attempt_count": orig + 1}
            else:
                # 失败：attempt_count+1、last_error=脱敏 error_code、processed_at=NULL；
                # 永不物理删除（删除属后续压缩链阶段）
                payload = {"processing_status": "failed",
                           "processed_at": None,
                           "batch_id": batch_id,
                           "last_error": error_code,
                           "attempt_count": orig + 1}
            res = await asyncio.to_thread(
                lambda p=payload, i=ids: supabase_service.table("memory_events")
                .update(p).in_("id", i)
                .eq("processing_status", "processing").execute())
            updated += len(getattr(res, "data", None) or [])
    except Exception as e:  # noqa: BLE001
        _log(f"事件状态更新失败: {type(e).__name__}")
        return False
    if updated != total:
        _log(f"事件状态更新数不匹配 updated={updated} expected={total}（可能有并发方介入）")
        return False
    return True


async def _release_events(supabase_service, event_ids):
    """把已认领但未收尾的事件释放回 pending（条件 UPDATE，只动仍为 processing 的行）。

    释放失败只记日志：事件将停留在 processing，由下一轮并发防护跳过并等待人工核查
    （绝不删除，也绝不盲写覆盖其他流程的认领）。"""
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_events")
            .update({"processing_status": "pending"})
            .in_("id", list(event_ids))
            .eq("processing_status", "processing").execute())
        _log(f"已释放 {len(getattr(res, 'data', None) or [])} 条事件回 pending")
    except Exception as e:  # noqa: BLE001
        _log(f"释放事件失败({CODE_RELEASE_FAILED}): {type(e).__name__}")


async def _count_pending(supabase_service, user_id):
    """剩余 pending 事件数（backlog 遥测）。失败返回 None（不影响主流程）。"""
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_events")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("processing_status", "pending")
            .limit(1).execute())
        count = getattr(res, "count", None)
        if count is not None:
            return int(count)
        return len(getattr(res, "data", None) or [])
    except Exception as e:  # noqa: BLE001
        _log(f"pending 计数失败: {type(e).__name__}")
        return None


async def run_auto_extraction(supabase_service, *, user_id,
                              batch_limit=DEFAULT_BATCH_LIMIT,
                              auto_active_threshold=DEFAULT_AUTO_ACTIVE_THRESHOLD,
                              llm_call):
    """一轮全自动提取。永不抛未处理异常，返回脱敏统计 dict（供日志/测试断言）。

    stats 字段：ok（本轮流程是否正常走完，含跳过）、skipped（并发防护跳过）、
    scanned/claimed/extracted/active/pending_review/duplicate_skipped/
    failed_events/remaining_pending（计数）、error_code（脱敏错误码或 None）。
    提取失败（LLM 错误/全部候选被拒）属正常收尾：事件标 failed、ok=True、
    error_code 携带原因；写入/去重失败属运行错误：事件释放回 pending、ok=False。"""
    stats = {"ok": False, "skipped": False, "error_code": None,
             "scanned": 0, "claimed": 0, "extracted": 0,
             "active": 0, "pending_review": 0, "duplicate_skipped": 0,
             "failed_events": 0, "remaining_pending": None}
    if not supabase_service:
        stats["error_code"] = CODE_SERVICE_UNAVAILABLE
        _log("service_role 客户端不可用，跳过本轮")
        return stats

    claimed_ids = []
    settled = False  # 已认领事件是否已收尾（processed/failed/释放）
    try:
        # 1. 并发防护（取批前）：同 user_id 已有 processing 中的非空批次 → 跳过本轮，
        #    防多进程/重入并发提取（本流程认领与收尾全部用原子条件 UPDATE）
        busy = await asyncio.to_thread(
            lambda: supabase_service.table("memory_events")
            .select("id")
            .eq("user_id", user_id)
            .eq("processing_status", "processing")
            .limit(1).execute())
        if getattr(busy, "data", None):
            stats["ok"] = True
            stats["skipped"] = True
            _log("检测到 processing 中的批次，跳过本轮（防并发重入）")
            return stats

        # 2. 取批：pending、created_at 升序（最旧优先）单调消化积压
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_events")
            .select("*")
            .eq("user_id", user_id)
            .eq("processing_status", "pending")
            .order("created_at", desc=False)
            .limit(int(batch_limit)).execute())
        events = [e for e in (getattr(res, "data", None) or [])
                  if isinstance(e, dict) and e.get("id")]
        stats["scanned"] = len(events)
        if not events:
            stats["ok"] = True
            stats["remaining_pending"] = await _count_pending(supabase_service, user_id)
            _log(f"无 pending 事件，本轮结束（剩余pending={stats['remaining_pending']}）")
            return stats

        # 3. 原子认领：pending → processing（条件 UPDATE，只认领仍为 pending 的行；
        #    若有并发方抢先认领部分事件，则只处理实际认领到的子集）
        ids = [str(e.get("id")) for e in events]
        claim_res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_events")
            .update({"processing_status": "processing"})
            .in_("id", ids)
            .eq("processing_status", "pending").execute())
        claimed_rows = getattr(claim_res, "data", None)
        if claimed_rows is None:
            claimed_ids = list(ids)  # 客户端未回传更新行时按全部认领处理
        else:
            claimed_ids = [str(r.get("id")) for r in claimed_rows
                           if isinstance(r, dict) and r.get("id")]
        stats["claimed"] = len(claimed_ids)
        claimed_set = set(claimed_ids)
        claimed_events = [e for e in events if str(e.get("id")) in claimed_set]
        if not claimed_events:
            stats["error_code"] = CODE_CLAIM_MISMATCH
            _log("认领数为 0（事件可能已被其他流程处理），本轮结束")
            return stats

        # 4. 提取（memory_extractor 永不抛异常、永不写库；llm_call 为同步 callable）
        ai_name = (os.environ.get("AI_NAME") or "").strip() or "助手"
        user_name = (os.environ.get("USER_NAME") or "").strip() or "用户"
        result = await mx.extract_memory_candidates(
            claimed_events, llm_call, user_id=user_id,
            ai_name=ai_name, user_name=user_name)
        if result.get("ok"):
            stats["extracted"] = len(result.get("candidates") or [])

        # 5. 分写 memory_items（高置信 active / 低置信 pending_review；跨批精确去重）
        write_error = None
        if result.get("ok"):
            try:
                counts = await _write_memory_items(
                    supabase_service, user_id, result.get("candidates") or [],
                    auto_active_threshold, result.get("batch_id"))
                stats["active"] = counts["active"]
                stats["pending_review"] = counts["pending_review"]
                stats["duplicate_skipped"] = counts["duplicate_skipped"]
            except _WriteStageError as we:
                write_error = we.code
                stats["error_code"] = we.code

        # 6. 事件状态收尾（三选一：processed / failed / 释放回 pending）
        if not result.get("ok"):
            stats["error_code"] = result.get("error_code") or "EXTRACTION_FAILED"
            await _finalize_events(supabase_service, claimed_events, ok=False,
                                   error_code=stats["error_code"],
                                   batch_id=result.get("batch_id"))
            settled = True
            stats["failed_events"] = len(claimed_ids)
        elif write_error is not None:
            await _release_events(supabase_service, claimed_ids)
            settled = True
        else:
            await _finalize_events(supabase_service, claimed_events, ok=True,
                                   error_code=None, batch_id=result.get("batch_id"))
            settled = True

        # 7. backlog 遥测：剩余 pending（只打计数，不打正文）
        stats["remaining_pending"] = await _count_pending(supabase_service, user_id)
        stats["ok"] = write_error is None
        _log(f"本轮完成: scanned={stats['scanned']} claimed={stats['claimed']} "
             f"extracted={stats['extracted']} active={stats['active']} "
             f"pending_review={stats['pending_review']} "
             f"duplicate={stats['duplicate_skipped']} "
             f"failed_events={stats['failed_events']} "
             f"剩余pending={stats['remaining_pending']}"
             + (f" error_code={stats['error_code']}" if stats["error_code"] else ""))
        return stats
    except Exception as e:  # noqa: BLE001 —— 兜底：未预期异常只记类型，绝不向上抛
        _log(f"本轮未预期异常: {type(e).__name__}")
        stats["error_code"] = CODE_UNEXPECTED
        if claimed_ids and not settled:
            # 已认领未收尾 → 释放回 pending，防事件卡死在 processing
            await _release_events(supabase_service, claimed_ids)
        return stats
