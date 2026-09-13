# -*- coding: utf-8 -*-
"""阶段 A5 专项测试 —— memory_events 全自动提取 worker。

全部 unittest + mock + 脱敏假数据（SYNTHETIC_A5_*），不连接 Supabase / LLM。

覆盖：
  A. 取批最旧优先（created_at 升序单调消化）
  B. 并发防护：已有 processing 批次 → 跳过本轮，不取批不调 LLM
  C. 原子认领：并发抢走部分事件时只处理实际认领到的子集
  D. 分写：高置信 active / 低置信 pending_review
  E. 跨批去重：重复候选跳过不插入（计 duplicate_skipped）且事件仍标 processed
  F. 事件状态：成功 processed（attempt_count+1/batch_id）；提取失败 failed
     （attempt_count+1、last_error 脱敏、processed_at=NULL）；写入失败释放回 pending
  G. backlog 遥测计数 + 日志不含正文/user_id
  H. heartbeat worker：门控默认关时不跑；开启后按 env 周期调用 run_auto_extraction
  I. 源码约束：run_background_process 已注册 memory_extraction 任务

运行：  python -m unittest test_memory_auto_extract_phaseA5 -v
"""

import asyncio
import contextlib
import datetime
import hashlib
import io
import json
import os
import time
import unittest
from unittest.mock import patch

import heartbeat
import memory_auto_extract
import memory_extractor


TEST_USER = "test-user"
_TEST_NOW = "2026-09-12T12:00:00+00:00"


# ==========================================
# 有状态假 Supabase 客户端（支持本 worker 用到的查询路径）
# ==========================================

class FakeResult:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Q:
    def __init__(self, owner, table):
        self._owner = owner
        self._table = table
        self._path = []

    def _rec(self, method, *args, **kwargs):
        self._path.append((method, args, kwargs))
        return self

    def select(self, *a, **k): return self._rec("select", *a, **k)
    def insert(self, *a, **k): return self._rec("insert", *a, **k)
    def update(self, *a, **k): return self._rec("update", *a, **k)
    def eq(self, *a, **k): return self._rec("eq", *a, **k)
    def in_(self, *a, **k): return self._rec("in_", *a, **k)
    def order(self, *a, **k): return self._rec("order", *a, **k)
    def limit(self, *a, **k): return self._rec("limit", *a, **k)

    def execute(self):
        self._owner.calls.append((self._table, list(self._path)))
        return self._owner._dispatch(self._table, self._path)


class FakeService:
    """最小状态假客户端：memory_events / memory_items 两表，覆盖 worker 全部路径。"""

    def __init__(self):
        self.events = {}          # id -> 行 dict（含 processing_status）
        self.items = []
        self.calls = []
        self.insert_fail_on_hash = None   # 注入：插入该 hash 时抛异常
        self.dedup_fail = False           # 注入：去重查询抛异常
        self.claim_drop_ids = set()       # 注入：模拟并发方抢走部分认领
        self.release_fail = False         # 注入：释放 UPDATE 抛异常

    # ---- 数据构造 ----
    def add_event(self, idx, role="user", status="pending", attempts=0,
                  occurred_at=None, uid=TEST_USER, content=None):
        occurred_at = occurred_at or (
            datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
            + datetime.timedelta(minutes=idx)).isoformat()
        row = {"id": f"ev-{idx:04d}", "user_id": uid, "session_id": None,
               "channel": "web", "role": role,
               "content": content or f"SYNTHETIC_A5_EVENT_{idx}",
               "content_hash": f"h-{idx}", "occurred_at": occurred_at,
               "created_at": occurred_at, "processing_status": status,
               "attempt_count": attempts, "last_error": None,
               "processed_at": None, "batch_id": None, "metadata": {}}
        self.events[row["id"]] = row
        return row

    def by_status(self, status):
        return {i: r for i, r in self.events.items()
                if r["processing_status"] == status}

    # ---- 查询分派 ----
    def table(self, name):
        return _Q(self, name)

    def _dispatch(self, table, path):
        method, margs, mkwargs = path[0]
        if table == "memory_items":
            return self._dispatch_items(method, margs, mkwargs, path)
        return self._dispatch_events(method, margs, mkwargs, path)

    @staticmethod
    def _conds(path):
        eqs = [(a[0], a[1]) for m, a, k in path if m == "eq"]
        ins = [(a[0], list(a[1])) for m, a, k in path if m == "in_"]
        return eqs, ins

    def _dispatch_events(self, method, margs, mkwargs, path):
        eqs, ins = self._conds(path)
        rows = list(self.events.values())
        for col, val in eqs:
            rows = [r for r in rows if r.get(col) == val]
        for col, vals in ins:
            rows = [r for r in rows if r.get(col) in set(vals)]
        if method == "select":
            total = len(rows)
            if any(m == "order" for m, a, k in path):
                desc = next((k.get("desc", False) for m, a, k in path if m == "order"), False)
                rows = sorted(rows, key=lambda r: r.get("created_at") or "",
                              reverse=desc)
            lim = next((a[0] for m, a, k in path if m == "limit"), None)
            if lim is not None:
                rows = rows[:lim]
            count = total if mkwargs.get("count") == "exact" else None
            return FakeResult([dict(r) for r in rows], count=count)
        if method == "update":
            payload = margs[0]
            if self.release_fail:
                raise RuntimeError("mock release failure")
            ids = next((v for col, v in ins if col == "id"), [])
            updated = []
            for r in rows:
                if r["id"] in set(ids):
                    if (payload.get("processing_status") == "processing"
                            and r["id"] in self.claim_drop_ids):
                        continue  # 模拟并发方抢走：该行保持 pending 不被本流程认领
                    r.update(payload)
                    updated.append(dict(r))
            return FakeResult(updated)
        raise AssertionError(f"未预期的 memory_events 操作: {method}")

    def _dispatch_items(self, method, margs, mkwargs, path):
        if method == "select":
            if self.dedup_fail:
                raise RuntimeError("mock dedup failure")
            eqs, ins = self._conds(path)
            rows = self.items
            for col, val in eqs:
                rows = [r for r in rows if r.get(col) == val]
            for col, vals in ins:
                rows = [r for r in rows if r.get(col) in set(vals)]
            return FakeResult([dict(r) for r in rows])
        if method == "insert":
            row = dict(margs[0])
            if self.insert_fail_on_hash and row.get("content_hash") == self.insert_fail_on_hash:
                raise RuntimeError("mock insert failure")
            self.items.append(row)
            return FakeResult([dict(row)])
        raise AssertionError(f"未预期的 memory_items 操作: {method}")


# ==========================================
# 公共执行辅助
# ==========================================

def _cand_json(conf, content="用户喜欢喝无糖咖啡。", memory_type="long_term"):
    return {"memory_type": memory_type, "content": content, "importance": 4,
            "confidence": conf, "valid_at": None, "invalid_at": None,
            "expires_at": None, "source_event_indexes": [0], "subject_key": None}


def _std_events(fake, n=4):
    """user/assistant 交替的 pending 事件（user 在前，created_at 递增）。"""
    for i in range(n):
        fake.add_event(i, role="user" if i % 2 == 0 else "assistant")


def _run(fake, *, candidates=None, llm=None, batch_limit=20, threshold=0.75,
         user_id=TEST_USER):
    """执行一轮 run_auto_extraction。candidates 为 None 时 LLM 返回空 memories。"""
    if llm is None:
        payload = {"memories": candidates or []}
        raw = json.dumps(payload, ensure_ascii=False)

        def llm(prompt):
            return raw
    return asyncio.run(memory_auto_extract.run_auto_extraction(
        fake, user_id=user_id, batch_limit=batch_limit,
        auto_active_threshold=threshold, llm_call=llm))


# ==========================================
# A+B+C. 取批 / 并发防护 / 认领
# ==========================================

class TestBatchClaim(unittest.TestCase):

    def test_oldest_first_batch(self):
        fake = FakeService()
        for i in range(30):
            fake.add_event(i)
        stats = _run(fake, candidates=[], batch_limit=20)

        self.assertEqual(stats["scanned"], 20)
        processed = set(fake.by_status("processed"))
        pending = set(fake.by_status("pending"))
        self.assertEqual(len(processed), 20)
        self.assertEqual(len(pending), 10)
        oldest20 = {f"ev-{i:04d}" for i in range(20)}
        self.assertEqual(processed, oldest20, "按 created_at 升序消化最旧 20 条")

    def test_concurrency_guard_skips_round(self):
        fake = FakeService()
        _std_events(fake, 4)
        fake.add_event(99, status="processing")  # 已有别的批次在处理
        llm_called = {"n": 0}

        def llm(prompt):
            llm_called["n"] += 1
            return json.dumps({"memories": []})
        stats = _run(fake, llm=llm)

        self.assertTrue(stats["skipped"], "有 processing 批次 → 跳过本轮")
        self.assertTrue(stats["ok"])
        self.assertEqual(llm_called["n"], 0, "跳过轮次不得调用 LLM")
        self.assertEqual(len(fake.by_status("pending")), 4, "事件全部原样保留")
        self.assertEqual(fake.items, [], "不写入任何 memory_items")

    def test_claim_race_processes_only_claimed_subset(self):
        fake = FakeService()
        _std_events(fake, 4)
        fake.claim_drop_ids = {"ev-0000", "ev-0001"}  # 模拟并发方抢先认领
        stats = _run(fake, candidates=[_cand_json(0.9)])

        self.assertEqual(stats["claimed"], 2, "只处理实际认领到的子集")
        processed = set(fake.by_status("processed"))
        self.assertEqual(processed, {"ev-0002", "ev-0003"})
        dropped = {fake.events[i]["processing_status"] for i in ("ev-0000", "ev-0001")}
        self.assertEqual(dropped, {"pending"}, "被抢走的事件保持 pending（归并发方）")


# ==========================================
# D+E. 分写与去重
# ==========================================

class TestWriteSplitAndDedup(unittest.TestCase):

    def test_high_confidence_active_low_confidence_pending_review(self):
        fake = FakeService()
        _std_events(fake, 4)
        stats = _run(fake, candidates=[_cand_json(0.9, "用户喜欢喝无糖咖啡。"),
                                       _cand_json(0.5, "用户最近在准备考试。",
                                                  memory_type="current")])

        statuses = sorted(r["status"] for r in fake.items)
        self.assertEqual(statuses, ["active", "pending_review"])
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["pending_review"], 1)
        self.assertEqual(stats["extracted"], 2)
        # 事件照常收尾
        self.assertEqual(len(fake.by_status("processed")), 4)

    def test_active_row_fields(self):
        fake = FakeService()
        _std_events(fake, 2)
        _run(fake, candidates=[_cand_json(0.9, "用户喜欢喝无糖咖啡。")])
        row = fake.items[0]
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["created_by"], "memory_auto_extract")
        self.assertEqual(row["user_id"], TEST_USER)
        self.assertEqual(row["memory_type"], "long_term")
        self.assertEqual(row["source_event_ids"], ["ev-0000"])
        self.assertEqual(hashlib.sha256(row["content"].encode("utf-8")).hexdigest(),
                         row["content_hash"])

    def test_duplicate_skipped_but_event_processed(self):
        fake = FakeService()
        _std_events(fake, 2)
        stats1 = _run(fake, candidates=[_cand_json(0.9, "用户喜欢喝无糖咖啡。")])
        self.assertEqual(stats1["active"], 1)
        # 第二轮：新事件 + 相同内容的候选 → 跨批去重命中
        for i in range(10, 14):
            fake.add_event(i, role="user" if i % 2 == 0 else "assistant")
        stats2 = _run(fake, candidates=[_cand_json(0.9, "用户喜欢喝无糖咖啡。")])

        self.assertEqual(stats2["duplicate_skipped"], 1, "重复候选跳过并计数")
        self.assertEqual(stats2["active"], 0, "重复候选不重复插入")
        self.assertEqual(len(fake.items), 1, "memory_items 只有一份")
        self.assertEqual(len(fake.by_status("processed")), 6,
                         "重复轮次的事件同样标 processed（事实已存在即已消费）")

    def test_no_candidates_events_processed(self):
        fake = FakeService()
        _std_events(fake, 2)
        stats = _run(fake, candidates=[])  # 模型判断无内容可提取

        self.assertTrue(stats["ok"])
        self.assertEqual(stats["extracted"], 0)
        self.assertEqual(len(fake.by_status("processed")), 2,
                         "无信息轮次事件一次性消费，不无限重提")


# ==========================================
# F. 事件状态：processed / failed / 释放
# ==========================================

class TestEventStatusUpdates(unittest.TestCase):

    def test_success_updates_attempt_and_batch(self):
        fake = FakeService()
        fake.add_event(0, attempts=2)
        fake.add_event(1, role="assistant", attempts=0)
        _run(fake, candidates=[_cand_json(0.9)])

        for i in ("ev-0000", "ev-0001"):
            row = fake.events[i]
            self.assertEqual(row["processing_status"], "processed")
            self.assertEqual(row["attempt_count"],
                             (2 if i == "ev-0000" else 0) + 1,
                             "attempt_count 按原始值 +1")
            self.assertIsNotNone(row["processed_at"])
            self.assertIsNone(row["last_error"])
            self.assertTrue(row["batch_id"], "batch_id 回写溯源")

    def test_extraction_failure_marks_failed(self):
        fake = FakeService()
        _std_events(fake, 2)

        def llm(prompt):
            return ""  # 空响应 → ERR_EMPTY
        stats = _run(fake, llm=llm)

        self.assertTrue(stats["ok"], "提取失败属正常收尾（事件标 failed）")
        self.assertEqual(stats["error_code"], "EMPTY_RESPONSE")
        self.assertEqual(stats["failed_events"], 2)
        for i in ("ev-0000", "ev-0001"):
            row = fake.events[i]
            self.assertEqual(row["processing_status"], "failed")
            self.assertEqual(row["attempt_count"], 1, "失败 attempt_count+1")
            self.assertEqual(row["last_error"], "EMPTY_RESPONSE", "只存脱敏代码")
            self.assertIsNone(row["processed_at"])
        self.assertEqual(fake.items, [])

    def test_insert_failure_releases_events_back_to_pending(self):
        fake = FakeService()
        _std_events(fake, 2)
        cand = _cand_json(0.9, "用户喜欢喝无糖咖啡。")
        cand_hash = hashlib.sha256("用户喜欢喝无糖咖啡。".encode("utf-8")).hexdigest()
        fake.insert_fail_on_hash = cand_hash
        stats = _run(fake, candidates=[cand])

        self.assertFalse(stats["ok"])
        self.assertEqual(stats["error_code"], "MEMORY_ITEM_INSERT_FAILED")
        self.assertEqual(fake.items, [], "失败条目未插入")
        self.assertEqual(len(fake.by_status("pending")), 2,
                         "事件释放回 pending（幂等重试，不标 failed 不丢失）")
        self.assertEqual(fake.events["ev-0000"]["attempt_count"], 0,
                         "释放不增加 attempt_count")


# ==========================================
# G. backlog 遥测
# ==========================================

class TestTelemetry(unittest.TestCase):

    def test_stats_counts_and_log_no_content(self):
        fake = FakeService()
        _std_events(fake, 4)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stats = _run(fake, candidates=[_cand_json(0.9)])
        self.assertEqual(stats["remaining_pending"], 0, "全部消化后剩余 0")
        log = buf.getvalue()
        for key in ("scanned=", "extracted=", "active=", "pending_review=",
                    "duplicate=", "剩余pending="):
            self.assertIn(key, log, f"遥测缺少 {key}")
        self.assertNotIn("SYNTHETIC_A5_EVENT", log, "日志不得包含事件正文")
        self.assertNotIn("用户喜欢喝无糖咖啡", log, "日志不得包含候选正文")
        self.assertNotIn(TEST_USER, log, "日志不得包含 user_id")

    def test_no_pending_events_is_clean_round(self):
        fake = FakeService()
        stats = _run(fake, candidates=[])
        self.assertTrue(stats["ok"])
        self.assertEqual(stats["scanned"], 0)
        insert_calls = [c for c in fake.calls if c[0] == "memory_items"]
        self.assertEqual(insert_calls, [], "无事件时不发生任何 memory_items 写入")
        event_updates = [c for c in fake.calls
                         if c[0] == "memory_events" and c[1][0][0] == "update"]
        self.assertEqual(event_updates, [], "无事件时不发生任何事件状态更新")


# ==========================================
# H. heartbeat worker 门控与接线
# ==========================================

class TestWorkerGating(unittest.TestCase):

    def test_worker_disabled_by_default(self):
        """MEMORY_EXTRACTION_WORKER_ENABLED 缺省（false）→ worker 直接返回不跑。"""
        env = {k: v for k, v in os.environ.items()
               if k != "MEMORY_EXTRACTION_WORKER_ENABLED"}
        buf = io.StringIO()
        with patch.dict(os.environ, env, clear=True):
            with contextlib.redirect_stdout(buf):
                asyncio.run(asyncio.wait_for(
                    heartbeat.async_memory_extraction_worker(), timeout=3))
        self.assertIn("worker 不启动", buf.getvalue())

    def test_worker_enabled_runs_extraction_rounds(self):
        fake = FakeService()
        for i in range(3):
            fake.add_event(i)
        rounds = {"n": 0}
        real_run = memory_auto_extract.run_auto_extraction

        async def spy_run(sb, **kwargs):
            rounds["n"] += 1
            result = await real_run(sb, **kwargs)
            return result

        def fake_llm_factory():
            def _call(prompt):
                return json.dumps({"memories": [_cand_json(0.9)]},
                                  ensure_ascii=False)
            return _call

        async def main():
            task = asyncio.create_task(heartbeat.async_memory_extraction_worker())
            ok = await _run_until(lambda: rounds["n"] >= 2)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.assertTrue(ok, "worker 未按周期执行提取轮次")

        env = {"MEMORY_EXTRACTION_WORKER_ENABLED": "true",
               "MEMORY_EXTRACTION_INTERVAL": "0",
               "MEMORY_EXTRACTION_BATCH_SIZE": "20",
               "MEMORY_AUTO_ACTIVE_THRESHOLD": "0.75"}
        with patch.dict(os.environ, env), _patched_fast_sleep(), \
             patch.object(memory_auto_extract, "run_auto_extraction", spy_run), \
             patch.object(memory_extractor, "make_compression_llm_call",
                          fake_llm_factory):
            import server
            with patch.object(server, "supabase_service", fake), \
                 patch.object(server, "_resolve_pinecone_user_id",
                              lambda: TEST_USER):
                asyncio.run(main())
        self.assertGreaterEqual(rounds["n"], 2)
        self.assertGreaterEqual(len(fake.by_status("processed")), 3,
                                "worker 真实驱动了事件消化")

    def test_run_background_process_registers_task(self):
        """源码约束：run_background_process 任务列表注册 memory_extraction。"""
        import inspect
        src = inspect.getsource(heartbeat.run_background_process)
        self.assertIn("async_memory_extraction_worker()", src)
        self.assertIn('name="memory_extraction"', src)


# ==========================================
# 轮询辅助（与 phaseA 渠道测试同款）
# ==========================================

_POLL_INTERVAL = 0.02
_REAL_SLEEP = asyncio.sleep


async def _fast_sleep(delay=None, *args, **kwargs):
    try:
        d = float(delay)
    except (TypeError, ValueError):
        d = 0.0
    await _REAL_SLEEP(min(max(d, 0.0), _POLL_INTERVAL))


@contextlib.contextmanager
def _patched_fast_sleep():
    with patch.object(asyncio, "sleep", _fast_sleep):
        yield


async def _run_until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(_POLL_INTERVAL)
    return bool(pred())


if __name__ == "__main__":
    unittest.main()
