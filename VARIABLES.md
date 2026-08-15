# 📋 环境变量完整清单

本文档列出「通用 MCP 网关」**所有**支持的环境变量，按子系统分组。每项标注是否必填、默认值、来源代码与说明。

> - 标注 **【必填】**：缺失会导致对应功能无法启动。
> - 标注 **【可选】**：留空即自动禁用该功能，网关会优雅降级。
> - 兼容旧变量名（向后兼容）在「兼容别名」列注明。

---

## 目录
- [1. 基础部署](#1-基础部署)
- [2. 数据库 (Supabase)](#2-数据库-supabase)
- [3. 多模型 LLM](#3-多模型-llm)
- [4. 向量记忆 (Pinecone 单写)](#4-向量记忆-pinecone-单写)
- [5. 通讯渠道](#5-通讯渠道)
- [6. Google 集成](#6-google-集成)
- [7. 地图 / GPS](#7-地图--gps)
- [8. 多媒体生成](#8-多媒体生成)
- [9. 网页搜索](#9-网页搜索)
- [10. 云端笔记 (WebDAV)](#10-云端笔记-webdav)
- [11. NapCat QQ 接入](#11-napcat-qq-接入)
- [12. 后台心跳调度](#12-后台心跳调度)
- [13. 其他可选](#13-其他可选)
- [14. 欲望驱动系统 (情感 / 欲望引擎)](#14-欲望驱动系统-情感--欲望引擎)
- [最小可运行配置示例](#最小可运行配置示例)

> 💡 **运行时门控**：以下功能开关均通过桌面控制台 `/console` 管理，存储在 Supabase `user_facts.sys_config` JSON 字段中，无需重启即可生效：
> - `telegram_enabled` / `qq_enabled` —— 通讯渠道轮询
> - `emotion_enabled` —— 情感引擎总闸
> - `desire_driven` —— 欲望驱动总闸（另见 §14）
> - `chat_write_enabled` —— 对话写入数据库（网页/TG/QQ）
> - `vector_injection_enabled` —— Pinecone 向量记忆注入
>
> 所有开关默认 `true`（兼容旧行为），控制台关闭后 5 秒内生效。

---

## 1. 基础部署

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `PORT` | ✅ | `10000` | 网关监听端口（Dockerfile `EXPOSE 10000`） |
| `GATEWAY_HOST` | ❌ | `localhost:8000` | 反代场景下修正的 Host 头，一般留空 |
| `API_SECRET` | ✅ | 空 | `/api/*` 管理接口的安全密钥，防止未授权调用 |
| `LOG_FILE` | ❌ | 空 | 日志文件路径（供 `/api/logs` 读取，留空则用平台日志） |
| `RESTART_WEBHOOK_URL` | ❌ | 空 | 云平台重启回调 URL（供 `/api/restart` 调用） |

### 1.1 🧠 智能体身份（控制 `/v1/chat/completions` 的人格化行为）

仅当配置了 `SUPABASE_URL` 时生效（启用上文注入 + 存库）。不配则纯透传。
> 🌐 **全渠道生效**：以下注入（人设 / 画像 / 阶段总结 / Pinecone 向量记忆 / 设备快照 / 近期对话流水）
> 由 `server._build_channel_context()` 统一构建，**网页 (`/v1/chat/completions`)、Telegram 轮询、QQ (NapCat) 三个渠道共用**，
> 任一处注入失败均优雅降级，不影响回复。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `USER_NAME` | ❌ | `用户` | 用户称呼，注入到 system 提示与存库记录（如 `张三`） |
| `AI_NAME` | ❌ | `助手` | AI 角色称呼，注入到 system 提示与存库记录（如 `小橘`） |
| `USER_ID` | ❌ | `default` | 用户隔离 ID（Pinecone 向量记忆按此区分不同用户） |
| `AI_PERSONA` | ❌ | 空 | AI 人设完整文本，会拼接到 system 提示最前面 |
| `CHAT_TAG` | ❌ | `Web_Chat` | 存库时给本轮对话打的标签（用于区分网页/TG/QQ 渠道） |
| `CHANNEL_DISPLAY_MAP` | ❌ | 见默认 | 🆕 渠道标签→显示名映射（JSON），注入到 system prompt「当前聊天渠道」。默认：`{"Web_Chat":"橘子岛","QQ_MSG":"QQ","QQ_Chat":"QQ","QQ_Group":"QQ","TG_MSG":"TG","Email_Process":"邮件"}`。例：`CHANNEL_DISPLAY_MAP={"Web_Chat":"橘子岛","QQ_MSG":"qq","TG_MSG":"tg"}` |
| `SUMMARY_THRESHOLD` | ❌ | `30` | 🆕 自动总结阈值：全渠道（网页/QQ/TG/邮件）对话流水累计达到该条数时，自动调用聊天模型（`main_chat`）生成第一人称阶段总结，存入 `Core_Cognition` 并归档旧记录。依赖 `CHAT_API_KEY`。 |
| `DEVICE_CONTEXT_ENABLED` | ❌ | `true` | 🆕 是否把 `device_data` 最新一条快照注入 system prompt（只注入最新一条并标注更新时间）。网页/TG/QQ 全渠道生效。设 `false`/`0` 关闭。 |
| `DEVICE_CONTEXT_TOP_APPS` | ❌ | `5` | 🆕 设备快照中「应用使用榜单」条数（1–10） |
| `DEVICE_CONTEXT_MAX_NOTIFS` | ❌ | `3` | 🆕 设备快照中「最近通知」条数（去重后取最近 N 条，设 `0` 不要通知） |
| `INJECT_DB_HISTORY` | ❌ | `auto` | 🆕 对话历史注入策略：`auto`=客户端已带 >1 条非 system 消息时跳过 DB 历史注入（维持 prompt cache 前缀稳定，默认）、`always`=总是注入、`never`=从不注入。仅网页渠道 (`/v1/chat/completions`) 生效。 |
| `INJECT_CORE_SUMMARIES` | ❌ | `auto` | 🆕 阶段总结（`Core_Cognition`）注入策略：`auto`=客户端已带 >1 条非 system 消息时跳过（与 `INJECT_DB_HISTORY` 同模式，默认）、`always`=总是注入（=旧行为）、`never`=从不注入。仅网页渠道生效；TG/QQ 渠道始终注入。 |

---

## 2. 数据库 (Supabase)

记忆、画像、提醒、记忆小屋、记账、设备定位等持久化所需。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `SUPABASE_URL` | ✅ | 空 | Supabase 项目 URL，如 `https://xxxxx.supabase.co` |
| `SUPABASE_KEY` | ✅ | 空 | Supabase service_role key（生产推荐）或 anon key |

> 建表 SQL 见 `DEPLOY_ZEABUR.md` 附录。
>
> 💡 **设备状态快照**：配置 `SUPABASE_URL`/`SUPABASE_KEY` 后，网关会在每次智能体对话时（**网页 / TG / QQ 渠道均生效**）自动读取 `device_data` 最新一条（`order=id desc limit 1`），
> 压缩渲染成「【设备状态快照】更新时间：…」文本块拼进 system prompt（位置 / 前台应用 / 健康 / 睡眠 / 应用Top / 最近通知）。
> 可用 `DEVICE_CONTEXT_ENABLED`、`DEVICE_CONTEXT_TOP_APPS`、`DEVICE_CONTEXT_MAX_NOTIFS` 调整（见 1.1）。

---

## 3. 多模型 LLM

网关支持 **7 个 LLM 角色**，按任务类型自动路由。最小化配置只需 `CHAT_*`，其余角色未配置时自动回退到 `CHAT_*`。

| 角色 | 用途 | 环境变量前缀 | 回退顺序 |
|------|------|-----------|---------|
| `chat` (main_chat) | 日常聊天、回复生成 | `CHAT_*` | 自身 → `OPENAI_*` |
| `compression` | 对话总结、阶段总结、日记生成 | `COMPRESS_*` | 自身 → `CHAT_*` → `OPENAI_*` |
| `background` | 自主问候、自由活动、提醒播报 | `BACKGROUND_*` | 自身 → `CHAT_*` → `OPENAI_*` |
| `silicon1` | 便宜兜底、简单任务 | `SILICON1_*` | 自身 |
| `vision` | 图片识别 / OCR | `VISION_*` | 自身 |
| `voice` | STT 语音转文字 | `VOICE_*` | 自身 → `OPENAI_*` |
| `deepseek` | 消息情感分类 | `DEEPSEEK_*` | 自身 → `CHAT_*` → 中性 |

> 🆕 **v5.0 新增角色化架构**：`compression` 和 `background` 独立配置，可在控制台 `/console` →「模型」页为每个角色绑定不同模型（如 chat=Minimax、compression=DeepSeek、background=硅基流动），实现「好钢用在刀刃上」。

### 3.1 默认 / 通用模型 (OpenAI 兼容) ⚠️ 已废弃

> ⚠️ **自 v2.0 起，系统不再主动调用此模型**。所有对话/总结/日记已统一改用 `main_chat`（见 3.2）。
> 本组变量仅作**向后兼容保留**：若 `main_chat` 未配置，极少数回退逻辑（如 `_ask_llm_async` 的模型名兜底）仍会读取它。
> **推荐：直接配置 `CHAT_*` 即可，无需配置本组。**

| 变量名 | 必填 | 默认值 | 兼容别名 |
|--------|:---:|--------|---------|
| `OPENAI_API_KEY` | ❌ | 空 | `DEFAULT_API_KEY` |
| `OPENAI_BASE_URL` | ❌ | 空（用官方） | `DEFAULT_BASE_URL` |
| `OPENAI_MODEL_NAME` | ❌ | `gpt-3.5-turbo` | `DEFAULT_MODEL_NAME` |

> 支持任何 OpenAI 兼容服务：OpenAI / DeepSeek / 通义千问 / 硅基流动 / 自建 vLLM。第三方需配置 `OPENAI_BASE_URL`。

### 3.2 主对话模型 CHAT (日常聊天主力)

可被数据库 `user_facts` 表 `key='llm_settings'` 的 JSON 动态覆盖。

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `CHAT_API_KEY` | ❌ | 空 |
| `CHAT_BASE_URL` | ❌ | `https://api.minimaxi.com/v1` |
| `CHAT_MODEL_NAME` | ❌ | `abab6.5s-chat` |

### 3.2b 压缩/总结模型 COMPRESS

> 🆕 角色化多模型架构的一部分。负责对话总结、阶段总结、日记生成等「压缩类」任务。
> 未配置时自动回退到 `CHAT_*`（主对话模型）。

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `COMPRESS_API_KEY` | ❌ | 空 |
| `COMPRESS_BASE_URL` | ❌ | 空 |
| `COMPRESS_MODEL_NAME` | ❌ | 空 |

### 3.2c 后台任务模型 BACKGROUND

> 🆕 角色化多模型架构的一部分。负责自主问候、自由活动、提醒播报等「后台背景类」任务。
> 未配置时自动回退到 `CHAT_*`（主对话模型）。

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `BACKGROUND_API_KEY` | ❌ | 空 |
| `BACKGROUND_BASE_URL` | ❌ | 空 |
| `BACKGROUND_MODEL_NAME` | ❌ | 空 |

### 3.3 硅基流动 SILICON1 (便宜模型)

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `SILICON1_API_KEY` | ❌ | 空 |
| `SILICON1_BASE_URL` | ❌ | `https://api.siliconflow.cn/v1` |
| `SILICON1_MODEL_NAME` | ❌ | `Qwen/Qwen2.5-7B-Instruct` |

### 3.3b DeepSeek (消息情感分类专用) 🆕

情感/欲望引擎的「消息语义分类器」默认用这个便宜快的模型，把用户消息判成 16 类情感基调之一 + 置信度，喂给情感引擎。分类失败会自动降级为中性（`neutral`），绝不阻塞正常回复。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `DEEPSEEK_API_KEY` | ❌ | 空 | DeepSeek 官方 API Key。不填则分类回退到主对话模型 `main_chat`，再没有就降级中性 |
| `DEEPSEEK_BASE_URL` | ❌ | `https://api.deepseek.com/v1` | 官方地址，一般不用改 |
| `DEEPSEEK_MODEL_NAME` | ❌ | `deepseek-v4-flash` | 分类用模型。V4 默认开 thinking，代码已按官方写法（`extra_body={"thinking":{"type":"disabled"}}`）**关闭思考模式**以省钱提速 |
| `CLASSIFY_PROVIDER` | ❌ | `deepseek` | 分类走哪个 provider，可改为 `silicon1` / `main_chat` 等，无需改代码 |

> 💡 注：旧模型名 `deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 停用，现统一用 `deepseek-v4-flash`。
> 非思考模式支持 `temperature`（分类用 `0.0`）；思考模式才不支持 temperature。

### 3.4 视觉模型 VISION (图片识别 / OCR)

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `VISION_API_KEY` | ❌ | 空 |
| `VISION_BASE_URL` | ❌ | 空 |
| `VISION_MODEL_NAME` | ❌ | `gpt-4o-mini` |

### 3.5 语音模型 VOICE (STT 语音转文字)

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `VOICE_API_KEY` | ❌ | 回退到 `OPENAI_API_KEY` |
| `VOICE_BASE_URL` | ❌ | `https://api.openai.com/v1` |

### 3.6 向量嵌入 (Doubao / 硅基流动)

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `DOUBAO_API_KEY` | ❌ | 空 |
| `DOUBAO_EMBEDDING_EP` | ❌ | 空（如 `BAAI/bge-m3`） |

### 3.7 AI 人设

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `AI_PERSONA` | ❌ | `你是一个通用智能助手。` |

---

## 4. 向量记忆 (Pinecone 单写)

> 🆕 v2.1：已移除 Mem0，记忆统一写入 Pinecone 向量库，支持语义检索。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `PINECONE_API_KEY` | ❌ | 空 | Pinecone 向量库 Key |
| `PINECONE_INDEX_NAME` | ❌ | `notion-brain-v2` | Pinecone 索引名 |
| `MEM0_USER_ID` | ❌ | `default` | 用户隔离 ID（兼容旧变量名，仍用于区分用户） |

---

## 5. 通讯渠道

### 5.1 Telegram

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `TG_BOT_TOKEN` | ❌ | 空 | Telegram Bot Token |
| `TG_CHAT_ID` | ❌ | 空 | 默认推送目标（私聊 ID） |
| `TG_GROUP_ID` | ❌ | 空 | 群组 ID（可选） |

### 5.2 邮件 (Resend)

| 变量名 | 必填 | 默认值 | 兼容别名 |
|--------|:---:|--------|---------|
| `RESEND_API_KEY` | ❌ | 空 | — |
| `MY_EMAIL` | ❌ | 空 | `ADMIN_EMAIL` |
| `GMAIL_BRIDGE_URL` | ❌ | 空 | Gmail 桥接地址（供信箱巡视器轮询） |

---

## 6. Google 集成

Gmail 收发 & Google 日历。需要 Google OAuth 用户令牌。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `GOOGLE_USER_TOKEN_JSON` | ❌ | 空 | OAuth 用户令牌 JSON（序列化为单行字符串） |
| `GOOGLE_CALENDAR_ID` | ❌ | `primary` | 目标日历 ID |

> 最简单获取 `token.json` 的方式：本地用 Google 官方 [quickstart](https://developers.google.com/gmail/api/quickstart/python) 跑一次。

---

## 7. 地图 / GPS

高德地图服务，周边探索 / 天气。设备定位数据通过 Supabase 的 `device_data` 表写入。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `AMAP_API_KEY` | ❌ | 空 | [高德开放平台](https://lbs.amap.com) Web 服务 Key |

---

## 8. 多媒体生成

### 8.1 AI 音乐 / 翻唱 (Replicate)

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `REPLICATE_API_KEY` | ❌ | 空 | Replicate 官方 Token |
| `MUSIC_MODEL_VERSION` | ❌ | 空 | 原创音乐模型 version hash |
| `VOICE_MODEL_VERSION` | ❌ | 空 | RVC 翻唱音色模型 version hash |
| `MUSIC_API_KEY` | ❌ | 空 | 其他音乐生成服务 Key（可选） |
| `MUSIC_API_URL` | ❌ | 空 | 其他音乐生成服务地址（可选） |

### 8.2 HTML 转图片 (HCTI)

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `HCTI_API_ID` | ❌ | 空 |
| `HCTI_API_KEY` | ❌ | 空 |

---

## 9. 网页搜索

默认使用 DuckDuckGo 免费兜底（零配置）。配置 Tavily 后切换到高质量搜索。

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `TAVILY_API_KEY` | ❌ | 空 |

---

## 10. 云端笔记 (WebDAV)

支持坚果云等 WebDAV 服务。

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `WEBDAV_URL` | ❌ | 空 |
| `WEBDAV_USER` | ❌ | 空 |
| `WEBDAV_PASSWORD` | ❌ | 空 |

---

## 11. NapCat QQ 接入

通过 [NapCat](https://github.com/NapNeko/NapCatQQ) 协议实现 QQ 机器人。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `NAPCAT_WS_URL` | ❌ | 空 | 正向 WS 地址（如 `ws://host:3001`） |
| `NAPCAT_HTTP_URL` | ❌ | 空 | HTTP 回调地址 |
| `NAPCAT_BOT_QQ` | ❌ | 空 | 机器人 QQ 号 |
| `NAPCAT_TARGET_USER` | ❌ | 空 | 限定响应的私聊用户 QQ（留空则所有人可聊） |
| `NAPCAT_NOTIFY_QQ` | ❌ | 空 | 掉线通知 QQ，多个用逗号分隔 |
| `NAPCAT_NOTIFY_TG` | ❌ | 空 | 掉线同时通知 TG（`true`/`false`） |
| `NAPCAT_ALLOWED_GROUPS` | ❌ | 空 | 允许响应的群号，逗号分隔 |
| `NAPCAT_RECONNECT_DELAY` | ❌ | `5` | 重连初始延迟（秒） |
| `NAPCAT_BACKOFF_FACTOR` | ❌ | `1.5` | 退避乘数 |
| `NAPCAT_MAX_DELAY` | ❌ | `60` | 最大重连延迟（秒） |

---

## 12. 后台心跳调度

`heartbeat.py` 的主动问候、消息总结、日程播报相关。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `HEARTBEAT_INTERVAL` | ❌ | `7200` | 主动问候间隔（秒） |
| `SUMMARIZE_INTERVAL` | ❌ | `1800` | 消息总结间隔（秒） |
| `SCHEDULE_MORNING_TIME` | ❌ | `07:30` | 日程早播时间 |
| `SCHEDULE_EVENING_TIME` | ❌ | `22:00` | 日程晚播时间 |
| `DIARY_TIME` | ❌ | `03:00` | 🆕 每日日记生成时间（24小时制）。到点自动拉取昨日全部对话流水，调用聊天模型（`main_chat`）生成第一人称"昨日回溯"日记，存入 Core_Cognition。启动时若发现昨日日记缺失会自动补写。依赖 CHAT_API_KEY。 |
| `SYNC_KEYS` | ❌ | 空 | 额外热同步的环境变量键，逗号分隔 |

### 12.1 宠物小屋 tick（Phase 5）

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `PET_HOUSE_TICK_INTERVAL` | ❌ | `3600` | 宠物小屋 tick 间隔（秒）。默认 1 小时触发一次状态衰减。 |
| `PET_HOUSE_TICK_ENABLED` | ❌ | `true` | 是否启用宠物小屋 tick。`false` 时关闭状态衰减、换房捣乱和自动工资结算。 |

### 12.2 自由活动 + 工具调用循环

`heartbeat.py::async_free_activity` 的自主"自由活动"行为。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `FREE_ACTIVITY_ENABLED` | ❌ | `true` | 自由活动总开关。`false` 时整个协程不启动。 |
| `FREE_ACTIVITY_INTERVAL` | ❌ | `5400` | 自由活动触发间隔（秒），默认 1.5 小时；实际还会叠加 ±900s 抖动 / 自主心跳动态间隔。 |
| `FREE_ACTIVITY_TOOL_LOOP` | ❌ | `false` | 🆕 **工具调用循环开关（灰度）**。关：所有活动只走"单次 LLM 输出 `{activity, log}`"的轻量版（行为与改造前完全一致）。开：有工具的活动（如"记点小账"→`wallet_*`、"逛虚拟小屋"→`house_*`/`cat_*`）会真正调用 `home_system` 纯函数执行副作用，再基于真实工具结果生成 log。安全护栏：工具白名单 + 按 activity 动态裁剪 + JSON Schema 参数校验 + 固定身份注入（`wallet_id`/`user_id` 不让 LLM 控制）+ 单轮上限 + 错误隔离。详见 [tool_loop.py](tool_loop.py)。 |
| `FREE_ACTIVITY_TOOL_MAX_CALLS` | ❌ | `5` | 🆕 单轮自由活动最多调用多少个工具（防 LLM 刷工具）。超出部分截断。 |

> 💡 **开启建议**：先把 `FREE_ACTIVITY_TOOL_LOOP=true`，观察日志里 `🎈 [自由活动·工具循环] 工具 ...` 的执行结果一段时间，确认工具调用合理（没乱扣钱、没误删物品）再正式常开。默认关时零行为变化。

---

## 13. 其他可选

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `MUTE_KEYWORDS` | ❌ | 空 | 触发静音的关键词，逗号分隔 |
| `MUTE_DURATION` | ❌ | `300` | 静音持续秒数 |
| `OCR_ENABLED` | ❌ | `false` | 是否开启 QQ 图片 OCR |
| `OCR_MAX_IMAGES` | ❌ | `3` | 单次最多识别图片数 |
| `SAVE_THINKING` | ❌ | `false` | 存库时是否保留模型思考过程（`<think>…</think>`）。默认 `false`：写入 Supabase `memories`（及 Pinecone）前剥离思考块，只存正文，避免 thinking 占满字数导致正文被 2000 字截断。设 `true` 则保留旧行为（把 reasoning 包成 `<think>` 块拼在正文前）。仅影响存库内容，不影响实时回复。 |
| `REWRITE_THINKING_TAG` | ❌ | 空（自动） | 转发时是否把响应正文里的 `<thinking>…</thinking>` 标签改写成 `<think>…</think>`。留空=**自动**：仅当上游 `model` 名含 `claude` 时启用；设 `true/1/yes` 强制对所有模型开启，`false/0/no` 强制关闭。流式下用跨 chunk 状态机处理被切碎的标签，不会漏改。改写后的正文同时用于存库，可与 `SAVE_THINKING` 的 `<think>` 剥离逻辑对齐。 |
| `STABLE_PREFIX_TTL` | ❌ | `300` | 🆕 **Prompt Cache**：`user_prof`（用户画像）TTL 缓存秒数。窗口内不重新查 DB，保证 stable_system 前缀字节不变，让上游 prompt cache 能命中。设 `0` 关闭缓存（每轮都查 DB，缓存命中率会下降）。 |
| `CORE_SUMMARIES_TTL` | ❌ | 同 `STABLE_PREFIX_TTL` | 🆕 **Prompt Cache**：`core_summaries`（阶段总结）TTL 缓存秒数。总结只在日记/总结生成时变，可单独设比画像更长（如 `600`）进一步提升命中率。设 `0` 关闭。 |
| `INJECT_DB_HISTORY` | ❌ | `auto` | 🆕 **Prompt Cache**：DB 历史注入模式。`auto`=客户端已带历史（非 system 消息 >1）时跳过 DB 历史注入，避免每轮 DB 窗口滚动破坏缓存前缀；`always`=总是注入（旧行为）；`never`=从不注入。 |
| `CLAUDE_CACHE_CONTROL` | ❌ | `auto` | 🆕 **Prompt Cache**：是否给 Claude（中转站）的 system 消息加 `cache_control:{type:ephemeral}` 标记。`auto`=仅 model 含 `claude` 时启用；`true`/`1`/`yes`=强制对所有模型启用；`false`/`0`/`no`=关闭。⚠️ 依赖中转站透传该字段，部分中转站会剥离导致无效，需实测。 |
| `SILICON_API_KEY` | ❌ | 空 | STT 语音识别 Key（硅基流动） |
| `SILICON_STT_BASE_URL` | ❌ | 空 | STT 服务地址 |
| `SILICON_STT_MODEL` | ❌ | 空 | STT 模型名 |
| `MINIMAX_API_KEY` | ❌ | 空 | Minimax TTS 文字转语音 Key |
| `ZEABUR_API_KEY` | ❌ | 空 | Zeabur 平台 API Token（API 触发重启） |
| `NAPCAT_PROJECT_ID` | ❌ | 空 | Zeabur 项目 ID |
| `NAPCAT_SERVICE_ID` | ❌ | 空 | Zeabur 服务 ID |

---

## 14. 欲望驱动系统 (情感 / 欲望引擎) 🆕

把「16 维情感 → 8 维欲望驱动 → 想做的事」串起来的自主行为系统。所有开关**默认关**（灰度原则：先只算只存、观测数据，确认没问题再逐个打开覆盖行为）。分类模型见 [3.3b DeepSeek](#33b-deepseek-消息情感分类专用)。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `DESIRE_DRIVEN` | ❌ | `false` | **总闸**。关：只算 + 只存快照供观测，不覆盖行为。开：把「此刻最想做的事」作为倾向注入自由活动 prompt |
| `DESIRE_COUPLING` | ❌ | `false` | v2① 维度耦合网。开：让驱动维度间联动（如压力大→更想念、累→不想探索），带全局阻尼防自激 |
| `HEARTBEAT_AUTONOMY` | ❌ | `false` | v2⑤ 自主心跳。开：心跳间隔按张力/疲劳/时段动态变化（张力高醒得勤、累了拉长、深夜勿扰）。关：固定 15 分钟 |
| `DESIRE_BASELINE_DRIFT` | ❌ | `false` | v2④ 想念基线漂移。开：久没互动时 attachment 地板缓慢抬高（硬封顶 0.45，主人一互动就拉回）。🛑 安全阀：想念可涨但永不压人 |
| `SLEEP_FROM_DEVICE` | ❌ | `false` | v2⑥ 设备睡眠同步。开：心跳每拍从 `device_data` 最新一条读真实睡眠（`sleepStartMs`/`sleepWakeupMs`），检测到新的一觉时注入 `sleep_start`+`sleep_end` 事件，让疲惫按真实睡眠时长回血/起床点重算（去重 + 时长 1~16h 校验）。关：疲惫走「清醒时长 + 昼夜节律」估算 |
| `EMOTION_TZ_OFFSET` | ❌ | `8` | 情感引擎昼夜节律用的时区偏移（UTC+N）。默认东八区 |

> 💡 开启顺序建议：先只开 `DESIRE_DRIVEN` 观察一段时间的意图日志（`💗 [欲望驱动]` / `💓 [自主心跳]` / `😴 [设备睡眠]`），确认行为合理后再逐个打开 `DESIRE_COUPLING` / `HEARTBEAT_AUTONOMY` / `DESIRE_BASELINE_DRIFT` / `SLEEP_FROM_DEVICE`。
> 状态全部持久化在 Supabase `user_facts` 表（`desire_*` 系列 key），重启不丢。

---

## 15. 小钱包 (Virtual Wallet) 🆕

> 🆕 Phase 2 新增。所有写操作通过 PostgreSQL RPC 原子完成，Python 层只做参数校验和调用封装。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `WALLET_WEEK_CAP` | ❌ | `80` | 每周入账上限（北京时间周一 00:00 重置） |
| `WALLET_OVERTIME_RATE` | ❌ | `0.5` | 超出周上限部分折算到加班银行的比率（0.5 = 超出 100 只存 50） |
| `WALLET_BIRTHDAY_WEEK` | ❌ | `true` | 生日周（4月5日 / 11月15日所在周）是否取消上限 |
| `WALLET_OVERTIME_WITHDRAW_MAX` | ❌ | `20` | 单次从加班银行取出的上限 |
| `WALLET_ALLOWANCE_WEEKLY` | ✅ | `25` | 每周固定零花钱的默认金额。已由 `home_system.py` 的 `WALLET_ALLOWANCE_WEEKLY` 读取，作为 `wallet_allowance()` 函数的默认金额。当前 `wallet_allowance()` 无调用方（备用扩展），实际发放由 console.html 面板或 AI 调用 `wallet_earn(bypass_cap=True)` 触发。 |

**说明**：
- `wallet_earn` 新增可选参数 `bypass_cap`（默认 false）；Finn 发零花钱/打赏时传 true，自行接活赚的钱传 false（正常计入周上限与加班银行）。
- `wallet_check` / `wallet_earn` / `wallet_spend` / `wallet_exchange` / `wallet_overtime_withdraw` / `wallet_log` 六个 MCP 工具在 `server.py` 注册，调用 `home_system.py` 中对应的 DB IO 函数。
- `wallet_exchange` 硬编码兑换率：`tea=50` / `gift=100`（单位与 `currency` 一致，默认 CNY）。
- `source_key` 幂等：重复提交相同 `source_key` 时，`rpc_wallet_earn` 会返回 `DUPLICATE_SOURCE` 错误，防止重复入账。

---

## 最小可运行配置示例

只配置以下 3 项，网关即可正常启动并提供基础 MCP 工具：

```env
# 必填：基础 + LLM（主对话模型 CHAT_*）
PORT=10000
API_SECRET=请改成你的随机密钥
CHAT_API_KEY=sk-xxxxxxxx
CHAT_MODEL_NAME=abab6.5s-chat
# 注：OPENAI_* 已废弃，所有对话/总结统一用 CHAT_*，无需配置

# 可选但推荐：数据库 + 推送
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=eyJhbGci...
TG_BOT_TOKEN=123456:ABC-DEF...
TG_CHAT_ID=123456789
AI_PERSONA=你是一个通用智能助手。
```

> 💡 其余所有变量均为可选，按需启用对应功能即可。未配置的功能会优雅降级而非报错。

---

## 变量生效与热更新

- **启动时读取**：所有变量在网关启动时读取并缓存在内存中。
- **热更新**：通过 `POST /api/config` 接口可热更新部分变量（需 `API_SECRET` 鉴权），无需重启。
- **重启生效**：修改变量后，调用 `POST /api/restart` 或在云平台重新部署即可完整生效。

> 📚 部署细节请参考 [DEPLOY_ZEABUR.md](DEPLOY_ZEABUR.md)，项目总览请参考 [README.md](README.md)。

---

## 天气工具（wttr.in，无需 API Key）

| 变量 | 默认 | 说明 |
|---|---|---|
| WEATHER_TOOLS_ENABLED | true | 天气功能总开关 |
| WEATHER_DEFAULT_CITY | Beijing（建议设韶关） | GPS 缺失时回退城市 |
| WEATHER_TIMEOUT_SEC | 12 | wttr.in 请求超时秒 |
| WEATHER_KEYWORD_INJECT | true | 聊天命中天气关键词时主动注入（保流式） |
| WEATHER_TOOL_LOOP | false | 网页聊天天气 tool loop 开关（开则模型可 function-call 天气，轮次内非真流式） |
| WEATHER_TOOL_MAX_ROUNDS | 3 | tool loop 最大轮次 |
| FREE_ACTIVITY_TOOL_LOOP | false | 后台活动工具循环总开关（不影响查天气专用路径） |

定位策略：默认取 device_data 表最新 lat/lon → wttr.in/{lat},{lon}；无 GPS 回退 WEATHER_DEFAULT_CITY；city 入参可查任意城市。虚拟小屋"查天气"活动落 house_do 时 weather 字段取自用户 GPS 天气，与用户定位一致。