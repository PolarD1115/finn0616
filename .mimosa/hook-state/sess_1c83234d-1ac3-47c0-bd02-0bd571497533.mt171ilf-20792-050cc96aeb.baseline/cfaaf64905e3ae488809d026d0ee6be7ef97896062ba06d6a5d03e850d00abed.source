from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from rapidfuzz import fuzz


logger = logging.getLogger("ombre_brain.memory_write_gate")


DEFAULT_AUTO_SOURCES = {"operit", "workflow", "worker", "auto"}

LOW_SIGNAL_TERMS = {
    "刚才",
    "随手",
    "临时",
    "测试",
    "试试",
    "看一下",
    "没什么",
    "不用记",
    "无需记",
    "闲聊",
    "流水",
    "日志",
}

DURABLE_TERMS = {
    "偏好",
    "喜欢",
    "以后",
    "记得",
    "承诺",
    "决定",
    "需要",
    "项目",
    "进度",
    "原则",
    "策略",
    "规则",
    "配置",
    "接口",
    "关系",
    "记忆",
    "写入",
    "计划",
    "待办",
    "任务",
    "todo",
    "to-do",
    "未完成",
    "未做完",
    "进行中",
    "已完成",
    "done",
    "completed",
    "finished",
    "下一步",
    "后续",
    "结论",
    "发现",
    "修复",
    "部署",
    "vps",
    "api",
    "mcp",
    "gateway",
    "operit",
}

TASK_STATUS_TERMS = {
    "待办",
    "任务",
    "todo",
    "to-do",
    "未完成",
    "未做完",
    "进行中",
    "已完成",
    "done",
    "completed",
    "finished",
    "下一步",
    "后续",
}

ACTION_TERMS = {
    "要",
    "需要",
    "以后",
    "记得",
    "下次",
    "继续",
    "优先",
    "不要",
    "必须",
    "应该",
    "决定",
    "承诺",
}


@dataclass(frozen=True)
class WriteGateDecision:
    allow: bool
    decision: str
    surprise_score: float
    candidate_id: str
    source: str
    reasons: tuple[str, ...]
    repeat_count: int = 0
    max_existing_similarity: float = 0.0
    max_candidate_similarity: float = 0.0


class MemoryWriteGate:
    """Small local gate for automatic memory-write summaries."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        gate_cfg = config.get("memory_write_gate", {}) or {}
        state_dir = config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(config.get("buckets_dir", "buckets"))),
            "state",
        )
        self.enabled = bool(gate_cfg.get("enabled", True))
        self.auto_sources = {
            str(item).strip().lower()
            for item in gate_cfg.get("auto_sources", sorted(DEFAULT_AUTO_SOURCES))
            if str(item).strip()
        } or set(DEFAULT_AUTO_SOURCES)
        self.pending_threshold = self._clamp(gate_cfg.get("pending_threshold", 0.42), 0.42)
        self.grow_threshold = self._clamp(gate_cfg.get("grow_threshold", 0.72), 0.72)
        self.duplicate_similarity = self._clamp(gate_cfg.get("duplicate_similarity", 0.88), 0.88)
        self.repeat_similarity = self._clamp(gate_cfg.get("repeat_similarity", 0.82), 0.82)
        self.repeat_promote_count = max(2, int(gate_cfg.get("repeat_promote_count", 2) or 2))
        self.max_recent_candidates = max(20, int(gate_cfg.get("max_recent_candidates", 120) or 120))
        log_name = str(gate_cfg.get("candidate_log", "memory_write_candidates.jsonl") or "").strip()
        if os.path.isabs(log_name):
            self.path = log_name
        else:
            self.path = os.path.join(state_dir, log_name or "memory_write_candidates.jsonl")

    def should_gate(self, *, auto: bool = False, source: str = "") -> bool:
        if not self.enabled:
            return False
        if auto:
            return True
        source_key = self._source_key(source)
        return any(source_key == item or item in source_key for item in self.auto_sources)

    async def evaluate(
        self,
        content: str,
        *,
        source: str = "",
        bucket_mgr: Any = None,
        auto: bool = False,
    ) -> WriteGateDecision:
        text = str(content or "").strip()
        source_key = self._source_key(source) or ("auto" if auto else "manual")
        candidate_id = self._fingerprint(source_key, text)
        if not text:
            decision = WriteGateDecision(False, "skipped", 0.0, candidate_id, source_key, ("empty_content",))
            self.record(decision, text)
            return decision

        existing_similarity = await self._max_existing_similarity(text, bucket_mgr)
        if existing_similarity is None:
            decision = WriteGateDecision(
                False,
                "skipped",
                0.0,
                candidate_id,
                source_key,
                ("existing_memory_check_failed",),
            )
            self.record(decision, text)
            return decision
        recent = self._read_recent()
        repeat_count, candidate_similarity = self._repeat_stats(text, source_key, recent)
        score, reasons = self._score(
            text,
            existing_similarity=existing_similarity,
            repeat_count=repeat_count,
            candidate_similarity=candidate_similarity,
        )
        allow = False
        decision_name = "pending"
        if existing_similarity >= self.duplicate_similarity:
            decision_name = "skipped"
            score = min(score, 0.30)
            reasons.append("duplicate_existing_memory")
        elif score < self.pending_threshold:
            decision_name = "skipped"
            reasons.append("low_surprise")
        elif score >= self.grow_threshold:
            decision_name = "grow"
            allow = True
            reasons.append("high_surprise")
        elif repeat_count + 1 >= self.repeat_promote_count:
            decision_name = "grow"
            allow = True
            reasons.append("repeated_pending")
        else:
            decision_name = "pending"
            reasons.append("medium_surprise")

        decision = WriteGateDecision(
            allow,
            decision_name,
            round(score, 4),
            candidate_id,
            source_key,
            tuple(dict.fromkeys(reasons)),
            repeat_count=repeat_count,
            max_existing_similarity=round(existing_similarity, 4),
            max_candidate_similarity=round(candidate_similarity, 4),
        )
        self.record(decision, text)
        return decision

    def record(self, decision: WriteGateDecision, content: str) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            record = {
                "candidate_id": decision.candidate_id,
                "source": decision.source,
                "decision": decision.decision,
                "surprise_score": decision.surprise_score,
                "reasons": list(decision.reasons),
                "repeat_count": decision.repeat_count,
                "max_existing_similarity": decision.max_existing_similarity,
                "max_candidate_similarity": decision.max_candidate_similarity,
                "content": str(content or "").strip(),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("Memory write candidate log failed | path=%s error=%s", self.path, exc)

    def list_recent(self, limit: int = 20) -> list[dict]:
        return self._read_recent(limit=limit)

    def _score(
        self,
        text: str,
        *,
        existing_similarity: float,
        repeat_count: int,
        candidate_similarity: float,
    ) -> tuple[float, list[str]]:
        reasons: list[str] = []
        novelty = max(0.0, min(1.0, 1.0 - existing_similarity))
        if existing_similarity <= 0.05:
            novelty = 0.72
        if novelty >= 0.65:
            reasons.append("novel")

        durability = self._durability_score(text)
        if durability >= 0.60:
            reasons.append("durable_signal")
        elif durability <= 0.25:
            reasons.append("weak_durability")
        if self._has_task_status(text):
            reasons.append("task_status_signal")

        specificity = self._specificity_score(text)
        if specificity >= 0.55:
            reasons.append("specific")

        repeat_signal = 0.0
        if repeat_count > 0:
            repeat_signal = min(1.0, 0.55 + repeat_count * 0.25)
            reasons.append("seen_before")
        elif candidate_similarity >= self.repeat_similarity:
            repeat_signal = 0.45
            reasons.append("similar_pending_candidate")

        score = (
            durability * 0.35
            + novelty * 0.25
            + specificity * 0.20
            + repeat_signal * 0.20
        )
        return max(0.0, min(1.0, score)), reasons

    def _durability_score(self, text: str) -> float:
        lowered = text.lower()
        hits = sum(1 for term in DURABLE_TERMS if term in lowered)
        score = min(1.0, 0.12 + hits * 0.16)
        if any(term in lowered for term in ACTION_TERMS):
            score += 0.18
        if len(text) >= 45:
            score += 0.12
        if any(term in lowered for term in LOW_SIGNAL_TERMS):
            score -= 0.34
        return max(0.0, min(1.0, score))

    @staticmethod
    def _specificity_score(text: str) -> float:
        lowered = text.lower()
        score = 0.0
        if len(text) >= 45:
            score += 0.25
        if re.search(r"\d{4}-\d{2}-\d{2}|\d+(?:\.\d+)+|[A-Za-z]+/[A-Za-z0-9._-]+", text):
            score += 0.25
        if re.search(r"\b[A-Z][A-Za-z0-9._:-]{2,}\b|[\u4e00-\u9fff]{2,}(?:系统|项目|插件|工具|记忆|策略|偏好)", text):
            score += 0.25
        if any(term in lowered for term in ACTION_TERMS):
            score += 0.25
        if any(term in lowered for term in TASK_STATUS_TERMS):
            score += 0.20
        return max(0.0, min(1.0, score))

    @staticmethod
    def _has_task_status(text: str) -> bool:
        lowered = text.lower()
        return any(term in lowered for term in TASK_STATUS_TERMS)

    async def _max_existing_similarity(self, text: str, bucket_mgr: Any) -> float | None:
        if bucket_mgr is None:
            return 0.0
        try:
            buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as exc:
            logger.warning("Memory write existing-memory check failed: %s", exc)
            return None
        normalized = self._normalize(text)
        max_score = 0.0
        for bucket in buckets or []:
            meta = bucket.get("metadata", {}) if isinstance(bucket, dict) else {}
            haystack = self._normalize(
                " ".join(
                    [
                        str(meta.get("name") or ""),
                        " ".join(str(tag) for tag in meta.get("tags", []) or []),
                        " ".join(str(item) for item in meta.get("domain", []) or []),
                        str(bucket.get("content") or ""),
                    ]
                )
            )
            if not haystack:
                continue
            max_score = max(max_score, fuzz.token_set_ratio(normalized, haystack) / 100.0)
        return max_score

    def _repeat_stats(self, text: str, source: str, recent: list[dict]) -> tuple[int, float]:
        normalized = self._normalize(text)
        repeat_count = 0
        max_score = 0.0
        for record in recent:
            if str(record.get("source") or "").lower() != source:
                continue
            if record.get("decision") not in {"pending", "skipped"}:
                continue
            other = self._normalize(record.get("content") or "")
            if not other:
                continue
            score = fuzz.token_set_ratio(normalized, other) / 100.0
            max_score = max(max_score, score)
            if score >= self.repeat_similarity:
                repeat_count += 1
        return repeat_count, max_score

    def _read_recent(self, limit: int | None = None) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        rows: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
        except OSError:
            return []
        cap = limit or self.max_recent_candidates
        return rows[-max(1, int(cap)) :]

    @staticmethod
    def _normalize(text: str) -> str:
        text = str(text or "").lower()
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _source_key(source: str) -> str:
        return re.sub(r"\s+", "-", str(source or "").strip().lower())

    @staticmethod
    def _fingerprint(source: str, text: str) -> str:
        body = f"{source}\n{MemoryWriteGate._normalize(text)}".encode("utf-8")
        return hashlib.sha256(body).hexdigest()[:16]

    @staticmethod
    def _clamp(value: Any, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return max(0.0, min(1.0, number))
