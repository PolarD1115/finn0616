# -*- coding: utf-8 -*-
"""第 10 阶段 —— 手动事实提取预览（只读、零写入）。

职责：供 gateway 的受保护 POST /api/memory-extraction-preview 接口调用——
只读选择少量 pending Web memory_events → 敏感内容筛查 → 复用
memory_extractor.extract_memory_candidates（compression ≤1 次）→
组装脱敏的候选预览响应。

零写入红线：本模块只执行 .select() 查询；不写 memory_items、不更新
memory_events 任何字段、不执行状态计划、不触碰 Pinecone、不自动调度。
响应不包含事件 ID、user_id、source_event_id、content_hash、batch_id、
metadata、Prompt 或模型原始响应。
"""

import asyncio
import re

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
                    "created_at,source_event_id,processing_status,metadata")
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

    return {
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
