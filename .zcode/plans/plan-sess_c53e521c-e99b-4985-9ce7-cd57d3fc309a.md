# Home Runtime 后台自主生活接入计划

## 架构决策（已确认）
- **独立循环**：仿宠物照料（`run_pet_care_tool_loop` + `async_pet_house_tick`），新建独立协程 + 循环函数，不动通用 `call_tool`
- **全量注册 + 分层开关**：16 个 Home 工具一次注册，用 `HOME_AUTONOMY_PHASE` 环境变量按阶段裁剪白名单
- **action_key 代码自动注入**：在循环函数内生成唯一 key 塞进 args，不让 LLM 控制
- **护栏**：进程内存 cooldown（按工具）+ 连续失败熔断 + 固定身份 fixed_args

## 改动文件清单（3 个文件）

---

### 文件 1：`tool_loop.py`（主改动，6 处）

#### A. 软导入 home.service（约 42 行后，weather_tools 软导入之后）
```python
# 🏠 Home Runtime（软导入，后台自主生活用）
try:
    import home.service as _home_svc
    _HAS_HOME_RUNTIME = True
except Exception:
    _home_svc = None
    _HAS_HOME_RUNTIME = False
```

#### B. 新增 Home Autonomy 常量（约 48 行后，MAX_TOOL_CALLS 之后）
```python
# ============================================================
# 🏠 Home Runtime 后台自主生活
# ============================================================
HOME_AUTONOMY_ENABLED = os.environ.get("HOME_AUTONOMY_ENABLED", "false").strip().lower() in ("1", "true", "yes")
HOME_AUTONOMY_PHASE = int(os.environ.get("HOME_AUTONOMY_PHASE", "0"))  # 0关/1只读/2+信件/3+种植烹饪/4+基础生活
HOME_AUTONOMY_INTERVAL = int(os.environ.get("HOME_AUTONOMY_INTERVAL", "7200"))  # 默认2小时
```

#### C. TOOL_REGISTRY 新增 16 个 Home 工具条目（约 480 行后，TOOL_REGISTRY 闭合 `}` 前）

参照猫工具模板（345-417 行），分两类：

**只读工具（4 个，无 fixed_args）**：
- `home_observe` → callable: `_home_svc.observe_home`
- `garden_observe` → callable: `_home_svc.garden_observe`
- `pantry_observe` → callable: `_home_svc.pantry_observe`
- `list_letters` → callable: `_home_svc.list_letters`，schema 有可选 `status_filter`

**写工具（12 个，fixed_args 固定身份，action_key 不放 fixed_args 由循环注入）**：
- `plant_seed` → fixed_args: `{"actor_key": "ai_primary"}`，schema required: `["seed_key"]`
- `water_plant` → fixed_args: `{"actor_key": "ai_primary"}`，schema required: `["plant_id"]`
- `harvest_plant` → fixed_args: `{"actor_key": "ai_primary"}`，schema required: `["plant_id"]`
- `cook_recipe` → fixed_args: `{"actor_key": "ai_primary"}`，schema required: `["recipe_key"]`
- `eat_dish` → fixed_args: `{"actor_key": "ai_primary"}`，schema required: `["dish_id"]`
- `feed_member` → fixed_args: `{"actor_key": "ai_primary"}`，schema required: `["target_key", "dish_id"]`
- `write_letter` → fixed_args: `{"author_key": "ai_primary"}`，schema required: `["title", "content"]`，可选 `preview/room_key`
- `leave_note` → fixed_args: `{"author_key": "ai_primary"}`，schema required: `["room_key", "content"]`
- `home_enter_room` → callable: `_home_svc.enter_room`，fixed_args: `{"actor_key": "ai_primary"}`，schema required: `["room_key"]`
- `home_rest` → callable: `_home_svc.rest`，fixed_args: `{"actor_key": "ai_primary", "mode": "rest"}`，schema required: `["duration_minutes"]`
- `home_sleep` → callable: `_home_svc.sleep`，fixed_args: `{"actor_key": "ai_primary"}`，schema required: `["duration_minutes"]`
- `home_spend_time` → callable: `_home_svc.spend_time`，fixed_args: `{"actor_key": "ai_primary"}`，schema required: `["target_key", "activity", "duration_minutes"]`

**关键设计**：action_key 不在 schema properties 里（LLM 不需要知道），不在 required 里，不在 fixed_args 里。循环函数执行前代码注入 args。`_validate_args` 宽松放行额外字段（555 行），不会拦截。

#### D. 新增分层白名单 + 限频/熔断常量（ACTIVITY_TOOL_MAP 之后，约 502 行后）
```python
# ============================================================
# 🏠 Home Runtime 后台自主工具白名单（按 phase 分层）
# ============================================================
_HOME_PHASE_TOOLS: dict[int, list[str]] = {
    1: ["home_observe", "garden_observe", "pantry_observe", "list_letters"],
    2: ["home_observe", "garden_observe", "pantry_observe", "list_letters",
        "write_letter", "leave_note"],
    3: ["home_observe", "garden_observe", "pantry_observe", "list_letters",
        "write_letter", "leave_note",
        "plant_seed", "water_plant", "harvest_plant", "cook_recipe", "eat_dish", "feed_member"],
    4: ["home_observe", "garden_observe", "pantry_observe", "list_letters",
        "write_letter", "leave_note",
        "plant_seed", "water_plant", "harvest_plant", "cook_recipe", "eat_dish", "feed_member",
        "home_enter_room", "home_rest", "home_sleep", "home_spend_time"],
}
_HOME_WRITE_TOOLS = {
    "write_letter", "leave_note", "plant_seed", "water_plant", "harvest_plant",
    "cook_recipe", "eat_dish", "feed_member",
    "home_enter_room", "home_rest", "home_sleep", "home_spend_time",
}
_HOME_OBSERVE_ONLY = {"home_observe", "garden_observe", "pantry_observe", "list_letters"}

# 按工具的冷却时间（秒）——靠 cooldown 控制日频次，无需 DB 查询日上限
_HOME_TOOL_COOLDOWN: dict[str, int] = {
    "write_letter": 43200,      # 12h → 日最多2次
    "leave_note": 28800,        # 8h  → 日最多3次
    "plant_seed": 43200,        # 12h
    "water_plant": 14400,       # 4h
    "harvest_plant": 7200,      # 2h（状态机拦截未成熟）
    "cook_recipe": 28800,       # 8h
    "eat_dish": 14400,          # 4h
    "feed_member": 28800,       # 8h
    "home_enter_room": 14400,   # 4h
    "home_rest": 14400,         # 4h
    "home_sleep": 28800,        # 8h
    "home_spend_time": 14400,   # 4h
}
# 进程内状态：冷却时间戳 + 连续失败计数（进程重启归零，保守可接受）
_home_tool_last_fire: dict[str, float] = {}
_home_tool_fail_count: dict[str, int] = {}
_HOME_BREAKER_THRESHOLD = 3  # 连续失败3次 → 本轮跳过该工具
```

#### E. 新增 action_key 生成函数（call_tool 函数之前，约 916 行前）
```python
def _gen_home_action_key(tool_name: str, now_bj) -> str:
    """为后台自主 Home 工具调用生成唯一 action_key。
    格式：auto_{tool}_{YYYYMMDDHHmmss}_{hex6}
    每次调用独立 key，保证唯一；service 层状态机校验防不合逻辑的重复。"""
    import secrets
    ts = now_bj.strftime("%Y%m%d%H%M%S")
    rnd = secrets.token_hex(3)
    return f"auto_{tool_name}_{ts}_{rnd}"
```

#### F. 新增 `run_home_autonomy_tool_loop` 函数（文件末尾，run_pet_care_tool_loop 之后）

仿 `run_pet_care_tool_loop`（1300-1441 行）四阶段结构：

```
async def run_home_autonomy_tool_loop(client, ask_llm, system_ctx, now_bj, log_prefix="🏠 [Home自主]") -> tuple[str, list[str]] | None:
    """Home Runtime 后台自主生活工具循环。

    按 HOME_AUTONOMY_PHASE 裁剪可用工具，先观察家庭状态，再让 LLM 自主决策
    做什么（种植/烹饪/写信/休息等），执行工具调用（注入 action_key + 限频 + 熔断），
    最后生成一条生活日记。

    返回 (log_text, tools_used)；None 表示本轮跳过。
    """
    # 0. phase 检查
    phase = HOME_AUTONOMY_PHASE
    if phase < 1:
        return None
    allowed_tools = _HOME_PHASE_TOOLS.get(phase, [])

    # 阶段1：home_observe 拿全局状态（只读，不限频）
    obs_res = await call_tool("home_observe", {})
    obs_text = obs_res.get("text", "")

    # 阶段2：构建工具 schema + LLM 决策
    #   - 拼接 allowed_tools 的 schema_block（仿 run_pet_care_tool_loop 1338-1355 行）
    #   - prompt 含观察状态 + 时间 + 工具列表 + 规则（最多 MAX_TOOL_CALLS 个、
    #     先 observe 拿 UUID 再操作、不要调白名单外工具）
    #   - LLM 输出 {"tool_calls": [{"name": "...", "args": {...}}]}

    # 阶段3：执行 tool_calls
    #   for tc in tc_list[:MAX_TOOL_CALLS]:
    #     name, args = ...
    #     if name not in allowed: skip
    #     if name in _HOME_WRITE_TOOLS:
    #       # 限频检查
    #       last = _home_tool_last_fire.get(name, 0)
    #       if now - last < _HOME_TOOL_COOLDOWN[name]: skip (冷却中)
    #       # 熔断检查
    #       if _home_tool_fail_count.get(name, 0) >= _HOME_BREAKER_THRESHOLD: skip
    #       # 注入 action_key
    #       args["action_key"] = _gen_home_action_key(name, now_bj)
    #     res = await call_tool(name, args)
    #     if res["ok"]:
    #       _home_tool_last_fire[name] = now
    #       _home_tool_fail_count[name] = 0  # 重置熔断
    #     else:
    #       _home_tool_fail_count[name] += 1

    # 阶段4：基于真实结果生成生活日记（仿 run_pet_care_tool_loop 1416-1429 行）
    #   返回 (final_log, tools_used_list)
```

**限频/熔断细节**：
- 只读工具（observe/list_letters）不限频、不熔断
- 写工具执行前检查 cooldown（进程内存 `_home_tool_last_fire`）和熔断计数（`_home_tool_fail_count`）
- 成功后重置熔断计数、更新冷却时间戳；失败后递增熔断计数
- 熔断只跳过当前工具，不影响同轮其他工具

---

### 文件 2：`heartbeat.py`（2 处改动）

#### G. 新增 `async_home_autonomy_tick` 协程（`async_pet_house_tick` 之后，约 1434 行后）

仿 `async_pet_house_tick`（1382-1433 行）+ `async_free_activity`（399-589 行）模式：
```python
async def async_home_autonomy_tick():
    """🏠 Home Runtime 后台自主生活 tick。
    按 HOME_AUTONOMY_INTERVAL（默认 7200s）触发，让 AI 自主观察家庭状态并决定做什么。
    """
    from server import _get_llm_client, _ask_llm_async, _save_memory_to_db, _get_now_bj, _build_channel_context
    import tool_loop

    print("🏠 Home 自主生活神经已上线...")
    interval = int(os.environ.get("HOME_AUTONOMY_INTERVAL", "7200"))
    enabled = os.environ.get("HOME_AUTONOMY_ENABLED", "false").strip().lower() not in ("0", "false", "no")
    if not enabled:
        print("🏠 Home 自主生活已关闭 (HOME_AUTONOMY_ENABLED=false)")
        return

    while True:
        await asyncio.sleep(interval)
        try:
            client = _get_llm_client("background")
            if not client:
                continue
            now_bj = _get_now_bj()
            system_ctx = await _build_channel_context("家庭自主生活观察", channel_tag="TG_MSG")
            result = await tool_loop.run_home_autonomy_tool_loop(
                client=client, ask_llm=_ask_llm_async,
                system_ctx=system_ctx, now_bj=now_bj,
            )
            if result is None:
                continue
            log_text, tools_used = result
            await asyncio.to_thread(
                _save_memory_to_db,
                f"🏠 家庭自主·生活", log_text, "记事", "平静", "Home_Autonomy"
            )
            print(f"🏠 [Home自主] 做了 {tools_used}: {log_text[:30]}...")
        except Exception as e:
            print(f"❌ Home 自主生活出错: {e}")
```

#### H. 注册到 `run_background_process` 的 tasks 列表（1450 行 `async_pet_house_tick` 后）
```python
asyncio.create_task(async_home_autonomy_tick(), name="home_autonomy"),
```

---

### 文件 3：`VARIABLES.md`（文档更新）

在后台循环相关变量区（§12，约 330-353 行）新增：
```
HOME_AUTONOMY_ENABLED       默认 false。Home Runtime 后台自主生活总开关。
HOME_AUTONOMY_PHASE         默认 0。分层灰度：0关/1只读观察/2+信件便利贴/3+种植烹饪/4+基础生活。
HOME_AUTONOMY_INTERVAL      默认 7200（2小时）。Home 自主生活触发间隔（秒）。
```

---

## 不改动的部分（明确边界）
- **不改通用 `call_tool`**：action_key 在循环函数内注入 args，call_tool 原样合并执行
- **不改 `gateway.py`**：本次用环境变量开关（与 FREE_ACTIVITY_ENABLED 一致），不加 sys_config 热开关（留 P2）
- **不改 `home/service.py`**：service 层已完整，无需改动
- **不改 `server.py`**：MCP 工具壳不受影响
- **不接 `cook_freestyle`**：它有 JSON 解析特殊逻辑，本次不纳入后台白名单
- **不加 DB 日上限查询**：靠 cooldown 时长控制日频次（信件 12h=日2次、便利贴 8h=日3次），简化实现

## 安全护栏汇总
| 护栏 | 机制 | 位置 |
|------|------|------|
| 总开关 | `HOME_AUTONOMY_ENABLED` 默认 false | heartbeat.py 协程入口 |
| 分层灰度 | `HOME_AUTONOMY_PHASE` 0-4 | tool_loop.py 循环函数 phase 检查 |
| 固定身份 | fixed_args `actor_key="ai_primary"` | TOOL_REGISTRY 条目 |
| action_key 幂等 | 代码自动生成 `auto_{tool}_{ts}_{hex6}` | 循环函数注入 args |
| 限频 | 按工具 cooldown（进程内存） | 循环函数执行前检查 |
| 熔断 | 连续失败 3 次跳过（进程内存） | 循环函数执行前检查 |
| 单轮上限 | `MAX_TOOL_CALLS=5`（已有） | 循环函数 tc_list 截断 |
| 错误隔离 | call_tool try/except 吞异常（已有） | call_tool 951-960 行 |
| 可观测 | print 日志 + memories 写入 tag=Home_Autonomy | 循环函数 + heartbeat 协程 |

## 验收方式
1. `HOME_AUTONOMY_ENABLED=false HOME_AUTONOMY_PHASE=0` → 协程启动即返回，不进循环
2. `HOME_AUTONOMY_ENABLED=true HOME_AUTONOMY_PHASE=1` → 只读 observe，无写入，LLM 生成观察日记
3. `HOME_AUTONOMY_ENABLED=true HOME_AUTONOMY_PHASE=2` → 可写信/便利贴，action_key 自动注入，cooldown 生效
4. 检查 home_action_runs 表有 `auto_*` 前缀的 action_key 记录
5. 检查 memories 表有 tag=Home_Autonomy 的日记记录
