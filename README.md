# Finn0616 — MCP 通用网关

> 基于 FastMCP v1 的多 LLM 网关，提供统一的 MCP 工具接口、多模型调度、长期记忆、消息推送与后台任务调度。

---

## 功能概览

| 模块 | 文件 | 说明 |
|------|------|------|
| MCP Server | `server.py` | 主入口，注册全部 MCP tools |
| Gateway | `gateway.py` | ASGI 中间件（/v1/* 代理、/api/* 管理、/qq-ws、/console） |
| Heartbeat | `heartbeat.py` | 后台任务协调器（问候/日记/提醒/日程/宠物 tick） |
| Background | `background.py` | 后台进程入口 |
| Supervisor | `run.py` | 双进程监管脚本 |
| Console SPA | `console.html` | 桌面管理面板 |

---

## 三系统联动

### 🏠 小屋 (Memory House)

| 工具 | 参数 | 说明 |
|------|------|------|
| `house_look` | `room_id` | 查看房间：物品 + 日记 |
| `house_do` | `room_id`, `entry_type`, `content` | 在房间做某事（写日记） |
| `house_put` | `room_id`, `name`, `emoji`, `description` | 放置物品到房间 |
| `house_take` | `object_id` | 从房间拿走物品 |
| `house_update_desc` | `room_id`, `description` | 更新房间描述 |

### 🐱 小满 (Home Cat)

| 工具 | 参数 | 说明 |
|------|------|------|
| `cat_status` | — | 查询宠物状态 |
| `cat_feed` | `item_id` | 喂食 |
| `cat_play` | `item_id` | 玩耍 |
| `cat_clean` | `item_id` | 清洁 |
| `cat_pet` | — | 抚摸 |
| `cat_restore_energy` | — | 恢复精力 |
| `cat_shop_list` | — | 商店列表 |
| `cat_shop_buy` | `item_id`, `qty` | 购买物品（扣钱包） |

### 💰 小钱包 (Virtual Wallet)

| 工具 | 参数 | 说明 |
|------|------|------|
| `wallet_check` | — | 查余额/周统计 |
| `wallet_earn` | `amount`, `source_key`, `reason` | 入账（幂等） |
| `wallet_spend` | `amount`, `reason` | 支出 |
| `wallet_exchange` | `target`, `reason` | 兑换 tea/gift |
| `wallet_overtime_withdraw` | `amount`, `reason` | 从加班银行取出 |
| `wallet_log` | `limit`, `offset` | 查流水 |

---

## 后台 Tick 系统

`heartbeat.py` 中的 `async_pet_house_tick()` 负责：
- **状态衰减**：每小时饥饿 -2 / 快乐 -1.5 / 清洁 -1
- **睡眠滞回**：精力 < 20 自动入睡，精力 >= 40 自动醒来
- **阈值事件**：饥饿度从 >=30 降到 <30 时触发 `hungry_cat` 事件
- **受控捣乱**：30% 概率换房 + 物品轻微破坏
- **自动工资**：日记 2 CNY/篇 + 陪聊 1 CNY/小时

---

## 部署

```bash
# 本地启动（单进程模式）
python server.py

# 双进程模式（生产推荐）
python run.py
```

详见 [VARIABLES.md](VARIABLES.md) 环境变量说明。
