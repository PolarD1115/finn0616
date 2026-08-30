# -*- coding: utf-8 -*-
"""第 19 阶段 —— pending_review 只读管理与人工审批接口。

职责：供 gateway 的受保护 GET /api/memory-review 与 POST /api/memory-review/decision
调用——只读列出 memory_items 中 status=pending_review 的候选（最旧优先），
生成短时 review_session_token 缓存内部快照；用户逐条显式 approve 或 reject。
数据库操作仅限 memory_items 的 SELECT + 条件 UPDATE。

approve 语义：仅 pending_review → active（写 status/updated_at/last_confirmed_at）；
approve 前做两类只读冲突检查（同 user 的 active subject_key 冲突 / active 精确
hash 重复），命中即拒绝本次批准（不自动 supersede、不自动 rejected、不消费 index）。
reject 语义：仅 pending_review → rejected（只写 status/updated_at），绝不删除记录，
不写理由、不动 metadata、不动 last_confirmed_at / invalid_at。

边界红线（与第 10/17 阶段一脉相承）：
- 不触碰 memory_events / Pinecone / 旧 memories / user_facts；
- 不调用 LLM、不自动调度、不接入正式聊天上下文（本阶段 active 仍不进上下文）；
- 无 DELETE / UPSERT / DROP / TRUNCATE / 存储过程；
- 不新增环境变量；TTL 与容量均为代码常量；
- 客户端只见 review_index 与不透明 token，绝不返回数据库 item ID / user_id /
  content_hash / source_event_ids / source_batch_id / metadata；
- 请求体严格白名单（confirm / token / index / decision），客户端不提交、不修改
  候选正文，不提交 status / user_id / importance 等任何字段。

review session 为单进程内存缓存：多 worker、多实例或进程重启后 token 失效
（统一返回 REVIEW_SESSION_NOT_FOUND_OR_EXPIRED）；本阶段不引入 Redis 或新表。
缓存不持久化、不写入日志；清理策略为访问时惰性清理 + 超容量移除最旧（进程
内存清理，不是任何数据库删除）；不建立后台线程。
"""

import asyncio
import datetime
import secrets
import time

# ════════════════════════════════════════════════════════════
# 常量（代码常量，不新增环境变量）
# ════════════════════════════════════════════════════════════

REVIEW_TOKEN_TTL_SECONDS = 900        # 15 分钟
REVIEW_SESSION_MAX_ENTRIES = 20       # 超容量移除最旧 session
MAX_REVIEW_ITEMS_PER_SESSION = 20     # 单 session 最多候选数（= 列表 limit 上限）
DEFAULT_REVIEW_LIMIT = 20

CONFIRM_DECISION = "DECIDE_MEMORY_REVIEW"
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
_ALLOWED_DECISIONS = (DECISION_APPROVE, DECISION_REJECT)

PRIVACY_HINT = "REVIEW_REQUIRED"      # 固定隐私提示：不做自动敏感分类

CODE_NO_PENDING = "NO_PENDING_REVIEW_ITEMS"
CODE_READY = "REVIEW_ITEMS_READY"
CODE_QUERY_FAILED = "REVIEW_QUERY_FAILED"
CODE_UPDATE_FAILED = "REVIEW_UPDATE_FAILED"
CODE_STATE_CHANGED = "REVIEW_ITEM_STATE_CHANGED"
CODE_SUBJECT_CONFLICT = "ACTIVE_SUBJECT_CONFLICT"
CODE_EXACT_DUPLICATE = "ACTIVE_EXACT_DUPLICATE"
CODE_SESSION_NOT_FOUND = "REVIEW_SESSION_NOT_FOUND_OR_EXPIRED"
CODE_INDEX_NOT_FOUND = "REVIEW_INDEX_NOT_FOUND"
CODE_INDEX_ALREADY_DECIDED = "REVIEW_INDEX_ALREADY_DECIDED"
CODE_INVALID_REQUEST = "INVALID_REVIEW_REQUEST"
CODE_INVALID_DECISION = "INVALID_DECISION"
CODE_INTERNAL = "INTERNAL_ERROR"

# 列表 SELECT 字段：响应白名单字段 + 内部快照字段（后者绝不进响应）
_SELECT_COLUMNS = ("id,user_id,status,subject_key,content_hash,created_at,"
                   "memory_type,content,importance,confidence,source,"
                   "valid_at,invalid_at,expires_at")

# 列表响应白名单（无 id / user_id / content_hash / source_event_ids /
# source_batch_id / metadata / superseded_by / created_by / last_confirmed_at /
# updated_at / 任何 request·batch·event ID）
_ITEM_RESPONSE_FIELDS = ("memory_type", "content", "importance", "confidence",
                         "subject_key", "valid_at", "invalid_at", "expires_at",
                         "source", "created_at")

# 内部快照字段（仅存进程内缓存，供乐观条件更新与冲突检查使用）
_SNAPSHOT_FIELDS = ("item_id", "user_id", "status", "subject_key",
                    "content_hash", "created_at")

# 乐观条件更新条件列：id = 缓存快照 item_id 且 status 仍为 pending_review
_PENDING_STATUS = "pending_review"


# ════════════════════════════════════════════════════════════
# review session 进程内缓存（token → review_index → 内部快照）
# ════════════════════════════════════════════════════════════

_review_sessions = {}   # token -> {"items": {index: snapshot}, "decided": {index: decision},
                        #              "created_at": ts, "expires_at": ts}


def _purge_review_sessions(now=None):
    """惰性清理：移除过期 session + 超容量时移除最旧（仅进程内存，不触碰数据库）。"""
    now = time.time() if now is None else now
    for t in [t for t, s in _review_sessions.items() if s.get("expires_at", 0) <= now]:
        _review_sessions.pop(t, None)
    while len(_review_sessions) > REVIEW_SESSION_MAX_ENTRIES:
        oldest = min(_review_sessions.items(), key=lambda kv: kv[1].get("created_at", 0))[0]
        _review_sessions.pop(oldest, None)


def _store_review_session(snapshots):
    """缓存一次候选快照映射，返回不透明随机 token。
    token 用 secrets 随机源（不可预测），不含数据库 ID / user_id / hash / 正文。"""
    _purge_review_sessions()
    token = secrets.token_urlsafe(32)
    now_ts = time.time()
    _review_sessions[token] = {
        "items": dict(snapshots),
        "decided": {},
        "created_at": now_ts,
        "expires_at": now_ts + REVIEW_TOKEN_TTL_SECONDS,
    }
    while len(_review_sessions) > REVIEW_SESSION_MAX_ENTRIES:
        oldest = min(_review_sessions.items(), key=lambda kv: kv[1].get("created_at", 0))[0]
        _review_sessions.pop(oldest, None)
    return token


def _peek_review_session(token):
    """查询 session：不存在 / 过期 / 非字符串 → None（统一 NOT_FOUND 语义）。"""
    if not isinstance(token, str) or not token:
        return None
    _purge_review_sessions()
    return _review_sessions.get(token)


def _consume_review_index(token, session, review_index, decision):
    """成功决策后消费该 index；session 中全部 index 处理完则消费整个 token。"""
    session["items"].pop(review_index, None)
    session["decided"][review_index] = decision
    if not session["items"]:
        _review_sessions.pop(token, None)


# ════════════════════════════════════════════════════════════
# 响应构造（脱敏）
# ════════════════════════════════════════════════════════════

def _error_response(code, stats=None):
    """脱敏错误响应：只含 ok/code/stats 安全计数，绝不含 token/正文/ID/hash/
    user_id/SQL/数据库异常原文。"""
    return {"ok": False, "code": code, "stats": dict(stats or {})}


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ════════════════════════════════════════════════════════════
# 只读列表（GET /api/memory-review 的执行体）
# ════════════════════════════════════════════════════════════

async def run_list(supabase_service, limit=DEFAULT_REVIEW_LIMIT):
    """只读列出 pending_review 候选（最旧优先），生成 review session。

    supabase_service: server.supabase_service（service_role；只读使用）。
    limit: 1～20（gateway 已校验，此处再夹取防御）。
    返回：API 安全响应 dict——零写入、不返回数据库 ID 等内部字段。
    """
    limit = max(1, min(MAX_REVIEW_ITEMS_PER_SESSION, int(limit)))
    stats = {"count": 0}
    if supabase_service is None:
        return _error_response(CODE_QUERY_FAILED, stats)

    # 1. 只读查询 pending_review（最旧优先，只拉 limit 条；不查 active/rejected）
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items")
            .select(_SELECT_COLUMNS)
            .eq("status", _PENDING_STATUS)
            .order("created_at", desc=False)
            .limit(limit)
            .execute())
        rows = [r for r in (getattr(res, "data", None) or []) if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001 —— 数据库异常只返回脱敏代码
        print(f"⚠️ 记忆审核失败：stage=list_query error={type(e).__name__}")
        return _error_response(CODE_QUERY_FAILED, stats)

    stats["count"] = len(rows)
    if not rows:
        print("🔎 记忆待审核列表：count=0")
        return {"ok": True, "code": CODE_NO_PENDING, "stats": stats, "items": []}

    # 2. 构造脱敏响应条目 + 内部快照（client 只见白名单字段与 review_index）
    items, snapshots = [], {}
    for i, r in enumerate(rows, start=1):
        items.append({
            "review_index": i,
            **{f: r.get(f) for f in _ITEM_RESPONSE_FIELDS},
            "privacy_hint": PRIVACY_HINT,
        })
        snapshots[i] = {f: r.get(f) for f in _SNAPSHOT_FIELDS} | {
            "item_id": str(r.get("id")) if r.get("id") is not None else None}

    token = _store_review_session(snapshots)
    print(f"🔎 记忆待审核列表：count={len(items)}")

    return {
        "ok": True,
        "code": CODE_READY,
        "review_session_token": token,
        "expires_in_seconds": REVIEW_TOKEN_TTL_SECONDS,
        "stats": stats,
        "items": items,
    }


# ════════════════════════════════════════════════════════════
# 人工单条决策（POST /api/memory-review/decision 的执行体）
# ════════════════════════════════════════════════════════════
# 本区段是全模块唯一允许写库的地方，且数据库操作仅限：
#   memory_items : SELECT（approve 冲突检查）+ UPDATE（乐观条件更新，恰 1 行）
# 硬边界：不删除任何记录、不 UPSERT、不触碰 memory_events / Pinecone / 上下文；
# approve 只允许 pending_review → active；reject 只允许 pending_review → rejected；
# 已 active/rejected/superseded/expired 的记录不会被本接口再次修改（条件更新
# 包含 status=pending_review，返回 0 行即 REVIEW_ITEM_STATE_CHANGED，不消费 index）。

async def run_decision(supabase_service, review_session_token, review_index, decision):
    """人工单条决策执行器（gateway 的 /api/memory-review/decision 调用）。

    supabase_service: server.supabase_service（service_role；仅上表两种操作）。
    review_session_token: 列表接口返回的不透明凭证；候选定位全部取自进程内缓存。
    review_index: 1 起始整数（gateway 已校验类型，此处再防御）。
    decision: "approve" 或 "reject"（二选一，无默认，无批量）。

    执行顺序（任一步失败不消费 index，session 中其他 index 不受影响）：
      1. token / index / decision 校验（进程内，不查库）
      2. approve 专属只读冲突检查（active subject_key 冲突 / active 精确 hash 重复）
      3. 乐观条件 UPDATE（id=缓存 item_id 且 status=pending_review，返回必须恰 1 行）
      4. 仅成功后消费该 index（全部 index 处理完消费整个 token）
    返回 API 安全响应 dict（不回显 token/正文/ID/hash/user_id/SQL/异常原文）。
    """
    stats = {}
    if supabase_service is None:
        return _error_response(CODE_UPDATE_FAILED, stats)

    # 1a. decision 校验（模块层防御；gateway 已先行校验）
    if decision not in _ALLOWED_DECISIONS:
        return _error_response(CODE_INVALID_DECISION, stats)

    # 1b. session 校验（进程内缓存；不查库。不存在/过期/已全部处理 → 统一 NOT_FOUND）
    session = _peek_review_session(review_session_token)
    if not isinstance(session, dict):
        return _error_response(CODE_SESSION_NOT_FOUND, stats)

    # 1c. index 校验：必须是 int 且非 bool（gateway 已校验，此处防御直接调用）
    if isinstance(review_index, bool) or not isinstance(review_index, int):
        return _error_response(CODE_INVALID_REQUEST, stats)
    decided = session.get("decided") or {}
    if review_index in decided:
        return _error_response(CODE_INDEX_ALREADY_DECIDED, stats)
    snapshot = (session.get("items") or {}).get(review_index)
    if not isinstance(snapshot, dict):
        return _error_response(CODE_INDEX_NOT_FOUND, stats)
    if snapshot.get("status") != _PENDING_STATUS:
        # 缓存快照已不是 pending_review（理论上不会发生）：按状态已变化处理
        return _error_response(CODE_STATE_CHANGED, stats)

    item_id = snapshot.get("item_id")
    user_id = snapshot.get("user_id")
    if not item_id or not user_id:
        return _error_response(CODE_INTERNAL, stats)

    # 2. approve 专属只读冲突检查（reject 不需要：reject 不产生 active 事实）
    if decision == DECISION_APPROVE:
        subject_key = snapshot.get("subject_key")
        content_hash = snapshot.get("content_hash")

        # 2a. 同 user + subject_key 的 active 记录冲突（不含自身）。
        #     命中即拒绝批准：不自动 supersede 旧 active、不返回旧记录任何内容。
        if subject_key:
            try:
                res = await asyncio.to_thread(
                    lambda: supabase_service.table("memory_items")
                    .select("id")
                    .eq("user_id", user_id)
                    .eq("subject_key", subject_key)
                    .eq("status", "active")
                    .neq("id", item_id)
                    .limit(1)
                    .execute())
                conflicts = [r for r in (getattr(res, "data", None) or [])
                             if isinstance(r, dict)]
            except Exception as e:  # noqa: BLE001
                print(f"⚠️ 记忆审核失败：stage=conflict_check error={type(e).__name__}")
                return _error_response(CODE_QUERY_FAILED, stats)
            if conflicts:
                print(f"⚠️ 记忆审核冲突：stage=subject_conflict count={len(conflicts)}")
                return _error_response(CODE_SUBJECT_CONFLICT, stats)

        # 2b. 同 user + content_hash 的 active 精确重复（不含自身）。
        #     命中即拒绝批准：不自动 rejected 当前项、不删除任何记录。
        if content_hash:
            try:
                res = await asyncio.to_thread(
                    lambda: supabase_service.table("memory_items")
                    .select("id")
                    .eq("user_id", user_id)
                    .eq("content_hash", content_hash)
                    .eq("status", "active")
                    .neq("id", item_id)
                    .limit(1)
                    .execute())
                dupes = [r for r in (getattr(res, "data", None) or [])
                         if isinstance(r, dict)]
            except Exception as e:  # noqa: BLE001
                print(f"⚠️ 记忆审核失败：stage=duplicate_check error={type(e).__name__}")
                return _error_response(CODE_QUERY_FAILED, stats)
            if dupes:
                print(f"⚠️ 记忆审核冲突：stage=exact_duplicate count={len(dupes)}")
                return _error_response(CODE_EXACT_DUPLICATE, stats)

    # 3. 乐观条件更新：id = 缓存 item_id 且 status 仍为 pending_review。
    #    返回行数必须恰为 1；0 行 = 状态已被其他流程改变 → 不消费 index。
    now_iso = _utc_now_iso()
    if decision == DECISION_APPROVE:
        payload = {"status": "active", "updated_at": now_iso,
                   "last_confirmed_at": now_iso}
        ok_code, new_status = "MEMORY_APPROVED", "active"
    else:
        payload = {"status": "rejected", "updated_at": now_iso}
        ok_code, new_status = "MEMORY_REJECTED", "rejected"
    try:
        res = await asyncio.to_thread(
            lambda: supabase_service.table("memory_items")
            .update(payload)
            .eq("id", item_id)
            .eq("status", _PENDING_STATUS)
            .execute())
        updated = len([r for r in (getattr(res, "data", None) or [])
                       if isinstance(r, dict)])
    except Exception as e:  # noqa: BLE001 —— 只记异常类型，不外泄数据库异常原文
        print(f"⚠️ 记忆审核失败：stage=update error={type(e).__name__}")
        return _error_response(CODE_UPDATE_FAILED, stats)
    if updated != 1:
        print(f"⚠️ 记忆审核失败：stage=update error=state_changed rows={updated}")
        return _error_response(CODE_STATE_CHANGED, stats)

    # 4. 成功 → 消费该 index；全部 index 处理完则消费整个 token
    _consume_review_index(review_session_token, session, review_index, decision)
    print(f"✅ 记忆审核完成：decision={decision} new_status={new_status}")
    return {
        "ok": True,
        "code": ok_code,
        "result": {"review_index": review_index, "new_status": new_status},
    }
