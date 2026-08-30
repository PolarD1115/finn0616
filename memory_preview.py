# -*- coding: utf-8 -*-
"""第 10 阶段 —— 手动事实提取预览（只读、零写入）＋ 第 17 阶段人工提交执行器。

职责：供 gateway 的受保护 POST /api/memory-extraction-preview 接口调用——
只读选择少量 pending Web memory_events → 敏感内容筛查 → 复用
memory_extractor.extract_memory_candidates（compression ≤1 次）→
组装脱敏的候选预览响应。

第 17 阶段新增 commit 写入执行器（两步人工确认的第二步）：
- PREVIEW_READY 时生成一次性 preview_token，进程内缓存完整内部预览上下文
  （TTL/容量为代码常量、访问时惰性清理；不持久化、不入日志、重启即失效）；
- run_commit 只信服务端缓存候选：memory_items 仅查询＋逐条插入（强制
  status=pending_review / created_by=memory_extractor），memory_events 仅
  条件更新为 processed；全部成功才消费 token；失败不消费、不做补偿删除，
  依赖精确去重实现幂等重试。

预览零写入红线：run_preview 只执行 .select() 查询；不写 memory_items、不更新
memory_events 任何字段、不执行状态计划、不触碰 Pinecone、不自动调度。
响应不包含事件 ID、user_id、source_event_id、content_hash、batch_id、
metadata、Prompt 或模型原始响应。
"""

import asyncio
import datetime
import re
import secrets
import time

import memory_extractor as mx

MAX_PREVIEW_EVENTS = 10
MIN_PREVIEW_EVENTS = 2
SELECT_WINDOW = 30  # 只读查询窗口（最近 N 条 pending Web 事件，在内存中分组选择）

CODE_NO_PENDING = "NO_PENDING_EVENTS"
CODE_NO_COMPLETE_GROUPS = "NO_COMPLETE_EVENT_GROUPS"
CODE_SENSITIVE_BATCH = "SENSITIVE_BATCH_SKIPPED"
CODE_SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
CODE_DB_ERROR = "DB_ERROR"
QUALITY_HINT = "NEEDS_HUMAN_REVIEW"

# ════════════════════════════════════════════════════════════
# 第 17 阶段 —— preview_token 进程内短期缓存
# ════════════════════════════════════════════════════════════
# 仅适用于单 Web 进程部署：多 worker 或请求路由到不同实例时 token 可能找不到，
# 此时返回 PREVIEW_TOKEN_NOT_FOUND_OR_EXPIRED；本阶段不为此引入 Redis 或新表。
# 缓存不持久化到数据库或文件、不写入日志；进程重启后自然失效。
# 清理策略：每次访问惰性清理过期项；超容量时移除最旧缓存项（进程内存清理，
# 不是任何数据库删除）；不建立后台清理线程。

PREVIEW_TOKEN_TTL_SECONDS = 900   # 15 分钟（代码常量，不新增环境变量）
PREVIEW_CACHE_MAX_ENTRIES = 20    # 超容量时移除最旧缓存项

_preview_cache = {}        # token -> 完整内部预览上下文（未消费）
_preview_used_tokens = {}  # token -> expires_at（已成功消费的墓碑，仅用于区分"已用"与"不存在"）
_commit_inflight = set()   # 正在执行中的 token（防并发双击重复写入；结束即移除）
_TOKEN_USED = object()     # peek 哨兵：token 已被成功 commit 消费


def _purge_preview_caches(now=None):
    """惰性清理：移除过期项 + 超容量时移除最旧（仅进程内存，不触碰数据库）。"""
    now = time.time() if now is None else now
    for t in [t for t, e in _preview_cache.items() if e.get("expires_at", 0) <= now]:
        _preview_cache.pop(t, None)
    for t in [t for t, exp in _preview_used_tokens.items() if exp <= now]:
        _preview_used_tokens.pop(t, None)
    while len(_preview_cache) > PREVIEW_CACHE_MAX_ENTRIES:
        oldest = min(_preview_cache.items(), key=lambda kv: kv[1].get("created_at", 0))[0]
        _preview_cache.pop(oldest, None)


def _store_preview_entry(entry):
    """缓存一次 PREVIEW_READY 的完整内部上下文，返回不透明随机 token。
    token 用 secrets 随机源（不可预测），不含 user_id / 事件 ID / hash / 正文。"""
    _purge_preview_caches()
    token = secrets.token_urlsafe(32)
    _preview_cache[token] = entry
    while len(_preview_cache) > PREVIEW_CACHE_MAX_ENTRIES:
        oldest = min(_preview_cache.items(), key=lambda kv: kv[1].get("created_at", 0))[0]
        _preview_cache.pop(oldest, None)
    return token


def _peek_preview_entry(token):
    """查询 token：返回缓存 dict / _TOKEN_USED（已被成功消费）/ None（不存在或已过期）。"""
    if not isinstance(token, str) or not token:
        return None
    _purge_preview_caches()
    if token in _preview_used_tokens:
        return _TOKEN_USED
    return _preview_cache.get(token)


def _consume_preview_entry(token):
    """成功 commit 后消费 token（移入墓碑，随 TTL 惰性清除）。失败路径绝不调用。"""
    entry = _preview_cache.pop(token, None)
    if entry is not None:
        _preview_used_tokens[token] = entry.get(
            "expires_at", time.time() + PREVIEW_TOKEN_TTL_SECONDS)
    return entry

# 敏感内容筛查（命中任一 → 整批跳过，不调用模型；只返回计数不返回命中内容）
_SENSITIVE_RULES = (
    ("CREDENTIAL_PATTERN", re.compile(
        r"(?i)(password|passwd|secret|密码|api[_-]?key\s*[:=]|bearer\s+[a-z0-9]|authorization\s*:)")),
    ("ID_CARD_PATTERN", re.compile(r"\d{17}[\dXx]|\b\d{15}\b")),
    ("BANK_CARD_PATTERN", re.compile(r"\b\d{16,19}\b")),
)

_ROLE_SUFFIX_RE = re.compile(r":(user|assistant)$")

_ALLOWED_ROLES = ("user", "assistant")


def _has_valid_suffix(source_event_id):
    return isinstance(source_event_id, str) and bool(_ROLE_SUFFIX_RE.search(source_event_id))


def _request_prefix(source_event_id):
    return _ROLE_SUFFIX_RE.sub("", source_event_id)


def select_preview_events(rows, limit):
    """从最近窗口的 pending Web 事件中选择完整请求组。

    rows: 按 created_at 降序的事件 dict 列表（调用方已限定 channel=web、pending）。
    返回 (selected_events, stats)：
      stats = {"scanned": n, "illegal_suffix_skipped": n, "other_user_skipped": n,
               "incomplete_group_skipped": n, "user_events": n, "assistant_events": n,
               "distinct_users": n, "groups_selected": n}
    规则：不拆散完整请求组；单用户（最近完整组所属用户）；累计 ≤ limit；
    非法后缀跳过、其他用户跳过、放不下/不完整的组跳过并全部计数。
    """
    stats = {"scanned": len(rows), "illegal_suffix_skipped": 0, "other_user_skipped": 0,
             "incomplete_group_skipped": 0, "user_events": 0, "assistant_events": 0,
             "distinct_users": 0, "groups_selected": 0}
    legal = [r for r in rows if isinstance(r, dict)
             and r.get("role") in _ALLOWED_ROLES and _has_valid_suffix(r.get("source_event_id"))]
    stats["illegal_suffix_skipped"] = len(rows) - len(legal)
    if not legal:
        return [], stats

    # 按 (user_id, 请求前缀) 分组；first_idx 越小 = 时间越新（rows 时间降序）
    user_groups = {}
    for idx, r in enumerate(legal):
        uid = r.get("user_id")
        req = _request_prefix(r["source_event_id"])
        ug = user_groups.setdefault(uid, {})
        g = ug.setdefault(req, {"events": [], "has_user": False,
                                "has_assistant": False, "first_idx": idx})
        g["events"].append(r)
        if r.get("role") == "user":
            g["has_user"] = True
        else:
            g["has_assistant"] = True

    # 完整组按新到旧排序；owner = 最近完整组所属用户（任务 §七.9）
    complete = []
    for uid, ug in user_groups.items():
        for req, g in ug.items():
            if g["has_user"] and g["has_assistant"]:
                complete.append((g["first_idx"], uid, req, g))
    complete.sort(key=lambda x: x[0])
    if not complete:
        stats["other_user_skipped"] = len(legal)
        return [], stats
    owner = complete[0][1]
    stats["distinct_users"] = 1

    selected = []
    selected_reqs = set()
    for _, uid, req, g in complete:
        if uid != owner:
            continue
        if len(selected) + len(g["events"]) > limit:
            continue
        selected.extend(g["events"])
        selected_reqs.add(req)
        stats["groups_selected"] += 1

    # 跳过分类：非 owner 用户的合法事件 → other_user_skipped；
    # owner 未选中的组（不完整或放不下）→ incomplete_group_skipped
    for uid, ug in user_groups.items():
        for req, g in ug.items():
            if uid != owner:
                stats["other_user_skipped"] += len(g["events"])
            elif req not in selected_reqs:
                stats["incomplete_group_skipped"] += len(g["events"])
    stats["user_events"] = sum(1 for e in selected if e.get("role") == "user")
    stats["assistant_events"] = sum(1 for e in selected if e.get("role") == "assistant")
    return selected, stats


def screen_sensitive_events(events):
    """敏感内容筛查。返回 (clean_events, hit_counts)；
    hit_counts 为 {代码: 命中行数}，只返回计数与代码，不返回命中内容。"""
    clean, hits = [], {}
    for e in events:
        content = str(e.get("content", ""))
        hit_code = None
        for code, pattern in _SENSITIVE_RULES:
            if pattern.search(content):
                hit_code = code
                break
        if hit_code:
            hits[hit_code] = hits.get(hit_code, 0) + 1
        else:
            clean.append(e)
    return clean, hits


def _write_guards():
    return {"memory_items_written": False, "memory_events_updated": False,
            "pinecone_touched": False}


def _error_response(code, stats, extra=None):
    resp = {"ok": False, "code": code, "stats": stats, "candidates": [],
            "rejected": [], "status_plan": {"simulated_status": None,
                                            "event_count": 0, "executed": False},
            "write_guards": _write_guards()}
    if extra:
        for k, v in extra.items():
            resp[k] = v
    return resp


async def run_preview(supabase_service, limit=MAX_PREVIEW_EVENTS,
                      ai_name="助手", user_name="用户", llm_call=None):
    """预览主入口（async；gateway 的 async 处理函数直接 await）。

    supabase_service: server.supabase_service（只读使用，绝不调用其写入方法）。
    llm_call: 可注入的模型调用（测试用 mock）；None 时使用
              memory_extractor.make_compression_llm_call()（真实 compression）。
    返回：API 安全响应 dict——零写入、脱敏。"""
    limit = max(MIN_PREVIEW_EVENTS, min(MAX_PREVIEW_EVENTS, int(limit)))
    stats = {"selected_events": 0, "user_events": 0, "assistant_events": 0,
             "candidate_count": 0, "rejected_count": 0}
    if supabase_service is None:
        return _error_response(CODE_SERVICE_UNAVAILABLE, stats)

    # 1. 只读查询最近窗口（仅 select；绝不 insert/update/delete/rpc）
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_events")
            .select("id,user_id,session_id,channel,role,content,occurred_at,"
                    "created_at,source_event_id,processing_status,attempt_count,metadata")
            .eq("channel", "web").eq("processing_status", "pending")
            .in_("role", list(_ALLOWED_ROLES))
            .order("created_at", desc=True).limit(SELECT_WINDOW).execute())
        rows = list(getattr(res, "data", None) or [])
    except Exception as e:  # noqa: BLE001 —— 数据库异常只返回脱敏代码
        print(f"🧪 记忆提取预览失败：code={CODE_DB_ERROR} type={type(e).__name__}")
        return _error_response(CODE_DB_ERROR, stats)

    if not rows:
        print("🧪 记忆提取预览：无 pending Web 事件")
        return _error_response(CODE_NO_PENDING, stats)

    # 2. 成组选择
    selected, sel_stats = select_preview_events(rows, limit)
    stats["selected_events"] = len(selected)
    stats["user_events"] = sel_stats["user_events"]
    stats["assistant_events"] = sel_stats["assistant_events"]
    if not selected:
        print("🧪 记忆提取预览：无完整请求组")
        return _error_response(CODE_NO_COMPLETE_GROUPS, stats)

    # 3. 敏感内容筛查（命中 → 整批跳过，不调用模型）
    clean_events, hits = screen_sensitive_events(selected)
    if len(clean_events) < len(selected):
        print(f"🧪 记忆提取预览：敏感批次跳过 hits={sum(hits.values())}")
        resp = _error_response(CODE_SENSITIVE_BATCH, stats)
        resp["stats"]["sensitive_events"] = len(selected) - len(clean_events)
        resp["stats"]["sensitive_codes"] = sorted(hits.keys())
        return resp

    # 4. 提取（复用第 5 阶段提取器；compression ≤1 次；失败零写入）
    if llm_call is None:
        llm_call = mx.make_compression_llm_call()
    result = await mx.extract_memory_candidates(
        clean_events, llm_call, user_id=clean_events[0].get("user_id"),
        ai_name=ai_name, user_name=user_name)

    # 🔒 提取失败（LLM 异常/空响应/解析失败/全部候选被拒）必须映射为失败响应，
    #    绝不包装成 PREVIEW_READY——否则调用方无法区分"无候选"与"提取失败"。
    if not result.get("ok"):
        code = result.get("error_code") or "EXTRACTION_FAILED"
        print(f"🧪 记忆提取预览失败：code={code}")
        resp = _error_response(code, stats)
        resp["rejected"] = [{"reason_code": c} for c in result.get("rejected", [])]
        return resp

    # 5. 组装脱敏响应（绝不返回 ID/user_id/hash/batch/Prompt/原始响应）
    stats["candidate_count"] = len(result["candidates"])
    stats["rejected_count"] = len(result["rejected"])
    candidates = []
    for i, c in enumerate(result["candidates"]):
        candidates.append({
            "preview_index": i + 1,
            "memory_type": c.get("memory_type"),
            "content": c.get("content"),
            "importance": c.get("importance"),
            "confidence": c.get("confidence"),
            "subject_key": c.get("subject_key"),
            "valid_at": c.get("valid_at"),
            "invalid_at": c.get("invalid_at"),
            "expires_at": c.get("expires_at"),
            "status": c.get("status"),
            "quality_hint": QUALITY_HINT,
        })
    print(f"🧪 记忆提取预览：候选={len(candidates)} 拒绝={len(result['rejected'])}")

    # 🆕 第 17 阶段：仅 PREVIEW_READY 生成一次性 preview_token 并缓存完整内部
    #    上下文，供 /api/memory-extraction-commit 使用；失败响应一律不发 token。
    #    缓存内容（候选原文/来源 ID/hash/batch/user_id/各事件原始 attempt_count）
    #    绝不返回给客户端、绝不写入日志。
    now_ts = time.time()
    entry = {
        "candidates": list(result["candidates"]),   # 内部完整候选（供 commit 落库）
        "batch_event_ids": [str(eid) for eid in result["status_plan"]["event_ids"]],
        "event_attempt_counts": {str(e.get("id")): int(e.get("attempt_count") or 0)
                                 for e in selected},
        "source_batch_id": str(result["batch_id"]),
        "user_id": clean_events[0].get("user_id"),
        "created_at": now_ts,
        "expires_at": now_ts + PREVIEW_TOKEN_TTL_SECONDS,
    }
    token = _store_preview_entry(entry)
    print(f"🧪 记忆提取预览：已缓存人工提交上下文（{PREVIEW_TOKEN_TTL_SECONDS // 60} 分钟内一次性有效）")

    resp = {
        "ok": True,
        "code": "PREVIEW_READY",
        "stats": stats,
        "candidates": candidates,
        "rejected": [{"reason_code": code} for code in result["rejected"]],
        "status_plan": {
            "simulated_status": result["status_plan"]["processing_status"],
            "event_count": len(result["status_plan"]["event_ids"]),
            "executed": False,
        },
        "write_guards": _write_guards(),
    }
    resp["preview_token"] = token
    resp["expires_in_seconds"] = PREVIEW_TOKEN_TTL_SECONDS
    return resp


# ════════════════════════════════════════════════════════════
# 第 17 阶段：人工确认写入执行器（两步提交的第二步，commit）
# ════════════════════════════════════════════════════════════
# 本区段是全模块唯一允许写库的地方，且数据库操作仅限：
#   memory_items : SELECT（精确去重） + INSERT（逐条，失败即停）
#   memory_events: UPDATE（条件更新为 processed）
# 硬边界：不重新调用模型、不触碰 Pinecone、不删除任何数据、不使用 UPSERT 或
# 存储过程、不自动调度、不接入正式上下文。status/created_by 服务端强制覆盖，
# 客户端提交的任何候选内容（正文/类型/时间/来源）一律不信任——候选只来自
# preview_token 对应的服务端缓存。

CODE_COMMIT_COMPLETED = "COMMIT_COMPLETED"
CODE_DEDUP_FAILED = "DEDUP_CHECK_FAILED"
CODE_INSERT_FAILED = "MEMORY_ITEM_INSERT_FAILED"
CODE_EVENT_UPDATE_FAILED = "EVENT_STATUS_UPDATE_FAILED"
CODE_TOKEN_NOT_FOUND = "PREVIEW_TOKEN_NOT_FOUND_OR_EXPIRED"
CODE_TOKEN_USED = "PREVIEW_TOKEN_ALREADY_USED"
CODE_INVALID_SELECTION = "INVALID_SELECTION"

# 跨批精确去重只看这两种状态（不查 superseded/expired/rejected，不做语义近似，
# 不刷新 last_confirmed_at、不合并来源、不写替代链——本阶段保持简单可重试）
_DEDUP_STATUSES = ("pending_review", "active")

# memory_items 写入字段（与第 4 阶段真实表结构一一对应；
# id / created_at / updated_at 走数据库默认，绝不写入不存在的字段）
_MEMORY_ITEM_FIELDS = ("user_id", "memory_type", "content", "content_hash", "status",
                       "importance", "confidence", "source", "source_event_ids",
                       "source_batch_id", "subject_key", "valid_at", "invalid_at",
                       "expires_at", "last_confirmed_at", "superseded_by", "metadata",
                       "created_by")


def _commit_error(code, stats):
    """脱敏错误响应：只含 ok/code/stats 计数，绝不含 token/正文/ID/hash/异常原文。"""
    return {"ok": False, "code": code, "stats": dict(stats)}


def _memory_item_row(cand):
    """把服务端缓存中的内部候选转换为 memory_items 插入行。
    status 与 created_by 在此强制覆盖，与缓存取值无关、客户端更不可控。"""
    row = {
        "user_id": cand.get("user_id"),
        "memory_type": cand.get("memory_type"),
        "content": cand.get("content"),
        "content_hash": cand.get("content_hash"),
        "status": "pending_review",           # 🔒 强制覆盖
        "importance": cand.get("importance"),
        "confidence": cand.get("confidence"),
        "source": cand.get("source"),
        "source_event_ids": list(cand.get("source_event_ids") or []),
        "source_batch_id": cand.get("source_batch_id"),
        "subject_key": cand.get("subject_key"),
        "valid_at": cand.get("valid_at"),
        "invalid_at": cand.get("invalid_at"),
        "expires_at": cand.get("expires_at"),
        "last_confirmed_at": cand.get("last_confirmed_at"),
        "superseded_by": cand.get("superseded_by"),
        "metadata": cand.get("metadata") if isinstance(cand.get("metadata"), dict) else {},
        "created_by": "memory_extractor",     # 🔒 强制覆盖
    }
    return {k: row[k] for k in _MEMORY_ITEM_FIELDS}


async def run_commit(supabase_service, preview_token, selected_preview_indexes,
                     reviewed_all):
    """人工确认写入执行器（gateway 的 /api/memory-extraction-commit 调用）。

    supabase_service: server.supabase_service（service_role；只用于上表两种操作）。
    preview_token:    预览返回的一次性凭证；候选数据全部取自进程内缓存。
    selected_preview_indexes: 用户明确选中的 preview_index（1 起始整数、不重复）；
                      未选中的候选一律不写入（服务端绝不自动全选）。
    reviewed_all:     必须严格为 True——语义为「用户已审核整批候选；未选中者视为
                      本轮人工不采纳，不写入；本批事件无需再次自动提取」。

    执行顺序（任一步失败立即停止：不更新事件、不消费 token、不做补偿删除；
    已成功插入的条目保留，重试时经精确去重跳过——幂等重试设计）：
      1. 校验 reviewed_all / token / 人工选择（进程内，不查库）
      2. memory_items 精确去重（user_id + content_hash，含 pending_review/active）
      3. 逐条 INSERT 非重复候选（强制 pending_review）
      4. 确认全部选中候选「已插入或精确重复」
      5. 条件更新本批全部 memory_events 为 processed（返回行数必须等于整批数）
      6. 仅步骤 5 成功后消费 token
    返回 API 安全响应 dict（不回显 token/正文/ID/hash/batch/user_id/Prompt）。
    """
    stats = {"selected": 0, "inserted": 0, "duplicate_skipped": 0,
             "unselected_rejected": 0, "events_processed": 0}

    # 0. 人工整批审核确认（缺 reviewed_all=true 视为未完成人工审核）
    if reviewed_all is not True:
        return _commit_error(CODE_INVALID_SELECTION, stats)

    if supabase_service is None:
        return _commit_error(CODE_SERVICE_UNAVAILABLE, stats)

    # 1. token 校验（进程内缓存；不查库。不存在/过期 → NOT_FOUND；已成功消费 → USED）
    peeked = _peek_preview_entry(preview_token)
    if peeked is _TOKEN_USED:
        return _commit_error(CODE_TOKEN_USED, stats)
    entry = peeked
    if not isinstance(entry, dict):
        return _commit_error(CODE_TOKEN_NOT_FOUND, stats)

    candidates = entry.get("candidates") or []
    batch_event_ids = [str(e) for e in (entry.get("batch_event_ids") or [])]
    if not candidates or not batch_event_ids:
        return _commit_error("INTERNAL_ERROR", stats)

    idx_list = selected_preview_indexes
    if (not isinstance(idx_list, list) or not idx_list
            or any(isinstance(i, bool) or not isinstance(i, int) for i in idx_list)
            or len(set(idx_list)) != len(idx_list)
            or any(not (1 <= i <= len(candidates)) for i in idx_list)):
        return _commit_error(CODE_INVALID_SELECTION, stats)

    selected = [candidates[i - 1] for i in idx_list]
    entry_user_id = entry.get("user_id")
    if any(not isinstance(c, dict) or c.get("user_id") != entry_user_id
           or not c.get("content_hash") for c in selected):
        return _commit_error("INTERNAL_ERROR", stats)
    if len({c.get("content_hash") for c in selected}) != len(selected):
        # 选中候选出现重复 hash（提取器批内去重本应保证唯一）→ 数据不完整，拒绝落库
        return _commit_error("INTERNAL_ERROR", stats)
    stats["selected"] = len(selected)
    stats["unselected_rejected"] = len(candidates) - len(selected)

    # 防并发双击：同 token 的第二个并发请求按"已用"处理（结束即释放，不影响重试）
    if preview_token in _commit_inflight:
        return _commit_error(CODE_TOKEN_USED, stats)
    _commit_inflight.add(preview_token)
    try:
        return await _run_commit_locked(supabase_service, preview_token, entry,
                                        selected, batch_event_ids, stats)
    finally:
        _commit_inflight.discard(preview_token)


async def _run_commit_locked(supabase_service, preview_token, entry, selected,
                             batch_event_ids, stats):
    """run_commit 的实际写库流程（调用方已持有 inflight 标记）。"""
    # 2. 跨批精确去重：user_id + content_hash 精确匹配（一次批量查询）；
    #    查询失败视为 commit 失败——不插入、不更新事件、不消费 token
    entry_user_id = entry.get("user_id")
    hashes = sorted({str(c.get("content_hash")) for c in selected})
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items")
            .select("content_hash,status")
            .eq("user_id", entry_user_id)
            .in_("content_hash", hashes)
            .in_("status", list(_DEDUP_STATUSES))
            .execute())
        existing = {str(r.get("content_hash"))
                    for r in (getattr(res, "data", None) or []) if isinstance(r, dict)}
    except Exception as e:  # noqa: BLE001 —— 只记异常类型，不外泄数据库异常原文
        print(f"⚠️ 记忆人工提交失败：stage=dedup error={type(e).__name__}")
        return _commit_error(CODE_DEDUP_FAILED, stats)

    to_insert = [c for c in selected if str(c.get("content_hash")) not in existing]
    stats["duplicate_skipped"] = len(selected) - len(to_insert)

    # 3. 逐条 INSERT 非重复候选（任一失败立即停止：已成功项保留，供幂等重试）
    inserted = 0
    for cand in to_insert:
        row = _memory_item_row(cand)
        try:
            res = await asyncio.to_thread(
                lambda r=row: supabase_service.table("memory_items").insert(r).execute())
            if not (getattr(res, "data", None) or []):
                raise RuntimeError("empty insert result")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 记忆人工提交失败：stage=insert error={type(e).__name__}")
            stats["inserted"] = inserted
            return _commit_error(CODE_INSERT_FAILED, stats)
        inserted += 1
    stats["inserted"] = inserted

    # 4+5. 全部选中候选「已插入或精确重复」后，条件更新本批全部事件。
    #      按缓存中的原始 attempt_count 分组（同值一组 → 单条语句原子更新，
    #      避免先读后盲写）；更新条件含「仍为 pending」，绝不覆盖其他流程
    #      已处理的事件；返回行数总和必须等于本批事件数，少于即失败。
    processed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    attempt_map = entry.get("event_attempt_counts") or {}
    groups = {}
    for eid in batch_event_ids:
        groups.setdefault(int(attempt_map.get(eid, 0)), []).append(eid)
    updated = 0
    try:
        for orig, ids in sorted(groups.items()):
            res = await asyncio.to_thread(
                lambda o=orig, ids=ids: supabase_service.table("memory_events")
                .update({"processing_status": "processed",
                         "processed_at": processed_at,
                         "batch_id": entry.get("source_batch_id"),
                         "last_error": None,
                         "attempt_count": o + 1})
                .in_("id", ids)
                .eq("processing_status", "pending")
                .execute())
            updated += len(getattr(res, "data", None) or [])
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 记忆人工提交失败：stage=event_update error={type(e).__name__}")
        return _commit_error(CODE_EVENT_UPDATE_FAILED, stats)
    if updated != len(batch_event_ids):
        print("⚠️ 记忆人工提交失败：stage=event_update error=count_mismatch")
        return _commit_error(CODE_EVENT_UPDATE_FAILED, stats)
    stats["events_processed"] = len(batch_event_ids)

    # 6. 全部成功 → 消费 token（唯一消费点；失败路径 token 保留以便安全重试）
    _consume_preview_entry(preview_token)
    print(f"✍️ 记忆人工提交：selected={stats['selected']} inserted={inserted} "
          f"duplicate={stats['duplicate_skipped']} events={len(batch_event_ids)}")
    return {
        "ok": True,
        "code": CODE_COMMIT_COMPLETED,
        "stats": stats,
        "write_result": {"memory_items_status": "pending_review",
                         "memory_events_status": "processed"},
    }
