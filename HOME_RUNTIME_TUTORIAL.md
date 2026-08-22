# Home Runtime 使用教程

> 本教程聚焦三件事：**Home Runtime 有什么功能、怎么运作、怎么触发**，
> 并在最后给出**何时、如何把它接入后台自主生活**的判断依据与实施路径。
>
> 面向读者：已经能用 RikkaHub 等 MCP 客户端连上网关、会看 Zeabur 日志的使用者。
> 部署与排障细节请参考 `HOME_RUNTIME_GUIDE.md`；环境变量请参考 `VARIABLES.md`；
> 历次迭代记录请参考 `PROJECT_NOTES.md`。
>
> 全文数据均于 **2026-08-19** 用 Supabase 实际核实，非猜测。

---

## 目录

1. [Home Runtime 是什么](#1-home-runtime-是什么)
2. [当前家里到底有什么（核实数据）](#2-当前家里到底有什么核实数据)
3. [四大功能模块](#3-四大功能模块)
4. [运作机制：一次工具调用经历了什么](#4-运作机制一次工具调用经历了什么)
5. [如何触发](#5-如何触发)
6. [首次使用建议](#6-首次使用建议)
7. [什么时候把它接入后台自主生活](#7-什么时候把它接入后台自主生活)
8. [接入后台自主生活需要怎么做](#8-接入后台自主生活需要怎么做)
9. [当前限制与注意事项](#9-当前限制与注意事项)

---

## 1. Home Runtime 是什么

Home Runtime 是网关里一套**新的、显式的家庭生活工具系统**，覆盖：

- **房间观察**（看看家里什么样）
- **基础生活**（进房间、休息、睡眠、陪伴互动）
- **种植与厨房**（种菜、浇水、收获、烹饪、吃饭、喂小满）
- **信件与便利贴**（写信、拆信、在房间留便签）

它通过 `home_observe`、`plant_seed`、`cook_recipe`、`write_letter` 等 MCP 工具对外暴露能力。客户端（RikkaHub 等）连上网关后，AI 才能调用这些工具。

### ⚠️ 最关键的一条事实

> **新 Home Runtime 只有在「用户或受控 Agent 明确调用工具」时才会执行。**
> 工具存在 ≠ AI 已经会在后台自主种菜、做饭、写信或留便利贴。
> **当前后台进程不会调用任何 Home Runtime 工具。**

后台进程（`background.py`）现在只做这些事：旧自由活动、旧秘密日记（写 `memories` 表 `Secret_Diary` 标签）、旧宠物 tick（状态衰减）、以及把一份**只读的 Home Context** 注进聊天提示词让 AI「知道」家里状态——但**不给它执行 Home 工具的能力**。

### 新系统与旧系统的关系

项目里新旧两套并存：

| | 旧系统 | 新系统（Home Runtime） |
|---|---|---|
| 工具前缀 | `cat_*`、`house_*` | `home_*`、`plant_*`、`cook_*`、`write_letter`、`leave_note` 等 |
| 后台是否自动跑 | ✅ 是（宠物 tick、自由活动自动运行） | ❌ 否（只能显式调用） |
| 小满生理状态 | `pets` 表负责（饱食/清洁/精力） | Home 只管「关系/情绪状态」 |
| 私密日记 | `memories` 表 `Secret_Diary` 标签（后台仍在写） | `home_private_diaries` 表存在但为空，且 MCP 工具已移除 |

**两者是分开的，不要直接比较。** 小满的「饱食度」来自旧 `pets` 表；Home 的 `home_member_states` 里小满那行只是初始化时从 `pets` 表拍的一张只读快照，之后不再同步。

---

## 2. 当前家里到底有什么（核实数据）

下面是 2026-08-19 直接查 Supabase 的结果。

### 2.1 房间（`home_rooms`，9 个）

| 房间 key | 是否隐藏 |
|---|:---:|
| living_room（客厅） | 否 |
| bedroom（卧室） | 否 |
| kitchen（厨房） | 否 |
| study（书房） | 否 |
| studio（工作室） | 否 |
| garden（花园） | 否 |
| seaside（海边） | 否 |
| observatory（观星台） | **是**（需解锁条件） |
| basement（地下室） | **是**（需解锁条件） |

> 已知缺口：旧宠物系统有 `balcony`（阳台）房间，新 Home Runtime **没有**。旧机制可能把小满挪到 balcony，新系统观察不到。

### 2.2 成员（`home_members`，2 个）

| 成员 | stable_key | 类型 | 状态 |
|---|---|---|---|
| Finn | `ai_primary` | ai | alive（活跃） |
| 小满 | `pet_xiaoman` | pet | alive（活跃，profile 里记录了来自 `pets` 表的 legacy_id） |

### 2.3 成员状态（`home_member_states`，初始化值）

| 指标 | Finn（AI） | 小满（宠物） | 取值范围 |
|---|---|---|---|
| hunger 饱腹 | 70 | ≈35.3 | 0–100 |
| energy 精力 | 70 | 45.0 | 0–100 |
| mood 心情 | 65 | ≈47.6 | 0–100 |
| comfort 舒适 | 60 | 60 | 0–100 |
| connection 连结 | 60 | 30 | 0–100 |
| intimacy 亲密度 | 50 | 30 | 0–100 |
| health 健康 | 100 | 100 | 0–100 |
| cleanliness 清洁 | 80 | ≈42.6 | 0–100 |

> Finn 是「保守中性初始值」；小满是初始化时从 `pets` 表拍的快照。两者 `last_settled_at` 都停在 2026-08-18 初始化时刻——因为之后**没有任何动作触发过结算**（见第 4 节）。

### 2.4 种子目录（`home_seed_catalog`，5 种）

| 种子 key | 名称 | 生长时间 | 基础产量 | 浇水间隔 |
|---|---|---|---|---|
| `mint` | 🌿 薄荷 | 30 分钟 | 2 | 20 分钟 |
| `lettuce` | 🥬 生菜 | 45 分钟 | 2 | 25 分钟 |
| `tomato` | 🍅 番茄 | 60 分钟 | 3 | 30 分钟 |
| `carrot` | 🥕 胡萝卜 | 90 分钟 | 2 | 40 分钟 |
| `strawberry` | 🍓 草莓 | 120 分钟 | 3 | 30 分钟 |

### 2.5 菜谱目录（`home_recipe_catalog`，3 个）

| 菜谱 key | 名称 | 所需食材 | 产出份数 | 恢复（饱腹/心情/精力） |
|---|---|---|---|---|
| `vegetable_soup` | 🍲 蔬菜汤 | 胡萝卜×1 + 生菜×1 | 2 | 25 / 10 / 15 |
| `tomato_egg` | 🍳 番茄炒蛋 | **鸡蛋×1** + 番茄×2 | 2 | 35 / 15 / 20 |
| `mint_tea` | 🍵 薄荷茶 | 薄荷×2 | 1 | 10 / 20 / 10 |

> ⚠️ **`tomato_egg` 需要鸡蛋，但种子目录里没有鸡蛋，鸡蛋无法通过种植获取**，所以这个菜谱目前**不能自然完成**。`vegetable_soup`（胡萝卜+生菜）和 `mint_tea`（薄荷）可以走通「种→收→做」全链路。

### 2.6 动态生活数据（全部为空）

以下表当前**都是 0 行**，说明新功能从未在生产环境端到端跑通过：

```
home_objects / home_events / home_action_runs / home_jobs
home_plants / home_inventory / home_dishes
home_letters / home_notes / home_private_diaries
```

> 这些表为空 ≠ 服务报错。`home_observe`、`garden_observe`、`pantry_observe`、`list_letters`、`list_room_notes` 返回空列表是正常的，只是还没人用过。

---

## 3. 四大功能模块

按真实注册的 MCP 工具划分。每个工具标注**用途 / 主要参数 / 是否写库 / 风险**。`actor_key` 默认 `ai_primary`（Finn）。

### 3.1 模块一：家庭观察（全部只读）

| 工具 | 用途 | 主要参数 |
|---|---|---|
| `home_observe` | 观察整个家庭：房间、活跃成员、近期事件 | 无 |
| `home_observe_room` | 观察指定房间详情/物品/事件 | `room_key`（如 `living_room`） |
| `home_observe_member` | 观察指定成员信息/状态/事件 | `member_key`（如 `ai_primary`） |
| `home_timeline` | 家庭事件时间线（倒序，排除私密事件） | `limit`（1–100，默认 20）、`event_type`（可选） |

**特点**：不改任何数据，是确认连接、了解现状的最安全入口。

### 3.2 模块二：基础生活（会改变成员状态）

| 工具 | 用途 | 主要参数 | 副作用 |
|---|---|---|---|
| `home_enter_room` | 成员进入房间，结算状态后更新位置 | `actor_key`、`room_key`、`action_key` | 改位置 + 触发结算 |
| `home_rest` | 休息，恢复精力/舒适 | `actor_key`、`duration_minutes`（1–1440，默认 30）、`action_key` | 改状态 |
| `home_sleep` | 睡眠，大幅恢复精力 | `actor_key`、`duration_minutes`（1–1440，默认 480）、`action_key` | 改状态 |
| `home_spend_time` | 与另一成员陪伴互动 | `actor_key`、`target_key`、`activity`、`duration_minutes`（1–480，默认 30）、`action_key` | 改双方状态 + 亲密度（每日上限） |

**状态结算规则**（AI 本体，宠物不结算——宠物走 `pets` 表）：
- 清醒：hunger −1.5/h、energy −1.0/h、comfort −0.2/h、connection −0.1/h、cleanliness −0.2/h
- 休息：energy +1.0/h、comfort +0.3/h
- 睡眠：energy +2.0/h、comfort +0.5/h、hunger −0.5/h
- 不衰减：intimacy、health、mood
- 陪伴：comfort +2、connection +1.5、intimacy +1（每日上限 3）
- 单次最大跨度 48h，最小 60s，时钟回拨不结算

### 3.3 模块三：种植与厨房（会改变库存 / 成员状态）

观察类（只读）：

| 工具 | 用途 |
|---|---|
| `garden_observe` | 观察花园：植物、种子目录、近期种植事件 |
| `pantry_observe` | 观察库存：食材、菜品份数、可烹饪菜谱 |

写入类：

| 工具 | 用途 | 主要参数 | 副作用 |
|---|---|---|---|
| `plant_seed` | 种下一颗种子 | `actor_key`、`seed_key`（如 `mint`）、`action_key` | 占坑 + 写库存种子位 |
| `water_plant` | 浇水（水分恢复 100） | `actor_key`、`plant_id`（植物 UUID）、`action_key` | 改植物水分 |
| `harvest_plant` | 收获成熟植物，食材入库存 | `actor_key`、`plant_id`、`action_key` | 标记收获 + 增加库存 |
| `cook_recipe` | 按菜谱烹饪 | `actor_key`、`recipe_key`（如 `mint_tea`）、`action_key` | 原子扣食材 + 生成菜品 |
| `cook_freestyle` | 自由烹饪（最多 5 种食材、总量 ≤20） | `actor_key`、`ingredient_choices`（JSON 如 `{"tomato":2}`）、`action_key` | 扣食材 + 生成菜品 |
| `eat_dish` | 吃一份菜品 | `actor_key`、`dish_id`、`action_key` | 扣份数 + 改成员状态 |
| `feed_member` | 喂菜品给另一成员 | `actor_key`、`target_key`、`dish_id`、`action_key` | 扣份数 + 改目标状态 + 亲密度（日限） |

**植物生长链路**：`planted → growing → mature → harvested`
- 水分每小时 −10；水分为 0 时健康 −5/h，水分充足时健康 +2/h
- 生长时间由种子目录 `growth_minutes` 决定（30–120 分钟）
- 只有 `mature` 可收获，收获后不可重复；收获数量 = `base_yield`

**完整可走通的链路**（以薄荷为例）：
```
plant_seed(mint) → water_plant → 等 30 分钟 → harvest_plant → pantry_observe → cook_recipe(mint_tea) → eat_dish
```

### 3.4 模块四：信件与便利贴（低风险写入）

| 工具 | 用途 | 主要参数 | 副作用 |
|---|---|---|---|
| `write_letter` | 写信保存为未拆封 | `author_key`、`title`、`content`、`action_key`、`preview`（可选）、`room_key`（可选） | 写信 |
| `list_letters` | 信件列表（只返回标题/摘要，**不返回未拆信正文**） | `status_filter`（可选） | 只读 |
| `open_letter` | 拆信返回完整正文（唯一可见路径），标记已拆 | `letter_key`、`action_key` | 标记已拆 |
| `archive_letter` | 软归档信件（不删除） | `letter_key`、`action_key` | 软归档 |
| `leave_note` | 在指定房间留便利贴 | `author_key`、`room_key`、`content`（≤2000 字）、`action_key` | 写便签 |
| `list_room_notes` | 房间便利贴列表（只返回预览） | `room_key`、`include_read`（可选） | 只读 |
| `read_note` | 读取便利贴全文并标记已读 | `note_key`、`action_key` | 标记已读 |
| `archive_note` | 软归档便利贴 | `note_key`、`action_key` | 软归档 |

**可见性设计**（隐私边界）：
- 未拆信正文：谁都看不到，必须 `open_letter`
- 便利贴全文：必须 `read_note`，列表只给前 50 字预览
- 私密日记：**已从 MCP 完全移除**（FastMCP v1 无法区分调用者身份，任何能连 MCP 的客户端都可伪造身份）。`write_private_diary` 等函数仅保留为服务层内部受控函数，不对外暴露。

---

## 4. 运作机制：一次工具调用经历了什么

理解这条链路，就能判断「为什么工具能/不能在后台自动跑」。

### 4.1 调用链路

```
MCP 客户端（RikkaHub）
   │  调用工具，例如 plant_seed(actor_key=ai_primary, seed_key=mint, action_key=xxx)
   ▼
MCP 工具层  server.py  （@mcp.tool 装饰器静态注册，共 73 个工具）
   │  参数校验 + 拼装
   ▼
服务层  home/service.py  （业务逻辑）
   ▼
仓储层  home/repository.py  （封装 Supabase 调用）
   │  调用 PostgreSQL RPC
   ▼
PostgreSQL RPC  （SECURITY DEFINER 函数，原子事务）
   │  例如 rpc_home_plant_seed(action_key, actor_key, seed_key)
   ▼
数据库表  home_plants / home_inventory / home_events / home_action_runs ...
```

**几个关键设计**：

1. **写操作全部走 PostgreSQL RPC（`SECURITY DEFINER`）**：不是 Python 拼 SQL，而是数据库里的原子函数。例如烹饪是「锁定库存行 → 验证食材 → 扣库存 → 生成菜品 → 写事件」一个事务，要么全成要么全不成。
2. **`action_key` 幂等**：每个写动作带一个唯一编号。网络重试用同一个 `action_key`，系统返回已有结果而非重复执行；新动作用新编号。这防止重复扣库存、重复收获。
3. **`elapsed-time` 状态结算**：成员状态不是实时连续衰减，而是「下次有动作时，按距离上次结算的时间差一次性算」。所以 `last_settled_at` 停在旧时间很正常——一旦你调用 `home_enter_room` 等动作，会先结算掉这段时间的衰减再更新。
4. **权限收紧**：所有 Home RPC 对 `anon`/`authenticated` 撤销了执行权限，只有 `service_role`（后端密钥）能调。客户端无法绕过网关直连数据库改状态。
5. **RLS**：所有 `home_*` 表启用行级安全，`authenticated` 只读，写需 `service_role`。

### 4.2 Home Context：只读注入，不等于能执行

后台进程会把一份**只读的 Home Context** 文本注入聊天提示词，让 AI「知道」家里有什么（房间、成员、未拆信数量、花园植物摘要等）。但：

> **Home Context 注入 ≠ 后台执行 Home 工具。**
> 后台 LLM 调用的工具白名单里**没有任何 Home 工具**，模型即使「想」种菜也调不到 `plant_seed`。

这是当前「AI 知道家里什么样，但不会自己在后台动手」的根本原因。

---

## 5. 如何触发

### 5.1 唯一触发方式：显式调用

当前**只有一种**触发方式：通过连上网关的 MCP 客户端（RikkaHub 等），由 AI 在对话中调用工具。表现形式是——你跟 AI 说话，AI 决定调用某个 Home 工具，工具执行后返回结果，AI 再回复你。

后台进程**不会**自动触发任何 Home 工具。

### 5.2 触发示例（对 AI 说）

| 你说的话 | 预期 AI 调用的工具 | 风险 |
|---|---|---|
| 「先观察一下家里现在的状态，不要做任何修改。」 | `home_observe` | 只读 |
| 「看看小满现在怎么样，只读取状态。」 | `home_observe_member` | 只读 |
| 「去花园看看现有植物，不要浇水。」 | `garden_observe` | 只读 |
| 「看看厨房库存有什么。」 | `pantry_observe` | 只读 |
| 「去卧室休息半小时。」 | `home_rest` | 改状态 |
| 「种一颗薄荷，然后浇点水。」 | `plant_seed` + `water_plant` | 改库存 |
| 「做一杯薄荷茶，然后自己喝掉。」 | `cook_recipe` + `eat_dish` | 改库存 |
| 「给我写一封信保存到小屋里。」 | `write_letter` | 写信 |
| 「在客厅留一张便利贴，内容是……」 | `leave_note` | 写便签 |

> ⚠️ 这些是示例话术，**不能保证模型一定调用工具**——取决于模型对工具的理解。第一次连接时**不要**让模型「自由试用全部工具」，先用只读工具确认连接正常。

### 5.3 `action_key` 怎么填

所有写工具都需要 `action_key`（唯一编号）：

- **生成**：客户端或模型生成一个唯一字符串，推荐 UUID。
- **重试复用**：网络超时重试时，用**同一个** `action_key`，系统返回原结果不重复执行。
- **新动作新编号**：每次新动作换一个新的。
- **不要用固定值**如 `"test"`：会导致后续同名动作被误判为重复。

---

## 6. 首次使用建议

按风险从低到高，**只读优先**：

1. **只读冒烟**：`home_observe` → `home_observe_member`（小满）→ `garden_observe` → `pantry_observe` → `list_letters` → `list_room_notes`。确认连接正常、数据能返回（多数会返回空列表，因为表是空的，属正常）。
2. **低风险写入**：用你**愿意长期保留的真实内容**做一次，比如「在客厅留一张便利贴：今天天气不错」。会产生永久生活记录，不要当临时测试数据。
3. **资源状态链路**：建议第一次用 `mint`（30 分钟最快）走通「种 → 浇 → 等 → 收 → 做薄荷茶 → 喝」。
   - ⚠️ 植物按**真实时间**生长，必须等满生长时间，**不要**伪造时间或直接改成熟状态。
4. **钱包**：先只读（`wallet_check` / `wallet_log`），确有必要再小金额写入。

> 完整的部署后检查清单、HTTP 错误排查、MCP 连接配置见 `HOME_RUNTIME_GUIDE.md` 第 5、6、13 节。

---

## 7. 什么时候把它接入后台自主生活

这是本教程的核心问题。先说结论：

> **现在还不合适。** Home Runtime 的真实数据库写链尚未端到端验收（所有动态表为空、单元测试用 mock），贸然接入后台自主运行会带来不可控的副作用。需要先满足一组先决条件，再分阶段接入。

### 7.1 先决条件清单（全部满足才考虑接入）

| # | 先决条件 | 当前状态 | 说明 |
|---|---|:---:|---|
| 1 | 真实 DB 写链端到端验收通过 | ❌ | `home_plants`/`inventory`/`dishes`/`events`/`action_runs`/`letters`/`notes` 全为 0 行，从未在生产跑通。单元测试用 mock。 |
| 2 | 至少手动跑通一次完整生活链路 | ❌ | 如「种薄荷→收→做薄荷茶→喝」全程在真实库留痕。 |
| 3 | 状态结算在真实时间下表现合理 | ❌ | `last_settled_at` 停在初始化时刻，未验证长时间衰减/恢复数值是否合理。 |
| 4 | 后台工具白名单 + 安全护栏设计就绪 | ❌ | 当前 `tool_loop.py` 白名单只有 `wallet_*`/`house_*`/`cat_*`，无 Home 工具。 |
| 5 | 幂等与并发在后台高频下可靠 | ⚠️ 未验证 | `action_key` 幂等已实现，但后台自动生成 action_key 的去重策略未设计。 |
| 6 | 有可观测与回滚手段 | ⚠️ 部分 | 有事件表可追溯，但无「后台自主行为」的开关/限频/熔断机制。 |

### 7.2 时机判断

满足以下任一信号，说明**可以开始小范围灰度接入**：

- ✅ 先决条件 1–3 全部满足：你亲手用 MCP 客户端在真实库里走通过至少一条完整链路（种植/烹饪/信件），数据落表正确、状态结算数值合理。
- ✅ 先决条件 4 已设计并通过评审：Home 工具进了后台白名单，但带严格护栏（见第 8 节）。
- ✅ 你愿意接受「AI 可能在你不在时自己种菜/做饭/写信」带来的不可逆生活记录。

满足以下任一信号，说明**应该暂缓**：

- ❌ 还没在真实库手动跑通任何写链路。
- ❌ 你不希望 AI 在无人监督下产生永久生活记录（信件/便利贴/种植记录不可轻易删除）。
- ❌ 担心后台高频调用导致库存/状态异常（如反复种同一颗、频繁浇水）。

### 7.3 为什么不能现在就接

后台自主运行和手动调用的根本区别在于**频率与监督**：

- **手动调用**：你在场，每次调用你都知道，出问题能立刻发现。
- **后台自主**：AI 在你不在时按间隔自己决策调用，可能短时间内连续调用多个写工具，产生大量永久记录，且无人即时监督。

Home 工具大多有**不可逆副作用**（种的菜、写的信、留的便签都进永久生活记录），且当前**没有枯萎/冷却/限频**等约束机制（浇水无冷却、植物不会枯萎）。一旦后台失控，清理成本很高。所以必须先把护栏做好。

---

## 8. 接入后台自主生活需要怎么做

这一节是**实施方案**，当前**尚未实现**，是需要开发的工作。基于现有架构（`background.py` + `tool_loop.py`）给出可行路径。

### 8.1 现有后台自主行为架构（接入点）

后台自主行为目前由两套机制承载：

1. **`heartbeat.py::async_free_activity`**（自由活动）：每隔一段时间（默认 1.5h）从活动清单里让 LLM 选一件做，写一条行动日志。当前是「轻量版」——只让模型**描述**做了什么并写日志，**不真正调用工具产生副作用**。
2. **`tool_loop.py`**（工具调用循环）：开关 `FREE_ACTIVITY_TOOL_LOOP`（默认 `false`）。开启后，自由活动会真正调用白名单工具执行副作用。已有 `wallet_*`/`house_*`/`cat_*` 在白名单，**没有 Home 工具**。
   - 关键结构：`TOOL_REGISTRY`（工具注册表 + JSON Schema 参数校验）、`ACTIVITY_TOOL_MAP`（活动→工具映射）、`call_tool`（固定身份注入，不让 LLM 控制 `wallet_id`/`user_id`）、单轮调用上限 `FREE_ACTIVITY_TOOL_MAX_CALLS`。

**接入 Home Runtime = 把 Home 工具纳入这套工具循环，并设计对应的自由活动候选。**

### 8.2 分阶段实施路径

#### 阶段 0：先决条件验收（必须先做）

在动后台代码之前，先用 MCP 客户端在真实库手动验收：

1. 走通种植链路：`plant_seed(mint)` → `water_plant` → 等待 → `harvest_plant` → 确认 `home_inventory` 有食材。
2. 走通烹饪链路：`cook_recipe(mint_tea)` → `eat_dish` → 确认 `home_dishes` 扣份、成员状态变化。
3. 走通信件链路：`write_letter` → `list_letters`（看不到正文）→ `open_letter`（看到正文）→ 确认 `home_letters` 有记录。
4. 走通基础生活：`home_enter_room` → `home_rest` → 确认 `home_member_states` 的 `last_settled_at` 推进、数值合理。
5. 检查 `home_events` / `home_action_runs` 是否有对应事件记录、`action_key` 幂等是否生效（重复用同一 action_key 应返回原结果）。

> 只有这些在真实库跑通，才有资格谈后台接入。

#### 阶段 1：只读观察接入（零风险，可先做）

把 Home 观察工具作为后台「感知」手段接入，**只读不写**：

- 在 `async_free_activity` 的活动清单里新增「观察家里」类活动（如「巡视花园」「看看小满状态」）。
- 这些活动只调用 `home_observe` / `garden_observe` / `home_observe_member` / `pantry_observe` 等只读工具。
- 把观察结果写进自由活动日志，让 AI 的「日常」更有生活感。
- **不进入写工具白名单**。

这一步零副作用，可以先验证「后台能正确调用 Home 工具并处理返回」。

#### 阶段 2：低风险写入灰度（小范围、强护栏）

只接入**低风险写工具**，且加严格护栏：

- 候选活动：「留一张便利贴」「写一封信」（`leave_note` / `write_letter`）。
- 护栏：
  - **限频**：每类活动每天最多 N 次（如便利贴 ≤2/天、信件 ≤1/天），在 `home_action_runs` 上查最近记录计数。
  - **内容审核**：信件/便利贴内容进永久记录，建议加一道 LLM 自审或关键词过滤，避免后台写出不当内容。
  - **action_key 生成策略**：后台用「活动名 + 日期 + 随机」生成，保证唯一且可追溯。
  - **总开关**：新增环境变量（如 `HOME_AUTONOMY_ENABLED`，默认 `false`），独立于 `FREE_ACTIVITY_TOOL_LOOP`。
  - **日志可观测**：每次后台调用 Home 工具打 `🏠 [Home自主]` 日志，含工具名、参数、结果。
- 在 `tool_loop.py` 的 `TOOL_REGISTRY` 注册 `leave_note` / `write_letter`，在 `ACTIVITY_TOOL_MAP` 加对应映射，参数走 JSON Schema 校验。

灰度观察 1–2 周，确认信件/便利贴内容合理、频率不过载。

#### 阶段 3：资源类写入（种植/烹饪，需更谨慎）

种植和烹饪涉及库存原子事务，且当前**无枯萎/冷却**机制，风险更高：

- 候选活动：「打理花园」「做顿饭」。
- 额外护栏：
  - **种植上限**：同时存活植物数上限（如 ≤5 株），防止后台疯狂种菜塞满花园。
  - **浇水冷却**：当前浇水无冷却，建议加最短间隔（如同一株 1 小时内不重复浇）。
  - **烹饪前置检查**：先 `pantry_observe` 确认有食材再 `cook_recipe`，避免空库存报错刷日志。
  - **收获时机校验**：只对 `mature` 状态植物调用 `harvest_plant`。
  - **吃/喂上限**：每日吃饭/喂食次数上限，防止状态被刷到异常。
- 这一步建议**先只让 AI 做、不让 AI 吃**（即只种/收/做，`eat_dish`/`feed_member` 暂不进后台白名单），等你确认库存链路稳定再放开。

#### 阶段 4：基础生活自主（进房间/休息/睡眠/陪伴）

这类工具改成员状态，副作用相对可控，但要注意：

- **避免与旧系统冲突**：旧宠物 tick 仍在跑，`home_enter_room` 会触发结算；要确认两套状态机制不打架。
- **休息/睡眠时长合理性**：后台自动 `home_sleep(duration_minutes=480)` 之类要结合真实时间，避免「睡 48 小时」这种异常。
- **陪伴的亲密度日限**：已有每日上限 3，但仍建议限频，防止后台频繁刷陪伴。
- 建议作为最后阶段接入，且优先接 `home_enter_room`（轻）再接 `home_rest`/`home_sleep`。

### 8.3 通用安全护栏（每个阶段都要有）

| 护栏 | 做法 |
|---|---|
| **总开关 + 分阶段开关** | `HOME_AUTONOMY_ENABLED`（总闸）+ 每类活动独立开关，默认全关，存 `sys_config` 可热生效 |
| **限频** | 每类后台 Home 动作按天/小时限次，查 `home_action_runs` 计数 |
| **action_key 唯一** | 后台生成规则固定且唯一，防重复执行 |
| **身份固定** | 沿用 `tool_loop.call_tool` 的固定身份注入，不让 LLM 控制 `actor_key`/`wallet_id` |
| **单轮上限** | 沿用 `FREE_ACTIVITY_TOOL_MAX_CALLS`，限制单次自由活动调几个工具 |
| **可观测** | 每次调用打日志，含工具/参数/结果/耗时 |
| **熔断** | 连续失败 N 次自动停用该活动，打告警 |
| **可回滚的「软」优先** | 信件/便利贴是软归档不删除，相对安全；种植/烹饪不可逆，更谨慎 |
| **不删数据** | 后台自主产生的记录不自动清理，需人工审阅 |

### 8.4 一个最小可行接入示例（伪代码示意）

> 以下是说明性伪代码，**不是已实现的代码**，实际开发需对照 `tool_loop.py` 真实结构。

```python
# tool_loop.py 新增（示意）
TOOL_REGISTRY["leave_note"] = {
    "schema": {"author_key": "str", "room_key": "str", "content": "str", "action_key": "str"},
    "fixed_args": {"author_key": "ai_primary"},  # 不让 LLM 冒充作者
    "handler": lambda args: home_service.leave_note(**args),
}

ACTIVITY_TOOL_MAP["留便利贴"] = ["leave_note"]

# heartbeat.py 新增活动候选 + 限频（示意）
if home_autonomy_enabled() and note_quota_today() < 2:
    candidate_activities.append("留便利贴")
```

接入后，先在日志里观察 `🏠 [Home自主]` 行，确认工具调用合理（没乱留便签、没超频），再逐步放开更多活动。

---

## 9. 当前限制与注意事项

### 9.1 功能性限制

- **后台不自主**：当前后台不会自动种菜/做饭/写信/留便利贴，只能显式调用（见第 7–8 节）。
- **真实写链未验收**：所有动态表为空，单元测试用 mock，不代表真实库已跑通。
- **`tomato_egg` 不能自然完成**：缺鸡蛋，鸡蛋无法种植获取。
- **浇水无冷却**：可频繁浇水（水分恢复 100）。
- **植物不会枯萎**：种下后只需等成熟，没有枯萎机制。
- **`garden_observe` 可能显示未结算状态**：状态结算在进房间等动作时触发，纯观察可能看到旧状态。
- **`balcony` 房间缺口**：旧宠物有阳台，新 Home Runtime 没有，观察不到旧机制挪过去的小满。
- **`pet_agent_outbound` 死队列**：有 9 条 pending 记录无消费者（后台宠物照料实际走 RPC 返回值，不经此队列）。

### 9.2 安全边界

- **`API_SECRET` 必须配置**：否则受保护入口 503，且 `/v1/*` 无鉴权暴露。
- **不把 service_role key 给客户端**：它能绕过 RLS 直读写全部数据。客户端只需网关地址 + `API_SECRET`。
- **私密日记不通过 MCP 读写**：工具已移除，防止伪造身份。
- **未拆信正文不可见**：`list_letters` 只给标题/摘要，必须 `open_letter`。
- **任何数据库删除操作必须先取得用户明确同意**。

### 9.3 使用原则

1. **先用只读工具确认连接**，不要一上来就让模型自由试用全部工具。
2. **写操作用愿意长期保留的真实内容**，不要当临时测试数据（记录不可轻易删）。
3. **植物按真实时间生长**，不要伪造时间或直接改成熟状态。
4. **RikkaHub 只配置网关地址 + `API_SECRET`**，不填数据库密钥。
5. **接入后台前先满足第 7.1 节先决条件**，按第 8.2 节分阶段灰度。

---

## 附：文档导航

| 文档 | 用途 |
|---|---|
| `HOME_RUNTIME_TUTORIAL.md`（本文） | 功能使用、运作机制、触发方式、后台接入时机与方案 |
| `HOME_RUNTIME_GUIDE.md` | 部署、环境变量、验收流程、HTTP 排障、工具全表 |
| `PROJECT_NOTES.md` | Phase 2–8 历次迭代变更日志 |
| `VARIABLES.md` | 全部环境变量清单 |

> 本教程的「接入后台自主生活」部分是**基于现有架构的实施建议**，当前项目**尚未实现**该功能。任何后台自主行为的上线都应先完成第 7.1 节先决条件验收，并按第 8.2 节分阶段灰度。
