# -*- coding: utf-8 -*-
"""Pinecone 用户消息近重复清理（一次性手工脚本）。

只删「用户侧消息」的重复向量；旧 assistant 混合格式一律跳过、不删。

判定为用户消息（可参与去重 / 可删）：
  - memory_type == chat_user_raw，或
  - source_role == user，且正文不是 assistant 混合格式
去重键：从正文抽出 user 部分再归一化（相等 / 双向子串）。
每组只留 rank 最高的 1 条（优先 v2、较新 created_at）。

用法：
  python pinecone_dedup_cleanup.py              # dry-run
  python pinecone_dedup_cleanup.py --commit     # 真正删除
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# 压掉 Pinecone SDK 刷屏
logging.getLogger("pinecone").setLevel(logging.WARNING)
logging.getLogger("pinecone_plugin_interface").setLevel(logging.WARNING)

FETCH_BATCH = 200
FETCH_WORKERS = 6
DELETE_BATCH = 100


def _vid(item) -> str:
    return item.id if hasattr(item, "id") else str(item)


def _is_assistant_format(memory_text: str) -> bool:
    """旧 user|assistant 混合格式：本脚本一律跳过，不删。"""
    if not isinstance(memory_text, str):
        return False
    for line in memory_text.split("\n"):
        stripped = line.strip().lower()
        if stripped.startswith("assistant:") or "| assistant:" in stripped:
            return True
    return False


def _extract_user_body(text: str):
    """从 Pinecone metadata.text 抽出用户正文；抽不出则返回 None。"""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t:
        return None
    m = re.match(r"(?is)^user\s*[:：]\s*(.*)$", t, re.DOTALL)
    if m:
        body = m.group(1).strip()
        parts = re.split(r"\s*\|\s*assistant\s*[:：]", body, maxsplit=1, flags=re.I)
        body = parts[0].strip()
        return body or None
    return None


def _is_user_message_vector(meta: dict, text: str) -> bool:
    if _is_assistant_format(text):
        return False
    if meta.get("memory_type") == "chat_user_raw":
        return True
    if meta.get("source_role") == "user":
        return True
    if re.match(r"(?is)^user\s*[:：]", text.strip()):
        return True
    return False


def _keep_rank(meta: dict) -> tuple:
    sv = 1 if meta.get("schema_version") == "v2" else 0
    mtype = 1 if meta.get("memory_type") == "chat_user_raw" else 0
    created = str(meta.get("created_at") or "")
    return (sv, mtype, created)


def _fetch_batch(index, batch_ids):
    fetched = index.fetch(ids=batch_ids)
    return getattr(fetched, "vectors", None) or {}


def scan(index, user_id: str, text_norm, norm_is_dup):
    print("[scan] listing vector ids ...", flush=True)
    all_ids = []
    for page in index.list(limit=100):
        for item in page:
            all_ids.append(_vid(item))
        if len(all_ids) % 2000 < 100:
            print(f"   listed {len(all_ids)} ...", flush=True)
    print(f"   index total ids={len(all_ids)}", flush=True)

    batches = [all_ids[i:i + FETCH_BATCH] for i in range(0, len(all_ids), FETCH_BATCH)]
    records = []
    skipped_assistant = 0
    skipped_non_user = 0
    fetched_n = 0
    t0 = time.time()

    print(
        f"[scan] fetching {len(batches)} batches "
        f"(size={FETCH_BATCH}, workers={FETCH_WORKERS}) ...",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futs = {pool.submit(_fetch_batch, index, b): len(b) for b in batches}
        for fut in as_completed(futs):
            vectors = fut.result()
            fetched_n += futs[fut]
            for vid, vec in vectors.items():
                md = dict(vec.metadata or {})
                if md.get("user_id") != user_id:
                    continue
                text = md.get("text", "")
                if not isinstance(text, str):
                    text = str(text) if text is not None else ""
                if _is_assistant_format(text):
                    skipped_assistant += 1
                    continue
                if not _is_user_message_vector(md, text):
                    skipped_non_user += 1
                    continue
                body = _extract_user_body(text)
                if not body:
                    skipped_non_user += 1
                    continue
                records.append({
                    "id": vid,
                    "text": text,
                    "body": body,
                    "meta": md,
                    "norm": text_norm(body),
                })
            print(
                f"   fetched~{min(fetched_n, len(all_ids))}/{len(all_ids)}  "
                f"user_msgs={len(records)}  "
                f"skip_asst={skipped_assistant}  "
                f"skip_other={skipped_non_user}  "
                f"elapsed={time.time()-t0:.0f}s",
                flush=True,
            )

    candidates = sorted(records, key=lambda r: _keep_rank(r["meta"]), reverse=True)
    to_delete = set()
    kept_norms = []
    for r in candidates:
        if not r["norm"]:
            continue
        if norm_is_dup(r["norm"], kept_norms):
            to_delete.add(r["id"])
            continue
        kept_norms.append(r["norm"])

    stats = {
        "index_total": len(all_ids),
        "user_message_scanned": len(records),
        "skipped_assistant_format": skipped_assistant,
        "skipped_non_user": skipped_non_user,
        "user_near_dup": len(to_delete),
        "would_delete": len(to_delete),
        "would_keep_user_msgs": len(records) - len(to_delete),
        "elapsed_s": round(time.time() - t0, 1),
    }
    samples = {"user_near_dup": []}
    id_to_rec = {r["id"]: r for r in records}
    for rid in to_delete:
        if len(samples["user_near_dup"]) >= 5:
            break
        rec = id_to_rec.get(rid)
        preview = (rec["body"][:100].replace("\n", " ") if rec else "")
        samples["user_near_dup"].append(preview)
    return sorted(to_delete), stats, samples


def delete_ids(index, ids: list) -> int:
    deleted = 0
    for i in range(0, len(ids), DELETE_BATCH):
        batch = ids[i:i + DELETE_BATCH]
        index.delete(ids=batch)
        deleted += len(batch)
        print(f"   deleted {deleted}/{len(ids)}", flush=True)
    return deleted


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Pinecone user-message near-duplicate cleanup "
                    "(skips old assistant-format vectors)"
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="actually delete (default is dry-run)",
    )
    args = parser.parse_args(argv)

    import server as s

    pm = s.pinecone_memory
    if not pm or not pm.index:
        print("[error] Pinecone not configured")
        return 1
    user_id = s._resolve_pinecone_user_id()
    print(f"[info] user_id={user_id}  index={pm.index_name}  commit={args.commit}", flush=True)
    print(
        "[info] policy: ONLY delete duplicate user messages; "
        "never delete assistant-format vectors",
        flush=True,
    )

    delete_list, stats, samples = scan(
        pm.index, user_id, s._pinecone_text_norm, s._pinecone_norm_is_dup,
    )

    print("\n[stats]", flush=True)
    for k, v in stats.items():
        print(f"   {k}: {v}", flush=True)
    print("\n[samples] duplicate user bodies to delete (up to 5)", flush=True)
    for p in samples.get("user_near_dup", []):
        print(f"   - {p!r}", flush=True)

    if not delete_list:
        print("\n[ok] nothing to delete", flush=True)
        return 0

    if not args.commit:
        print(
            f"\n[dry-run] would delete {len(delete_list)} user-message vectors. "
            f"Re-run with --commit to apply.",
            flush=True,
        )
        return 0

    print(f"\n[delete] deleting {len(delete_list)} vectors ...", flush=True)
    n = delete_ids(pm.index, delete_list)
    print(f"[ok] deleted {n}", flush=True)
    try:
        st = pm.index.describe_index_stats()
        print(
            f"   index total_vector_count~={getattr(st, 'total_vector_count', '?')}",
            flush=True,
        )
    except Exception as e:
        print(f"   stats refresh skipped: {type(e).__name__}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
