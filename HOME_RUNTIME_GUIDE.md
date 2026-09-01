# AI 伴侣小窝部署与使用指南

> 本指南面向会使用 Zeabur、会填写环境变量、会配置 MCP 客户端的实际使用者。
> 不要求你熟悉 Python、PostgreSQL 或 FastMCP 内部实现。
> 首次出现的英文术语会附一句普通中文解释。
>
> 本指南所有地址、工具名、参数均来自当前项目代码与数据库的实际核实（核实日期：2026-08-19），
> 不含猜测。涉及 RikkaHub / Zeabur 界面的部分，以官方文档或客户端当前界面为准。

---

## 1. 当前系统是什么

### 1.1 网关（Gateway）

「网关」是一个用 Python 写的常驻服务，基于 **FastMCP**（一个让你用 Python 函数快速注册 MCP 工具的库）。
MCP（Model Context Protocol，模型上下文协议）是一种让 AI 客户端发现并调用外部工具的标准协议。
本网关做两件事：

1. 对外暴露一组 **MCP 工具**（种植、烹饪、写信、喂猫、查钱包等），AI 客户端连接后可以调用。
2. 同时提供一个 **OpenAI 兼容的聊天代理**（`/v1/chat/completions`）和一些管理页面（`/console`、`/miniapp`）。

网关由两个进程组成，必须同时运行：

| 进程 | 启动文件 | 角色 | 职责 |
|---|---|---|---|
| 进程 A · 消息进程 | `server.py` | `GATEWAY_ROLE=message` | MCP 工具 + `/v1` 聊天代理 + QQ/TG 实时收发，对外提供服务 |
| 进程 B · 后台进程 | `background.py` | `GATEWAY_ROLE=background` | 主动思考、日记、总结、提醒、日程、宠物 tick、环境热同步 |

> SSE 是一种由服务器持续向客户端发送事件的连接方式，当前网关用它承载 MCP 会话。

### 1.2 Home Runtime（新家庭运行时）

Home Runtime 是一套**新的、显式的工具系统**，覆盖：房间观察、进入/休息/睡眠/陪伴、种植、烹饪、信件、便利贴。
它通过 `home_observe`、`plant_seed`、`cook_recipe`、`write_letter` 等 MCP 工具暴露能力。

**关键事实（最重要的一条）：**

> 新 Home Runtime 只有在用户或受控 Agent **明确调用工具时**才会执行。
> 工具存在 ≠ AI 已经会在后台自主种植、烹饪、写信或留便利贴。
> 当前后台进程**不会**调用任何新 Home Runtime 工具。

### 1.3 旧系统和新系统的关系

项目里同时存在「旧系统」和「新系统」：

- **旧系统**：`cat_*` 宠物工具、`house_*` 小屋工具、旧秘密日记（写在 `memories` 表，标签 `Secret_Diary`，只读保留）、旧宠物 tick。宠物 tick 仍在后台自动运行；🆕 C5 起旧自由活动循环不再由后台主进程调度，已并入统一自主调度（Home 活动走真实 `home_*` 工具）；`house_*` 工具保留但不再进入统一自主候选。
- **新系统**：Home Runtime 的 `home_*`、`plant_*`、`cook_*`、`write_letter`、`leave_note` 等。这些是**显式工具**；🆕 C5 起部分工具（观察/种植/烹饪/信件/休息/陪伴，按 phase 分层）已可被统一自主调度按 `activity_id` 真实调用。

小满（宠物猫）的**生理状态**（饱食度、清洁度、精力等）由旧 `pets` 系统负责；Home Runtime 的 `home_member_states` 只负责**关系/情绪状态**。两者是分开的，不应直接比较。

---

## 2. 当前功能

下文按真实注册的 MCP 工具分类。所有工具均在 `server.py` 中通过 `@mcp.tool()` 装饰器静态注册，共 **73 个**。
每个工具标注：用途、主要参数、是否写数据库、风险等级。

风险等级含义：
- **只读**：不修改任何数据。
- **低风险写入**：写生活记录/记忆，通常不影响钱包、库存或成员状态。
- **会改变成员状态**：修改精力、舒适度、亲密度等。
- **会改变库存**：扣食材、加菜品、收菜。
- **会改变钱包**：入账、支出、扣款。

### 2.1 家庭观察（只读）

| 工具 | 用途 | 主要参数 | 写数据库 | 风险等级 |
|---|---|---|:---:|---|
| `home_observe` | 观察整个家庭：房间、活跃成员、近期事件 | 无 | 否 | 只读 |
| `home_observe_room` | 观察指定房间详情/物品/事件 | `room_key`（如 living_room） | 否 | 只读 |
| `home_observe_member` | 观察指定成员信息/状态/事件 | `member_key`（如 ai_primary） | 否 | 只读 |
| `home_timeline` | 家庭生活事件时间线（倒序，排除私密事件） | `limit`（1-100，默认20）、`event_type`（可选） | 否 | 只读 |

### 2.2 基础生活

| 工具 | 用途 | 主要参数 | 写数据库 | 风险等级 |
|---|---|---|:---:|---|
| `home_enter_room` | 成员进入房间，结算状态后更新位置 | `actor_key`、`room_key`、`action_key` | 是 | 会改变成员状态 |
| `home_rest` | 成员休息，恢复精力/舒适度 | `actor_key`、`duration_minutes`（1-1440，默认30）、`action_key` | 是 | 会改变成员状态 |
| `home_sleep` | 成员睡眠，大幅恢复精力 | `actor_key`、`duration_minutes`（1-1440，默认480）、`action_key` | 是 | 会改变成员状态 |
| `home_spend_time` | 与另一成员陪伴互动 | `actor_key`、`target_key`、`activity`、`duration_minutes`（1-480，默认30）、`action_key` | 是 | 会改变成员状态 |

> `actor_key` 默认 `ai_primary`（即 Finn）。`action_key` 是动作幂等键，见第 10 节。

### 2.3 种植和厨房

| 工具 | 用途 | 主要参数 | 写数据库 | 风险等级 |
|---|---|---|:---:|---|
| `garden_observe` | 观察花园：植物、种子目录、近期种植事件 | 无 | 否 | 只读 |
| `plant_seed` | 种下一颗种子 | `actor_key`、`seed_key`（如 tomato）、`action_key` | 是 | 会改变库存 |
| `water_plant` | 给指定植物浇水（水分恢复 100） | `actor_key`、`plant_id`（植物 UUID）、`action_key` | 是 | 低风险写入 |
| `harvest_plant` | 收获成熟植物，食材入库存 | `actor_key`、`plant_id`、`action_key` | 是 | 会改变库存 |
| `pantry_observe` | 观察库存：食材、菜品份数、可烹饪菜谱 | 无 | 否 | 只读 |
| `cook_recipe` | 按菜谱烹饪，原子扣食材、生成菜品 | `actor_key`、`recipe_key`（如 tomato_egg）、`action_key` | 是 | 会改变库存 |
| `cook_freestyle` | 自由烹饪（最多 5 种食材、总量 ≤20） | `actor_key`、`ingredient_choices`（JSON 如 `{"tomato":2}`）、`action_key` | 是 | 会改变库存 |
| `eat_dish` | 吃一份菜品，恢复饱腹/心情/精力 | `actor_key`、`dish_id`、`action_key` | 是 | 会改变成员状态 + 库存 |
| `feed_member` | 喂菜品给另一成员，亲密度小幅增加（每日上限） | `actor_key`、`target_key`、`dish_id`、`action_key` | 是 | 会改变成员状态 |

**当前种子目录（来自数据库 `home_seed_catalog`，共 5 种）：**

| 种子 key | 名称 | 生长时间 | 基础产量 | 需水值 |
|---|---|---|---|---|
| `tomato` | 🍅 番茄 | 60 分钟 | 3 | 50 |
| `mint` | 🌿 薄荷 | 30 分钟 | 2 | 50 |
| `lettuce` | 🥬 生菜 | 45 分钟 | 2 | 60 |
| `carrot` | 🥕 胡萝卜 | 90 分钟 | 2 | 40 |
| `strawberry` | 🍓 草莓 | 120 分钟 | 3 | 55 |

**当前菜谱目录（来自数据库 `home_recipe_catalog`，共 3 个）：**

| 菜谱 key | 名称 | 所需食材 | 产出份数 | 恢复（饱腹/心情/精力） |
|---|---|---|---|---|
| `vegetable_soup` | 🍲 蔬菜汤 | 胡萝卜×1 + 生菜×1 | 2 | 25 / 10 / 15 |
| `tomato_egg` | 🍳 番茄炒蛋 | **鸡蛋×1** + 番茄×2 | 2 | 35 / 15 / 20 |
| `mint_tea` | 🍵 薄荷茶 | 薄荷×2 | 1 | 10 / 20 / 10 |

> ⚠️ **重要**：`tomato_egg` 需要 `egg`（鸡蛋），但种子目录里**没有鸡蛋**，鸡蛋**无法通过种植获取**。
> 因此 `tomato_egg` 目前**不能通过"种植→收获→烹饪"自然完成**。
> `vegetable_soup`（胡萝卜+生菜）和 `mint_tea`（薄荷）可以自然完成。
> 请勿假设全部菜谱都能自然做出，必须以目录数据为准。

### 2.4 信件和便利贴

| 工具 | 用途 | 主要参数 | 写数据库 | 风险等级 |
|---|---|---|:---:|---|
| `write_letter` | 写信保存为未拆封（正文需拆信才可见） | `author_key`、`title`、`content`、`action_key`、`preview`（可选）、`room_key`（可选） | 是 | 低风险写入 |
| `list_letters` | 信件列表，只返回标题/摘要/时间，**不返回未拆信正文** | `status_filter`（unopened/opened/archived，可选） | 否 | 只读 |
| `open_letter` | 拆信返回完整正文（唯一可见路径），标记已拆 | `letter_key`、`action_key` | 是 | 低风险写入 |
| `archive_letter` | 软归档信件（不删除） | `letter_key`、`action_key` | 是 | 低风险写入 |
| `leave_note` | 在指定房间留便利贴 | `author_key`、`room_key`、`content`（≤2000字）、`action_key` | 是 | 低风险写入 |
| `list_room_notes` | 房间便利贴列表，只返回预览 | `room_key`、`include_read`（可选） | 否 | 只读 |
| `read_note` | 读取便利贴全文并标记已读 | `note_key`、`action_key` | 是 | 低风险写入 |
| `archive_note` | 软归档便利贴 | `note_key`、`action_key` | 是 | 低风险写入 |

> **私密日记工具已从 MCP 完全移除**。`write_private_diary`、`read_private_diary`、`archive_private_diary`、`list_private_diary` 均不再注册为 MCP 工具（代码 `server.py:2375-2392` 有明确安全注释）。原因是 FastMCP v1 无法区分调用者身份，任何能连 MCP 的客户端都可伪造身份读写私密日记。这些函数仅保留为服务层内部受控函数。

### 2.5 宠物（旧 `cat_*` 工具）

小满的生理状态（饱食度、清洁度、精力、快乐等）由旧 `pets` 系统负责，后台会自动 tick（衰减）。

| 工具 | 用途 | 主要参数 | 写数据库 | 风险等级 |
|---|---|---|:---:|---|
| `cat_status` | 查看小满状态/属性/冷却/库存摘要 | 无 | 否 | 只读 |
| `cat_feed` | 喂食（仅 food 类型），扣库存加饱食度 | `item_id`（如 fish） | 是 | 会改变库存 + 宠物状态 |
| `cat_play` | 陪玩（玩具可选），睡眠/精力低时拒绝 | `item_id`（可选，如 ball） | 是 | 会改变成员状态 |
| `cat_clean` | 清洁 | `item_id`（可选，如 brush） | 是 | 会改变成员状态 |
| `cat_pet` | 抚摸，快乐+5，10 分钟冷却 | 无 | 是 | 会改变成员状态 |
| `cat_restore_energy` | 恢复精力（受限恢复路径） | 无 | 是 | 会改变成员状态 |
| `cat_shop_list` | 猫商店 10 个白名单物品及价格 | 无 | 否 | 只读 |
| `cat_shop_buy` | 购买猫用品：钱包扣款+流水+库存原子事务 | `item_id`、`qty`（1-99） | 是 | 会改变钱包 + 库存 |

### 2.6 钱包

| 工具 | 用途 | 主要参数 | 写数据库 | 风险等级 |
|---|---|---|:---:|---|
| `wallet_check` | 查余额、本周已赚、加班银行、周上限、生日周 | 无 | 否 | 只读 |
| `wallet_earn` | 入账（source_key 幂等防重，受门控+周上限约束） | `amount`、`source_key`、`reason` | 是 | 会改变钱包 |
| `wallet_spend` | 支出，余额不足报错 | `amount`、`reason` | 是 | 会改变钱包 |
| `wallet_exchange` | 物品兑换余额（tea=50 / gift=100） | `target`、`reason` | 是 | 会改变钱包 |
| `wallet_overtime_withdraw` | 加班银行取款到主账户（单次上限 20） | `amount`、`reason` | 是 | 会改变钱包 |
| `wallet_log` | 查最近交易流水 | `limit`（1-100）、`offset` | 否 | 只读 |

钱包规则说明：

- **MCP 自主入账（`wallet_earn`）不能使用 `bypass_cap`**：代码中 `bypass_cap=False` 被硬编码（`server.py:1586`），MCP 工具不暴露该参数。受 `money_earning_enabled` 门控和周上限（默认 80）约束。
- **零花钱和打赏走后端管理 API**：`/api/wallet/allowance`（每周零花钱，后端固定 `bypass_cap=True`、按周幂等）和 `/api/wallet/tip`（打赏，金额 0-200）。这些需要 `API_SECRET` 鉴权，是给管理面板用的，不是给 AI 客户端用的。
- **钱包以 `wallet` 和 `wallet_log` 表为权威源**。

### 2.7 记忆与搜索

| 工具 | 用途 | 主要参数 | 写数据库 | 风险等级 |
|---|---|---|:---:|---|
| `save_memory` | 保存记忆到 `memories` 表 + Pinecone（先做价值判断+语义去重） | `title`、`content`、`category`（默认"事件"） | 是 | 低风险写入 |
| `search_memory` | 向量+关键词搜索记忆 | `query` | 否 | 只读 |
| `manage_user_fact` | 新增/更新用户画像（upsert） | `key`、`value` | 是 | 低风险写入 |
| `get_user_profile` | 读取所有用户画像事实 | 无 | 否 | 只读 |
| `organize_knowledge_base` | 知识库 CRUD（可删除记忆/画像） | `target`、`action`、`query_or_data` | 是 | 中风险（可删除） |
| `manage_memory_house` | 记忆小屋 list/do（delete 被拒绝） | `action`、`room`、`activity`、`content`、`record_id` | 是 | 低风险写入 |

记忆安全说明：

- **普通记忆搜索（`search_memory`）不会返回 `Secret_Diary`**：私密标签被排除。
- **私密日记不会进入普通 Home Context**：后台注入的 Home Context 只读且不含私密日记。
- **旧秘密日记仍保存在旧系统**：旧后台写 `memories` 表标签 `Secret_Diary`；新 `home_private_diaries` 表存在但当前为空，且无 MCP 工具可读写。

### 2.8 天气

| 工具 | 用途 | 主要参数 | 写数据库 | 风险等级 |
|---|---|---|:---:|---|
| `query_weather` | 查当前天气（city 空则取最新 GPS 定位） | `city`（可选） | 否 | 只读 |
| `query_weather_forecast` | 查未来 1-3 天预报 | `city`（可选）、`days`（默认3） | 否 | 只读 |

### 2.9 其他旧/通用工具（摘要）

另有约 26 个通用工具，包括：`echo`（回声测试）、`web_search`（联网搜索）、`send_notification`（Telegram 推送）、`manage_reminder`（提醒 CRUD）、邮件系列（`check_inbox`/`read_full_email`/`reply_external_email`）、日历系列（`add_calendar_event`/`get_calendar_events`/`modify_calendar_event`）、记账（`save_expense`/`check_expense_report`）、零钱罐（`manage_piggy_bank`）、旧小屋（`house_look`/`house_do`/`house_put`/`house_take`/`house_update_desc`）、设备状态（`device_status`）、Obsidian 云笔记（`list/read/write_obsidian_cloud`）、AI 作曲/翻唱（`compose_music`/`cover_existing_song`）、HTML 渲染图片（`render_html_to_image`）等。

> 这些工具多数依赖外部服务（Google、Telegram、Replicate 等），不配置对应环境变量时会优雅降级。

---

## 3. Zeabur 部署

### 3.1 构建方式

项目根目录有 `Dockerfile`，Zeabur 会自动识别并使用 Dockerfile 构建。**不需要**额外配置 `zeabur.json` / `zbpack.json`（项目根目录没有这些文件）。

Dockerfile 关键内容（已核实）：

```dockerfile
FROM python:3.11-slim
# ... 安装依赖 ...
EXPOSE 10000
HEALTHCHECK ... CMD ... 'http://localhost:10000/health'
CMD ["python", "run.py"]
```

### 3.2 启动命令

**启动命令是 `python run.py`**（Dockerfile 的 `CMD`，无需在 Zeabur 另设自定义启动命令）。

`run.py` 是生产推荐入口，它用 `subprocess.Popen` 同时拉起两个子进程并互相守护：

- 进程 A：`python server.py`（消息进程，`GATEWAY_ROLE=message`）
- 进程 B：`python background.py`（后台进程，`GATEWAY_ROLE=background`）

任一进程退出 → 立即终止另一个 → `run.py` 以非零码退出 → 容器 restart 策略整体重启。

> ⚠️ **重要**：如果你在 Zeabur 面板里把启动命令改成了 `python server.py`，会退化成**单进程模式**，后台进程（主动思考、日记、宠物 tick、提醒）不会运行。**请勿修改启动命令，保持 Dockerfile 默认的 `python run.py`。**
>
> 项目自带的 `DEPLOY_ZEABUR.md` 第 12 行写的"部署命令：`python server.py`"**已过时且与 Dockerfile 矛盾**，请以本指南和 Dockerfile 为准。

### 3.3 监听端口

- 端口由环境变量 `PORT` 控制，默认 **10000**（`server.py:2458`）。
- Dockerfile `EXPOSE 10000`。
- Zeabur 会把自己的 `PORT` 环境变量注入容器；应用读取该变量监听对应端口。
- 在 Zeabur 面板的"网络/端口"设置里，暴露端口填 **10000**（或 Zeabur 自动检测的端口）。

### 3.4 健康检查

- 端点：`GET /health`
- 返回：`200 {"status":"ok","service":"generic-mcp-gateway"}`
- **不需要认证**。
- Dockerfile 内置 HEALTHCHECK 每 30 秒请求一次该端点。

### 3.5 公网域名

Zeabur 会为你的服务分配一个公网域名（形如 `xxx.zeabur.app`）。所有路由都是根路径相对路径，公网 URL 直接拼接：

```
https://你的域名/health
https://你的域名/sse
https://你的域名/console
https://你的域名/miniapp
```

### 3.6 如何确认两个进程都已启动

1. 访问 `https://你的域名/health`，返回 200 即消息进程（进程 A）在线。
2. 查看 Zeabur 部署日志，应看到两行启动信息：
   - `▶️ 启动 进程A · 消息进程: ... server.py (GATEWAY_ROLE=message)`
   - `▶️ 启动 进程B · 后台进程: ... background.py (GATEWAY_ROLE=background)`
3. 如果只看到进程 A、没有进程 B，说明可能被改成了单进程模式（见 3.2 警告）。

### 3.7 查看日志与重启

- **日志**：在 Zeabur 控制台进入该服务 → "Logs / 日志"标签，可看到 `run.py` 及两个子进程的输出。网关关闭了 uvicorn 访问日志，日志里主要是网关自己的活动输出。
- **重启**：在 Zeabur 控制台点 "Redeploy / 重新部署" 即可。`run.py` 的互相守护机制保证：任一进程崩溃 → 整体退出 → 容器自动重启。

---

## 4. 必填环境变量

以下是从代码和 `VARIABLES.md` 核实的环境变量。**最小可运行配置**至少需要前 6 项。

| 变量 | 是否必填 | 用途 | 缺失时表现 |
|---|:---:|---|---|
| `PORT` | 建议填 | 服务监听端口 | 缺省回退 10000 |
| `API_SECRET` | **必填** | 连接网关的访问密钥（保护 `/api/*`、`/sse`、`/messages`） | 受保护入口返回 **503**；`/v1/*` 在此情况下**不拦截**（见下方说明） |
| `SUPABASE_URL` | **必填** | Supabase 项目地址 | 数据库功能全部禁用（记忆、画像、Home Runtime、钱包等） |
| `SUPABASE_KEY` | **必填** | Supabase **anon** key（用于只读查询，受 RLS 保护） | 读查询全部返回空 |
| `SUPABASE_SERVICE_KEY` | **必填** | Supabase **service_role** key（用于 RPC 写操作：种植/烹饪/钱包等） | 写操作全部返回 `SERVICE_KEY_MISSING`；读操作不受影响 |
| `CHAT_API_KEY` | **必填** | 主对话模型的 API Key | 聊天不可用 |
| `CHAT_BASE_URL` | 可填 | 模型服务地址 | 缺省 `https://api.minimaxi.com/v1` |
| `CHAT_MODEL_NAME` | 可填 | 主模型名称 | 缺省 `abab6.5s-chat` |

> ⚠️ **关于 `API_SECRET` 与 `/v1/*` 的一个细节**（以代码为准）：
> 当 `API_SECRET` 为空时，`/api/*`、`/sse`、`/messages` 返回 503；但 `/v1/*`（OpenAI 兼容代理）的鉴权是"可选"的——仅当 `API_SECRET` 非空时才校验，为空时 `/v1/*` **完全开放**。
> 因此**务必配置 `API_SECRET`**，否则聊天代理将无鉴权暴露。`VARIABLES.md` 中"`/v1/*` 未配置时返回 503"的描述与代码不符，以代码为准。

### 示例（脱敏，请勿使用这些值）

```env
PORT=10000
API_SECRET=YOUR_RANDOM_SECRET_HERE
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_KEY=YOUR_ANON_KEY_HERE
SUPABASE_SERVICE_KEY=YOUR_SERVICE_ROLE_KEY_HERE
CHAT_API_KEY=YOUR_API_KEY_HERE
CHAT_BASE_URL=https://api.minimaxi.com/v1
CHAT_MODEL_NAME=abab6.5s-chat
```

### 必须强调的安全事项

- 网关使用**双 client 架构**：`SUPABASE_KEY`（anon key）用于只读查询，受 RLS 保护；`SUPABASE_SERVICE_KEY`（service_role key）仅用于 RPC 写操作（种植/烹饪/钱包等），绕过 RLS。两个 key 都在 `server.py` 初始化，绝不暴露给客户端。
- **`SUPABASE_SERVICE_KEY` 是后端密钥（service_role key），绝对不能放进客户端**（RikkaHub、浏览器等）。它能绕过 RLS 直接读写数据库。
- **`API_SECRET` 是连接网关的访问密钥**，客户端（RikkaHub）只需要网关地址和 `API_SECRET`。
- **`API_SECRET` 为空时，受保护入口返回 503**（无法使用）。
- **不要把 service_role key 填进 RikkaHub**。
- **客户端只需要：网关地址（Zeabur 域名）+ `API_SECRET`**，不需要数据库地址、数据库密码、模型服务密钥。
- 不要把 `.env` 的全部内容复制进客户端。

> 其余可选环境变量（Pinecone、Telegram、Google、搜索、NapCat、心跳间隔、宠物、自由活动、欲望系统等）详见 `VARIABLES.md`。未配置的可选变量会优雅降级，不报错。

---

## 5. 部署后检查

按风险从低到高依次检查。所有 URL 中 `你的域名` 替换为你的 Zeabur 公网域名。

### 检查 1：健康检查（无需认证）

```text
GET https://你的域名/health
```

- 期望：HTTP **200**
- 响应体：`{"status":"ok","service":"generic-mcp-gateway"}`
- 若返回 502/503/超时：服务未启动或端口不对，查 Zeabur 日志。

### 检查 2：控制台页面（无需认证）

```text
https://你的域名/console      （桌面端管理控制台 HTML）
https://你的域名/miniapp       （移动端配置面板 HTML）
```

- 这两个页面本身**不需要认证**即可打开。
- 页面内的管理操作（如钱包）会调用 `/api/*`，需要填入 `API_SECRET`（存在浏览器 localStorage）。
- 若页面打不开（404）：检查路径拼写；`/console` 和 `/console/` 都可以，`/miniapp` 和 `/miniapp/` 都可以。

### 检查 3：MCP 入口（需要认证）

MCP 使用 **SSE** 传输：

- **SSE 端点**：`GET https://你的域名/sse`
- **Messages 端点**：`POST https://你的域名/messages/?session_id=<由 SSE 握手下发>`
  - 注意：messages 路径带尾部斜杠 `/messages/`，session_id 由 SSE 握手的 `endpoint` 事件下发，不用手填。

认证方式（`API_SECRET` 放在以下任一 Header，二者等价）：

| Header | 格式 |
|---|---|
| `Authorization` | `Bearer YOUR_API_SECRET` |
| `X-Api-Key` | `YOUR_API_SECRET` |

- **错误密钥**（或缺失）：返回 HTTP **401** `{"error":"Unauthorized: Missing or invalid API key"}`
- **空密钥**（`API_SECRET` 未配置）：返回 HTTP **503** `{"error":"Service unavailable: API_SECRET not configured"}`
- **客户端不支持自定义 Header 时**：部分 MCP 客户端只支持 `Authorization: Bearer`，那就用这个 Header。本网关同时支持 `Authorization` 和 `X-Api-Key`。
- 当前网关**不支持 Streamable HTTP**（没有 `/mcp` 单一端点）；生产环境只走 SSE。

### 检查 4：工具发现

连接 MCP 后，客户端应能发现工具列表。确认方法：

1. 在客户端的 MCP 服务列表里，该服务显示"已连接/Connected"。
2. 工具数量应为数十个（当前共 73 个），其中包含 `home_observe`、`plant_seed`、`cook_recipe`、`cat_status`、`wallet_check` 等名字。
3. 若工具列表为空：见第 13 节"工具列表为空"排查。

---

## 6. RikkaHub 类 MCP 客户端接入

### 6.1 通用配置

MCP 客户端连接远程服务通常需要填写以下字段：

| 字段 | 应填内容 |
|---|---|
| 名称 | 自定义，如 `AI Companion Home` |
| Transport（传输类型） | **SSE** |
| Server URL（服务地址） | `https://你的域名/sse` |
| Headers（请求头） | `Authorization: Bearer YOUR_API_SECRET`（或 `X-Api-Key: YOUR_API_SECRET`） |
| 是否启用 | 是 |

脱敏示例（如果你的客户端用 JSON 配置）：

```json
{
  "name": "AI Companion Home",
  "transport": "sse",
  "url": "https://YOUR_ZEABUR_DOMAIN/sse",
  "headers": {
    "Authorization": "Bearer YOUR_API_SECRET"
  }
}
```

> 如果你的客户端配置不是 JSON 形式（而是表单字段），按上表对应填写即可，不必强行用 JSON。
> Transport 类型必须选 **SSE**，不是 Streamable HTTP。

### 6.2 RikkaHub

> 在 RikkaHub 的 MCP 服务配置中新增远程服务，选择当前网关支持的 **SSE** 传输，填写 Zeabur 域名（`https://你的域名/sse`）和认证 Header（`Authorization: Bearer YOUR_API_SECRET`）。不同版本的菜单名称可能不同，以客户端当前界面为准。

> 说明：RikkaHub 不同版本的界面布局会变化，本指南不臆测具体按钮名称和位置。
> 核心是三件事：**选 SSE 传输、填 `/sse` 地址、填 `API_SECRET` 认证头**。

### 6.3 不要填写的内容

在 RikkaHub（或任何 MCP 客户端）中**不要**填写：

- ❌ `SUPABASE_KEY`（service_role key，仅后端用）
- ❌ 数据库密码
- ❌ 模型服务密钥（`CHAT_API_KEY`）
- ❌ service role 任何形式
- ❌ `.env` 的全部内容
- ❌ Pinecone / Google / Telegram 等其他服务的密钥

客户端**只需要**：网关地址 + `API_SECRET`。也不要把 `API_SECRET` 发给不受信任的人。

---

## 7. 第一次连接建议

**不要一开始就让模型"自由试用所有工具"**，否则可能改变状态、库存或钱包。

建议按以下只读优先的顺序测试：

| 步骤 | 推荐对 AI 说 | 预期调用工具 | 预期看到 | 不应发生 |
|:---:|---|---|---|---|
| 1 | "请只观察当前家庭状态，不要执行任何写操作。" | `home_observe` | 房间、成员、近期事件概览 | 任何写入 |
| 2 | "看看小满现在怎么样，只读取状态。" | `home_observe_member` 或 `cat_status` | 小满的状态数值 | 改变小满状态 |
| 3 | "去花园看看现有植物，不要浇水。" | `garden_observe` | 植物列表、种子目录 | 浇水/种植 |
| 4 | "看看厨房库存有什么。" | `pantry_observe` | 食材、菜品、可做菜谱 | 烹饪/消耗 |
| 5 | "查一下钱包余额和最近流水。" | `wallet_check`、`wallet_log` | 余额、流水 | 入账/支出 |

> 这些都是只读工具，不会修改数据库。先用它们确认连接正常、数据能返回。

---

## 8. 功能验收流程

### A 级：只读验收（不改变数据库）

依次调用以下只读工具，确认都能正常返回：

```text
home_observe
home_observe_room
home_observe_member
home_timeline
garden_observe
pantry_observe
list_letters
list_room_notes
cat_status
wallet_check
wallet_log
```

> 注意：当前生产库的 `home_plants`、`home_inventory`、`home_dishes`、`home_events`、`home_action_runs`、`home_letters`、`home_notes` 表**均为空**（0 行）。所以这些只读工具可能返回空列表——**这不一定是服务错误**，只是还没有人用过新功能。

### B 级：低风险写入（会产生永久生活记录）

这些操作会写 `home_action_runs` 和/或 `home_events` 等表：

```text
home_enter_room      （进入房间）
home_rest            （休息）
home_sleep           （睡眠）
home_spend_time      （陪伴）
leave_note           （留便利贴）
write_letter         （写信）
```

> ⚠️ **提醒**：
> - 这些操作会产生**永久生活记录**，不要当成"临时测试数据"。
> - 本项目**不允许未经确认删除测试记录**。
> - 测试内容应当是**你愿意长期保留的真实内容**（例如真实想留的便利贴、真实想写的信）。
> - 每个写入工具都需要 `action_key`（见第 10 节）。

### C 级：资源状态测试（会改变植物、库存或菜品）

测试顺序必须基于真实生长规则，**不要伪造时间或直接改成熟状态**：

```text
1. plant_seed        （种一颗种子，如 mint 薄荷，30 分钟成熟）
2. garden_observe    （确认已种下）
3. water_plant       （浇水）
4. 等待满足真实生长时间（薄荷 30 分钟、番茄 60 分钟、生菜 45 分钟、胡萝卜 90 分钟、草莓 120 分钟）
5. harvest_plant     （收获，食材入库存）
6. pantry_observe    （确认库存有食材）
7. cook_recipe       （按菜谱烹饪，如 mint_tea 薄荷茶 = 薄荷×2）
8. eat_dish          （吃菜品）或 feed_member（喂给小满）
```

> ⚠️ **重要限制**：
> - 不要提供伪造时间、直接改成熟状态或数据库 UPDATE 的方法。
> - 植物按真实时间生长，必须等待。
> - 当前种子目录 5 种：tomato / mint / lettuce / carrot / strawberry。
> - 当前菜谱 3 个：`vegetable_soup`（胡萝卜+生菜）、`tomato_egg`（**鸡蛋+番茄**）、`mint_tea`（薄荷）。
> - **`tomato_egg` 需要鸡蛋，但鸡蛋无法通过种植获取**，因此这个菜谱目前不能自然完成。
> - `vegetable_soup` 和 `mint_tea` 可以自然完成。
> - 建议第一次用 `mint`（30 分钟最快）走通"种→浇→收→做薄荷茶→吃"全链路。

### D 级：钱包测试（风险最高）

**优先只读**：

```text
wallet_check    （查余额）
wallet_log      （查流水）
```

如确实要测试写入：

- 使用**真实、愿意保留**的零花钱或打赏，**不使用"测试后删除"**。
- **不向生产钱包随意加钱或扣钱**。
- 金额保持最小且有真实理由。
- 重复 `source_key` 会被幂等保护（不会重复入账）。
- MCP `wallet_earn` **不允许** bypass 周上限（`bypass_cap=False` 硬编码）。
- 管理端零花钱/打赏使用后端 API（`/api/wallet/allowance`、`/api/wallet/tip`），需要 `API_SECRET`。

---

## 9. 建议的真实使用方式

以下是对 AI 说的示例，**不能保证模型一定调用工具**（取决于模型理解）：

| 示例话术 | 风险类型 |
|---|---|
| "先观察一下家里现在的状态，不要做任何修改。" | 只读 |
| "看看小满现在怎么样，只读取状态。" | 只读 |
| "去花园看看现有植物，不要浇水。" | 只读 |
| "如果厨房有能做的菜，先告诉我需要哪些食材，不要马上做。" | 只读 |
| "去卧室休息半小时，使用 Home Runtime 的休息工具。" | 会写入（改变状态） |
| "给我写一封信并保存到小屋里，不要发到 QQ 或 Telegram。" | 会写入 |
| "在客厅留一张便利贴，内容是……" | 会写入 |
| "种一颗薄荷，然后浇点水。" | 会改变库存 |
| "做一杯薄荷茶，然后自己喝掉。" | 会改变库存 |

---

## 10. action_key 说明

> `action_key` 是一次生活动作的**唯一编号**。网络重试时重复使用同一个编号，可以防止重复扣库存、重复收获或重复写入。

说明：

- **哪些工具需要 `action_key`**：所有会写数据库的 Home Runtime 工具（进入房间、休息、睡眠、陪伴、种植、浇水、收获、烹饪、吃、喂、写信、拆信、归档信、留便利贴、读便利贴、归档便利贴）。
- **如何生成**：客户端或模型生成一个唯一字符串，推荐用 **UUID**。
- **重试时必须复用原 `action_key`**：如果网络超时重试，用同一个 `action_key`，系统会返回已有结果而不是重复执行。
- **新动作使用新 `action_key`**：每次新的动作都要换一个新的。
- **不要使用固定的 `"test"`**：这会导致后续同名动作被误判为重复。
- **不展示内部数据库 UUID**：`action_key` 是你生成的，不是数据库内部的行 ID。

示例：

```text
action_550e8400e29b41d4a716446655440000
```

---

## 11. 当前后台行为

> ⚠️ **C5 起本节已按统一自主调度现状更新**（更早的历史描述见下文备注）：

```text
当前后台（C5 统一自主调度 async_unified_autonomy）会：
- 按统一候选（普通自由活动 + 外向 + 秘密日记 + Home 活动）让模型选择一个稳定 activity_id
- Home 候选按 HOME_AUTONOMY_PHASE 分层，选中后执行真实 Home 工具
  （home:observe 看看家里 / home:garden 照料花园 / home:kitchen 做饭和用餐 等），
  可用工具 = 当前 phase 工具集 ∩ 该活动工具组
- 执行旧宠物 tick（cat_tick，状态衰减）与紧急照料链路（不属于统一调度）
- 秘密日记只写 home_private_diaries（C4 起权威源）

当前后台不会：
- 再启动旧 async_free_activity / async_home_autonomy_tick 双循环（仅保留为兼容入口）
- 通过"逛虚拟小屋"开放 house_do/house_put/house_take/house_update_desc（旧工具保留但不进统一候选）
- 模型输出非法 activity_id 时随机兜底执行另一个活动
- 写旧 memories.Secret_Diary（C4 起新日记不写旧表）
```

进一步说明：

- **统一调度每次唤醒只产生一条 `activity_logs`**（`source=unified_autonomy`），start 位于选择模型与所有真实副作用之前；两个旧执行器内部都不再管理顶层日志。
- 欲望引擎（DESIRE_DRIVEN）的旧中文建议经 `activity_registry.py` 映射为 `activity_id`，只在本轮候选内作为倾向，不能绕过淘宝/冲浪门控、Home phase 与 FREE/HOME 开关。
- 防连续重复以规范 `activity_id` 计（`activity_logs` 的 `unified_autonomy`/`free_activity`/`home_autonomy` 三个 source 兼容，旧 `memories` 补足）。
- `pet_agent_outbound` 表当前有 9 条 `pending` 记录，但**没有消费者**（死队列）——后台的宠物照料实际走的是 RPC 返回值里的 `threshold_event`，不经过这个队列。
- 下文第 12 节"功能状态表"为 C2 之前的历史快照，其中"后台自主调用 ❌"的表述已被 C5 统一调度的 Home 工具执行取代；真实数据库写链仍以实际验收为准。

---

## 12. 功能状态表

| 能力 | 已实现（MCP 工具） | 单元测试 | 真实数据库写链 | 后台自主调用 |
|---|:---:|:---:|:---:|:---:|
| 房间观察（home_observe 等） | ✅ | ✅（mock） | ❌ 未验证（表为空） | ❌ |
| 进入房间（home_enter_room） | ✅ | ✅（mock） | ❌ 未验证 | ❌ |
| 休息（home_rest） | ✅ | ✅（mock） | ❌ 未验证 | ❌ |
| 睡眠（home_sleep） | ✅ | ✅（mock） | ❌ 未验证 | ❌ |
| 陪伴（home_spend_time） | ✅ | ✅（mock） | ❌ 未验证 | ❌ |
| 种植（plant_seed） | ✅ | ✅（mock） | ❌ 未验证（home_plants 为空） | ❌ |
| 浇水（water_plant） | ✅ | ✅（mock） | ❌ 未验证 | ❌ |
| 收获（harvest_plant） | ✅ | ✅（mock） | ❌ 未验证 | ❌ |
| 库存（pantry_observe） | ✅ | ✅（mock） | ❌ 未验证（home_inventory 为空） | ❌ |
| 正式烹饪（cook_recipe） | ✅ | ✅（mock） | ❌ 未验证 | ❌ |
| 自由烹饪（cook_freestyle） | ✅ | ✅（mock） | ❌ 未验证 | ❌ |
| Finn 食用（eat_dish） | ✅ | ✅（mock） | ❌ 未验证 | ❌ |
| 小满喂食（feed_member） | ✅ | ✅（mock） | ❌ 未验证 | ❌ |
| 信件（write_letter 等） | ✅ | ✅（mock） | ❌ 未验证（home_letters 为空） | ❌ |
| 便利贴（leave_note 等） | ✅ | ✅（mock） | ❌ 未验证（home_notes 为空） | ❌ |
| 新私密日记 | ❌（已从 MCP 移除） | ✅（安全断言） | ❌ 未验证（表为空） | ❌ |
| 钱包（wallet_*） | ✅ | ✅ | ✅（wallet 表有数据） | 部分（旧自动收入已移除） |
| 宠物状态（cat_*） | ✅ | ✅ | ✅（pets 表有数据） | ✅（旧 tick 自动运行） |
| Home Context 注入 | ✅ | ✅ | —（只读） | ✅（注入但不执行工具） |

> **关键说明**：新 Home Runtime 的单元测试全部使用 **mock**（模拟数据库），**不代表真实数据库写链已验证**。
> 当前生产库的 `home_plants`、`home_inventory`、`home_dishes`、`home_events`、`home_action_runs`、`home_letters`、`home_notes` 均为 0 行，说明这些链路**尚未在生产环境端到端跑通过**。
> 钱包和宠物（旧系统）有真实数据，已验证可用。

---

## 13. 常见问题排查

### HTTP 503

```text
原因：API_SECRET 未配置（为空）。
表现：{"error":"Service unavailable: API_SECRET not configured"}
解决：在 Zeabur 环境变量里配置 API_SECRET，重新部署。
注意：仅 /api/* /sse /messages 返回 503；/v1/* 在 API_SECRET 为空时不拦截（见第 4 节）。
```

### HTTP 401

```text
原因：API_SECRET 缺失或错误。
表现：{"error":"Unauthorized: Missing or invalid API key"}
解决：检查客户端 Header 里的 API_SECRET 是否与网关配置一致。
      支持的 Header：Authorization: Bearer <secret> 或 X-Api-Key: <secret>。
```

### MCP 连不上

依次检查：

1. Zeabur 服务是否在线（`/health` 是否返回 200）。
2. URL 是否正确（应为 `https://你的域名/sse`）。
3. Transport 是否选了 **SSE**（不是 Streamable HTTP）。
4. Header 是否正确（`Authorization: Bearer <API_SECRET>`）。
5. 是否用了 HTTPS（不是 HTTP）。
6. 路径是否正确（`/sse`，不是 `/mcp`）。
7. 查看 Zeabur 日志有无报错。
8. 客户端是否支持远程 MCP（SSE 类型）。

### 工具列表为空

检查：

1. 是否连到了正确的 MCP 入口（`/sse`）。
2. 是否通过了认证（401 会导致连不上）。
3. FastMCP transport 是否匹配（客户端选 SSE）。
4. Zeabur 是否启动了正确入口（`run.py`，不是单独 `server.py`）。
5. 查看日志是否有 MCP SDK 版本错误（需要 `mcp>=1.10,<2.0`）。

### Home Runtime 返回空数据

当前生产库以下表可能为空：

```text
home_plants
home_inventory
home_dishes
home_events
home_action_runs
home_letters
home_notes
```

`home_observe`、`garden_observe`、`pantry_observe`、`list_letters`、`list_room_notes` 返回空列表**不一定是服务错误**，只是还没有人用过新功能。

### 小满状态与旧页面不同

- 生理状态（饱食度、清洁度、精力）来自 `pets` 表（旧系统）。
- Home 关系/情绪状态来自 `home_member_states` 表（新系统）。
- 两者是分开的，**不应直接比较**两个表的旧快照。

### `balcony` 显示 unknown

旧宠物系统有 `balcony`（阳台）房间，旧 `rpc_cat_room_mischief` 可能把小满移到 `balcony`；但新 Home Runtime 的 `home_rooms` **没有 balcony**（只有 9 个房间：living_room/bedroom/kitchen/study/studio/garden/seaside/observatory/basement）。这是已知的**房间映射缺口**。

### 钱包按钮失败

检查：

1. `API_SECRET` 是否正确配置。
2. 前端是否调用了 `/api/wallet/*`（而不是直接调 Supabase RPC）。
3. 前端**不能**再通过 anon RPC 写钱包（RPC 已对 anon/authenticated 撤销执行权限）。
4. **不能**把 service_role key 放入浏览器/前端。

### 某个动作返回 `ACTION_EXISTS`

说明：

- 该 `action_key` 已经使用过。
- 如果是**同一次网络重试**，应读取已有结果（系统会返回原结果，不会重复执行）。
- 如果是**新动作**，换一个新的 `action_key`。

### 完整测试不是全绿

当前验收结果（截至 2026-08-19）：

```text
733 tests
725 passed
6 failed
2 skipped
```

6 个失败**全部集中在旧 `test_tool_loop`**（与 Home Runtime 无关，主要是天气 schema 和自由活动回退逻辑的断言未更新）。2 个 skipped 在 `test_console`。**不能写成"全部测试通过"**。

---

## 14. 已知限制

根据最终验收事实：

- **新 Home Runtime 尚未接入后台自主生活**：后台不会自动种植/烹饪/写信/留便利贴。
- **真实数据库写链尚未端到端验收**：新 Home 表均为空，单元测试用 mock。
- **`pet_agent_outbound` 无消费者**：9 条 pending 死队列，持续增长。
- **`balcony` 房间映射缺口**：旧宠物有 balcony，新 Home Runtime 没有。
- **浇水没有冷却**：可以频繁浇水（水分恢复 100）。
- **植物不会枯萎**：没有枯萎机制，种下后只需等成熟。
- **`garden_observe` 可能显示未结算状态**：状态结算在进入房间等动作时触发，纯观察可能看到未结算的旧状态。
- **cook 的库存位置限制**：烹饪只从库存扣食材，库存为空时无法烹饪。
- **ownership 边界**：工具默认 `actor_key=ai_primary`（Finn），部分操作有目标校验。
- **Phase 4/5 迁移 SQL 是 stub**：`migrations/20260818_010` 和 `011` 只有注释头，实际表/RPC 通过 Supabase `apply_migration` 直接部署，未纳入版本控制。Phase 8 的部分修复文件同样是纯注释记录。
- **完整测试仍有失败**：6 个失败在旧 `test_tool_loop`。
- **`egg` 无法通过种植获取**：`tomato_egg` 菜谱不能自然完成。
- **`/v1/*` 在 `API_SECRET` 为空时不拦截**：与 `VARIABLES.md` 描述不符，务必配置 `API_SECRET`。

---

## 15. 安全说明

- **`API_SECRET` 必须配置**：不配置则受保护入口 503，且 `/v1/*` 无鉴权暴露。
- **不公开 Zeabur MCP 地址和密钥组合**：两者一起泄露等于完全开放。
- **不把 service_role key 给客户端**：它能绕过 RLS 直接读写全部数据。
- **不开放 Supabase 写 RPC 给 anon/authenticated**：钱包和 Home RPC 已对 anon/authenticated 撤销执行权限（仅 service_role 可执行）；`cat_*` RPC（除 shop_buy）对 anon 仍开放（历史遗留）。
- **私密日记不通过普通 MCP 读取**：私密日记工具已从 MCP 移除。
- **`search_memory` 不返回 Secret_Diary**：私密标签被排除。
- **未拆信件列表不返回正文**：`list_letters` 只返回标题/摘要，正文需 `open_letter`。
- **不在日志中打印密钥和私密正文**。
- **不使用数据库后台直接改状态**：应通过工具或 API。
- **任何数据库删除操作必须先取得用户明确同意**。

---

## 16. Zeabur 更新流程

> 本地目录当前**不是 Git 仓库**。Zeabur 的更新方式取决于你的实际代码来源（GitHub 关联或手动上传）。以下为通用流程：

1. **更新代码**：推送代码到关联的 GitHub 仓库，或在 Zeabur 重新上传。
2. **触发重新部署**：Zeabur 检测到代码变更会自动重新构建部署；也可手动点 Redeploy。
3. **检查构建日志**：确认 `pip install` 成功、无 MCP SDK 版本错误（需要 `mcp>=1.10,<2.0`）。
4. **检查健康端点**：`GET https://你的域名/health` 返回 200。
5. **检查消息进程**：日志里有 `进程A · 消息进程` 启动信息，`/health` 正常。
6. **检查后台进程**：日志里有 `进程B · 后台进程` 启动信息。
7. **检查 MCP 工具发现**：客户端重新连接，确认工具列表正常。
8. **检查 `API_SECRET`**：确认环境变量仍在（Zeabur 重新部署不会清空环境变量）。
9. **执行只读冒烟测试**：调用 `home_observe`、`cat_status`、`wallet_check` 确认连接。
10. **不在更新后立即执行钱包或库存写测试**：先确认只读正常，再考虑写入。

---

## 17. 备份和恢复

只写当前真实可行的方法：

- **Supabase 生产数据在执行未来迁移前应备份**：Zeabur 只部署代码，数据库在 Supabase，代码备份不等于数据备份。
- **当前迁移文件可能不足以完整重建生产 RPC**：Phase 4/5/8 的迁移是 stub/注释，真实 SQL 在版本控制之外（通过 Supabase `apply_migration` 直接部署）。仅靠 `migrations/` 目录无法完整重建。
- **不要只备份代码**：还要备份数据库结构、RPC 函数和 Policy。
- **记录当前表结构、RPC 和 Policy**：可用 Supabase Dashboard 的数据库导出，或 `pg_dump --schema-only`。
- **不要提供未经验证的自动恢复脚本**：本指南不提供自动恢复。
- **不执行任何实际备份或恢复**：本指南仅说明方法。

---

## 18. 附录

### A. MCP 地址模板

```text
SSE 端点：    https://你的域名/sse
Messages 端点：https://你的域名/messages/?session_id=<由SSE握手下发>
健康检查：    https://你的域名/health
控制台：      https://你的域名/console
移动面板：    https://你的域名/miniapp
```

### B. Header 模板（脱敏）

```text
Authorization: Bearer YOUR_API_SECRET
```

或

```text
X-Api-Key: YOUR_API_SECRET
```

两者等价，任选其一。

### C. 只读测试清单

```text
home_observe          home_observe_room     home_observe_member
home_timeline         garden_observe        pantry_observe
list_letters          list_room_notes       cat_status
cat_shop_list         wallet_check          wallet_log
query_weather         get_user_profile      echo
```

### D. 低风险写入清单

```text
home_enter_room       home_rest             home_sleep
home_spend_time       leave_note            write_letter
save_memory           manage_user_fact      manage_memory_house
```

> 这些会产生永久记录，请用愿意长期保留的真实内容。

### E. 高风险操作清单

```text
plant_seed            water_plant           harvest_plant
cook_recipe           cook_freestyle        eat_dish
feed_member
cat_feed              cat_play              cat_clean
cat_pet               cat_restore_energy    cat_shop_buy
wallet_earn           wallet_spend          wallet_exchange
wallet_overtime_withdraw
organize_knowledge_base（可删除记忆/画像）
```

> 这些会改变库存、成员状态、宠物状态或钱包，请谨慎使用，并使用唯一 `action_key`。

### F. 当前已知测试失败

```text
总计：733 tests
通过：725
失败：6（全部在 test_tool_loop，与 Home Runtime 无关）
跳过：2（在 test_console）

失败项（均为旧自由活动/天气 schema 相关的断言未更新）：
1. test_build_tool_schema_block_empty_activity
2. test_avout_hint_passed_to_stage1
3. test_disabled_degrades_single_call
4. test_empty_draft_no_tools_returns_none
5. test_enabled_no_tools_activity_single_call
6. test_invalid_activity_fallback_random
```

### G. 工具快速索引

| 类别 | 工具名 |
|---|---|
| 家庭观察 | `home_observe` `home_observe_room` `home_observe_member` `home_timeline` |
| 基础生活 | `home_enter_room` `home_rest` `home_sleep` `home_spend_time` |
| 种植厨房 | `garden_observe` `plant_seed` `water_plant` `harvest_plant` `pantry_observe` `cook_recipe` `cook_freestyle` `eat_dish` `feed_member` |
| 信件便利贴 | `write_letter` `list_letters` `open_letter` `archive_letter` `leave_note` `list_room_notes` `read_note` `archive_note` |
| 宠物 | `cat_status` `cat_feed` `cat_play` `cat_clean` `cat_pet` `cat_restore_energy` `cat_shop_list` `cat_shop_buy` |
| 钱包 | `wallet_check` `wallet_earn` `wallet_spend` `wallet_exchange` `wallet_overtime_withdraw` `wallet_log` |
| 记忆搜索 | `save_memory` `search_memory` `manage_user_fact` `get_user_profile` `organize_knowledge_base` `manage_memory_house` |
| 天气 | `query_weather` `query_weather_forecast` |
| 通用/旧 | `echo` `web_search` `send_notification` `manage_reminder` `send_email_via_api` `check_inbox` `read_full_email` `reply_external_email` `add_calendar_event` `get_calendar_events` `modify_calendar_event` `save_expense` `check_expense_report` `manage_piggy_bank` `house_look` `house_do` `house_put` `house_take` `house_update_desc` `device_status` `render_html_to_image` `list_obsidian_cloud` `read_obsidian_cloud` `write_obsidian_cloud` `compose_music` `cover_existing_song` |

---

> **使用本指南时最重要的原则：**
> 1. 先用只读工具确认连接。
> 2. 写操作使用愿意长期保留的真实内容。
> 3. 不要将 service_role key 填进 RikkaHub。
> 4. RikkaHub 只配置 Zeabur 网关地址和 `API_SECRET`。
> 5. 不要让模型第一次连接就"自由尝试全部工具"。
> 6. 当前新 Home Runtime 不会在后台自主运行。
