from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import load_config


DEFAULT_BRIDGE_DB = Path(r"D:\haven_bridge\data\haven.db")
DEFAULT_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
DEFAULT_MODEL = "mimo-v2.5"
DEFAULT_API_KEY_ENV = "HANDOFF_SUMMARIZER_API_KEY"

ALLOWED_TAGS = {
    "relationship_event",
    "project_event",
    "stable_preference",
    "boundary",
    "signal",
    "commitment",
    "todo",
    "wish",
    "identity",
    "memory_system",
    "from_haven_bridge",
    "auto_memory_worker",
}


class WorkerError(RuntimeError):
    pass


@dataclass
class WorkerConfig:
    bridge_db: Path
    state_file: Path
    base_url: str
    model: str
    api_key_env: str
    timeout_seconds: int
    max_tokens: int
    max_items: int
    duplicate_score: float
    dry_run: bool
    mark_seen: bool
    allow_initial_write: bool
    settle_seconds: float


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    runtime_config = load_config()
    default_state = Path(runtime_config["state_dir"]) / "local_memory_worker.json"
    parser = argparse.ArgumentParser(
        description="Select durable Haven Bridge chat moments and write them into Ombre memory."
    )
    parser.add_argument(
        "--bridge-db",
        default=os.environ.get("OMBRE_LOCAL_MEMORY_BRIDGE_DB", str(DEFAULT_BRIDGE_DB)),
        help="Path to Haven Bridge haven.db.",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("OMBRE_LOCAL_MEMORY_STATE", str(default_state)),
        help="Worker checkpoint JSON path.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OMBRE_LOCAL_MEMORY_BASE_URL", DEFAULT_BASE_URL),
        help="OpenAI-compatible base URL for the selector model.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OMBRE_LOCAL_MEMORY_MODEL", DEFAULT_MODEL),
        help="Selector model name.",
    )
    parser.add_argument(
        "--api-key-env",
        default=os.environ.get("OMBRE_LOCAL_MEMORY_API_KEY_ENV", DEFAULT_API_KEY_ENV),
        help="Environment variable name holding the selector API key.",
    )
    parser.add_argument("--limit", type=int, default=40, help="Max bridge messages to inspect per run.")
    parser.add_argument("--session-id", type=int, default=0, help="Restrict to one Haven Bridge session id.")
    parser.add_argument("--since-id", type=int, default=None, help="Override checkpoint and read messages after id.")
    parser.add_argument("--max-items", type=int, default=3, help="Max memories the selector may return.")
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument("--max-tokens", type=int, default=800)
    parser.add_argument("--duplicate-score", type=float, default=90.0)
    parser.add_argument("--write", action="store_true", help="Actually call hold(). Default is dry-run.")
    parser.add_argument("--mark-seen", action="store_true", help="Advance checkpoint even in dry-run.")
    parser.add_argument(
        "--allow-initial-write",
        action="store_true",
        help="Allow --write when no checkpoint exists. Otherwise first run stays dry-run.",
    )
    parser.add_argument("--settle-seconds", type=float, default=2.0, help="Keep loop alive after writes.")
    parser.add_argument("--loop", action="store_true", help="Run forever instead of once.")
    parser.add_argument("--interval-minutes", type=float, default=10.0)
    return parser.parse_args(argv)


def make_config(args: argparse.Namespace) -> WorkerConfig:
    return WorkerConfig(
        bridge_db=Path(args.bridge_db),
        state_file=Path(args.state_file),
        base_url=str(args.base_url).strip().rstrip("/"),
        model=str(args.model).strip(),
        api_key_env=str(args.api_key_env).strip(),
        timeout_seconds=max(5, int(args.timeout_seconds)),
        max_tokens=max(100, int(args.max_tokens)),
        max_items=max(1, min(int(args.max_items), 5)),
        duplicate_score=max(0.0, min(float(args.duplicate_score), 100.0)),
        dry_run=not bool(args.write),
        mark_seen=bool(args.mark_seen),
        allow_initial_write=bool(args.allow_initial_write),
        settle_seconds=max(0.0, float(args.settle_seconds)),
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"last_message_id": 0, "written_hashes": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"last_message_id": 0, "written_hashes": []}
    if not isinstance(data, dict):
        return {"last_message_id": 0, "written_hashes": []}
    data.setdefault("last_message_id", 0)
    data.setdefault("written_hashes", [])
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_bridge_messages(
    db_path: Path,
    *,
    since_id: int,
    limit: int,
    session_id: int = 0,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        raise WorkerError(f"Haven Bridge db not found: {db_path}")

    params: list[Any] = []
    clauses = ["((role = 'user' AND source = 'chat') OR (role = 'assistant' AND source = 'codex'))"]
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)

    if since_id > 0:
        clauses.append("id > ?")
        params.append(since_id)
        query = f"""
            SELECT id, session_id, role, content, source, metadata_json, created_at
            FROM messages
            WHERE {' AND '.join(clauses)}
            ORDER BY id ASC
            LIMIT ?
        """
        params.append(max(1, min(int(limit), 200)))
    else:
        query = f"""
            SELECT id, session_id, role, content, source, metadata_json, created_at
            FROM messages
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC
            LIMIT ?
        """
        params.append(max(1, min(int(limit), 200)))

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(query, params).fetchall()
    conn.close()
    messages = [row_to_message(row) for row in rows]
    if since_id <= 0:
        messages.reverse()
    return [item for item in messages if item["content"].strip()]


def row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    except Exception:
        item["metadata"] = {}
    item["content"] = str(item.get("content") or "")
    return item


def build_selector_prompt(messages: list[dict[str, Any]], max_items: int) -> str:
    transcript = format_messages(messages)
    return "\n".join(
        [
            "你是 Ombre-Brain 的本地记忆筛选 worker。",
            "任务：从 Haven Bridge 最近聊天里挑出值得写入长期记忆的片段。",
            "",
            "只写这些内容：稳定偏好、明确边界、承诺/待办、仍会影响未来执行的项目状态、关系连续性锚点、称呼/暗号/重要约定。",
            "不要写：普通撒娇、问候、临时情绪、已过期的调试过程、命令输出、重复爱意、没有未来价值的聊天流水。",
            "保守一点。看不准就返回空数组。",
            "",
            f"最多返回 {max_items} 条。每条 content 用中文，1 到 3 句，尽量带具体日期或语境，不要粘贴长原文。",
            "tags 只能从这些里面选：relationship_event, project_event, stable_preference, boundary, signal, commitment, todo, wish, identity, memory_system。",
            "importance 只能是 4 到 7；普通项目状态 4-5，承诺/边界/关系锚点 6-7。",
            "source_message_ids 必须只使用输入里出现的消息 id。",
            "",
            "只输出纯 JSON，不要 Markdown：",
            '{"memories":[{"content":"...","tags":["project_event"],"importance":5,"reason":"为什么值得长期记住","source_message_ids":[123]}]}',
            '如果没有值得记的内容，输出 {"memories":[]}',
            "",
            "最近聊天：",
            transcript,
        ]
    )


def format_messages(messages: list[dict[str, Any]], max_total_chars: int = 12000) -> str:
    lines: list[str] = []
    used = 0
    for item in messages:
        text = compact_text(item["content"])
        if len(text) > 1200:
            text = text[:1200].rstrip() + "..."
        line = f"[#{item['id']} {item.get('created_at', '')} {item['role']}] {text}"
        if used + len(line) > max_total_chars and lines:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def call_selector(prompt: str, cfg: WorkerConfig) -> str:
    if not cfg.base_url:
        raise WorkerError("selector base_url is empty")
    if not cfg.model:
        raise WorkerError("selector model is empty")
    api_key = os.environ.get(cfg.api_key_env, "").strip() if cfg.api_key_env else ""
    if not api_key:
        raise WorkerError(f"{cfg.api_key_env} is empty")

    url = cfg.base_url if cfg.base_url.endswith("/chat/completions") else f"{cfg.base_url}/chat/completions"
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": "You select durable long-term memories and return strict JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": cfg.max_tokens,
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise WorkerError(f"selector HTTP {exc.code}: {body}") from exc
    except Exception as exc:
        raise WorkerError(f"selector request failed: {exc}") from exc

    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except Exception as exc:
        raise WorkerError("selector response missing choices[0].message.content") from exc


def parse_selector_json(text: str, valid_message_ids: set[int]) -> list[dict[str, Any]]:
    data = json.loads(extract_json_object(text))
    raw_items = data.get("memories") if isinstance(data, dict) else []
    if not isinstance(raw_items, list):
        return []

    memories = []
    seen_content: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        content = compact_text(raw.get("content") or "")
        if len(content) < 8:
            continue
        if content in seen_content:
            continue
        seen_content.add(content)
        tags = normalize_tags(raw.get("tags"))
        importance = clamp_int(raw.get("importance"), 5, 4, 7)
        source_ids = normalize_source_ids(raw.get("source_message_ids"), valid_message_ids)
        memories.append(
            {
                "content": content[:900],
                "tags": tags,
                "importance": importance,
                "reason": compact_text(raw.get("reason") or "")[:240],
                "source_message_ids": source_ids,
                "hash": memory_hash(content),
            }
        )
    return memories


def extract_json_object(text: str) -> str:
    clean = str(text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\s*```$", "", clean)
    start = clean.find("{")
    end = clean.rfind("}")
    if start < 0 or end <= start:
        raise WorkerError("selector did not return a JSON object")
    return clean[start : end + 1]


def normalize_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_tags = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        raw_tags = [str(item).strip() for item in value]
    else:
        raw_tags = []
    tags = [tag for tag in raw_tags if tag in ALLOWED_TAGS and not tag.startswith("flavor_")]
    tags.extend(["from_haven_bridge", "auto_memory_worker"])
    return list(dict.fromkeys(tags))[:10]


def normalize_source_ids(value: Any, valid_message_ids: set[int]) -> list[int]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        try:
            message_id = int(item)
        except Exception:
            continue
        if message_id in valid_message_ids:
            result.append(message_id)
    return list(dict.fromkeys(result))


def clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(low, min(parsed, high))


def memory_hash(content: str) -> str:
    normalized = re.sub(r"\s+", "", str(content or "").lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


async def write_memories(memories: list[dict[str, Any]], state: dict[str, Any], cfg: WorkerConfig) -> list[dict[str, Any]]:
    if cfg.dry_run:
        return [{"status": "dry_run", **memory} for memory in memories]

    import server

    written_hashes = set(str(item) for item in state.get("written_hashes", []))
    results = []
    for memory in memories:
        if memory["hash"] in written_hashes:
            results.append({"status": "skipped_duplicate_hash", **memory})
            continue
        duplicate = await find_duplicate_bucket(server.bucket_mgr, memory["content"], cfg.duplicate_score)
        if duplicate:
            results.append({"status": "skipped_existing_bucket", "existing_bucket_id": duplicate["id"], **memory})
            written_hashes.add(memory["hash"])
            continue
        response = await server.hold(
            content=memory["content"],
            tags=",".join(memory["tags"]),
            importance=memory["importance"],
        )
        written_hashes.add(memory["hash"])
        results.append({"status": "written", "hold_response": response, **memory})

    state["written_hashes"] = sorted(written_hashes)[-500:]
    if cfg.settle_seconds > 0:
        await asyncio.sleep(cfg.settle_seconds)
    return results


async def find_duplicate_bucket(bucket_mgr: Any, content: str, threshold: float) -> dict[str, Any] | None:
    try:
        matches = await bucket_mgr.search(content, limit=1, include_archive=True)
    except Exception:
        return None
    if matches and float(matches[0].get("score", 0.0)) >= threshold:
        return matches[0]
    return None


async def run_once(args: argparse.Namespace, cfg: WorkerConfig) -> dict[str, Any]:
    state = load_state(cfg.state_file)
    had_checkpoint = bool(state.get("last_message_id"))
    since_id = int(args.since_id if args.since_id is not None else state.get("last_message_id", 0) or 0)
    messages = read_bridge_messages(
        cfg.bridge_db,
        since_id=since_id,
        limit=args.limit,
        session_id=int(args.session_id or 0),
    )
    if not messages:
        return {"status": "idle", "since_id": since_id, "message_count": 0, "memories": []}

    effective_dry_run = cfg.dry_run
    if not cfg.dry_run and not had_checkpoint and args.since_id is None and not cfg.allow_initial_write:
        effective_dry_run = True

    effective_cfg = WorkerConfig(**{**cfg.__dict__, "dry_run": effective_dry_run})
    valid_ids = {int(item["id"]) for item in messages}
    prompt = build_selector_prompt(messages, effective_cfg.max_items)
    raw = call_selector(prompt, effective_cfg)
    memories = parse_selector_json(raw, valid_ids)
    results = await write_memories(memories, state, effective_cfg)

    max_message_id = max(int(item["id"]) for item in messages)
    initial_write_guard = effective_cfg.dry_run and not cfg.dry_run
    if (not effective_cfg.dry_run) or cfg.mark_seen or initial_write_guard:
        state["last_message_id"] = max(max_message_id, int(state.get("last_message_id", 0) or 0))
        state["last_run_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save_state(cfg.state_file, state)

    status = "dry_run" if effective_cfg.dry_run else "ok"
    if effective_dry_run and not cfg.dry_run:
        status = "initial_dry_run"
    return {
        "status": status,
        "since_id": since_id,
        "max_message_id": max_message_id,
        "message_count": len(messages),
        "memory_count": len(memories),
        "memories": results,
        "state_file": str(cfg.state_file),
    }


def print_result(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = make_config(args)
    while True:
        try:
            print_result(await run_once(args, cfg))
        except Exception as exc:
            print_result({"status": "error", "error": str(exc)})
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        await asyncio.sleep(max(5.0, float(args.interval_minutes) * 60.0))


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
