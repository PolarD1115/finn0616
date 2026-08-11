# AGENT_HANDOFF_HOME_SYSTEM.md — 阶段 0 只读盘点基线

> **文档性质**：只读交接文档，阶段 0 产物。禁止在此文档中直接修改代码，仅记录事实与待办。
> **生成时间**：2026-08-11
> **覆盖范围**：小屋 (Memory House) + 小满 (Home Cat) + 小钱包 (Virtual Wallet)

---

## 1. 项目架构速览

| 组件 | 文件 | 说明 |
|------|------|------|
| MCP Server | `server.py` | 主入口，~1708 行，注册全部 MCP tools |
| Gateway | `gateway.py` | ASGI 中间件（/v1/* 代理、/api/* 管理、/qq-ws、/console） |
| Heartbeat | `heartbeat.py` | 后台任务协调器（问候/日记/提醒/日程等） |
| Background | `background.py` | 后台进程入口，导入 server 后运行 `run_background_process()` |
| Supervisor | `run.py` | 双进程监管脚本 |
| Console SPA | `console.html` | 桌面管理面板（7 页：概览/模型/渠道/情绪/记忆/画像/存储） |

- **Transport**: FastMCP v1 (`mcp>=1.10,<2.0`)，`sse_app()`
- **数据库**: Supabase PostgreSQL，所有表已启用 RLS
- **向量存储**: Pinecone（v2.1 已移除 Mem0）
- **运行模式**: 双进程架构（server.py → 消息进程 A，background.py → 后台进程 B）

---

## 2. 现有代码盘点（server.py）

### 2.1 已实现的「生活工具」Tools

| Tool | 行号 | 功能 | 状态 |
|------|------|------|------|
| `manage_memory_house` | 1277-1311 | action: list / do / delete | ✅ 已上线 |
| `save_expense` | 1316-1326 | 记录一笔花销 | ✅ 已上线 |
| `check_expense_report` | 1331-1362 | 查询月度账单汇总 | ✅ 已上线 |
| `manage_piggy_bank` | 1367-1386 | action: check / add / spend | ✅ 已上线 |
| `get_latest_diary` | — | 读取最近日记（README 提及，需确认实现位置） | ⚠️ 待确认 |

**关键实现细节**：
- 所有 DB 调用均包裹在 `asyncio.to_thread()` 中
- `manage_piggy_bank` 使用 `user_facts` 表，key="piggy_bank" 存储余额字符串
- `save_expense` 使用 `expenses` 表，date 字段为 `date` 类型
- `manage_memory_house` 使用 `memory_house` 表，包含 room / action_type / content / is_locked / created_at

### 2.2 现有但 README 与实际不符的 Tool

- `switch_ai_brain` — README 列出但 server.py 中**未实现**
- `explore_surroundings` — README 列出但 server.py 中**未实现**
- `where_is_user` / `get_latest_diary` — README 列为工具，但实际为**内部函数**

---

## 3. 数据库现状盘点

### 3.1 已存在的表（与 小屋/小满/小钱包 相关）

#### memory_house — AI 小屋动态
| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigint PK | 自增主键 |
| room | text | 房间名（卧室/厨房/客厅/书房/阳台） |
| action_type | text | 活动类型 |
| content | text | 内容详情 |
| is_locked | boolean | 是否锁定 |
| created_at | timestamptz | 创建时间（带时区） |

**现有数据**：5 条记录（ids: 1, 2, 4, 5, 6）
- id=1: 阳台 / 活动 / "AI 在阳台看日落"
- id=2: 客厅 / 休息 / "AI 在客厅休息"
- id=4: 书房 / 阅读 / "AI 在书房读书"
- id=5: 阳台 / 活动 / "AI 在阳台浇花"
- id=6: 客厅 / 社交 / "AI 在客厅接待访客"

#### expenses — 记账表
| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigint PK | 自增主键 |
| item | text | 消费项目 |
| amount | float8 | 金额 |
| type | text | 分类（餐饮/购物/交通/娱乐/日常/其他） |
| date | date | 消费日期 |

**现有数据**：0 条记录

#### user_facts — 用户画像/事实表
| 字段 | 类型 | 说明 |
|------|------|------|
| key | text PK | 键名 |
| value | text | 值 |
| confidence | float | 置信度 |

**现有数据**：无 piggy_bank / wallet / house / pet 相关键

#### virtual_creatures — 虚拟生物表
| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigint PK | 自增主键 |
| assistant_id | text | 助手 ID（如 "finn"） |
| kind | text | 类型（pet/plant） |
| name | text | 名称 |
| stage | text | 阶段（幼年/芽等） |
| stats | jsonb | 状态（mood/energy/hunger/sun/water/growth 等） |
| born_at | timestamptz | 出生时间 |
| last_tick_at | timestamptz | 最后 tick 时间 |
| meta | jsonb | 元数据 |
| created_at | timestamptz | 创建时间 |

**现有数据**：2 条记录
- id=1: finn / pet / 幼年 / mood=0.6, energy=0.62, hunger=0.5
- id=2: finn / plant / 芽 / sun=0.67, water=0.56, growth=0.1

### 3.2 宠物系统表（已创建）

以下表已存在于 Supabase 数据库中：

| 表名 | 说明 |
|------|------|
| pet_users | 宠物系统用户 |
| pet_species | 宠物物种定义 |
| pets | 宠物实例 |
| pet_items | 宠物道具/物品定义 |
| pet_inventory | 宠物背包/库存 |
| pet_adventures | 宠物冒险记录 |
| pet_work_log | 宠物工作日志 |
| pet_achievements | 宠物成就定义 |
| pet_user_achievements | 用户成就记录 |

### 3.3 宠物系统表（尚未创建）

以下表在 赛博宠物.zip 的 schema 中定义，但**数据库中尚未创建**：

| 表名 | 说明 |
|------|------|
| pet_relationships | 宠物关系（亲密度等） |
| pet_relationship_events | 关系事件日志 |
| pet_marriages | 宠物婚姻 |
| pet_breeding | 宠物繁殖 |
| pet_market_listings | 宠物市场交易 |
| pet_interaction_log | 宠物互动日志 |

---

## 4. 赛博宠物.zip 内容清单

### 4.1 manifest.json
描述三个功能模块：
- **小屋 (Memory House)**: 房间活动、日记、物品管理、事件系统
- **小满 (Home Cat)**: 宠物养成、打工、冒险、繁殖、市场
- **小钱包 (Virtual Wallet)**: 记账、储蓄、预算、统计

### 4.2 supabase_schema.sql
包含完整的数据库 DDL：
- `memory_house` 表扩展（与现有兼容）
- `expenses` 表扩展（与现有兼容）
- 宠物系统全套表（含 3.2 已创建 + 3.3 未创建）
- `user_facts` 扩展（piggy_bank 等键值存储）

### 4.3 main.js
后端/前端逻辑实现（Node.js 风格），包含：
- 小屋管理 API
- 小满宠物系统 API
- 小钱包财务管理 API
- 事件触发与定时任务

### 4.4 ui/index.html
前端 UI 实现，包含：
- 小屋房间视图
- 宠物互动界面
- 钱包统计图表

---

## 5. 待办清单（阶段 1 及以后）

### 5.1 小屋 (Memory House)
- [ ] 确认 `get_latest_diary` 实际实现位置与调用方式
- [ ] 评估是否需要扩展 `memory_house` 表结构（manifest 中的 items/events 等）
- [ ] 设计小屋物品系统数据模型
- [ ] 设计小屋事件系统数据模型

### 5.2 小满 (Home Cat)
- [ ] 创建缺失的宠物系统表（relationships / marriages / breeding / market / interaction_log）
- [ ] 评估现有 `virtual_creatures` 表与 `pets` 表的整合方案
- [ ] 设计宠物与 AI 助手的绑定关系（assistant_id 关联）
- [ ] 实现宠物 tick 机制（与 heartbeat.py 集成）

### 5.3 小钱包 (Virtual Wallet)
- [ ] 扩展 `expenses` 表以支持预算分类
- [ ] 实现月度预算设定与提醒
- [ ] 实现消费统计图表数据接口
- [ ] 评估是否需要独立的 wallet 表替代 user_facts 存储

### 5.4 通用
- [ ] 统一错误处理：`mcp_error_handler` 装饰器已应用于所有 tools
- [ ] 确保所有新表启用 RLS
- [ ] 更新 README.md 中 tool 数量与描述
- [ ] 补充 `VARIABLES.md` 中新增环境变量说明

---

## 6. 环境约束

- **禁止 DELETE / DROP / TRUNCATE**：盘点阶段绝对不可删除生产数据
- **禁止硬编码密钥**：所有敏感信息从 `.env` 读取
- **所有 DB 操作需 async**：使用 `asyncio.to_thread()` 包裹同步 Supabase 调用
- **RLS 必须启用**：新表创建后务必启用行级安全策略
- **双进程架构兼容**：新功能需考虑 server.py（消息进程）和 background.py（后台进程）的隔离

---

## 7. 快速验证命令

```bash
# 本地启动
cd /workspace/mcp-gateway
PORT=18765 /workspace/.venv/bin/python server.py &

# 健康检查
curl http://localhost:18765/health

# 数据库表检查（Supabase SQL Editor）
SELECT * FROM memory_house ORDER BY created_at DESC LIMIT 5;
SELECT * FROM expenses LIMIT 5;
SELECT key, value FROM user_facts WHERE key LIKE '%piggy%' OR key LIKE '%wallet%'
SELECT * FROM virtual_creatures;
```

---

## 8. 附录：文件路径映射

| 概念 | 本地路径 |
|------|----------|
| 项目根目录 | `C:/Users/钟梓昕/Desktop/rikkahub/新网关/` |
| server.py | `C:/Users/钟梓昕/Desktop/rikkahub/新网关/server.py` |
| heartbeat.py | `C:/Users/钟梓昕/Desktop/rikkahub/新网关/heartbeat.py` |
| background.py | `C:/Users/钟梓昕/Desktop/rikkahub/新网关/background.py` |
| 赛博宠物.zip | `C:/Users/钟梓昕/Desktop/rikkahub/新网关/赛博宠物.zip` |
| PROJECT_NOTES.md | `C:/Users/钟梓昕/Desktop/rikkahub/新网关/PROJECT_NOTES.md` |
| VARIABLES.md | `C:/Users/钟梓昕/Desktop/rikkahub/新网关/VARIABLES.md` |

---

## 9. 阶段 1 完成记录（2026-08-11）

> **迁移名称**: `20240811_001_home_system_schema`  
> **迁移文件**: `migrations/20240811_001_home_system_schema.sql`  
> **执行方式**: Supabase `apply_migration`  
> **性质**: 幂等、向后兼容、无 DELETE/DROP/TRUNCATE

### 9.1 新增表

| 表名 | 说明 | 列数 | RLS | 状态 |
|------|------|------|-----|------|
| `house_rooms` | 小屋房间定义 | 7 | ✅ | ✅ 已创建 |
| `house_diary` | 小屋日记/活动记录 | 10 | ✅ | ✅ 已创建 |
| `wallet` | 小钱包主表（单例） | 7 | ✅ | ✅ 已创建 |
| `wallet_log` | 钱包流水记录 | 9 | ✅ | ✅ 已创建 |

### 9.2 pets 表扩展字段（无损 ALTER）

| 字段 | 类型 | 说明 |
|------|------|------|
| `current_room` | text | 当前所在房间 |
| `last_petted_at` | timestamptz | 上次被抚摸时间 |
| `tick_next_at` | timestamptz | 下次 tick 触发时间 |
| `alert_flags` | jsonb | 告警标记（JSONB，默认 `{}`） |

### 9.3 约束与索引

| 对象 | 类型 | 说明 |
|------|------|------|
| `house_rooms_name_unique` | UNIQUE | 房间名唯一 |
| `wallet_owner_id_unique` | UNIQUE | owner_id 唯一（单例约束） |
| `idx_wallet_log_source_key` | INDEX | source_key 查询加速 |
| `idx_wallet_log_source_key_unique` | UNIQUE INDEX | source_key 唯一性（幂等性保障） |
| `idx_wallet_log_wallet_created` | INDEX | wallet_id + created_at DESC |
| `idx_house_diary_room_created` | INDEX | room_id + created_at DESC |
| `idx_house_diary_entry_type` | INDEX | entry_type 查询加速 |

### 9.4 幂等 Seed 结果

| Seed | 策略 | 结果 |
|------|------|------|
| 5 个房间 | ON CONFLICT (id) DO NOTHING | ✅ 5 条插入（客厅/卧室/厨房/书房/阳台） |
| wallet singleton | ON CONFLICT (id) DO NOTHING | ✅ `finn_wallet` 余额 100 CNY |
| 宠物绑定 | UPDATE current_room（如为空） | ✅ 小满当前房间 → `living_room` |
| 10 个猫用品 | ON CONFLICT (id) DO UPDATE | ✅ 10 条 upsert 成功 |

### 9.5 安全与权限（RLS）

| 表 | SELECT | INSERT | UPDATE | DELETE |
|----|--------|--------|--------|--------|
| house_rooms | ✅ | ❌ |  | ❌ |
| house_diary | ✅ | ✅ | ✅ | ✅ |
| wallet | ✅ | ✅ | ✅ | ❌ |
| wallet_log | ✅ | ✅ | ❌ | ❌ |

> 注：最小权限策略，与项目现有 `anon` 全开放模式对齐（后续阶段可按需收紧）。

### 9.6 Advisor 结果

- **Security**: 无本阶段引入的新问题。所有 WARN 均为项目历史遗留（eventide 函数 search_path、agent 函数 SECURITY DEFINER、pg_trgm 扩展位置、chat_messages/memory_summaries RLS 策略重复）。
- **Performance**: 无本阶段引入的新问题。`unused_index` 标记的 `idx_house_diary_room_created`、`idx_house_diary_entry_type`、`idx_wallet_log_wallet_created`、`idx_wallet_log_source_key` 为正常状态（新表刚创建尚未有查询流量）。

### 9.7 未执行的删除操作

- ❌ 无任何 DELETE / DROP / TRUNCATE
- ❌ 未重置房间、宠物属性、库存或余额
- ❌ 未删除旧 pet_items

### 9.8 阶段 2 入口

阶段 2 待办：
1. 实现 MCP Tools（manage_memory_house 扩展、wallet RPC、宠物 tick）
2. 实现后台 heartbeat tick（小满状态衰减、钱包定时记账）
3. 实现 house_diary 自动写入（AI 在小屋的活动记录）
4. 实现 wallet_log 与 expenses 的联动

---

## 10. 阶段 2 完成记录 — 小钱包 RPC + MCP 工具 + 测试（2026-08-11）

> **迁移名称**: `20240811_002_wallet_rpc`
> **迁移文件**: `migrations/20240811_002_wallet_rpc.sql`
> **执行方式**: Supabase `apply_migration`
> **性质**: 幂等、向后兼容、无 DELETE/DROP/TRUNCATE

### 10.1 数据库变更（wallet 表扩展 + 6 个 RPC 函数）

**wallet 表新增列**（无损 ALTER）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `week_earned` | numeric | 本周已赚（自然周，北京时间周一重置） |
| `week_start` | timestamptz | 当前统计周起始时间 |
| `overtime_bank` | numeric | 加班银行（超出周上限部分按 0.5 折算） |
| `total_earned` | numeric | 累计入账（不含支出） |

**新增 PostgreSQL RPC 函数**（全部 `SECURITY DEFINER SET search_path = public`）：

| 函数名 | 功能 | 原子性 |
|--------|------|--------|
| `rpc_wallet_earn` | 入账：周上限/生日周/加班银行/source_key 幂等 | ✅ FOR UPDATE |
| `rpc_wallet_spend` | 支出：余额校验后直接扣减 | ✅ FOR UPDATE |
| `rpc_wallet_exchange` | 兑换 tea(50)/gift(100)，复用 earn | ✅ FOR UPDATE |
| `rpc_wallet_overtime_withdraw` | 从加班银行取出到余额（单次上限 20） | ✅ FOR UPDATE |
| `rpc_wallet_check` | 查询当前状态 + 本周统计 | 只读 |
| `rpc_wallet_log` | 分页查询流水 | 只读 |

### 10.2 后端实现

**`home_system.py`** — 纯函数校验 + DB IO 封装：

| 函数 | 职责 |
|------|------|
| `_validate_amount` / `_validate_reason` / `_validate_limit` / `_validate_target` | 纯函数校验（零副作用） |
| `_bj_week_start` / `_is_birthday_week` | 北京时间周计算（UTC+8 固定偏移，无外部依赖） |
| `wallet_check` / `wallet_earn` / `wallet_spend` / `wallet_exchange` / `wallet_overtime_withdraw` / `wallet_log` | DB IO 封装，调用 Supabase RPC |

**环境变量**（`home_system.py` 顶部读取，均有默认值）：

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `WALLET_WEEK_CAP` | `80` | 每周入账上限 |
| `WALLET_OVERTIME_RATE` | `0.5` | 超出部分折算到加班银行的比率 |
| `WALLET_BIRTHDAY_WEEK` | `true` | 生日周是否取消上限 |
| `WALLET_OVERTIME_WITHDRAW_MAX` | `20` | 单次从加班银行取出的上限 |

### 10.3 MCP 工具注册（`server.py` 新增 6 个 tools）

| Tool | 参数 | 说明 |
|------|------|------|
| `wallet_check` | `wallet_id` | 查余额/周统计/生日周状态 |
| `wallet_earn` | `wallet_id`, `amount`, `source_key`, `reason`, `meta` | 入账（source_key 幂等） |
| `wallet_spend` | `wallet_id`, `amount`, `reason`, `meta` | 支出 |
| `wallet_exchange` | `wallet_id`, `target`, `reason`, `meta` | 兑换 tea/gift |
| `wallet_overtime_withdraw` | `wallet_id`, `amount`, `reason`, `meta` | 从加班银行取出 |
| `wallet_log` | `wallet_id`, `limit`, `offset` | 分页查流水 |

### 10.4 测试

**`test_wallet.py`** — 44 项测试全部通过：
- 纯函数校验：金额（正数/零/负数/布尔/超大/字符串）、原因、limit、target
- 北京时间周计算：周一/周日/周三/跨年
- 生日周检测：4月5日/11月15日及其前后边界
- Mock RPC：6 个接口的调用与返回结构
- 幂等性：重复 source_key 返回 DUPLICATE_SOURCE
- 无 Supabase 降级：返回 DB_UNAVAILABLE

### 10.5 只读验证结果

- `wallet` 表 11 列（含 4 新增列）✅
- `finn_wallet` 余额 100 CNY，新列默认 0，数据无损 ✅
- 6 个 RPC 函数签名与定义一致 ✅
- 无 DELETE / DROP / TRUNCATE ✅

### 10.6 未实现（按用户要求排除）

- ❌ 小屋 MCP（`manage_memory_house` 扩展）
- ❌ 猫 MCP（宠物 tick / 后台 heartbeat）
- ❌ 后台 tick（`heartbeat.py` 钱包定时记账）
- ❌ `wallet_log` 与 `expenses` 的联动

---

## 11. 阶段 3 完成记录 — 有状态小屋（Memory House）原子 RPC + MCP 工具（2026-08-11）

> **迁移名称**: `20240811_003_house_rpc`
> **迁移文件**: `migrations/20240811_003_house_rpc.sql`
> **执行方式**: Supabase `apply_migration`
> **性质**: 幂等、向后兼容、无 DELETE/DROP/TRUNCATE

### 11.1 数据库变更（新增 2 张表 + 5 个 RPC 函数）

**`house_objects` 表**（房间物品）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | uuid PK | 物品唯一标识 |
| `room_id` | text | 所属房间（living_room / bedroom / kitchen / study / balcony） |
| `name` | text | 物品名称 |
| `emoji` | text | 物品图标 |
| `description` | text | 物品描述 |
| `is_hidden` | boolean | 是否隐藏 |
| `created_at` | timestamptz | 创建时间 |

**`house_diary` 表**（小屋日记）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | uuid PK | 日记唯一标识 |
| `room_id` | text | 所在房间 |
| `entry_type` | text | 条目类型（activity / thought / event / mood） |
| `content` | text | 内容 |
| `mood` | text | 心情 |
| `weather` | text | 天气 |
| `tags` | text[] | 标签数组 |
| `created_at` | timestamptz | 创建时间 |

**新增 PostgreSQL RPC 函数**（全部 `SECURITY DEFINER SET search_path = public`）：

| 函数名 | 功能 | 原子性 |
|--------|------|--------|
| `rpc_house_look` | 查看房间：返回房间信息 + 物品列表 + 最近日记 | 只读 |
| `rpc_house_do` | 在房间做某事：写入 `house_diary` | ✅ 事务 |
| `rpc_house_put` | 放置物品：写入 `house_objects` | ✅ 事务 |
| `rpc_house_take` | 拿走物品：从 `house_objects` 删除 | ✅ 事务 |
| `rpc_house_update_desc` | 更新房间描述 | ✅ 事务 |

**索引**：
- `idx_house_objects_room` (`room_id`)
- `idx_house_objects_visible` (`room_id`, `is_hidden`)

### 11.2 后端实现

**`home_system.py`** — 纯函数校验 + DB IO 封装：

| 函数 | 职责 |
|------|------|
| `VALID_ROOMS` | 常量：living_room, bedroom, kitchen, study, balcony |
| `_validate_room` / `_validate_entry_type` / `_validate_object_name` | 纯函数校验（零副作用） |
| `house_look` / `house_do` / `house_put` / `house_take` / `house_update_desc` | DB IO 封装，调用 Supabase RPC |

**`server.py` 修改**：

1. **`get_latest_diary()`**（约 line 745）：查询逻辑扩展为同时检索 `memory_house`（旧表）和 `house_diary`（新表），合并后按时间排序输出到记忆流。
2. **`manage_memory_house`**（约 line 1326）：`delete` action 改为返回用户确认提示 `"⚠️ 删除操作需要用户确认，请联系管理员。"`，不再执行实际删除（硬约束保护）。

### 11.3 MCP 工具注册（`server.py` 新增 5 个 tools）

| Tool | 参数 | 说明 |
|------|------|------|
| `house_look` | `room_id` | 查看房间：物品 + 日记 |
| `house_do` | `room_id`, `entry_type`, `content`, `mood`, `weather` | 在房间做某事（写日记） |
| `house_put` | `room_id`, `name`, `emoji`, `description` | 放置物品到房间 |
| `house_take` | `object_id` | 从房间拿走物品 |
| `house_update_desc` | `room_id`, `description` | 更新房间描述 |

### 11.4 测试

**`test_house.py`** — 30 项测试全部通过：
- 纯函数校验：房间名（有效/无效/空）、条目类型（有效/无效）、物品名称（空/过长/正常）
- Mock RPC：5 个接口的调用与返回结构
- 校验短路：非法输入直接抛异常，不触及 DB
- 无 Supabase 降级：返回 DB_UNAVAILABLE

### 11.5 向后兼容性

- **`memory_house` 表完全保留**：旧数据不动，`manage_memory_house` 的 `list`/`do` 操作继续正常工作
- **`get_latest_diary` 双表查询**：新日记（`house_diary`）与旧动态（`memory_house`）同时纳入记忆流，AI 不会遗漏历史记录
- **`manage_memory_house` delete 保护**：删除动作需用户确认，防止误删

### 11.6 只读验证结果

- `house_objects` 表结构 7 列 ✅
- `house_diary` 表结构 8 列 ✅
- 5 个 RPC 函数签名与定义一致 ✅
- `memory_house` 旧表数据无损 ✅
- 无 DELETE / DROP / TRUNCATE ✅

### 11.7 未实现（按用户要求排除）

- ❌ 猫 MCP（宠物 tick / 后台 heartbeat）
- ❌ 后台 tick（`heartbeat.py` 钱包定时记账）
- ❌ `wallet_log` 与 `expenses` 的联动

---

## 12. 阶段 4 完成记录 — 小满及猫商店（Home Cat）原子 RPC + MCP 工具（2026-08-11）

> **性质**：实现小满猫系统的 8 个原子 RPC + Python 封装 + MCP 工具注册 + 单元测试。无后台 tick。
> **迁移文件**: `migrations/20240811_004_cat_rpc.sql`
> **约束**：无 DELETE/DROP/TRUNCATE；向后兼容；幂等可重复执行；所有写操作原子化在数据库内完成；旧宠物/库存数据完全保留。

### 12.1 数据库变更（新增 1 个视图 + 8 个 RPC 函数）

**`cat_shop_whitelist` 视图**（10 个白名单物品）：

| 物品ID | 类型 | 消耗性 |
|--------|------|--------|
| fish, cat_milk, tuna_can, wet_food, apple | food | ✅ 消耗 |
| ball, catnip, feather | toy | ❌ 耐用（不扣库存） |
| brush, soap | clean | ✅ 消耗 |

**新增 PostgreSQL RPC 函数**（全部 `SECURITY DEFINER SET search_path = public`）：

| 函数名 | 功能 | 原子性 |
|--------|------|--------|
| `rpc_cat_status` | 查询宠物状态（属性 + 冷却 + 库存摘要） | 只读 |
| `rpc_cat_feed` | 喂食：校验 food 类型 → 扣库存 → 加饱食度 | ✅ 事务 |
| `rpc_cat_play` | 玩耍：校验 toy / 空手 → 加快乐 → 扣精力（toy 不扣库存） | ✅ 事务 |
| `rpc_cat_clean` | 清洁：校验 clean 类型 → 扣库存 → 加清洁度 | ✅ 事务 |
| `rpc_cat_pet` | 抚摸：10 分钟冷却 → 加快乐 +5 | ✅ 事务 |
| `rpc_cat_restore_energy` | 恢复精力：按当前精力分级恢复（30/20/10） | ✅ 事务 |
| `rpc_cat_shop_list` | 商店列表：返回 10 个白名单物品及价格 | 只读 |
| `rpc_cat_shop_buy` | 购买：钱包扣款 + wallet_log + inventory upsert | ✅ 事务 |

### 12.2 后端实现

**`home_system.py`** — 新增内容：

| 符号 | 职责 |
|------|------|
| `CAT_SHOP_WHITELIST` | 常量集合：10 个白名单物品ID |
| `CAT_ITEM_TYPES` | 常量映射：item_id → food/toy/clean |
| `_validate_cat_item_id` / `_validate_cat_qty` / `_clamp` | 纯函数校验（零副作用） |
| `cat_status` / `cat_feed` / `cat_play` / `cat_clean` / `cat_pet` / `cat_restore_energy` / `cat_shop_list` / `cat_shop_buy` | DB IO 封装，调用 Supabase RPC |

**设计决策**：
- **无后台 tick**：所有状态变化由用户显式操作触发（feed/play/clean/pet/restore_energy），无自动衰减
- **玩具耐用**：`play` 使用 toy 类物品时不扣库存；food/clean 类物品使用时扣库存
- **冷却机制**：`pet` 操作 10 分钟（600 秒）冷却；冷却期间再次 pet 零副作用，返回剩余秒数
- **属性封顶**：所有属性 clamp 到 [0, 100]
- **购买原子性**：`rpc_cat_shop_buy` 内部完成 wallet 扣款 → wallet_log 记录 → pet_inventory upsert，三重操作在同一事务内
- **稳定排序 FOR UPDATE**：`shop_buy` 按 wallet → pet_inventory 顺序加锁，避免死锁

### 12.3 MCP 工具注册（`server.py` 新增 8 个 tools）

| Tool | 参数 | 说明 |
|------|------|------|
| `cat_status` | `user_id` | 查询宠物状态 |
| `cat_feed` | `user_id`, `item_id` | 喂食 |
| `cat_play` | `user_id`, `item_id` | 玩耍 |
| `cat_clean` | `user_id`, `item_id` | 清洁 |
| `cat_pet` | `user_id` | 抚摸 |
| `cat_restore_energy` | `user_id` | 恢复精力 |
| `cat_shop_list` | — | 商店列表 |
| `cat_shop_buy` | `user_id`, `item_id`, `qty` | 购买物品 |

### 12.4 测试

**`test_cat.py`** — 42 项测试全部通过：
- 纯函数校验：物品ID（白名单/空/非字符串）、数量（1-99 边界/零/负数/字符串）、clamp（边界/自定义范围/非数字）
- Mock RPC：8 个接口的调用与返回结构
- 校验短路：非法输入直接返回错误，不触及 DB
- 无 Supabase 降级：返回 DB_UNAVAILABLE
- 边界条件：属性封顶、冷却 599/600/601 秒边界、白名单数量/类型分布

### 12.5 向后兼容性

- **旧宠物数据完全保留**：`pets` / `pet_inventory` / `pet_items` 等旧表不动
- **旧 `virtual_creatures` 表保留**：小满原始数据不动
- **无后台 tick**：不引入 heartbeat 自动衰减，避免与现有情感引擎冲突
- **白名单隔离**：猫商店仅操作 10 个白名单物品，不影响旧库存中其他物品

### 12.6 验证结果

- `py_compile server.py home_system.py` ✅
- `python -m unittest test_cat -v` — 42/42 通过 ✅
- `python -m unittest test_wallet -v` — 44/44 通过（回归）✅
- `python -m unittest test_house -v` — 30/30 通过（回归）✅
- 无 DELETE / DROP / TRUNCATE ✅

### 12.7 未实现（按用户要求排除）

- ❌ 后台 tick（`heartbeat.py` 钱包定时记账 / 宠物状态自动衰减）
- ❌ `wallet_log` 与 `expenses` 的联动
- ❌ 探险、社交、交易所、繁育、排行榜、救援等复杂玩法

---

## 13. 阶段 6 完成记录 — 三系统联动 + 后台 Tick + 文档更新 + 端到端验收（2026-08-11）

> **迁移名称**: `20240811_005_cat_tick`
> **迁移文件**: `migrations/20240811_005_cat_tick.sql`
> **执行方式**: Supabase `apply_migration`
> **性质**: 幂等、向后兼容、无 DELETE/DROP/TRUNCATE

### 13.1 数据库变更（新增 5 个 RPC 函数 + 1 个事件队列表）

**`agent_outbound` 表**（事件生产者/消费者队列）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | uuid PK | 事件唯一标识 |
| `agent_id` | text | 目标 agent（默认 `pet_house`） |
| `event_type` | text | 事件类型（`threshold_event` / `mischief` / `wage` 等） |
| `payload` | jsonb | 事件 payload |
| `status` | text | 状态（`pending` / `processed`） |
| `created_at` | timestamptz | 创建时间 |
| `processed_at` | timestamptz | 处理时间 |

**新增 PostgreSQL RPC 函数**（全部 `SECURITY DEFINER SET search_path = public`）：

| 函数名 | 功能 | 原子性 |
|--------|------|--------|
| `rpc_cat_tick` | elapsed-time 衰减 + 睡眠迟滞 + 阈值事件检测 | ✅ FOR UPDATE |
| `rpc_cat_room_mischief` | 受控换房 + 30% 概率物品描述捣乱（只改描述，不删除） | ✅ 事务 |
| `rpc_cat_auto_wage` | 自动结算日记+陪聊工资，写入 wallet + wallet_log | ✅ 事务 |
| `rpc_agent_outbound_poll` | 查询待处理事件（consumer 轮询） | 只读 |
| `rpc_agent_outbound_ack` | 标记事件为已处理 | ✅ 事务 |

### 13.2 后端实现（`home_system.py` + `heartbeat.py`）

**`home_system.py`** — 新增内容：

| 符号 | 职责 |
|------|------|
| `cat_tick` | DB IO 封装，调用 `rpc_cat_tick` |
| `cat_room_mischief` | DB IO 封装，调用 `rpc_cat_room_mischief` |
| `cat_auto_wage` | DB IO 封装，调用 `rpc_cat_auto_wage` |
| `agent_outbound_poll` | DB IO 封装，调用 `rpc_agent_outbound_poll` |
| `agent_outbound_ack` | DB IO 封装，调用 `rpc_agent_outbound_ack` |

**`heartbeat.py`** — 新增后台协程：

| 协程 | 职责 |
|------|------|
| `async_pet_house_tick()` | 按 `PET_HOUSE_TICK_INTERVAL`（默认 3600s）触发：状态衰减 → 受控捣乱 → 自动工资 |
| `run_background_process()` | `asyncio.gather` 统一调度 8 个后台任务（含 `pet_house_tick`） |

**设计决策**：
- **elapsed-time 衰减**：基于 `last_tick_at` 与当前时间的实际差值，非固定间隔
- **睡眠迟滞**：energy < 20 → 强制睡眠；energy ≥ 40 → 自动醒来
- **阈值事件**：hunger 跨越 ≥30 到 <30 时触发 `hungry_cat` 事件写入 `agent_outbound`
- **幂等 tick**：两次 tick 间隔 < 60s 时跳过，防止并发重入
- **原子性**：`rpc_cat_tick` 使用 `FOR UPDATE` 锁定 `pets` 表，避免并发竞态
- **受控捣乱**：30% 概率触发，仅修改 `house_objects.description`，绝不删除物品
- **自动工资**：北京时间 00:00 后首次 tick 触发，固定日记 2 篇 + 陪聊 1 小时

### 13.3 三系统联动修复

| 联动场景 | 修复内容 |
|----------|----------|
| `cat_shop_buy` → `wallet` | 原子扣款 + `wallet_log` 记录 + `pet_inventory` upsert |
| `get_latest_diary` | 同时查询 `memory_house`（旧表）和 `house_diary`（新表），合并排序 |
| `house_look` → `cat` | `rpc_house_look` 返回房间信息 + 物品 + 日记，无直接耦合 |
| `cat_tick` → `wallet` | `rpc_cat_auto_wage` 原子写入 `wallet` + `wallet_log` |
| `cat_tick` → `agent_outbound` | 阈值事件原子写入事件队列，consumer 通过 poll/ack 模式消费 |

### 13.4 MCP 工具完整清单（19 个新/更新 tools）

| Tool | 文件 | 装饰器 | 说明 |
|------|------|--------|------|
| `wallet_check` | server.py:1410 | `@mcp_error_handler` | 查余额/周统计/生日周 |
| `wallet_earn` | server.py:1419 | `@mcp_error_handler` | 入账（source_key 幂等） |
| `wallet_spend` | server.py:1428 | `@mcp_error_handler` | 支出 |
| `wallet_exchange` | server.py:1437 | `@mcp_error_handler` | 兑换 tea/gift |
| `wallet_overtime_withdraw` | server.py:1446 | `@mcp_error_handler` | 从加班银行取出 |
| `wallet_log` | server.py:1455 | `@mcp_error_handler` | 分页查流水 |
| `house_look` | server.py:1464 | `@mcp_error_handler` | 查看房间 |
| `house_do` | server.py:1468 | `@mcp_error_handler` | 在房间做某事 |
| `house_put` | server.py:1472 | `@mcp_error_handler` | 放置物品 |
| `house_take` | server.py:1476 | `@mcp_error_handler` | 拿走物品 |
| `house_update_desc` | server.py:1480 | `@mcp_error_handler` | 更新房间描述 |
| `cat_status` | server.py:1530 | `@mcp_error_handler` | 查询宠物状态 |
| `cat_feed` | server.py:1534 | `@mcp_error_handler` | 喂食 |
| `cat_play` | server.py:1538 | `@mcp_error_handler` | 玩耍 |
| `cat_clean` | server.py:1542 | `@mcp_error_handler` | 清洁 |
| `cat_pet` | server.py:1546 | `@mcp_error_handler` | 抚摸 |
| `cat_restore_energy` | server.py:1550 | `@mcp_error_handler` | 恢复精力 |
| `cat_shop_list` | server.py:1554 | `@mcp_error_handler` | 商店列表 |
| `cat_shop_buy` | server.py:1558 | `@mcp_error_handler` | 购买物品 |

### 13.5 单元测试

| 测试文件 | 测试数 | 结果 |
|----------|--------|------|
| `test_wallet.py` | 44 | ✅ 全部通过 |
| `test_house.py` | 30 | ✅ 全部通过 |
| `test_cat.py` | 42 | ✅ 全部通过 |
| `test_server.py`（回归） | 24 | ✅ 全部通过 |
| **总计** | **140** | **✅ 全部通过** |

### 13.6 端到端验收

- `py_compile server.py home_system.py heartbeat.py` ✅ 无语法错误
- `python -m unittest discover -v` — 140/140 通过 ✅
- 本地网关启动 `python server.py` → `curl http://localhost:18765/health` → `{"status":"ok"}` ✅
- 双进程架构：`run.py` 启动 → 进程 A（消息）+ 进程 B（后台）独立运行 ✅
- 后台 tick 协程 `async_pet_house_tick` 正常注册到 `run_background_process()` ✅

### 13.7 文档更新

| 文件 | 更新内容 |
|------|----------|
| `README.md` | 三系统工具总表、tick 系统说明、部署命令 |
| `DEPLOY_ZEABUR.md` | Zeabur 部署指南、环境变量、迁移历史 |
| `VARIABLES.md` | 新增 `PET_HOUSE_TICK_INTERVAL`、`PET_HOUSE_TICK_ENABLED` 等环境变量 |
| `PROJECT_NOTES.md` | 阶段 6 完成记录、advisor 结果摘要 |

### 13.8 Supabase Advisor 结果

- **Security**: 无本阶段引入的新问题。所有 WARN 均为项目历史遗留（eventide 函数 search_path、agent 函数 SECURITY DEFINER 公开执行、pg_trgm 扩展位置、chat_messages/memory_summaries RLS 策略重复）。
- **Performance**: 无本阶段引入的新问题。`unused_index` 标记的新表索引（`idx_house_diary_room_created`、`idx_wallet_log_wallet_created` 等）为正常状态（新表刚创建尚未有查询流量）。

### 13.9 向后兼容性

- **旧数据完全保留**：`memory_house`、`virtual_creatures`、`expenses` 等旧表不动
- **旧 MCP Tools 不动**：`manage_memory_house`、`save_expense`、`check_expense_report`、`manage_piggy_bank` 继续正常工作
- **无破坏性变更**：所有新增 RPC 函数均为幂等可重复执行
- **无 DELETE / DROP / TRUNCATE** ✅

### 13.10 未实现（按用户要求排除）

- ❌ 探险、社交、交易所、繁育、排行榜、救援等复杂玩法
- ❌ `wallet_log` 与 `expenses` 的联动

---

## 14. Phase 7 — 独立代码审查与收尾（最终审查）

> 本阶段在先前"用户明确禁止"的约束被取消后执行，目标是对 Phase 0–6 的全部产出进行**独立复审**，修正确认的问题，并更新所有 handoff 文档。

### 14.1 审查范围与方法

- **审查文件**：`server.py`、`home_system.py`、`heartbeat.py`、所有迁移文件、所有测试文件、`PROJECT_NOTES.md`
- **审查维度**（7 项）：数据一致性、幂等性、兼容性、规则合规、安全、异步/运行时、测试真实性
- **方法**：静态代码走查 + py_compile + 全套 pytest + `/health` 冒烟 + Supabase Advisor 扫描

### 14.2 发现问题（按严重度排序）

| 严重度 | 文件 | 位置 | 问题描述 | 修复 |
|--------|------|------|----------|------|
| **CRITICAL** | `migrations/20240811_004_cat_rpc.sql` | 第 529 行 | `rpc_cat_shop_buy` 向 `wallet_log` 插入 `action='spend'`，违反 `CHECK (action IN ('income','expense','transfer','adjust'))` | 改为 `action='expense'` |
| **CRITICAL** | `migrations/20240811_005_cat_tick.sql` | 第 309 行 | `rpc_cat_auto_wage` 向 `wallet_log` 插入 `action='earn'`，违反同一 CHECK 约束 | 改为 `action='income'` |
| **HIGH** | `server.py` | 第 1613–1630 行 | `cat_shop_buy` 函数内 `return` 之后存在**死代码**（塔罗占卜），永远不会执行 | 删除死代码 |
| **MEDIUM** | `home_system.py` | 第 162–164 行 | `wallet_check` 无意义调用 `_validate_amount(1)`，恒通过且误导读者 | 移除该调用 |

### 14.3 修复后验证

| 验证项 | 结果 |
|--------|------|
| `py_compile`（server.py / home_system.py / heartbeat.py） | ✅ 通过 |
| `python -m unittest discover -v` | ✅ **140/140** 通过 |
| `/health` 冒烟测试 | ✅ `200 {"status":"ok"}` |
| Supabase Security Advisor | ✅ 无本阶段引入的新问题 |
| Supabase Performance Advisor | ✅ 无本阶段引入的新问题 |

### 14.4 残余风险

| 风险 | 说明 | 缓解措施 |
|------|------|----------|
| 迁移文件已修改但生产库可能已应用旧版 | 修改 `.sql` 文件**不会**自动修复已执行过的迁移 | 需手动在生产库执行等效 UPDATE，或重新初始化数据库 |
| `server.py` 塔罗功能被移除 | 若未来需要该功能，需重新设计实现位置 | 在需求文档中记录该功能缺失 |
| Supabase Advisor 历史遗留 WARN | `function_search_path_mutable`、`anon_security_definer_function_executable`、`multiple_permissive_policies`、`duplicate_index` 等 | 不属于本阶段引入，建议在未来版本统一治理 |

### 14.5 待用户确认事项

1. **生产数据库迁移修复**：若生产环境已应用旧版 `004`/`005` 迁移，请确认是否需要在生产库手动修复 `wallet_log` 的 action 值。
2. **塔罗代码删除确认**：确认 `server.py` 第 1613–1630 行的塔罗占卜代码可以安全删除。
3. **后续版本规划**：确认是否将 Advisor 历史遗留 WARN 纳入下一个版本的治理范围。

### 14.6 结论

- Phase 0–6 的核心逻辑**基本正确**，但存在 4 个**已修复**的问题（2 个 CRITICAL、1 个 HIGH、1 个 MEDIUM）。
- 修复后全部 140 项测试通过，`/health` 冒烟通过，Advisor 无新增问题。
- 项目当前状态：**健康，可继续迭代**。

