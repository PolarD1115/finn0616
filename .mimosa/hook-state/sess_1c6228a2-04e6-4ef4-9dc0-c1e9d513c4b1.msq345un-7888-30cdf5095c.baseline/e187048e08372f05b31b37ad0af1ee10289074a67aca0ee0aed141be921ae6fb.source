"""
消息聚合器 (Message Aggregator)
================================
让 AI 更像真人：收到消息不立刻回，而是等一个短窗口，把用户连发的好几条
消息合并成一轮上下文再交给 LLM 处理。

设计：
- 每个会话 (session_key，如 TG 的 chat_id / QQ 的 target_id) 独立缓冲。
- 收到消息 → 入队 + 重置"静默计时器"(默认 10s)；窗口内再来消息就再次重置。
- 计时器到点 (用户停手了) → 把队列里的多条合并，调用注册的 handler 处理。
- 双重保护，避免用户一直刷屏导致永远不回：
    - 硬上限条数 (MAX_MSGS)：攒够就立即触发
    - 硬上限时长 (MAX_WAIT)：从第一条起超过就强制触发
- 纯内存、纯 asyncio，无外部依赖。每个渠道各自 import 复用。

用法 (在渠道的消息入口)：
    agg = get_aggregator("TG", handler=_handle_merged)   # handler(session_key, merged_text, items)
    await agg.feed(chat_id, text, meta={...})            # 收到一条就 feed 一次

    async def _handle_merged(session_key, merged_text, items):
        # 这里写原来"调 LLM + 回复 + 存记忆"的逻辑，text 用 merged_text
        ...

环境变量 (可调，缺省即用默认)：
    MSG_AGGREGATE_ENABLED   总开关，默认 true；设 false 则 feed 立即触发 (等价旧行为)
    MSG_AGGREGATE_WINDOW    静默窗口秒数，默认 10.0
    MSG_AGGREGATE_MAX_MSGS  单会话最多攒几条，默认 8
    MSG_AGGREGATE_MAX_WAIT  单会话最长等待秒数，默认 20.0
"""

import os
import time
import asyncio


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _enabled() -> bool:
    return os.environ.get("MSG_AGGREGATE_ENABLED", "true").strip().lower() not in ("0", "false", "no")


class _Session:
    """单个会话的聚合状态。"""
    __slots__ = ("items", "timer", "first_ts", "lock")

    def __init__(self):
        self.items = []          # [(text, meta), ...] 按到达顺序
        self.timer = None        # asyncio.TimerHandle
        self.first_ts = 0.0      # 本轮第一条消息的时间戳
        self.lock = asyncio.Lock()


class MessageAggregator:
    """一个渠道一个实例；内部按 session_key 分桶聚合。"""

    def __init__(self, name: str, handler):
        """
        name    : 渠道名 (仅用于日志区分，如 "TG"/"QQ")
        handler : async 回调 handler(session_key, merged_text, items) —— 真正的处理逻辑
        """
        self.name = name
        self.handler = handler
        self._sessions: dict[str, _Session] = {}

    # ---- 对外主入口 ----
    async def feed(self, session_key, text: str, meta: dict | None = None):
        """喂入一条消息。停手后 (静默窗口到) 会自动触发合并处理。"""
        text = (text or "").strip()
        if not text:
            return
        session_key = str(session_key)
        meta = meta or {}

        # 关闭聚合：退化为"来一条立即处理一条"(等价改造前行为)
        if not _enabled():
            await self._dispatch(session_key, [(text, meta)])
            return

        window = _env_float("MSG_AGGREGATE_WINDOW", 10.0)
        max_msgs = _env_int("MSG_AGGREGATE_MAX_MSGS", 8)
        max_wait = _env_float("MSG_AGGREGATE_MAX_WAIT", 20.0)

        sess = self._sessions.get(session_key)
        if sess is None:
            sess = _Session()
            self._sessions[session_key] = sess

        async with sess.lock:
            now = time.monotonic()
            if not sess.items:
                sess.first_ts = now
            sess.items.append((text, meta))

            # 取消上一个静默计时器
            if sess.timer is not None:
                sess.timer.cancel()
                sess.timer = None

            # 条数上限：立即触发
            if len(sess.items) >= max_msgs:
                await self._flush(session_key, reason=f"达到{max_msgs}条上限")
                return

            # 时长上限：从第一条起超时则立即触发
            if now - sess.first_ts >= max_wait:
                await self._flush(session_key, reason=f"等待超过{max_wait}s")
                return

            # 否则：重置静默计时器，等用户停手
            loop = asyncio.get_running_loop()
            sess.timer = loop.call_later(
                window,
                lambda: asyncio.ensure_future(self._on_timeout(session_key)),
            )

    async def _on_timeout(self, session_key: str):
        sess = self._sessions.get(session_key)
        if sess is None:
            return
        async with sess.lock:
            if sess.items:
                await self._flush(session_key, reason="静默窗口到")

    async def _flush(self, session_key: str, reason: str = ""):
        """把当前会话缓冲的消息合并并派发。调用方需已持有 sess.lock。"""
        sess = self._sessions.get(session_key)
        if sess is None or not sess.items:
            return
        items = sess.items
        sess.items = []
        if sess.timer is not None:
            sess.timer.cancel()
            sess.timer = None
        # 处理期间不占用 lock 太久：复制后即释放语义 (这里已在 lock 内，dispatch 内部不再碰 sess)
        await self._dispatch(session_key, items, reason=reason)

    async def _dispatch(self, session_key: str, items: list, reason: str = ""):
        """合并文本并调用业务 handler。"""
        if not items:
            return
        if len(items) == 1:
            merged = items[0][0]
        else:
            # 多条：按行拼接，保留顺序，让模型看到用户是分几条发的
            merged = "\n".join(t for t, _ in items)
        try:
            await self.handler(session_key, merged, items)
        except Exception as e:
            # 聚合器本身不吞业务异常的语义，但要防止一个会话崩溃影响整体
            print(f"⚠️ [{self.name} 聚合器] handler 处理失败 session={session_key}: {e}")


# ---- 每渠道单例管理 ----
_instances: dict[str, MessageAggregator] = {}


def get_aggregator(name: str, handler) -> MessageAggregator:
    """按渠道名取/建聚合器单例。handler 只在首次创建时绑定。"""
    inst = _instances.get(name)
    if inst is None:
        inst = MessageAggregator(name, handler)
        _instances[name] = inst
    return inst
