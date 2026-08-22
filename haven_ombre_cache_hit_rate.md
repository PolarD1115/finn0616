# Haven-Ombre 缓存命中率处理 —— 代码移植参考

> 来源仓库: https://github.com/Yinglianchun/Haven-Ombre.git (commit 284c9c7, 2026-08-15 拉取)
> 涉及文件: `gateway.py`、`gateway_state.py`
> 本地克隆: `新网关/.haven_ombre_ref/`

## 一句话总结

Haven-Ombre 的"缓存命中率处理"实际是 **LLM 上游 prompt cache 命中统计**，不是自建缓存。
它从上游返回的 `usage` 里提取两套字段（OpenAI 一套、Anthropic 一套），统一写入 SQLite
`upstream_usage` 表（保留最近 200 条），并打一条 info 日志。**仓库没有现成的命中率百分比**，
需要自行计算 `hit / (hit + miss)`。

整套逻辑分三层：**触发缓存 → 提取 usage → 归一化存储 + 日志 + debug 端点**。

---

## 1. 触发上游缓存（让上游更容易命中）

`gateway.py:3369` `GatewayService._apply_prompt_cache_hints`

```python
def _apply_prompt_cache_hints(self, payload: dict[str, Any], session_id: str) -> None:
    model = str(payload.get("model") or "").strip()
    route = self._resolve_upstream_for_model(model)
    upstream = route["upstream"]
    strategy = str(upstream.get("prompt_cache") or "").strip().lower()
    if strategy != "openai":
        return
    # 给 OpenAI 上游打 cache_key，让同一 session 的 prompt 走上游缓存
    payload.setdefault("prompt_cache_key", session_id)
    retention = str(upstream.get("prompt_cache_retention") or "").strip()
    if retention:
        payload.setdefault("prompt_cache_retention", retention)
```

- 由配置 `upstreams[].prompt_cache` 控制，目前只支持 `"openai"` 策略。
- Anthropic 侧的 `cache_control` 注入不在此函数（在 Anthropic 协议转换链路里）。

---

## 2. 存储层：SQLite `upstream_usage` 表

`gateway_state.py` — `GatewayStateStore`（SQLite，文件位于 `buckets_dir/gateway_state.db`）

### 2.1 建表（gateway_state.py:117）

```python
CREATE TABLE IF NOT EXISTS upstream_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    round_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    route TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    prompt_cache_hit_tokens INTEGER,      -- OpenAI 专有
    prompt_cache_miss_tokens INTEGER,     -- OpenAI 专有
    cached_tokens INTEGER,                -- OpenAI prompt_tokens_details.cached_tokens
    cache_read_input_tokens INTEGER,      -- Anthropic 专有
    cache_creation_input_tokens INTEGER,  -- Anthropic 专有
    usage_json TEXT NOT NULL DEFAULT '{}' -- 原始 usage 完整 JSON（保底）
)
CREATE INDEX IF NOT EXISTS idx_upstream_usage_lookup
    ON upstream_usage (session_id, id DESC)
```

设计要点：
- 同时容纳两套厂商字段，互不干扰；任何一厂商缺字段即为 NULL。
- 额外存 `usage_json` 原文，避免后续字段扩展时丢信息。

### 2.2 写入（gateway_state.py:549 `record_upstream_usage`）

```python
def record_upstream_usage(
    self, *, session_id: str, round_id: int, model: str, route: str,
    usage: dict[str, Any], max_entries: int = 200,
) -> int:
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    safe_usage = dict(usage or {})
    prompt_tokens = safe_usage.get("prompt_tokens") or safe_usage.get("input_tokens")
    completion_tokens = safe_usage.get("completion_tokens") or safe_usage.get("output_tokens")
    prompt_details = safe_usage.get("prompt_tokens_details")
    cached_tokens = None
    if isinstance(prompt_details, dict):
        cached_tokens = prompt_details.get("cached_tokens")

    conn = self._connect()
    cursor = conn.execute(
        """
        INSERT INTO upstream_usage (
            session_id, round_id, created_at, model, route,
            prompt_tokens, completion_tokens,
            prompt_cache_hit_tokens, prompt_cache_miss_tokens,
            cached_tokens, cache_read_input_tokens, cache_creation_input_tokens,
            usage_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(session_id or "default"), int(round_id), created_at,
            str(model or ""), str(route or ""),
            _optional_int(prompt_tokens), _optional_int(completion_tokens),
            _optional_int(safe_usage.get("prompt_cache_hit_tokens")),
            _optional_int(safe_usage.get("prompt_cache_miss_tokens")),
            _optional_int(cached_tokens),
            _optional_int(safe_usage.get("cache_read_input_tokens")),
            _optional_int(safe_usage.get("cache_creation_input_tokens")),
            json.dumps(safe_usage, ensure_ascii=False),
        ),
    )
    usage_id = int(cursor.lastrowid or 0)
    # 仅保留最近 max_entries 条（默认 200），老的删除
    if max_entries > 0:
        conn.execute(
            """
            DELETE FROM upstream_usage
            WHERE id NOT IN (
                SELECT id FROM upstream_usage ORDER BY id DESC LIMIT ?
            )
            """,
            (max(1, int(max_entries)),),
        )
    conn.commit()
    conn.close()
    return usage_id
```

要点：
- `cached_tokens` 来自 `usage.prompt_tokens_details.cached_tokens`（OpenAI 嵌套结构），需要先解包再入库。
- **自动滚动清理**：保留最近 200 条，靠子查询 `id DESC LIMIT ?` 删旧。这是个简单的 ring-buffer 式保留策略。
- `prompt_tokens`/`completion_tokens` 同时兼容 OpenAI 和 Anthropic 命名（`input_tokens`/`output_tokens`）。

### 2.3 读取（gateway_state.py:611 `list_upstream_usage`）

```python
def list_upstream_usage(self, *, session_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
    safe_limit = max(1, min(100, int(limit or 20)))  # 强制 1~100
    conn = self._connect()
    if session_id:
        rows = conn.execute(
            """SELECT id, session_id, round_id, created_at, model, route,
                      prompt_tokens, completion_tokens,
                      prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                      cached_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                      usage_json
               FROM upstream_usage WHERE session_id = ? ORDER BY id DESC LIMIT ?""",
            (session_id, safe_limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, session_id, round_id, created_at, model, route,
                      prompt_tokens, completion_tokens,
                      prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                      cached_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                      usage_json
               FROM upstream_usage ORDER BY id DESC LIMIT ?""",
            (safe_limit,),
        ).fetchall()
    conn.close()
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            usage = json.loads(row["usage_json"] or "{}")
        except json.JSONDecodeError:
            usage = {}
        items.append({
            "id": row["id"], "session_id": row["session_id"], "round_id": row["round_id"],
            "created_at": row["created_at"], "model": row["model"] or "", "route": row["route"] or "",
            "prompt_tokens": row["prompt_tokens"], "completion_tokens": row["completion_tokens"],
            "prompt_cache_hit_tokens": row["prompt_cache_hit_tokens"],
            "prompt_cache_miss_tokens": row["prompt_cache_miss_tokens"],
            "cached_tokens": row["cached_tokens"],
            "cache_read_input_tokens": row["cache_read_input_tokens"],
            "cache_creation_input_tokens": row["cache_creation_input_tokens"],
            "usage": usage,  # 原始 usage，便于事后分析
        })
    return items
```

---

## 3. 网关层：提取、归一化、记录

### 3.1 从上游响应里提取 usage（gateway.py:5148）

非流式：`_log_cache_usage_from_response` → 取 `response.json().usage`。
流式：`_log_cache_usage_from_stream_state` → 取 `stream_state["usage"]`。
两者都委托给核心方法 `_log_cache_usage`。

```python
def _log_cache_usage_from_response(self, session_id, model, upstream_response, route):
    try:
        body = upstream_response.json()
    except ValueError:
        return None
    usage = body.get("usage") if isinstance(body, dict) else None
    if isinstance(usage, dict) and usage:
        self._log_cache_usage(session_id, model, route, usage)
        return usage          # 返回给上层用于入库
    return None

def _log_cache_usage_from_stream_state(self, session_id, model, stream_state, route):
    usage = stream_state.get("usage")
    if isinstance(usage, dict) and usage:
        self._log_cache_usage(session_id, model, route, usage)
        return usage
    return None
```

### 3.2 解析两套字段并打日志（gateway.py:5178 `_log_cache_usage`）

这是"命中率"原始数据的真正来源。注意它**只采集、不计算百分比**。

```python
def _log_cache_usage(self, session_id: str, model: str, route: str, usage: dict[str, Any]) -> None:
    # OpenAI 专有
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    # 通用
    prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
    completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens")
    # Anthropic 专有
    cache_read_tokens = usage.get("cache_read_input_tokens")
    cache_creation_tokens = usage.get("cache_creation_input_tokens")
    # OpenAI 嵌套
    prompt_details = usage.get("prompt_tokens_details")
    cached_tokens = None
    if isinstance(prompt_details, dict):
        cached_tokens = prompt_details.get("cached_tokens")

    # 任何缓存字段都没有就不记，避免噪音
    if (hit is None and miss is None and cached_tokens is None
            and cache_read_tokens is None and cache_creation_tokens is None):
        return

    logger.info(
        "Gateway upstream cache usage | session=%s model=%s route=%s "
        "prompt_tokens=%s completion_tokens=%s prompt_cache_hit_tokens=%s "
        "prompt_cache_miss_tokens=%s cached_tokens=%s cache_read_input_tokens=%s "
        "cache_creation_input_tokens=%s",
        session_id, model, route,
        prompt_tokens, completion_tokens,
        hit, miss, cached_tokens, cache_read_tokens, cache_creation_tokens,
    )
```

### 3.3 Anthropic usage → OpenAI usage 归一化（gateway.py:5568）

当上游是 Anthropic 协议、但网关对外是 OpenAI 协议时，把 Anthropic 的缓存字段
映射成 OpenAI 风格，供下游统一消费。

```python
def _anthropic_usage_to_openai_usage(self, usage: dict[str, Any]) -> dict[str, Any]:
    prompt_tokens = self._usage_int(
        usage.get("prompt_tokens") if usage.get("prompt_tokens") is not None else usage.get("input_tokens")
    )
    completion_tokens = self._usage_int(
        usage.get("completion_tokens") if usage.get("completion_tokens") is not None else usage.get("output_tokens")
    )
    openai_usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    cache_read = usage.get("cache_read_input_tokens")
    cache_creation = usage.get("cache_creation_input_tokens")
    if cache_read is not None:
        openai_usage["cache_read_input_tokens"] = self._usage_int(cache_read)
        # 同时填到 OpenAI 的 prompt_tokens_details.cached_tokens，兼容只认这个字段的消费方
        openai_usage["prompt_tokens_details"] = {"cached_tokens": openai_usage["cache_read_input_tokens"]}
    if cache_creation is not None:
        openai_usage["cache_creation_input_tokens"] = self._usage_int(cache_creation)
    return openai_usage

@staticmethod
def _usage_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
```

### 3.4 调用链：usage 如何流入存储

非流式（gateway.py:1907/1946）和流式（gateway.py:5074）拿到 usage 后，都走同一个
`_record_successful_round`，把 usage 作为 `upstream_usage` 参数传入：

```python
# _record_successful_round 片段 (gateway.py:3954)
if isinstance(upstream_usage, dict) and upstream_usage:
    try:
        self.state_store.record_upstream_usage(
            session_id=session_id, round_id=round_id,
            model=model, route=route, usage=upstream_usage,
        )
    except Exception as exc:
        logger.warning(
            "Gateway upstream usage record failed | session=%s round=%s error=%s",
            session_id, round_id, exc,
        )
```

流式路径（gateway.py:5063 `_finalize_stream_turn`）：

```python
async def _finalize_stream_turn(self, session_id, model, route, stream_state, recalled_ids,
                                user_message, client="", injection_debug=None):
    upstream_usage = self._log_cache_usage_from_stream_state(
        session_id, model, stream_state, route=route,
    )
    # ... 构建 assistant_message ...
    await self._record_successful_round(
        session_id, recalled_ids, injection_debug,
        user_message=user_message, assistant_message=assistant_message,
        model=model, client=client, route=route,
        upstream_usage=upstream_usage,
    )
```

---

## 4. Debug 端点

`gateway.py:2572` `handle_upstream_usage_debug`，路由 `GET /api/debug/upstream-usage`
（注册在 gateway.py:21364）。

```python
async def handle_upstream_usage_debug(self, request: Request) -> JSONResponse:
    auth_result = self._authorize(request.headers.get("Authorization", ""))
    if auth_result is not None:
        return auth_result
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        limit = 20
    session_id = str(request.query_params.get("session_id", "") or "").strip()
    return JSONResponse({
        "items": self.state_store.list_upstream_usage(session_id=session_id, limit=limit)
    })
```

用法示例：`GET /api/debug/upstream-usage?session_id=xxx&limit=50`

---

## 5. 命中率计算（仓库未实现，需自行补）

仓库只存了原始 token 数，没有算百分比。移植后若要展示命中率，按厂商分别算：

```python
def cache_hit_rate(row: dict) -> float | None:
    # OpenAI 路径：prompt_cache_hit/miss
    hit = row.get("prompt_cache_hit_tokens")
    miss = row.get("prompt_cache_miss_tokens")
    if hit is not None or miss is not None:
        hit = hit or 0
        miss = miss or 0
        total = hit + miss
        return (hit / total) if total else None
    # OpenAI 备用：prompt_tokens_details.cached_tokens / prompt_tokens
    cached = row.get("cached_tokens")
    prompt = row.get("prompt_tokens")
    if cached is not None and prompt:
        return cached / prompt
    # Anthropic 路径：cache_read / (cache_read + cache_creation)
    read = row.get("cache_read_input_tokens")
    creation = row.get("cache_creation_input_tokens")
    if read is not None or creation is not None:
        read = read or 0
        creation = creation or 0
        total = read + creation
        return (read / total) if total else None
    return None
```

---

## 6. 移植到新网关的要点

1. **存储**：复用现有 SQLite（项目里 gateway_state 类似设计），加 `upstream_usage` 表即可。
   注意 `cached_tokens` 是嵌套字段，入库前要解包。
2. **双协议**：新网关若同时对接 OpenAI 和 Anthropic 上游，务必保留两套字段；
   归一化函数 `_anthropic_usage_to_openai_usage` 可直接借用。
3. **采样而非全量**：仓库对每次成功请求都记，靠 200 条滚动清理控量。
   若新网关请求量大，考虑改用采样或更长保留窗口。
4. **日志先行**：`_log_cache_usage` 先打 info 日志再入库，即使入库失败也有日志可查——
   调试期很有用，建议保留。
5. **命中率要自己算**：仓库不提供，参考第 5 节。注意 OpenAI 的 `cached_tokens`
   和 `prompt_cache_hit_tokens` 是两个不同口径，别混用。
