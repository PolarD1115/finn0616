# -*- coding: utf-8 -*-
"""
home/activity_log.py — 阶段 C3：结构化行动日志（activity_logs）最小封装。

职责：
- 顶层自主活动开始前建立 running 记录（start-before-side-effect）；
- 结束后按真实业务结果 finalize（succeeded/observed/partial/failed/skipped）；
- tools_used 安全归一化：只保留 {name, ok, status, error_code}，
  不保存参数、UUID、action_key、原始返回与任何正文；
- thought_summary 清洗：只保留可展示的普通文本摘要，含 <think>/<thinking>
  等思维链载体的输入整体丢弃——绝不保存模型隐藏思维链。

写入全部走 service_role 客户端（复用 home.repository._get_supabase_service）；
activity_logs 表 RLS deny-by-default 且已 REVOKE anon/authenticated 全部权限。
所有函数返回统一 dict（{"ok": bool, ...}），不向上抛异常，不后台崩溃。
"""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 允许的最终状态（running 只在 start 时写入，finalize 不接受）
_FINAL_STATUSES = ("succeeded", "observed", "partial", "failed", "skipped")
_THOUGHT_MAX_LEN = 500
_RESULT_MAX_LEN = 1000
_TOOLS_MAX_ITEMS = 20          # ≥ MAX_TOOL_CALLS(5) 的硬上限
_TOOL_NAME_MAX_LEN = 60
_ERROR_CODE_MAX_LEN = 60

# 思维链载体标记（命中即整体丢弃，不提取标签内容）
_THOUGHT_FORBIDDEN_MARKERS = (
    "<think", "</think", "<thinking", "</thinking", "reasoning_content",
)


def _get_service_client():
    """懒加载 service_role 客户端（复用 home.repository 的实现）。"""
    try:
        from home.repository import _get_supabase_service
        return _get_supabase_service()
    except Exception as e:
        logger.warning("activity_log: service 客户端获取失败: %s", e)
        return None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_thought_summary(value) -> str:
    """thought_summary 清洗：只保留可展示的普通文本摘要。

    - 非字符串 → 空串；
    - 含 <think>/<thinking>/reasoning_content 等载体 → 整体丢弃（不提取标签内容）；
    - 去首尾空白；超长截断到 500 字。
    """
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if not v:
        return ""
    lowered = v.lower()
    for marker in _THOUGHT_FORBIDDEN_MARKERS:
        if marker in lowered:
            return ""
    return v[:_THOUGHT_MAX_LEN]


def sanitize_tools_used(items, max_items: int = _TOOLS_MAX_ITEMS) -> list:
    """tools_used 安全归一化：每项只保留 {name, ok, status, error_code}。

    - 非 dict 项跳过；无有效 name 的项跳过；
    - status 仅接受 succeeded/failed/skipped，否则按 ok 推断；
    - 超过 max_items 截断；未知字段一律丢弃。
    """
    out = []
    if not isinstance(items, list):
        return out
    for it in items:
        if len(out) >= max_items:
            break
        if not isinstance(it, dict):
            continue
        name = it.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        ok = bool(it.get("ok"))
        status = it.get("status")
        if status not in ("succeeded", "failed", "skipped"):
            status = "succeeded" if ok else "failed"
        err = it.get("error_code")
        err = err.strip()[:_ERROR_CODE_MAX_LEN] if isinstance(err, str) else ""
        out.append({
            "name": name.strip()[:_TOOL_NAME_MAX_LEN],
            "ok": ok,
            "status": status,
            "error_code": err,
        })
    return out


def start_activity_log(activity_key: str, source: str, started_at=None) -> dict:
    """建立 running 记录（幂等）。

    - 同一 activity_key 已存在：不产生第二行；
      已存在 running → {"ok": True, "created": False, "already_final": False}；
      已完成状态 → {"ok": True, "created": False, "already_final": True}（不回置 running）；
    - service_role 缺失 / 数据库异常 → {"ok": False, "error_code": ...}（调用方必须停止活动）。
    """
    if not activity_key or not isinstance(activity_key, str) or not activity_key.strip():
        return {"ok": False, "error_code": "EMPTY_ACTIVITY_KEY", "created": False}
    # C5：新增 unified_autonomy（统一自主调度）；free_activity/home_autonomy 保留
    # 供旧兼容循环（不由后台主进程调度）与历史数据使用。数据库 source 为 text，无需迁移。
    if source not in ("unified_autonomy", "free_activity", "home_autonomy"):
        return {"ok": False, "error_code": "INVALID_SOURCE", "created": False}
    sb = _get_service_client()
    if sb is None:
        return {"ok": False, "error_code": "SERVICE_KEY_MISSING", "created": False}
    key = activity_key.strip()
    try:
        existing = (sb.table("activity_logs").select("status")
                    .eq("activity_key", key).limit(1).execute())
        rows = existing.data or []
        if rows:
            prev = rows[0].get("status")
            return {"ok": True, "created": False, "already_final": prev != "running",
                    "status": prev, "activity_key": key}
        payload = {"activity_key": key, "source": source, "status": "running"}
        if started_at is not None:
            payload["started_at"] = started_at
        sb.table("activity_logs").insert(payload).execute()
        return {"ok": True, "created": True, "activity_key": key}
    except Exception as e:
        logger.warning("activity_log.start 失败（source=%s, key 前缀=%s…）: %s",
                       source, key[:6], e)
        return {"ok": False, "error_code": "DB_ERROR", "created": False}


def finalize_activity_log(activity_key: str, activity_id: str = "", activity_name: str = "",
                          status: str = "succeeded", thought_summary: str = "",
                          result_summary: str = "", tools_used=None,
                          finished_at=None) -> dict:
    """把 running 记录更新为最终状态（幂等）。

    - 只更新 activity_key 对应且仍为 running 的行（条件更新防并发覆盖）；
    - 已完成记录重复 finalize → 不覆盖第一次结果，返回 already_final；
    - 找不到记录（start 未成功）→ 返回 NOT_FOUND，不插入"已完成"记录掩盖；
    - 文本长度应用层限制；tools_used 经 sanitize_tools_used 归一化。
    """
    if not activity_key or not isinstance(activity_key, str) or not activity_key.strip():
        return {"ok": False, "error_code": "EMPTY_ACTIVITY_KEY"}
    if status not in _FINAL_STATUSES:
        return {"ok": False, "error_code": "INVALID_STATUS"}
    sb = _get_service_client()
    if sb is None:
        return {"ok": False, "error_code": "SERVICE_KEY_MISSING"}
    key = activity_key.strip()
    thought = sanitize_thought_summary(thought_summary)
    result = result_summary.strip()[:_RESULT_MAX_LEN] if isinstance(result_summary, str) else ""
    tools = sanitize_tools_used(tools_used)
    payload = {
        "activity_id": (activity_id or "").strip()[:200] or None,
        "activity_name": (str(activity_name) if activity_name else "")[:100],
        "status": status,
        "thought_summary": thought,
        "result_summary": result,
        "tools_used": tools,
        "finished_at": finished_at if finished_at is not None else _utcnow_iso(),
        "updated_at": _utcnow_iso(),
    }
    try:
        resp = (sb.table("activity_logs").update(payload)
                .eq("activity_key", key).eq("status", "running").execute())
        if resp.data:
            return {"ok": True, "finalized": True, "status": status}
        # 未更新到行：区分"不存在"与"已完成"（不插第二行、不掩盖 start 失败）
        cur = (sb.table("activity_logs").select("status")
               .eq("activity_key", key).limit(1).execute())
        rows = cur.data or []
        if not rows:
            return {"ok": False, "error_code": "NOT_FOUND"}
        return {"ok": True, "finalized": False, "already_final": True,
                "status": rows[0].get("status")}
    except Exception as e:
        logger.warning("activity_log.finalize 失败（key 前缀=%s…, status=%s）: %s",
                       key[:6], status, e)
        return {"ok": False, "error_code": "DB_ERROR"}


def fail_activity_log(activity_key: str, error_brief: str = "") -> dict:
    """异常/中断路径：把 running 记录 finalize 为 failed（安全简短摘要，不存堆栈）。"""
    brief = error_brief if isinstance(error_brief, str) else ""
    brief = brief.strip()[:200] or "活动执行异常"
    return finalize_activity_log(activity_key, status="failed", result_summary=brief)


def get_recent_completed_free_activities(limit: int = 2) -> list:
    """C4：读取最近完成的自由活动（防连续重复用，只读元数据）。

    - 仅 source=free_activity 且 status ∈ {succeeded, partial}：
      failed/skipped 不算"已经做过"；running 的当前/残留活动自然被排除；
    - 只返回 activity_name/activity_id/started_at/status 四个字段，
      不返回 thought_summary/result_summary，不读取任何正文；
    - service_role 只读；异常记录日志后返回空列表（调用方回退 memories 兼容源）。
    """
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 2
    sb = _get_service_client()
    if sb is None:
        return []
    try:
        resp = (sb.table("activity_logs")
                .select("activity_name,activity_id,started_at,status")
                .eq("source", "free_activity")
                .in_("status", ["succeeded", "partial"])
                .order("started_at", desc=True)
                .limit(limit)
                .execute())
        return resp.data or []
    except Exception as e:
        logger.warning("activity_log.get_recent_completed_free_activities 失败: %s",
                       type(e).__name__, exc_info=True)
        return []


def get_recent_completed_activities(limit: int = 2) -> list:
    """C5：读取最近完成的自主活动（跨 source，统一调度防重复用，只读元数据）。

    - source ∈ {unified_autonomy, free_activity, home_autonomy}（新统一 source
      与两个旧 source 兼容）且 status ∈ {succeeded, partial}：
      failed/skipped 不算"已经做过"；running 的当前活动自然被排除；
    - 只返回 activity_id/activity_name/started_at/status 四个字段，
      不返回 thought_summary/result_summary，不读取任何正文；
    - service_role 只读；异常记录日志后返回空列表（调用方回退 memories 兼容源）。
    """
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 2
    sb = _get_service_client()
    if sb is None:
        return []
    try:
        resp = (sb.table("activity_logs")
                .select("activity_id,activity_name,started_at,status")
                .in_("source", ["unified_autonomy", "free_activity", "home_autonomy"])
                .in_("status", ["succeeded", "partial"])
                .order("started_at", desc=True)
                .limit(limit)
                .execute())
        return resp.data or []
    except Exception as e:
        logger.warning("activity_log.get_recent_completed_activities 失败: %s",
                       type(e).__name__, exc_info=True)
        return []


# ============================================================
# C6: 行动日志只读分页查询（供受保护网关 API 使用）
# ============================================================

_QUERY_SOURCES = ("unified_autonomy", "free_activity", "home_autonomy")
_QUERY_STATUSES = ("running",) + _FINAL_STATUSES


def query_activity_logs(page: int = 1, size: int = 20, source: str = "",
                        status: str = "", activity_id: str = "") -> dict:
    """C6：行动日志只读分页查询（service_role，白名单投影）。

    - 只返回 activity_id/activity_name/source/status/thought_summary/
      result_summary/tools_used/started_at/finished_at；
      不返回 id/activity_key 及任何参数、UUID、raw、正文；
    - tools_used 经 sanitize_tools_used 二次白名单投影（不信任库内已有 JSON）；
    - source ∈ _QUERY_SOURCES；status ∈ _QUERY_STATUSES；activity_id ≤200 字；
    - started_at 倒序；page ≥1，size 1..100；
    - 参数非法返回 {"ok": False, "error_code": "INVALID_*"}；
      数据库异常返回 {"ok": False, "error_code": "DB_ERROR"}，均不向上抛。
    """
    try:
        page = int(page)
    except (TypeError, ValueError):
        return {"ok": False, "error_code": "INVALID_PAGE"}
    try:
        size = int(size)
    except (TypeError, ValueError):
        return {"ok": False, "error_code": "INVALID_SIZE"}
    if page < 1:
        return {"ok": False, "error_code": "INVALID_PAGE"}
    if size < 1 or size > 100:
        return {"ok": False, "error_code": "INVALID_SIZE"}
    source = (source or "").strip()
    status = (status or "").strip()
    if source and source not in _QUERY_SOURCES:
        return {"ok": False, "error_code": "INVALID_SOURCE"}
    if status and status not in _QUERY_STATUSES:
        return {"ok": False, "error_code": "INVALID_STATUS"}
    activity_id = (activity_id or "").strip()[:200]

    sb = _get_service_client()
    if sb is None:
        return {"ok": False, "error_code": "SERVICE_KEY_MISSING"}
    safe_cols = ("activity_id,activity_name,source,status,thought_summary,"
                 "result_summary,tools_used,started_at,finished_at")
    try:
        q = sb.table("activity_logs").select(safe_cols)
        if source:
            q = q.eq("source", source)
        if status:
            q = q.eq("status", status)
        if activity_id:
            q = q.eq("activity_id", activity_id)
        resp = q.order("started_at", desc=True).range((page - 1) * size, page * size - 1).execute()

        cq = sb.table("activity_logs").select("activity_key", count="exact")
        if source:
            cq = cq.eq("source", source)
        if status:
            cq = cq.eq("status", status)
        if activity_id:
            cq = cq.eq("activity_id", activity_id)
        total = getattr(cq.execute(), "count", None) or 0

        items = []
        for it in (resp.data or []):
            items.append({
                "activity_id": it.get("activity_id"),
                "activity_name": it.get("activity_name", ""),
                "source": it.get("source", ""),
                "status": it.get("status", ""),
                "thought_summary": it.get("thought_summary", ""),
                "result_summary": it.get("result_summary", ""),
                "tools_used": sanitize_tools_used(it.get("tools_used")),
                "started_at": it.get("started_at"),
                "finished_at": it.get("finished_at"),
            })
        return {
            "ok": True, "items": items, "total": total,
            "page": page, "size": size, "has_more": (page * size) < total,
        }
    except Exception as e:
        logger.warning("activity_log.query_activity_logs 失败: %s", type(e).__name__, exc_info=True)
        return {"ok": False, "error_code": "DB_ERROR"}
