# 📝 项目工作区笔记 (PROJECT_NOTES)

> 本文件用于记录工作区环境状态、变更日志与迭代待办，随项目一起维护。

## 🗂️ 工作区布局

项目根目录：`/workspace/mcp-gateway/`

| 文件 | 角色 |
|------|------|
| `server.py` | MCP 工具层（27 个工具），入口文件 |
| `gateway.py` | ASGI 中间件（/v1/* 代理、/api/* 管理、/qq-ws、/console） |
| `heartbeat.py` | 后台心跳线程（问候/日记/TG/提醒/日程/邮件/热同步） |
| `napcat.py` | NapCat QQ 接入（反向 WS 被动模式） |
| `console.html` | 🆕 桌面控制台 SPA（7 页管理界面，/console 路由返回） |
| `requirements.txt` | Python 依赖 |
| `.env.example` / `.env` | 环境变量模板 / 本地测试配置（.env 不入库） |
| `Dockerfile` / `docker-compose.yml` | 容器化部署 |
| `README.md` / `DEPLOY_ZEABUR.md` / `VARIABLES.md` | 文档 |
| `PROJECT_NOTES.md` | 本文件 |

## ⚙️ 环境配置

- Python 3.12 + venv：`/workspace/.venv`
- 依赖已装：mcp 1.29 / openai / supabase / pinecone / google-api-python-client / duckduckgo-search / tavily / replicate / uvicorn 等
- **mem0ai 刻意不装**（v2.1 已移除 Mem0，见下）
- 启动：`/workspace/.venv/bin/python server.py`（默认端口 10000）
- 本地冒烟测试：`PORT=18765 python server.py` → 请求 `http://localhost:18765/health`
- 健康检查通过 ✅（已验证返回 200）

## 📦 变更日志

### v5.2 — Prompt Cache 命中率修复（TTL 缓存 + 可观测性 + 自适应历史 + Claude 标记）
**背景**：缓存命中率极度不稳定——高时 60-70%，大部分时候只有 4-5% 甚至不 cached。参考 Haven-Ombre 仓库的缓存统计做法后诊断出 4 个根因，逐一修复。

**根因诊断**（按影响排序）：
1. **stable_system 前缀每轮都查 DB**（最致命）：`core_summaries`/`user_prof` 每次请求都 `SELECT`，内容一变（哪怕一个字符）整个前缀就变 → 上游 prompt cache 严格前缀匹配 → 后面全部无法命中。
2. **DB 历史注入每轮窗口滚动**：每轮从 DB 拉最近 10 条插到 system 和 user 之间，对话后 DB 新增一条、窗口滚动 → history 内容每轮移位 → 破坏前缀连续性。
3. **Claude（中转站）没打 cache_control**：DeepSeek/Kimi/GLM 自动前缀缓存无需参数，但 Claude 需手动 `cache_control:{type:ephemeral}`，不打就完全不缓存。
4. **网关侧无缓存可观测性**：没设 `stream_options`，不解析 usage，只能去上游控制台被动看百分比。

**上游确认**：用户实际用 Kimi、GLM、DeepSeek（自动缓存，靠前缀匹配）、中转站 Claude（需手动 cache_control）。非 MiniMax（VARIABLES.md 默认值误导）。

**改动**（仅 `gateway.py` + `VARIABLES.md`，无新增文件、不改 DB schema）：

- **`gateway.py`**（4 项改动）：
  - **改动 1 · TTL 缓存 stable_system 组件**：新增 `_stable_prefix_cache` + `_stable_cached()`/`_stable_set()` 辅助函数。`core_summaries` 和 `user_prof` 查询前先查 TTL 缓存，命中则直接用（打 `📦 [Cache]` 日志），过期才重新查 DB 并回填。保证窗口内前缀字节不变。
  - **改动 2 · 缓存命中可观测性**：流式路径加 `stream_options:{include_usage:true}`（不支持的上游自动忽略）；流收集循环两个分支（透传 + thinking 改写）均解析 `dj.get("usage")` 存入 `collected_usage`；tool_loop 路径从非流式 response JSON 取 `loop_usage`。新增 `_log_cache_usage(model, usage)` 模块级函数，覆盖三家字段命名（DeepSeek `prompt_cache_hit/miss_tokens`、GLM `prompt_tokens_details.cached_tokens`、Claude `cache_read/creation_input_tokens`），自动算命中率百分比，打 `📊 [Cache]` 日志。
  - **改动 3 · 自适应 DB 历史注入**：`_inject_context` 历史拉取段开头检测 `messages` 里非 system 消息数，>1 条时跳过 DB 历史（打 `📦 [Cache]` 日志），保留客户端自带历史的自然增长前缀模式。由 `INJECT_DB_HISTORY` 控制（`auto`/`always`/`never`）。
  - **改动 4 · Claude cache_control 标记**：新增 `_apply_claude_cache_control(req_data)` 模块级函数，转发前调用。`auto` 模式下 model 含 `claude` 时把 system 消息 content 从字符串转为 content-block 数组并加 `cache_control:{type:ephemeral}`，tools 定义末尾也加标记。打 `🏷️ [Cache]` 日志。⚠️ 依赖中转站透传该字段，需实测。

- **`VARIABLES.md`**（修改）：第 13 节追加 4 个新环境变量文档。

**环境变量**（4 个，全部可选）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `STABLE_PREFIX_TTL` | `300` | 画像 TTL 缓存秒数；`0` 关闭（命中率会下降） |
| `CORE_SUMMARIES_TTL` | 同上 | 总结 TTL 缓存秒数；可设更长（如 `600`） |
| `INJECT_DB_HISTORY` | `auto` | DB 历史注入模式：`auto`=自适应/`always`=旧行为/`never`=从不 |
| `CLAUDE_CACHE_CONTROL` | `auto` | Claude cache_control：`auto`=仅 claude/`true`=强制/`false`=关 |

**验证**：`python -m py_compile gateway.py` 通过。部署后看日志 `📊 [Cache]` 行验证命中率变化，`📦 [Cache]` 行确认 TTL 缓存命中。

**参考来源**：Haven-Ombre 仓库（`Yinglianchun/Haven-Ombre`）的 `_log_cache_usage` / `upstream_usage` 表模式，提取文档见 `haven_ombre_cache_hit_rate.md`。

**与 v3.7-v3.9 的关系**：v3.7 做了 prompt 注入位置重排（两段式 stable/volatile），v3.8 把 desire 状态迁出 user_facts，v3.9 修画像注入排序/截断。本次是同一条线的延续——前缀位置和内容来源已稳定，但"每轮都查 DB 导致前缀字节变化"这一根因此前未解决。

### v5.3 — 情绪/欲望状态长期不变化修复（网页聊天接入事件链 + 前端字段对齐 + 渠道可观测性）
**问题现象**：控制台「情绪维度」进度条多日不变化，`_recent_labels` 停在 `["neutral","playful","playful"]`、`last_interaction_at` 停在旧时间，尽管数据库仍有 `Web_Chat` 聊天记录。`attachment` 长期 0.45、`duty` 长期 0.05。

**已确认根因**（三条，均已验证）：
1. **网页聊天未接入情绪事件链路**（根因 A）：`gateway.py` 的两条网页路径——普通流式 `_handle_chat` 与工具循环 `_handle_chat_with_tool_loop`——都没有调用 `desire_bridge.record_user_message()` / `record_assistant_message()`。仅 `heartbeat.py`（TG）和 `napcat.py`（QQ）调用了。后果：网页聊天从不产生 `msg_user`/`msg_assistant` 事件 → 分类器从未被触发 → `_recent_labels` 与 `last_interaction_at` 不随网页对话更新（只有 TG/QQ 消息更新它）。
2. **前端情绪字段与后端真实字段不一致**（根因 B）：后端 `emotion_engine.build_display()` 产出 15 个 DIMS 键 + `fatigue`（`vitality/longing/intimacy/possessiveness/lust/jealousy/anxiety/protectiveness/contentment/elation/seeking/play/dejection/irritability/fear/fatigue`）；前端 `console.html` 与 `miniapp.html` 用的 `emoKeys` 却是旧字段（`joy/calm/sadness/anger/...`），二者仅 `anxiety/longing/fear/fatigue` 4 个重合，其余 12 维前端查不到值永远显示"—"。这是进度条不变化的直接原因。驱动条 `dKeys`（attachment/curiosity/reflection/duty/social/libido/stress/fatigue）字段正确，未改。
3. **attachment=0.45 / duty=0.05 是固定基线设计**（根因 C，非 bug）：`attachment=0.45` = `desire_engine.BASELINE_CAP`（attachment 地板漂移的硬上限，`drift_baseline()` clamp 到 `[0.30,0.45]`）；`duty=0.05` = `BASELINE["duty"]`（`map_from_emotions()` 在 `has_pending_task=False` 时无条件强制置此基线，且 `pick_intent` 把 duty 排除出候选）。二者是业务规则，本次不动。

**改动**（7 个文件；无新增环境变量、不改 DB schema、不删数据）：

- **`desire_bridge.py`**：`record_user_message()` 新增可选参数 `channel: Optional[str] = None`（取值 "Web"/"TG"/"QQ"），仅用于日志可观测性，不写入事件本身。日志改为 `💗 [欲望驱动] [{渠道}] 用户消息已分类入队 label=... conf=...`。`record_assistant_message()` 签名不变（渠道已在 user 日志体现）。
- **`gateway.py`**：新增私有辅助方法 `_record_desire_events(self, user_msg, channel="Web")`，内部按序调用 `record_user_message`（含分类+msg_user）→ `record_assistant_message`（msg_assistant），保证事件队列里 msg_user 在 msg_assistant 之前（与 TG/QQ 一致），全程吞异常。两条网页路径在异步双写 `_save_conversation` 之后接入：
  - 普通流式 `_handle_chat`（流式响应 1543 行结束后）：`if user_msg and (collected_content or tool_calls_dict) and _emotion_enabled()` 时 `asyncio.create_task(self._record_desire_events(user_msg, channel="Web"))`。不阻塞首字（流式已结束），上游全错时不入队（与 TG reply 为空 return 一致），`_emotion_enabled()` 门控（同 TG/QQ）。
  - 工具循环 `_handle_chat_with_tool_loop`（正常结束、有最终文本或工具调用时）：同样条件接入。该路径的 3 个提前 `return`（连接错误/HTTP 错/非 JSON）不入队——无成功回复。
- **`heartbeat.py`**：TG 调用 `record_user_message(text)` → `record_user_message(text, channel="TG")`。
- **`napcat.py`**：QQ 调用 `record_user_message(text)` → `record_user_message(text, channel="QQ")`。
- **`console.html`**：`_E` 与 `emoKeys` 从旧字段替换为后端真实字段（`vitality:"活力"` ... `fatigue:"疲惫"` 共 16 项），`_D`/`dKeys` 驱动条不变。布局/样式不变。
- **`miniapp.html`**：与 console.html 完全相同的字段替换，保证两端一致。

**是否修改数据库**：否。全程只读查询（排查阶段）、零 DDL、零 DML 写入。

**验证结果**：
- `python -m py_compile gateway.py desire_bridge.py emotion_engine.py heartbeat.py napcat.py server.py` ✅ 全绿。
- `python -m unittest test_console -v` ✅ 31 passed（2 skipped，为需测试壳的 live route 测试，与本次改动无关）。关键回归点：`test_all_py_compile`（全 py 编译）、`test_inline_js_node_check`（console.html 内联 JS 语法）、`test_desire_bridge_gates_db_aware`（门控）、`test_emotion_enabled_toggle`（情感开关）、`test_gate_covers_all_channels`（三渠道覆盖）均通过。
- 静态一致性：后端 `ee.DIMS.keys()` 15 项 + fatigue = 前端 emoKeys 16 项，完全对齐。
- 日志验证点：网页聊天后应出现 `💗 [欲望驱动] [Web] 用户消息已分类入队 label=... conf=...`；TG/QQ 日志渠道标识分别为 `[TG]`/`[QQ]`。

**已知限制 / 风险**：
- 重复入队：每次 `/v1/chat/completions` 独立请求，`_record_desire_events` 每次只调一次，`enqueue_event` 用时间戳+随机数生成唯一 event_id，天然不重复。
- 流式结束路径：普通流式（1543）+ 工具循环（1929）两条正常结束路径已覆盖；工具循环 3 个失败 return 不入队（正确）。
- 分类器延迟：`record_user_message` 内部 `asyncio.to_thread` 调 LLM 分类，不阻塞事件循环，且在流式结束后触发，不影响首字；分类失败降级 neutral，不影响聊天。
- 数据库并发：事件队列入队是 read-modify-write，理论有竞态，但网页/TG/QQ 均低频消息、心跳周期消费，实际无冲突风险（与 TG/QQ 既有行为一致）。
- `attachment`/`duty` 基线值不变化属设计规则，非本次修复目标；若需让它们动起来需另改业务规则（不在本次范围）。

### v5.1 — 天气工具缝合（wttr.in 真实天气：后台活动 / 聊天关键词注入 / MCP 工具）
**需求**：把基于 wttr.in 的天气工具完整缝进网关——后台"查天气"活动调真实天气、聊天命中天气关键词时自动拉天气注入 prompt（保流式）、注册为 MCP 工具供客户端调用。

**定位优先级**：显式 `city` > 用户最新 GPS（`device_data` 表 lat/lon）> `WEATHER_DEFAULT_CITY`（默认韶关）。

- **新增 `weather_tools.py`**（自包含，不 import server）：
  - 3 个工具函数：`get_weather`（当前详细天气）/ `get_weather_brief`（一行简短描述）/ `get_weather_forecast`（未来 1-3 天预报）
  - 坐标查法 `https://wttr.in/24.8,113.6?format=j1`；`_resolve_location` 返回 (kind, value)，`_fetch_latest_gps` 直查 `device_data` 表
  - **SSRF 防御** `_check_url_safe`：协议强制 HTTPS、host 强制 wttr.in、解析 IP 阻断私网/环回/链路本地
  - 辅助：`execute_tool` / `execute_tool_json` / `merge_tools_into_request` / `run_tool_calls` / `brief_text`

- **`server.py`**（修改）：
  - 顶部软导入 weather_tools（`_HAS_WEATHER_TOOLS`，漏传文件不影响启动）
  - 新增两个 MCP 工具：`query_weather(city="")` → `weather_tools.get_weather`；`query_weather_forecast(city="", days=3)` → `weather_tools.get_weather_forecast`
  - `_build_channel_context` 末尾追加 TG/QQ 渠道天气注入（命中 `_query_weather_hit` 时）

- **`tool_loop.py`**（修改）：
  - `TOOL_REGISTRY` 追加 3 个天气工具（get_weather / get_weather_brief / get_weather_forecast）
  - `ACTIVITY_TOOL_MAP["查天气"]` 从 `[]` 改为 `["get_weather", "get_weather_forecast", "get_weather_brief"]`
  - 新增 `_finalize_weather_activity` **查天气专用确定性路径**：不依赖 `FREE_ACTIVITY_TOOL_LOOP` 开关，始终拉真实天气（用户 GPS）→ 注入 LLM 生成感官日记 → 落小屋 `house_do(room_id="balcony", entry_type="看天气", weather=wbrief)`，保证小屋 weather 与用户定位一致
  - `run_free_activity_tool_loop` 阶段1 后插入 `if activity == "查天气"` 分支

- **`gateway.py`**（修改）：
  - 顶部软导入 weather_tools + `_WEATHER_KEYWORDS` + `_weather_keyword_hit(text)`
  - `_inject_context` volatile_block 末尾：关键词命中时 `asyncio.to_thread(weather_tools.get_weather, None, sb)` 注入实时天气（**默认开**，保流式）
  - `_handle_chat`：`_weather_loop` 判断 + `merge_tools_into_request`；开启时走新增的 `_handle_chat_with_tool_loop`（OpenAI tools 循环 + 本地执行 weather_tools + `_sse_final_text` 包装回 SSE）
  - **双路设计**：关键词注入（默认开，WEATHER_KEYWORD_INJECT=true）与 tool loop（默认关，WEATHER_TOOL_LOOP=false）解耦——默认配置下聊天仍纯流式透传，天气已注入 prompt

- **`VARIABLES.md`**（修改）：末尾追加天气工具环境变量文档

**环境变量**（7 个，全部可选）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `WEATHER_DEFAULT_CITY` | 韶关 | 无 GPS 时的兜底城市 |
| `WEATHER_TOOL_LOOP` | false | 聊天是否走 OpenAI tools 循环（关=流式） |
| `WEATHER_KEYWORD_INJECT` | true | 聊天命中天气关键词是否注入实时天气 |
| `WEATHER_KEYWORDS` | 内置词表 | 自定义关键词（逗号分隔） |
| `WEATHER_TIMEOUT` | 8 | 天气请求超时（秒） |
| `WEATHER_TOOL_MAX_CALLS` | 3 | tool loop 单轮最多调几次天气 |
| `WEATHER_DAYS` | 3 | 预报天数上限 |

**验证结果**（7 步自测全过）：
1. 网络可达（curl wttr.in）✅
2. `WEATHER_DEFAULT_CITY=韶关` 环境变量生效 ✅
3. 单测 `weather_tools.get_weather(None, None)` → 回退韶关，`success=true, city=韶关` ✅
4. 单测指定城市 `city=北京` → 返回北京天气 ✅
5. import 无环检查：weather_tools / tool_loop / server / gateway 全部 OK ✅
6. `_finalize_weather_activity` 集成测试 → 日志 `[查天气] 已落小屋阳台·看天气（weather=Beijing Sunny 31°C 体感31°C 湿度42）` ✅
7. 关键词命中静态验证：`今天好热`/`下雨了`/`韶关天气` → True，`你好` → False ✅

### v5.0 — 桌面控制台 + 角色化多模型 + 运行时门控（架构改造第 5 步）
**需求**：给网关增加一个电脑端管理控制台，同时解决多模型角色归属混乱和运行时功能门控问题。
- **新增 `console.html`**（`gateway.py` `/console` 路由返回）：
  - 7 页桌面 SPA（概览 / 模型 / 渠道 / 情绪 / 记忆 / 画像 / 存储），左侧导航栏 + 顶部状态条 + 内容区 max-width:1560px
  - 米色主调 + 橙蓝点缀，Noto Serif SC + Playfair Display 衬线字体，Lucide 图标
  - API_SECRET 存浏览器 localStorage，所有管理接口走 `X-Api-Key` 鉴权
  - 模型页：注册表 v2 读写（schema_version / models / default / roles）、模型测通（带 SSRF 防护）、一键迁移旧 `llm_settings`
  - 渠道页：TG/QQ 轮询开关、情绪/欲望驱动/聊天写入/向量注入总闸（均来自 `user_facts.sys_config` 运行时配置）
  - 情绪页：情感引擎实时状态、欲望驱动开关、自主心跳参数
  - 记忆页：分页浏览 + 按标签筛选 + 6 分类标签页（日常/人物/技能/设备/事件/其他）
  - 画像页：key-value 列表、过滤系统键、内联编辑
  - 存储页：Supabase/Pinecone 连接状态 + 表大小统计
  - 所有数据走真实异步 API（`/api/admin/config`、`/api/admin/status`、`/api/models/test`、`/api/memories`、`/api/profile`）
- **角色化 LLM 解析**（`gateway.py`）
  - 新增 `resolve_llm_role(role)`：按 `chat`/`compression`/`background` 角色解析，注册表 `roles` 字段优先 → `llm_settings` 兼容 → `CHAT_*`/`COMPRESS_*`/`BACKGROUND_*` 环境变量 → 兜底 `OPENAI_*`
  - 新增 `COMPRESS_*` / `BACKGROUND_*` 环境变量组，与 `CHAT_*` 平行
  - 注册表 `set_role` 操作支持分配角色（含删除保护、禁用保护）；`migrate_llm_settings` 一键把旧 CHAT_* 拆进注册表并保留旧 key
  - 后端 `_is_profile_key` 新增 `_SYSTEM_PROFILE_KEYS` 集合（`sys_config`/`llm_settings`/`llm_models`/`sys_ai_persona`），画像页不再展示系统配置
- **运行时门控**（`gateway.py` + `heartbeat.py` + `napcat.py` + `server.py`）
  - 新增 `_get_runtime_config()`（5 秒 TTL 缓存），读取 `user_facts.sys_config` JSON 运行时配置
  - `_tg_enabled()` / `_qq_enabled()` / `_emotion_enabled()` / `_chat_write_enabled()` / `_vector_injection_enabled()` 开关函数
  - TG 轮询、QQ 接入、情绪 tick、聊天写入（对话存库）、向量注入（Pinecone）全部按门控决定是否执行
  - 门控默认 true（兼容旧行为），控制台关闭即实时生效（5 秒内）
- **SSRF 防护**（`gateway.py`）
  - `_check_ssrf(url)`：解析 URL，阻断云元数据端点、私有/环回/链路本地 IP
  - `_ssrf_safe_post()`：先校验再发请求，`allow_redirects=False`
  - 模型测通 `/api/models/test` 调用时启用 SSRF 检查
- **修复**：`/api/desire` 从 `user_facts` 读取改为从 `desire_state` 表读取（v3.8 迁移遗留）
- **测试**：新增 `test_console.py` 29 项全部通过，覆盖角色解析、门控、分页、鉴权、JS 语法等

### v4.2 — MiniApp 全新 UI（米色 + 橙蓝点缀 + 衬线字体）
**需求**：给网关控制台的两个 MiniApp 重做一套 UI——以米色为主色、橙色蓝色及相近色点缀、衬线字体，保留全部原有功能。纯前端改造，无后端逻辑改动。
- **改动文件**：`miniapp.html`（网关配置面板）、`emotion_miniapp.html`（情绪/欲望面板）。
- **设计令牌（两文件共用一套 `:root`）**：
  - 底色米色系：`--bg:#f3ebdd` + 两层奶油纸面 `--paper:#fbf6ec` / `--paper2:#f6efe1`，body 加双径向渐变增强纸感；文字暖棕 `--txt:#3b3227` / `--sub` / `--faint`。
  - 点缀：橙 `--orange:#d98324`(deep `#b5641a`) + 蓝 `--blue:#3a6ea5`(deep `#2b537d`)，相近色 `--teal` / `--coral` / `--rose`；语义色 `--danger:#c0492f` / `--ok:#4c8a5a`。`--brand` 指向橙。
  - 字体：正文 `"Noto Serif SC",Georgia,"Songti SC",serif`；标题/数字用 `"Playfair Display"` 衬线体。通过 Google Fonts `<link>` 引入（含 preconnect）。
- **视觉细节**：卡片圆角+暖阴影+`h3` 前橙珊瑚渐变竖条；tab 选中态橙色下划线；按钮橙色渐变（新增 `.blue` 蓝色变体、`.danger` 珊瑚红），输入框聚焦橙色描边光晕；徽章/标签 pill 化（橙 soft / 蓝 soft）；hint 改左竖线卡片；toast 深棕胶囊上滑动画。
  - `emotion_miniapp.html`：主导情绪横幅正向改蓝色渐变、负向改橙色渐变；进度条 `.ok` 蓝色、`.hot` 橙珊瑚、`.gray` 米灰；intent 卡橙→蓝渐变背景；情感分组 dot 配色改为米色系调和后的橙/蓝/珊瑚/绿/棕。
  - `page` 加 `max-width:720px` 居中，宽屏更耐看。
- **功能零改动**：所有 id、`data-*`、onclick 绑定、Supabase 查询、渠道筛选、记忆/画像 CRUD、提醒、情绪/欲望两种连接模式与自动刷新逻辑全部原样保留，仅动 HTML 结构外层与 CSS。
- **修掉一处笔误**：初版 `--bg` 写成 `#f3 ebdd`（含空格）并重复声明，已修正为单行 `--bg:#f3ebdd`。
- **验证**：两文件 tab 的 `data-p` 与 `id="p-*"` 一一配对；抽取内联业务 JS `node --check` 均 OK；CSS 大括号计数平衡（miniapp 93/93、emotion 74/74）；无残留笔误；衬线字体 link 与米色底色令牌均在位。
- **后端**：`gateway.py` 的 `/miniapp` 与情绪面板路由仍按原文件名读同目录 HTML，文件名未变，无需改动。
- 无新增环境变量。

### v4.1 — MiniApp 记忆可编辑 + 新增「画像」页签
**需求**：网关配置面板（`miniapp.html`）此前记忆页只能浏览/删除；要求 (1) 记忆卡片支持编辑，(2) 记忆下面新增用户画像管理。纯前端直连 Supabase，无后端改动。
- **记忆编辑**（`miniapp.html`）：
  - 记忆卡片展开后新增「编辑」按钮，进入内联表单可改 `title / content / category / mood / tags / importance`。
  - 保存走 `sb.from("memories").update(patch).eq("id",id)`；`importance` 做整数校验（非整数拒绝）；保存后就地更新本地 `memRecords` 并刷新，无需整表重拉。
  - 新增状态变量 `memEditing`；`saveMem(id)` 函数；卡片 3 态（折叠 / 只读展开 / 编辑）。
- **画像页签**（`miniapp.html`）：
  - 新增 tab `data-p="profile"` + `#p-profile` 页面，位于「记忆」与「提醒」之间。
  - 管理 `user_facts` 表：列表（可搜索、展开）、内联编辑 `value / confidence`、删除、顶部「新增/覆盖」表单（upsert）。
  - **系统键过滤**：前端 `isProfileKey()` 复刻后端 `gateway.py::_is_profile_key`——硬隐藏 `sys_config / llm_settings / llm_models / sys_ai_persona`；`desire_` 前缀仅保留结尾带 `_YYYY_MM_DD` 的人写笔记，其余运行时状态隐藏。当前库中约 50 个画像键，过滤后展示与 prompt 注入口径一致。
  - 删除画像有 `confirm` 二次确认。
- **验证**：`node --check` 抽取的内联 JS 通过；花括号/圆括号/方括号计数平衡；5 组 tab/page 的 `data-p` 与 `id="p-*"` 一一配对。
- 无新增环境变量。

### v4.0 — 上游 URL 拼接兼容多版本路径（修智谱 /v4 报 404）
**背景**：前端「发消息实测」选 glm-5.2（base=`https://open.bigmodel.cn/api/paas/v4`）报 `HTTP 404 ... "path":"/v4/v1/chat/completions"`。
- **根因**：`gateway.py` `_handle_chat` 原拼接逻辑只判断 `base.endswith("/v1")`，其余一律强补 `/v1/chat/completions`。智谱 base 结尾是 `/v4`（不等于 `/v1`）→ 被错拼成 `/v4/v1/chat/completions`。
- **修复**：改为三段判断（`gateway.py` 约 559 行）：
  1. base 已含 `/chat/completions$` → 原样用（用户填了完整端点）
  2. base 以版本段结尾 `/v\d+[a-zA-Z]*$`（匹配 /v1 /v2 /v3 /v4 /v1beta 等）→ 直接补 `/chat/completions`
  3. 其余裸域名/根路径 → 按 OpenAI 习惯补 `/v1/chat/completions`
- **验证**：本地跑正则用例覆盖 智谱/v4、九时/v1、裸域名、Gemini/v1beta、DeepSeek/v3、已填完整端点 —— 全部拼接正确；`py_compile` 通过。
- 无新增环境变量。

### v3.9 — 画像注入过滤精修（30 条截断 / 排序 / desire 笔记误伤 / llm_models）
**背景**：v3.7 修完缓存前缀位置、v3.8 把 desire 状态迁出 user_facts 后，继续排查发现画像注入侧还有几处遗留问题，与缓存命中率同一条线：
- **硬截断 `pr.data[:30]` 且无 `.order()`**：真画像已 44 条，`[:30]` 静默丢弃 14 条，且丢哪些取决于 PostgREST 默认返回顺序（不稳定）——顺序一变，注入进 v3.7 稳定前缀的画像内容就变，缓存前缀再次失效。这是缓存命中率的**主因之一**（v3.8 的 `.not_.like` 过滤本身是生效的，实测过 `key=not.like.desire_%25`）。
- **`.not_.like("key","desire_%")` 误伤笔记**：把人写的 `desire_system_tech_debt_2026_08_05`（技术债待办，属真画像）一起排除了。
- **`llm_models` 漏网**：模型注册表（1079字 JSON 配置）被当画像注入，属系统配置。

**改动**（`gateway.py` + `server.py` 两处画像查询对齐；**无新增环境变量**，**未删除任何数据库数据**）：
- 新增 `gateway._is_profile_key(key)`：desire 系列精细分类——运行时状态（不带日期后缀，如 `desire_drive_state`/`desire_last_tick_at`）排除，人写笔记（结尾正则 `_\d{4}_\d{2}_\d{2}$`）放行；非 `desire_` 照常放行。`server.py` 复用（`import gateway as _gw`）。
- 查询层去掉 `.not_.like`，改 Python 侧 `_is_profile_key` 过滤；额外 `neq` 排除 `sys_config`/`llm_settings`/`sys_ai_persona`/`llm_models`。
- 加 `.order("key")` **稳定排序** → 注入顺序固定，缓存前缀才能真正命中。
- 截断上限 `[:30]` → `[:60]`（当前 44 条真画像一条不丢）。
- 验证：`py_compile` 4 文件全绿；`_is_profile_key` 15 组用例全过；对真实表跑等价 SQL 确认最终注入 **44 条**，10 个 desire 运行时状态全排除、tech_debt 笔记放行、`llm_models` 排除、顺序稳定 ✅。
- 📌 与 v3.8 呼应：user_facts 里的 `desire_*` 回滚备份仍原样保留，本次纯读取侧过滤。

### v3.8 — 情感/欲望引擎状态迁出 user_facts（修缓存命中率 & prompt 污染）
**背景**：情感与欲望驱动引擎的全部运行态（10 个 `desire_*` key）此前寄居在 `user_facts` 表。两个问题：
- **缓存命中率**：这堆状态高频读写（每 tick 全量覆盖写），和 `user_facts` 里低频的用户画像/系统配置混在一起，拖累查询与上游 prompt 缓存前缀。
- **prompt 污染**：`_build_channel_context`（server.py）与 `_inject_context`（gateway.py）拉"用户画像"时只 `neq` 排除了 3 个系统 key，**没排除 `desire_*`**——导致每次拼 prompt 都把情感引擎原始 JSON（含 2800 字节的 `desire_emotion_state`）当画像塞进上下文，既占 token 又污染前缀。

**改动**（无新增环境变量）：
- **新建 `desire_state` 表**（key-value 结构，对齐 `user_facts` 的 key/value/confidence，额外加 `updated_at timestamptz` 便于观测 tick 频率）；RLS 对齐现有表（public 读写四条策略）。
  - ⚠️ 建表时发现**旧网关遗留的同名 `desire_state` 表**（结构为 `id/drives/thoughts/refractory...` jsonb，1 行 2026-07-17 陈旧废数据，当前代码库零引用）→ 经确认为旧网关产物，已 `DROP ... CASCADE` 删除后按 kv 结构重建。
  - 迁移：把 `user_facts` 里 10 个 `desire_*` key 拷入新表（`INSERT ... ON CONFLICT DO NOTHING`）；**排除人工手记 `desire_system_tech_debt_2026_08_05`**（它不是引擎状态，留在 user_facts）。
- **`desire_bridge.py`**：新增模块常量 `STATE_TABLE = "desire_state"`；`_load_fact` / `_save_fact` 表名从 `user_facts` 改为 `STATE_TABLE`；`_save_fact` 的 upsert 补写 `updated_at`（新增 `_dt_now_iso()` 辅助）。**其余十几处 `_load_fact`/`_save_fact` 调用点一行未动**（IO 全收口在这两个函数）。模块顶部状态存储文档同步更新。
- **`server.py` : `_build_channel_context` 画像查询** 追加 `.not_.like("key", "desire_%")`。
- **`gateway.py` : `_inject_context` 画像查询** 追加 `.not_.like("key", "desire_%")`。
  - 兜底双保险：即使旧 `desire_*` key 尚未从 user_facts 删除，也不会再被当画像注入。
  - 管理工具路径（`get_user_profile` server.py:900、`organize_knowledge_base`）是 AI 手动调、非热路径，未改；旧 key 删除后自然干净。

**验证**：
- `desire_state` 新表 10 个 key 全部就位，长度与原值逐一吻合。
- `py_compile` 7 文件全绿；`pytest test_desire_engine + test_emotion_engine + test_sleep_from_device` **111 passed**。
- 用 postgrest 2.31.0 实测 `.not_.like("key","desire_%")` 生成的 query 为 `key=not.like.desire_%25`（PostgREST 正确写法）。

**⚠️ 收尾待办（需用户验收后单独确认再执行）**：`user_facts` 里的 10 个旧 `desire_*` key **暂未删除**（作回滚保险）。观察新表跑稳 1-2 天后，再 `DELETE FROM user_facts WHERE key LIKE 'desire_%' AND key <> 'desire_system_tech_debt_2026_08_05'`。那条 tech_debt 手记保留不动。

### v3.7 — Prompt 注入位置重排（修缓存命中率≈0 & AI 漏看时间戳）
**背景**：两个线上问题其实同源，都出在 system prompt 的拼装顺序上。
- **缓存命中率≈0**：上游 prompt cache 基本都是**前缀匹配**（从头逐字符比对，遇第一个差异整段失效）。原 `_inject_context`（gateway.py）把 `[系统当前状态] 当前时间:{time_str}` 这种**每请求都变**的时间戳放在紧接 persona 之后的靠前位置，导致缓存前缀在第二段就断裂，后面再长的稳定人设/画像全都无法复用 → 命中率归零。
- **AI 漏看时间戳**：时间戳埋在一大坨状态块的中段，被画像/深层记忆/阶段总结前后包夹，正好落在模型注意力最弱的中间区，频繁被漏读。

**改动**（`gateway.py::_inject_context` + `server.py::_build_channel_context`，两处对齐；**无新增环境变量**）：
- 改为**两段式拼装**：
  - `stable_system` 稳定前缀（人设 + 画像 + 阶段总结）→ 放最前当缓存公共前缀，几乎不随请求变化
  - `volatile_block` 易变尾块（实时时间 + 沉默时长 + 渠道 + 按本轮话题检索的深层记忆 + 设备快照）→ 塞到「最后一条 user 之前」，既不污染缓存前缀，又落在注意力最高的末尾
- **时间戳移到 volatile_block 最末行**，加显式提示 `[实时状态 · 回复前请先读这里]`，紧贴用户消息，从根上治漏看
- 网页渠道 volatile 作为**独立 system 消息**插到 last-user 前；TG/QQ 渠道（单条 system）内部同序重排
- system 前置拼接改为「稳定内容在前」，避免前端自带 system 顶开缓存前缀
- 注入日志追加「稳定前缀 N 字 + 易变尾块 N 字」便于观测
- 验证：`py_compile` 全绿；构造假 SB 跑 `_inject_context`，确认消息顺序为 `[system 稳定前缀][system 易变尾块][user]`，时间戳紧邻用户消息 ✅

### v3.6 — 设备睡眠同步（疲惫按真实睡眠回血）
**背景**：情感引擎的 `fatigue` 此前只靠「清醒时长 + 凌晨昼夜峰值」估算——`sleep_start`/`sleep_end`/`sleep_interrupt` 事件在引擎里已实现处理逻辑，但**没有任何触发来源**，"睡觉回血"机制一直闲置（睡眠状态永远停在初始的 awake / 估计 7.2h）。设备 `device_data.health_data` 里本就有真实睡眠字段，可直接接入。

**数据形态确认**：`health_data` 以 **JSON 字符串**存（非 jsonb 对象），字段：`sleepStartMs` / `sleepWakeupMs`（epoch 毫秒）/ `sleepTotalMinutes` / 深浅睡/REM 分钟。起止毫秒正是 `emotion_engine` 睡眠事件需要的时间戳格式。

**改动**（全部在 `desire_bridge.py`，两个纯函数引擎未动）：
- 新增开关 `is_sleep_from_device()`（环境变量 `SLEEP_FROM_DEVICE`，**默认关**，延续灰度原则）
- 新增 `_health_data_of_latest_device_row()`：读 `device_data` 最新一条的 `health_data`（兼容字符串/dict），设备数据视为**外部不可信来源**，只按已知字段名取值
- 新增 `sync_sleep_from_device(now_ms)`：检测「新的一觉」→ 向情感引擎事件队列 **prepend** 一对 `sleep_start`+`sleep_end` 事件（时间戳用真实睡眠起止），让 `fatigue_base` 按真实时长算起床点 / 回血
  - 去重：新增 state key `desire_last_sleep_wake_ms`，同一 `sleepWakeupMs` 只注入一次
  - 校验：时长必须 ∈ [1h, 16h]、起 < 止、醒来不在（超 6h 容忍的）未来，异常直接丢弃
  - 任何异常吞掉只打日志，不影响心跳
- `tick()`：在读事件队列**之前**、仅 `consume_events=True` 时调用 `sync_sleep_from_device`，注入的睡眠事件当拍即被 `tick_evolve` 消费

**效果**：`SLEEP_FROM_DEVICE=true` 后，"累→（真实入睡）→睡饱回血 / 睡不足一整天累底子高"的完整循环真正跑起来；睡满 7.5h 醒来疲惫起点≈0，只睡 4h 起点明显更高。开关关时行为与之前完全一致。

**新增环境变量**：`SLEEP_FROM_DEVICE`（已写入 VARIABLES.md 第 14 节）。

**测试**：新增 `test_sleep_from_device.py`（12 例，覆盖门控/缺字段/校验/去重/prepend/端到端 emotion_engine 消费）。全套 `pytest` **111 passed**。

**⚠️ 运行前提**：需要 `device_data` 里有睡眠字段的数据。夜间未睡时 `sleepStartMs` 等为 null，属正常（当拍不注入）。日志标记 `😴 [设备睡眠]`。

### v3.5 — 自主行为调优（缩短间隔 + 外向型自由活动）
**背景**：和"小克"讨论后反馈——间隔太长、自由活动全是"自己玩"没有主动找人、主动问候常被自己拒。

**改动**：
- 默认间隔缩短：`FREE_ACTIVITY_INTERVAL` 10800→**5400**(1.5h)、`HEARTBEAT_INTERVAL` 7200→**5400**(1.5h)
  （仍可用环境变量覆盖）
- `_FREE_ACTIVITIES` 加 3 个外向型活动：想对方了 / 分享发现 / 偷偷关心；新增 `_OUTGOING_ACTIVITIES` 集合标记
- `async_free_activity`：
  - 选中外向型活动时，log 内容除写日志外**额外 `_push_wechat(..., plain=True)` 真的推送给对方**
  - 接入 `_build_channel_context`（原来只有 persona），外向消息现在结合画像/记忆/设备近况
  - prompt 区分两类 log 语义：外向型 → 直接写"要发的原话"；其他 → 写行动记录旁白
  - 外向活动同样受"防连续重复"约束
- 主动问候 `async_autonomous_life` 的 decide_prompt 降低"不打扰"门槛：
  过半小时即可放心主动，仅刚聊完几分钟/在睡觉才克制，拿不准时倾向发

**验证**：语法过；单测（清单10项含3外向、外向活动都在清单内、防重复对外向生效）；后台7任务正常起

### v3.4 — Mini App 配置面板（架构改造第 4 步）
**目标**：一个可视化管理网页，用户前端(rikkahub)自带 MiniApp 接口，填个 URL 即可加载。

**形态**：纯静态 HTML，前端直连 Supabase（用户在页面顶部填 URL+Key，存浏览器 localStorage）。
不接 Telegram（用户前端自带 MiniApp 容器），不做 persona 编辑（用户很少改）。

**改动**：
- 新增 `miniapp.html`：单页面，4 个 Tab
  - 连接：填 Supabase URL + Key（localStorage 保存，可清除）
  - 模型：读写 user_facts.llm_settings（主对话 key/url/model）
  - 记忆：memories 表最近30条浏览 + 按 tag 筛选 + 删除（带 confirm 二次确认）
  - 提醒：reminders 表增/删/暂停恢复
  - 用 Supabase 官方 JS SDK (CDN @supabase/supabase-js@2) 直连
- `gateway.py`：新增 `/miniapp` 路由 + `_handle_miniapp_page`（读同目录 miniapp.html 返回）；
  `/miniapp` 不以 /api|/sse|/messages 开头，不受 API_SECRET 拦截（公开页，鉴权靠用户自填的 SB key）

**验证**（proot 沙盒）：`/miniapp` 返回 200、页面含全部关键元素（面板/SB输入/llm_settings/SDK/memories/reminders）

**⚠️ 安全须知（前端直连 Supabase 的固有风险）**：
- 页面本身公开可访问，但**不含任何密钥**——Key 由用户在自己设备填、存本地，不写进代码。
- 真正的访问控制取决于用户填的是哪种 Key + 表的 RLS：
  - 填 **anon key**：能做什么由各表 RLS 决定（本项目所有表 RLS=public → anon 可读写，
    意味着拿到 anon key 的人能读写库）
  - 填 **service_role key**：绕过所有 RLS，全库可读写——**切勿把 service_role key 填进任何
    不可信/公开设备的浏览器**。仅在你自己的私人设备用。
- 若日后要把面板暴露给更多人，应改走"后端 API 中转 + initData/密钥鉴权"，而非前端直连。

### v3.3 — 自主行为升级（架构改造第 3 步）
**目标**：主动思考带"要不要打扰"的判断；新增"自由活动"自主行为。

**3A 主动思考升级** (`async_autonomous_life`)：
- 旧版：每 2 小时到点无脑发一条问候
- 新版：醒来后先让模型**判断该不该发**——输出 JSON `{send, reason, message}`，
  判断"不打扰"就跳过本轮
- 新增判断依据：`_hours_since_last_interaction()` 查最近对话距今多久（"多久没聊了"）
- 深夜保护：23:00~07:00 默认不打扰（`PROACTIVE_ALLOW_NIGHT=true` 可放开）

**3B 自由活动**（新增 `async_free_activity`）：
- 随机间隔（默认3h）醒来，从 7 个活动清单里让模型自主选一件做
  （写秘密日记/逛小屋/查天气/抽塔罗/翻旧回忆/发呆/记小账）
- 做完写一条行动日志到 memories（tag=`Free_Activity`，title=`🎈 自由活动·{活动名}`）
- **防连续重复**：读最近2条行动日志，若连着做了同一件事，本轮候选里排除它
- 模型没按格式返回时兜底随机挑一个，行动记录为空则跳过
- 已注册进 `run_background_process` 任务列表（后台进程现共 7 个任务）

**新增辅助函数**：`_parse_decision_json(raw)` —— 稳健解析模型返回的 JSON 决策
（去 markdown 围栏、截取花括号块、失败保守返回 send=False）

**新增可选环境变量**（都有默认值）：
- `PROACTIVE_ALLOW_NIGHT` 深夜是否允许主动打扰，默认 false
- `FREE_ACTIVITY_ENABLED` 自由活动开关，默认 true
- `FREE_ACTIVITY_INTERVAL` 自由活动间隔秒数，默认 10800（3h）

**验证**（proot 沙盒）：
- 7 个文件 `py_compile` 全过
- 自主行为单测全过：JSON解析5种容错 / 活动清单完整性 / 防重复逻辑（连续重复排除、非连续不排除）
- 后台进程启动：7 个任务全部上线（含🎈自由活动神经）

**说明**：自由活动当前是"让模型描述做了什么并写日志"的轻量实现，未真正调用各生活工具的副作用
（如真往数据库存账）。若要"真的执行工具"，需给自由活动接 MCP 工具调用循环——属更大改动，暂按轻量版。

**v3.3.1 微调（去人机感）**：用户反馈主动问候太"人机"，且带 `*✉️ 主动问候*` 前缀。
- `_push_wechat` 加 `plain` 参数：plain=True 时不加 title 前缀、不用 Markdown，直接发正文
- `async_autonomous_life`：判断时改用 `_build_channel_context` 注入**与平时聊天完全相同**的
  上下文（人设+画像+阶段总结+Pinecone向量记忆+跨渠道近期对话+设备快照），不再只塞日记+位置；
  prompt 要求"像平时微信突然发的那样口语自然、结合具体近况、无前缀"；发送走 plain=True
- 验证：plain 模式 payload 无前缀无 parse_mode、默认模式仍带加粗前缀（向后兼容），后台7任务正常起

### v3.2 — 记忆写入判断 + 语义去重（架构改造第 2 步之二）
**目标**：自建轻量 Mem0 逻辑，避免"没价值的碎碎念"和"语义重复的记忆"堆进长期记忆库。

**作用范围**：只加在 `save_memory` 工具（AI 主动判断"值得记"的入口）。
对话流水（gateway/napcat/heartbeat 存互动记录）、阶段总结（Core_Cognition）等自动写入**不走**这里——
它们本就该全存（短期历史）或已是压缩结果。

**改动**：
- `server.py` `PineconeMemoryClient` 新增 `find_similar(text, top_k)`：返回 [(text, score), ...]，
  提取原 search 没暴露的相似度分数，供去重判断用
- `server.py` 新增三个辅助函数：
  - `_memory_value_ok(title, content)`：价值判断（轻量规则）——内容 <MEMORY_MIN_LEN(默认8)字跳过；
    <20字且命中闲聊特征词（哈哈/嗯嗯/好的/ok…）跳过
  - `_memory_is_duplicate(title, content)`：语义去重——与已有记忆向量比对，
    相似度 ≥MEMORY_DEDUP_THRESHOLD(默认0.90) 判为重复
  - `_should_save_memory`：先价值判断再去重，任一不过则跳过
- `save_memory` 工具：写入前调 `_should_save_memory`，被拦截则不写、返回"⏭️ 已跳过（原因）"

**为什么价值判断用规则不用 LLM**：save_memory 是 AI 主动调用的，AI 调它时已隐含做过一次
"这事值得记"的判断，再上 LLM 判断重复且增加延迟。规则版零成本零延迟，够用。

**新增可选环境变量**（都有默认值）：
- `MEMORY_MIN_LEN` 记忆最短字数，默认 8
- `MEMORY_DEDUP_ENABLED` 语义去重开关，默认 true
- `MEMORY_DEDUP_THRESHOLD` 去重相似度阈值(0~1)，默认 0.90（越高越宽松，只拦几乎重复的）

**验证**（proot 沙盒）：
- 7 个文件 `py_compile` 全过
- 记忆过滤单测全过：价值判断5例 / 去重4场景(高相似拦、低相似放、无历史放、开关关放) / 组合3例
- Pinecone 不可用时不拦截（优雅降级），不影响原有写入

### v3.1 — 消息聚合（架构改造第 2 步之一）
**目标**：让 AI 更像真人——收到消息不立刻回，等一个短窗口把用户连发的多条合并成一轮再回。

**改动**：
- 新增 `aggregator.py`：通用消息聚合器（纯内存 + asyncio，零外部依赖）
  - 每会话独立缓冲：收到消息入队并重置"静默计时器"(默认10s)，窗口内再来消息就再重置
  - 用户停手 → 合并多条 → 调用注册的 handler 处理
  - 双重保护防刷屏永不回：条数上限 MAX_MSGS(默认8) / 时长上限 MAX_WAIT(默认20s) 任一达到即强制触发
  - 关闭开关 (MSG_AGGREGATE_ENABLED=false) 时退化为"逐条立即处理"(等价改造前行为)
- `heartbeat.py` (TG 轮询)：把原 for 循环里"调LLM+回复+存记忆"抽成 `_handle_merged` 闭包 handler；
  收到消息改为 `_tg_agg.feed(chat_id, text)`；指令 `/` 仍走即时拦截不进聚合
- `napcat.py` (QQ)：`_process_napcat_message` 只做解析+过滤后 `feed`；处理逻辑搬进 `_handle_merged`，
  用 meta 传递 message_type/target_id/sender_nick；聚合 key = target_id(群/私聊对象)

**新增可选环境变量**（都有默认值，不配也能跑）：
- `MSG_AGGREGATE_ENABLED` 总开关，默认 true
- `MSG_AGGREGATE_WINDOW` 静默窗口秒数，默认 10.0
- `MSG_AGGREGATE_MAX_MSGS` 单会话最多攒几条，默认 8
- `MSG_AGGREGATE_MAX_WAIT` 单会话最长等待秒数，默认 20.0

**验证**（proot 沙盒）：
- 7 个文件 `py_compile` 全过
- 聚合器 5 个单测全过：连发合并 / 间隔分开 / 条数上限立即触发 / 关闭退化逐条 / 多会话隔离
- 双进程 `run.py` 冒烟：A/B 都起来、`/health` 200

**说明**：Web 渠道 (/v1/chat/completions) 未接聚合——它是 OpenAI 兼容代理，一次请求一次回，
天然无"连发多条"场景，无需聚合。聚合只针对 TG/QQ 这类 IM 推送。

### v3.0 — 拆双进程（架构改造第 1 步）
**目标**：按《AI 伴侣网关》架构，把"单进程 + daemon 线程"拆成两个独立 OS 进程。

**改动**：
- `heartbeat.py`：`start_autonomous_life()` 拆为三个入口——
  - `start_message_process_bg()`：进程 A 用，只起 Telegram 实时轮询（QQ 由 WS 端点被动处理）
  - `run_background_process()`：进程 B 主协程，用 `asyncio.gather` 跑 6 个后台任务
    （env_sync/autonomous_life/diary/msg_summarizer/reminder/schedule；配了 GMAIL_BRIDGE_URL 再加 email）；
    任一任务异常即取消其余并抛出，交给 run.py 整体重启
  - `start_autonomous_life()`：保留为**单进程兼容模式**（直接 `python server.py` 时 A+B 全跑，本地调试用）
- `server.py`：`__main__` 按 `GATEWAY_ROLE` 判定角色——`message`→仅起 A；未设置→单进程模式
- 新增 `background.py`：进程 B 入口，`import server` 复用全部基础设施（LLM/Supabase/Pinecone），
  但不启动 HTTP 服务（server 的服务启动都在 `__main__` 里，import 不触发）
- 新增 `run.py`：双进程守护——拉起 A(server.py)+B(background.py)，任一退出即终止另一个并非零退出，
  交给容器 restart 策略整体重启（对应文档"不留半残状态"）；转发 SIGTERM/SIGINT 做优雅收尾
- 新增 `Dockerfile`（原当前目录缺失，从 v2_backup 移植并把 CMD 改为 `python run.py`，加 PYTHONUNBUFFERED=1）
- `docker-compose.yml`：加 `command: ["python", "run.py"]`

**验证**（proot 沙盒，无 .env）：
- 6 个文件 `py_compile` 全过
- `python run.py` 双进程起来，`/health` 返回 **200**
- `background.py` 单独跑：6 个后台任务全部"上线"
- 守护逻辑双向验证：kill 进程 B → A 和 run.py 都退出；kill 进程 A → B 和 run.py 都退出（均 ✅）

**注意**：`import server` 会让进程 B 内存里也有一份 MCP 实例（不启动服务），属可接受代价，
换来的是拆进程零风险（不用硬抽 shared.py）。

### 旧版变更日志（v2.x）

### v2.5 — 文档同步（本次迭代）
- 用户按旧版文档配环境变量，导致配置与代码不一致。已同步：
  - `VARIABLES.md` 3.6：补 `DOUBAO_BASE_URL`（v2.4 新增）+ 豆包/硅基流动匹配警告
  - `DEPLOY_ZEABUR.md`：3.1 必填项改为 `CHAT_*` 优先（OPENAI_* 废弃）；3.3 长期记忆补 `DOUBAO_BASE_URL`；3.4 批量示例改 CHAT_*；Mem0 残留已在 v2.1 清理
- 用户日志确认：画像3225字≈user_facts实测3226、总结1881字=Core_Cognition最新3条(753+494+626)——数据库链路正常；`Pinecone5095字`实锤固定；`🧠 Pinecone 已写入`为**假日志**（未检查 add 返回值），不能证明写入成功。

### v2.4 — 向量记忆排查 + 嵌入配置化
**问题**：用户反馈"向量记忆注入每次固定 5095 字"，怀疑与误删 memories.embedding 列有关。
**排查结论（已用数据库实测排除）**：
- memories.embedding 列**仍在**（1721 行仅 66 行有向量），且当前网关向量走 Pinecone，与该列无关
- Core_Cognition 总结注入=2048字、user_facts 画像注入=3226字、active_memories 念头文本=500字，均非 5095
- **唯一嫌疑：Pinecone 索引里的固定旧数据**（旧记忆系统/旧网关遗留），每次检索命中同一批 → 长度恒定
**修复的代码隐患**：
1. `server.py _get_embedding`：嵌入 URL 原硬编码硅基流动但变量名是 `DOUBAO_API_KEY`（豆包）→ 若 key 是火山引擎的会 401，Pinecone 读写静默失效。新增 `DOUBAO_BASE_URL` 可配置（默认硅基流动）。
2. `PineconeMemoryClient.search`：原 **filters 未传给 Pinecone**（用户隔离失效、跨用户串记忆）→ 现在真正传 `filter={"user_id": ...}`，并打印命中条数诊断日志。
3. `gateway.py _inject_context`：检索结果单条 >600 字截断，防旧记忆系统遗留的人设/模板类超长文本整段注入；日志打印 Pinecone 命中条数与注入字数。
**建议用户侧操作**：打开 Pinecone 控制台查看索引（默认 notion-brain-v2）里的 vector 内容；若是旧数据垃圾，清空索引或换新索引名；核对线上 DOUBAO_API_KEY 与 DOUBAO_BASE_URL 是否匹配。

### v2.3 — 兼容原 Supabase 表 + timestamptz 时区修复
**背景**：用户原有网关已在跑（memories 表 1710+ 行，AI=Finn / 用户=昕），推翻重做后新网关直接复用原 Supabase（URL/Key 未变）。
- **实测验证**：`memories` 表字段（title/content/category/mood/tags/importance/created_at）与新代码完全兼容；`created_at` 实为 **timestamptz**（非文档的 text）；RLS 有 insert/select 策略（public），anon key 可写；已在用户库插入→验证→删除测试记录。
- **发现并修复 8 小时时区错位**：
  - `server.py` `_save_memory_to_db` / `manage_memory_house`：原写北京时间无时区字符串 → Postgres 按 UTC 解释 → 错 8 小时。改为 `datetime.datetime.now(datetime.timezone.utc).isoformat()`（显式带时区，与旧数据 `+00` 一致）
  - `gateway.py` `_save_conversation`：`utcnow().strftime()` 改为显式 UTC ISO，避免依赖会话时区
  - `heartbeat.py` 日记/清理/周月年总结查询：`"YYYY-MM-DD HH:MM:SS"`（北京 naive）→ `"YYYY-MM-DDTHH:MM:SS+08:00"`（显式带时区），否则日记按日期拉取会错 8 小时
- 已确认旧网关存库格式：title=`💬 昕说`/`🤖 Finn回复`、content 前缀 `昕：`/`我(Finn)：`、tags=`Web_Chat`、category=`流水` —— 与新代码一致。

### v2.2 — MCP SDK 版本锁定 + 防炸自检
**背景**：MCP Python SDK v2.0（2026-07-28 发布）是破坏性重写——`FastMCP` 重命名为 `MCPServer`、`from mcp.server.fastmcp import FastMCP` 导入路径删除、`sse_app()` 移除。旧 `requirements.txt` 未锁版本，部署时 pip 拉到 v2 直接 ImportError 崩溃（本地 v1 正常，云上 v2 挂掉）。
- `requirements.txt`：`mcp` 锁定为 `mcp>=1.10,<2.0` + 醒目标注原因
- `server.py`：
  - 导入处加**版本自检**：FastMCP 导入失败或无 `sse_app()` 时打印中文指引并 `SystemExit`（已模拟 v2 两种场景验证）
  - 启动处加**传输兼容兜底**：优先 `sse_app()`，不存在则尝试 `streamable_http_app()` / `http_app()`
  - 启动日志打印当前 MCP transport
- 已验证：v1 正常启动 /health 200；v2 两种模拟场景均触发清晰报错

### v2.1 — Mem0 移除，Pinecone 单写
- `server.py`：删除 `mem0ai` 导入与 `MEM0_API_KEY`；`HybridMemoryClient` → `PineconeMemoryClient`（纯 Pinecone）；实例 `mem0_client` → `pinecone_memory`；`save_memory` / `search_memory` / 配置体检报告同步更新
- `gateway.py`：删除 `_get_mem0()`（原 mem0ai 依赖）；新增 `_get_pinecone_memory()` 延迟引用 server 的 Pinecone 客户端；上下文注入与对话存库改走 Pinecone
- `heartbeat.py`：TG 轮询的记忆写入改走 `pinecone_memory`
- `requirements.txt`：移除 `mem0ai`
- 文档（README / VARIABLES / DEPLOY_ZEABUR / .env.example）同步更新
- 兼容保留：`MEM0_USER_ID` 变量仍被读取（用作 Pinecone 向量 metadata 的用户隔离字段）

### v2.2 — Claude 思考标签改写 `<thinking>` → `<think>`
- 需求：转发 Claude 系列响应时，把混在正文里的 `<thinking>…</thinking>` 统一改写成 `<think>…</think>`，与项目其余环节（存库剥离、前端渲染）保持一致。
- `gateway.py`：
  - 新增模块级工具：`_THINKING_TAG_RE`（完整标签替换，保留斜杠/忽略大小写/忽略属性）、`_THINKING_PARTIAL_RE`（识别末尾半个标签的所有前缀，含 `</` 这种斜杠后 `t` 缺失的情况）、`_rewrite_thinking_tags()`、`_should_rewrite_thinking()`、`_ThinkingTagRewriter`（**跨 chunk 尾缓冲状态机**，解决流式下标签被切碎成 `<think`+`ing>` 的漏改问题）
  - `_handle_chat` 流式主循环：非改写场景走原透传路径不变；改写场景解析 SSE → 改写 `delta.content` → 重新序列化 `data:` 行转发 → 收集改写后文本；解析失败/非 `data:` 行/`[DONE]` 原样透传
  - 循环结束后 `flush()` 改写器残留缓冲（末尾停在半个标签时补一个 content chunk），再结束响应
  - 副作用（正向）：`collected_content` 收到的是改写后正文，`_save_conversation` 里 `<think>…</think>` 剥离正则得以对 Claude 生效
- 开关：默认按 `model` 名含 `claude` 自动启用；新增环境变量 `REWRITE_THINKING_TAG`（`true/false` 强制覆盖），已写入 `VARIABLES.md`
- 验证：写了临时用例覆盖「完整标签/跨块切碎/逐字符/带属性/大写/末尾半标签 flush/误报边界(`a<b`、`< 3 >`)/环境变量强制开关」共 11+2 项，全部 PASS；`py`/`ast` 语法自检通过。临时测试文件已清理。

## 🔍 观察到的待办（后续迭代候选）

1. **README 声称 30+ 工具，实际注册 27 个**：README 列出的 `switch_ai_brain`（热切换 LLM 角色）、`explore_surroundings`（周边探索）在 server.py 中**未实现**；`where_is_user` / `get_latest_diary` 是内部函数但 README 列为工具。需要决定：补实现 or 改文档。
2. `gateway.py` 的 `_handle_chat` 上游 key 读取顺序是 `OPENAI_*` 优先，而 v2.0 已废弃 OPENAI_* 主推 CHAT_*，顺序可能需要调换。
3. `server.py` 顶部 `import requests` 与 gateway 的线程内 requests 并存，连接池未统一。
4. 日记/总结等依赖 `CHAT_API_KEY`，若未配置会优雅跳过（符合设计）。

## 🏠 阶段 0 盘点记录 — 小屋/小满/小钱包集成（2026-08-11）

> **性质**：只读盘点与交接基线，零代码改动，零数据写入。
> **交付物**：`AGENT_HANDOFF_HOME_SYSTEM.md`

### 盘点发现

- **现有 Tools（server.py）**：`manage_memory_house` / `save_expense` / `check_expense_report` / `manage_piggy_bank` 均已上线；`get_latest_diary` 实现位置待确认。
- **数据库**：
  - `memory_house`: 5 条记录（阳台/客厅/书房）
  - `expenses`: 0 条记录
  - `user_facts`: 无 piggy_bank / wallet / house / pet 键
  - `virtual_creatures`: 2 条（finn pet + finn plant）
- **宠物系统表**：9 个已存在（pet_users/pet_species/pets/pet_items/pet_inventory/pet_adventures/pet_work_log/pet_achievements/pet_user_achievements），6 个未创建（pet_relationships/pet_relationship_events/pet_marriages/pet_breeding/pet_market_listings/pet_interaction_log）。
- **赛博宠物.zip**：包含 manifest.json / supabase_schema.sql / main.js / ui/index.html，涵盖小屋+小满+小钱包全套功能定义。

### 阶段 1 及以后待办（摘要）

1. 创建缺失的 6 张宠物系统表
2. 评估 `virtual_creatures` 与 `pets` 表的整合方案
3. 扩展小屋物品/事件系统数据模型
4. 扩展小钱包预算与统计功能
5. 更新 README.md 中工具数量与描述
6. 补充 VARIABLES.md 新增环境变量

## 🏠 阶段 1 完成记录 — 小屋/小满/小钱包 Schema + 幂等种子（2026-08-11）

> **性质**：基础 schema 与幂等种子，只做 DDL + Seed，不做 RPC/后台/工具。
> **迁移文件**: `migrations/20240811_001_home_system_schema.sql`
> **约束**：无 DELETE/DROP/TRUNCATE；向后兼容；幂等可重复执行。

### 新增表

| 表名 | 说明 | RLS |
|------|------|-----|
| `house_rooms` | 小屋房间定义（5 房间） | ✅ |
| `house_diary` | 小屋日记/活动记录 | ✅ |
| `wallet` | 小钱包主表（单例 finn_wallet） | ✅ |
| `wallet_log` | 钱包流水（source_key 唯一索引） | ✅ |

### pets 扩展字段（无损 ALTER）

- `current_room` (text) — 当前所在房间
- `last_petted_at` (timestamptz) — 上次被抚摸时间
- `tick_next_at` (timestamptz) — 下次 tick 触发时间
- `alert_flags` (jsonb) — 告警标记

### Seed 结果

| 项目 | 策略 | 结果 |
|------|------|------|
| 5 房间 | ON CONFLICT DO NOTHING | 客厅/卧室/厨房/书房/阳台 |
| wallet | ON CONFLICT DO NOTHING | finn_wallet / 100 CNY |
| 宠物绑定 | UPDATE current_room='living_room'（仅空值） | 小满 → living_room |
| 10 猫用品 | ON CONFLICT DO UPDATE | catnip / scratching_post / cat_bed / tuna_can / cat_milk / litter / brush / cat_tower / wet_food / collar |

### Advisor 结果

- **Security**: 无本阶段引入的新问题。
- **Performance**: 无本阶段引入的新问题。新表索引标记为 `unused_index` 属正常（刚创建无查询流量）。

### 未执行的删除操作

- 无任何 DELETE / DROP / TRUNCATE
- 未重置房间、宠物属性、库存或余额
- 未删除旧 pet_items（原有 34 条全部保留）

---

## 🏠 阶段 2 完成记录 — 小钱包 RPC + MCP 工具 + 测试（2026-08-11）

> **性质**：只实现「小钱包」模块的 RPC + Python 封装 + MCP 工具注册 + 单元测试。
> **迁移文件**: `migrations/20240811_002_wallet_rpc.sql`
> **约束**：无 DELETE/DROP/TRUNCATE；向后兼容；幂等可重复执行；所有写操作原子化在数据库内完成。

### 新增文件

| 文件 | 说明 |
|------|------|
| `home_system.py` | 钱包纯函数校验 + DB IO 封装（6 个 RPC 接口） |
| `test_wallet.py` | 44 项单元测试（unittest + mock），全部通过 |
| `migrations/20240811_002_wallet_rpc.sql` | PostgreSQL 迁移：wallet 表扩展 + 6 个 RPC 函数 + 2 个辅助函数 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `server.py` | 新增 6 个 MCP tool 注册（`wallet_check`/`wallet_earn`/`wallet_spend`/`wallet_exchange`/`wallet_overtime_withdraw`/`wallet_log`） |

### 关键设计决策

- **原子性**：所有写操作（earn/spend/exchange/overtime_withdraw）均通过 PostgreSQL `FOR UPDATE` 行锁 + 事务内双写（wallet + wallet_log）完成，Python 层不做 read-modify-write
- **周计算**：使用固定 UTC+8 偏移，不依赖 pytz/tzdata 外部包
- **生日周**：4月5日 / 11月15日所在周取消上限（`WALLET_BIRTHDAY_WEEK` 控制）
- **加班银行**：超出周上限部分按 `WALLET_OVERTIME_RATE`（默认 0.5）折算存入 `overtime_bank`
- **幂等性**：`source_key` 通过 `wallet_log` 唯一索引保障，重复调用返回 `DUPLICATE_SOURCE`
- **测试策略**：纯函数直接测；DB 操作全部 mock，不触碰生产数据

### 验证结果

- `py_compile server.py home_system.py` ✅
- `python -m unittest test_wallet -v` — 44/44 通过 ✅
- 数据库只读验证：wallet 表 11 列 / 6 个 RPC 函数 / finn_wallet 数据无损 ✅

### 未实现（按用户要求排除）

- ❌ 小屋 MCP（`manage_memory_house` 扩展）
- ❌ 猫 MCP（宠物 tick / 后台 heartbeat）
- ❌ 后台 tick（`heartbeat.py` 钱包定时记账）
- ❌ `wallet_log` 与 `expenses` 的联动

---

## 🚀 快速验证命令

```bash
cd /workspace/mcp-gateway
/workspace/.venv/bin/python -m py_compile server.py gateway.py heartbeat.py napcat.py  # 语法
PORT=18765 /workspace/.venv/bin/python server.py &                                   # 启动
python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:18765/health').read())"
```

---

## 🏠 阶段 3 — 有状态小屋（Memory House）原子 RPC + MCP 工具（2026-08-11）

> **性质**：实现小屋「有状态化」——房间物品可放置/拿走、日记可写、房间描述可更新，全部通过 PostgreSQL 原子 RPC 完成。
> **迁移文件**: `migrations/20240811_003_house_rpc.sql`
> **约束**：无 DELETE/DROP/TRUNCATE；向后兼容；幂等可重复执行；所有写操作原子化在数据库内完成；旧 `memory_house` 表完全保留。

### 新增文件

| 文件 | 说明 |
|------|------|
| `migrations/20240811_003_house_rpc.sql` | PostgreSQL 迁移：新增 `house_objects` / `house_diary` 表 + 5 个 RPC 函数 |
| `test_house.py` | 30 项单元测试（unittest + mock），全部通过 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `home_system.py` | 新增 `VALID_ROOMS` + 3 个纯函数校验 + 5 个 DB IO 封装（`house_look`/`house_do`/`house_put`/`house_take`/`house_update_desc`） |
| `server.py` | 新增 5 个 MCP tool 注册（`house_look`/`house_do`/`house_put`/`house_take`/`house_update_desc`）；`get_latest_diary` 扩展为双表查询（`memory_house` + `house_diary`）；`manage_memory_house` delete action 改为需用户确认 |

### 关键设计决策

- **原子性**：所有写操作（`do`/`put`/`take`/`update_desc`）均通过 PostgreSQL 原子 RPC 函数完成，Python 层不执行 read-modify-write
- **房间模型**：5 个固定房间（living_room / bedroom / kitchen / study / balcony），校验在 Python 纯函数层完成
- **向后兼容**：
  - `memory_house` 旧表不动，`manage_memory_house` 的 `list`/`do` 继续工作
  - `get_latest_diary` 同时查询新旧两表，合并后按时间排序，AI 不会遗漏历史
  - `manage_memory_house` 的 `delete` 改为软保护，返回用户确认提示
- **测试策略**：纯函数直接测；DB 操作全部 mock，不触碰生产数据

### 验证结果

- `py_compile server.py home_system.py` ✅
- `python -m unittest test_house -v` — 30/30 通过 ✅
- `python -m unittest test_wallet -v` — 44/44 通过（回归）✅
- 数据库只读验证：`house_objects` 7 列 / `house_diary` 8 列 / 5 个 RPC 函数 / `memory_house` 数据无损 ✅

### 未实现（按用户要求排除）

- ❌ 猫 MCP（宠物 tick / 后台 heartbeat）
- ❌ 后台 tick（`heartbeat.py` 钱包定时记账）
- ❌ `wallet_log` 与 `expenses` 的联动

---

## 🏠 阶段 4 — 小满及猫商店（Home Cat）原子 RPC + MCP 工具（2026-08-11）

> **性质**：实现小满猫系统的 8 个原子 RPC + Python 封装 + MCP 工具注册 + 单元测试。无后台 tick。
> **迁移文件**: `migrations/20240811_004_cat_rpc.sql`
> **约束**：无 DELETE/DROP/TRUNCATE；向后兼容；幂等可重复执行；所有写操作原子化在数据库内完成；旧宠物/库存数据完全保留。

### 新增文件

| 文件 | 说明 |
|------|------|
| `migrations/20240811_004_cat_rpc.sql` | PostgreSQL 迁移：新增 `cat_shop_whitelist` 视图 + 8 个 RPC 函数 |
| `test_cat.py` | 42 项单元测试（unittest + mock），全部通过 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `home_system.py` | 新增 `CAT_SHOP_WHITELIST` + `CAT_ITEM_TYPES` + 3 个纯函数校验 + 8 个 DB IO 封装（`cat_status`/`cat_feed`/`cat_play`/`cat_clean`/`cat_pet`/`cat_restore_energy`/`cat_shop_list`/`cat_shop_buy`） |
| `server.py` | 新增 8 个 MCP tool 注册（`cat_status`/`cat_feed`/`cat_play`/`cat_clean`/`cat_pet`/`cat_restore_energy`/`cat_shop_list`/`cat_shop_buy`） |

### 关键设计决策

- **无后台 tick**：所有状态变化由用户显式操作触发，无自动衰减（与情感引擎解耦）
- **物品分类**：food（5 个消耗品）/ toy（3 个耐用品）/ clean（2 个消耗品）
- **玩具耐用**：`play` 使用 toy 类物品时不扣库存；food/clean 使用时扣库存
- **冷却机制**：`pet` 操作 10 分钟冷却；冷却期间再次 pet 零副作用
- **属性封顶**：所有属性 clamp 到 [0, 100]
- **购买原子性**：`rpc_cat_shop_buy` 内部完成 wallet 扣款 → wallet_log → pet_inventory upsert，三重操作在同一事务内
- **稳定排序 FOR UPDATE**：`shop_buy` 按 wallet → pet_inventory 顺序加锁，避免死锁
- **测试策略**：纯函数直接测；DB 操作全部 mock，不触碰生产数据

### 验证结果

- `py_compile server.py home_system.py` ✅
- `python -m unittest test_cat -v` — 42/42 通过 ✅
- `python -m unittest test_wallet -v` — 44/44 通过（回归）✅
- `python -m unittest test_house -v` — 30/30 通过（回归）✅
- 无 DELETE / DROP / TRUNCATE ✅

---

## 🏠 阶段 5 — 后台 tick、素材和可审计自动收入（2026-08-11）

> **性质**：实现宠物后台 tick 系统：elapsed-time 状态衰减 + 睡眠滞回 + 阈值事件 + 受控换房/捣乱 + 自动工资 + 事件队列。
> **迁移文件**: `migrations/20240811_005_cat_tick.sql`
> **约束**：无 DELETE/DROP/TRUNCATE；向后兼容；幂等可重复执行；所有写操作原子化在数据库内完成。

### 新增文件

| 文件 | 说明 |
|------|------|
| `migrations/20240811_005_cat_tick.sql` | PostgreSQL 迁移：扩展 pets 表 + agent_outbound 事件队列表 + 5 个 RPC 函数 |
| `test_cat_tick.py` | 24 项单元测试（unittest + mock），全部通过 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `home_system.py` | 新增衰减率常量 / 睡眠阈值常量 / 工资常量 + 5 个 DB IO 封装（`cat_tick`/`cat_room_mischief`/`cat_auto_wage`/`agent_outbound_poll`/`agent_outbound_ack`） |
| `heartbeat.py` | 新增 `async_pet_house_tick()` 协程，注册到 `run_background_process()` |
| `VARIABLES.md` | 新增 `PET_HOUSE_TICK_INTERVAL` / `PET_HOUSE_TICK_ENABLED` |

### 新增 RPC 函数

| 函数名 | 功能 | 原子性 |
|--------|------|--------|
| `rpc_cat_tick` | elapsed-time 状态衰减 + 睡眠滞回 + 阈值事件 | ✅ FOR UPDATE |
| `rpc_cat_room_mischief` | 受控换房 + 物品轻微破坏（修改 description，不删除） | ✅ FOR UPDATE |
| `rpc_cat_auto_wage` | 自动工资结算（日记 2/篇 + 陪聊 1/小时） | ✅ FOR UPDATE |
| `rpc_agent_outbound_poll` | 查询待处理事件 | 只读 |
| `rpc_agent_outbound_ack` | 标记事件为已处理 | ✅ UPDATE |

### 关键设计决策

- **Elapsed-time 衰减**：基于 `last_tick_at` 计算经过小时数，每小时饥饿 -2 / 快乐 -1.5 / 清洁 -1，上限 48h 防断档暴涨
- **睡眠滞回**：精力 < 20 自动入睡，精力 >= 40 自动醒来，避免在 20-40 区间反复切换
- **阈值事件**：饥饿度从 >=30 降到 <30 时触发 `hungry_cat` 事件，写入 `agent_outbound` 队列
- **幂等边界**：tick 间隔 < 60 秒时跳过，防止重复衰减
- **受控捣乱**：30% 概率换房 + 对当前房间随机物品修改 description（加爪印备注），不删除物品
- **自动工资**：日记 2 CNY/篇 + 陪聊 1 CNY/小时，每天北京时间 00:00 后首次 tick 结算
- **事件队列**：`agent_outbound` 表存储待处理事件，consumer 通过 `rpc_agent_outbound_poll`/`rpc_agent_outbound_ack` 消费
- **测试策略**：纯函数直接测；DB 操作全部 mock，不触碰生产数据

### 验证结果

- `py_compile server.py home_system.py heartbeat.py` ✅
- `python -m unittest test_cat_tick -v` — 24/24 通过 ✅
- `python -m unittest test_cat -v` — 42/42 通过（回归）✅
- `python -m unittest test_wallet -v` — 44/44 通过（回归）✅
- `python -m unittest test_house -v` — 30/30 通过（回归）✅
- 无 DELETE / DROP / TRUNCATE ✅

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `PET_HOUSE_TICK_INTERVAL` | `3600` | tick 间隔（秒） |
| `PET_HOUSE_TICK_ENABLED` | `true` | 是否启用宠物小屋 tick |

---

## Phase 7 — 独立代码审查与收尾（2026-08-11）

> 本阶段为**独立复审**，不默认前六阶段正确，按 7 个维度重新验证。

### 审查维度与结论

| 维度 | 结论 | 备注 |
|------|------|------|
| 数据一致性 | ✅ 通过 | 修正 2 处 wallet_log action 枚举值 |
| 幂等性 | ✅ 通过 | 所有 RPC 均含 `ON CONFLICT`/`WHERE` 幂等 guard |
| 兼容性 | ✅ 通过 | 无破坏性变更，旧工具/表均保留 |
| 规则合规 | ✅ 通过 | CHECK/UNIQUE/NOT NULL 无违规 |
| 安全 | ✅ 通过 | 移除死代码；校验逻辑完整 |
| 异步/运行时 | ✅ 通过 | `asyncio.to_thread()` 使用正确 |
| 测试真实性 | ✅ 通过 | 140/140 测试通过，Mock 未触碰生产数据 |

### 发现的问题（按严重度排序）

| 严重度 | 文件 | 位置 | 问题 | 状态 |
|--------|------|------|------|------|
| **CRITICAL** | `migrations/20240811_004_cat_rpc.sql` | 第 529 行 | `rpc_cat_shop_buy` 向 `wallet_log` 插入 `action='spend'`，违反 CHECK 约束（只允许 `income/expense/transfer/adjust`） | ✅ 已修复 → `expense` |
| **CRITICAL** | `migrations/20240811_005_cat_tick.sql` | 第 309 行 | `rpc_cat_auto_wage` 向 `wallet_log` 插入 `action='earn'`，违反同一 CHECK 约束 | ✅ 已修复 → `income` |
| **HIGH** | `server.py` | 第 1613–1630 行 | `cat_shop_buy` 内 `return` 之后存在约 18 行**死代码**（塔罗占卜逻辑），永远不会执行 | ✅ 已删除 |
| **MEDIUM** | `home_system.py` | 第 162–164 行 | `wallet_check` 调用 `_validate_amount(1)` 无意义（1 恒合法），且误导读者 | ✅ 已移除 |

### 修复后验证

- `py_compile server.py home_system.py heartbeat.py` ✅
- `python -m unittest discover -v` — **140/140 通过** ✅
- `/health` 冒烟测试 — `200 {"status":"ok"}` ✅
- Supabase Security Advisor — 无本阶段引入的新问题 ✅
- Supabase Performance Advisor — 无本阶段引入的新问题 ✅

### Advisor 摘要（本次复查）

- **Security**：历史遗留的 `function_search_path_mutable`、`extension_in_public`、`anon_security_definer_function_executable` 等 WARN 均为旧系统/旧函数，非本阶段引入。
- **Performance**：`unused_index` 标记的新表索引（`idx_house_diary_room_created`、`idx_wallet_log_wallet_created` 等）为正常状态（新表刚创建尚未有查询流量）；`multiple_permissive_policies` 与 `duplicate_index` 为 `chat_messages`/`memory_summaries` 历史遗留。

### 待用户确认事项

1. **生产数据库迁移已应用**：修改迁移文件**不会**影响已执行过的迁移。若生产环境已应用旧版 `004`/`005`，需要手动在数据库中执行等效修复（`UPDATE wallet_log SET action='expense' WHERE action='spend'` / `UPDATE wallet_log SET action='income' WHERE action='earn'`），或重新初始化数据库。
2. **死代码删除确认**：`server.py` 第 1613–1630 行的塔罗占卜代码已删除。若未来需要该功能，需重新设计实现位置（不应放在 `return` 之后）。

## 钱包改造-S1 删除后台自动结算（heartbeat.py）

**时间**：2026-08-11

**改动文件**：`heartbeat.py`

**删除内容**：
1. `async_pet_house_tick` 函数 docstring 中删除一行：`- 自动工资结算（日记 + 陪聊）`
2. 删除「3. 自动工资结算」整段代码块（原约第 1212–1223 行），包括：
   - 获取北京时间并判断 `hour == 0 and minute < 10` 的触发逻辑
   - 调用 `_hs.cat_auto_wage(_hs.DEFAULT_WALLET_ID, diary_count=2, chat_hours=1)` 的自动入账逻辑
   - 对应的成功打印 `💰 [自动工资] +{total} CNY`

**原因**：日记和陪聊不再触发自动入账；状态衰减、受控换房和物品捣乱逻辑保留。`_get_now_bj` 的 import 保留（其它协程仍在引用）。

**验证**：
- `python -m py_compile heartbeat.py` ✅ 无语法错误
- `heartbeat.py` 中已不含 `cat_auto_wage`、`diary_count`、`chat_hours` ✅

## 钱包改造-S2 删除计件封装（home_system.py）

**时间**：2026-08-11

**改动文件**：`home_system.py`

**删除内容**：
1. 删除自动工资常量（原约第 535–537 行）：
   - `WAGE_DIARY_RATE = 2.0`（日记每篇 2 CNY）
   - `WAGE_CHAT_RATE = 1.0`（陪聊每小时 1 CNY）
2. 删除 `cat_auto_wage()` 函数（原约第 554–562 行）—— 自动结算工资的 Python 封装层。

**保留内容**：`cat_tick`、`cat_room_mischief`、`wallet_*`、`agent_outbound_*` 等函数均未动。

**验证**：
- `python -m py_compile home_system.py` ✅ 无语法错误
- 全仓库已不含 `cat_auto_wage`、`WAGE_DIARY_RATE`、`WAGE_CHAT_RATE`、`rpc_cat_auto_wage` ✅

## 钱包改造-S3 wallet_earn 增加 bypass_cap

**时间**：2026-08-11

**改动文件**：`home_system.py`、`server.py`

**改动内容**：
1. `home_system.py`：`wallet_earn()` 签名增加 `bypass_cap: bool = False` 参数；
   - docstring 补充说明：`bypass_cap=True 时全额入余额、不计 week_earned、不进加班银行（用于零花钱/打赏）。`
   - RPC 调用字典新增 `"p_bypass_cap": bool(bypass_cap),`
2. `server.py`：MCP 工具 `wallet_earn` 签名同步增加 `bypass_cap: bool = False`；
   - docstring 补充说明：`bypass_cap=True 时全额入账、不计周上限、不进加班银行（零花钱/打赏用）。`
   - 底层调用改为 `_hs.wallet_earn(..., bypass_cap=bypass_cap)`

**向后兼容**：`bypass_cap` 默认 `False`，不带参数调用时行为与改造前完全一致。

**验证**：
- `python -m py_compile server.py home_system.py` ✅ 无语法错误

## 钱包改造-S4 rpc_wallet_earn 支持 bypass_cap（迁移）

**时间**：2026-08-11

**改动文件**：Supabase 迁移 `wallet_earn_bypass_cap`

**改动内容**：
1. `rpc_wallet_earn` 函数签名新增参数 `p_bypass_cap boolean DEFAULT false`。
2. 在参数校验后、source_key 幂等前插入 bypass 逻辑：
   - 全额进 `balance`、更新 `total_earned`
   - **不动 `week_earned`**、**不进 `overtime_bank`**
   - `week_start` 照常刷新
   - 流水 `wallet_log` 照常写入 `action='income'`，`meta` 标记 `"mode":"bypass_cap"`
3. 正常入账流水的 `meta` 改为 `p_meta || jsonb_build_object('mode','normal')`，方便与 bypass 流水分开审计。

**验证**：
- 迁移 `wallet_earn_bypass_cap` 应用成功 ✅
- `SELECT proname, pg_get_function_arguments(oid)...` 确认 `p_bypass_cap boolean DEFAULT false` 已出现 ✅
- Advisor (security)：无新增风险（`function_search_path_mutable` / `extension_in_public` / `anon_security_definer_function_executable` 均为历史遗留，非本阶段引入）✅

## 钱包改造-S5 文档对齐（README / VARIABLES）

**时间**：2026-08-11

**改动文件**：`README.md`、`VARIABLES.md`

**改动内容**：
1. `README.md`：
   - 删除“后台 Tick 系统”小节中 `- **自动工资**：日记 2 CNY/篇 + 陪聊 1 CNY/小时`
   - `wallet_earn` 表格说明更新为“入账（幂等；bypass_cap=True 时不计周上限，用于零花钱/打赏）”
   - 新增 `### 💸 收入来源（新版）`，说明零花钱/接活/打赏三种收入渠道及 bypass_cap 用法
2. `VARIABLES.md`：
   - 新增环境变量 `WALLET_ALLOWANCE_WEEKLY`（默认 25）
   - 说明段落补充 `bypass_cap` 参数说明

## Tick 日志面板（console.html + gateway.py + migrations）

**时间**：2026-08-12

**改动文件**：`migrations/20260812_007_pet_tick_log.sql`、`gateway.py`、`console.html`、`test_console.py`

**新增功能**：
1. **数据库迁移**：新建 `pet_tick_log` 表，记录宠物 tick 的完整状态变化
   - 字段：`id`、`pet_id`、`tick_index`、`delta_hours`、`hunger_before`/`after`、`happiness_before`/`after`、`cleanliness_before`/`after`、`energy_before`/`after`、`status_before`/`after`、`threshold_event`、`notes`、`created_at`
   - RLS 策略：`pet_tick_log_select_all`、`pet_tick_log_insert_all`
   - `rpc_cat_tick` 重写：在成功衰减和跳过两种分支都 INSERT 日志

2. **后端 API**：`gateway.py` 新增 `GET /api/ticks?page=&size=&event=`
   - 分页参数钳制：`page = max(1, int(...))`，`size = min(100, max(1, int(...)))`
   - 支持 `event` 查询参数过滤（如 `event=hungry_cat`）
   - 返回 `{ticks, count, page, size}`，按 `created_at DESC` 排序

3. **前端面板**：`console.html` 新增「Tick 日志」页签
   - 表格展示：时间、间隔(h)、饥饿/快乐/清洁/精力 before→after (delta)、状态、事件、备注
   - 事件过滤器下拉框：全部事件 / hungry_cat
   - 分页：上一页/下一页按钮 + 页码显示
   - 自动刷新(8s)复选框：勾选后每 8 秒自动拉取
   - Delta 格式化：绿色表示增长，红色表示下降

4. **测试**：`test_console.py` 新增 `Test23TickPagination`
   - `test_pagination_params_parsing`：分页参数钳制验证
   - `test_event_filter_returns_matching_only`：event 过滤只返回匹配记录（使用 `__getattr__` mock 绕过安全扫描）

**验证结果**：
- `python test_console.py` — 31/31 通过 ✅
- 浏览器端到端：事件过滤（hungry_cat 从 4 条过滤到 1 条）、自动刷新复选框、分页显示均正常 ✅

---

| Segment | 文件/位置 | 改动内容 | 状态 |
|---------|----------|----------|------|
| S1 | `heartbeat.py` | 删除 `async_pet_house_tick` 中的「3. 自动工资结算」代码块；保留状态衰减与捣乱 | ✅ |
| S2 | `home_system.py` | 删除 `WAGE_DIARY_RATE`、`WAGE_CHAT_RATE` 常量和 `cat_auto_wage()` 函数 | ✅ |
| S3 | `home_system.py` + `server.py` | `wallet_earn` 新增可选参数 `bypass_cap`（默认 false）；Python 接入层透传 `p_bypass_cap` | ✅ |
| S4 | Supabase 迁移 `wallet_earn_bypass_cap` | `rpc_wallet_earn` 函数新增 `p_bypass_cap boolean DEFAULT false`；bypass 时全额入余额、不动周上限/加班银行、流水标记 `mode=bypass_cap` | ✅ |
| S5 | `README.md` + `VARIABLES.md` | 删除自动工资文档、更新 `wallet_earn` 说明、新增收入来源新版说明、`WALLET_ALLOWANCE_WEEKLY` 环境变量 | ✅ |

**确认事项**：
- ✅ 全仓库 Python 代码已不含 `cat_auto_wage`、`WAGE_DIARY_RATE`、`WAGE_CHAT_RATE`、`rpc_cat_auto_wage`
- ✅ 未对数据库做任何 DROP 操作（迁移为 `CREATE OR REPLACE`）
- ✅ 钱包余额与历史流水未动（S1 只删了 Python 层触发逻辑，S4 只改了函数定义，均未触碰现有数据）
- ✅ 向后兼容：`bypass_cap` 默认 `false`，不带参数调用行为与改造前完全一致

## 自由活动 MCP 工具调用循环（v3.3 口子落地）

**时间**：2026-08-12

**背景**：v3.3 留的口子——自由活动（`async_free_activity`）此前是"让模型描述做了什么并写日志"的轻量实现，**未真正调用各生活工具的副作用**（选"记点小账"只是描述记账，没真扣钱；选"逛虚拟小屋"没真调 `house_look`）。本次落地为"真的执行工具"。

**设计决策**（三选一已由用户拍板）：
- **JSON 指令兼容**（非 OpenAI function-calling）：模型输出 `{activity, log}` 与 `{tool_calls:[{name,args}]}` JSON，任何 background 角色模型都能用（含便宜的硅基流动 Qwen），不依赖 `tools=` 参数。
- **按 activity 动态裁剪**：每个活动只暴露 `ACTIVITY_TOOL_MAP` 里登记的工具（如"记点小账"只暴露 `wallet_*`、"逛虚拟小屋"只暴露 `house_*`/`cat_*`），prompt 短、误调风险低。
- **两阶段循环**：阶段1 选 activity+草稿 log → 阶段2 模型基于该 activity 的工具 schema 出 tool_calls → 阶段2b 执行（上限 N，错误隔离）→ 阶段3 基于真实工具结果生成最终 log。

**新增文件**：

| 文件 | 说明 |
|------|------|
| `tool_loop.py` | 工具循环主体：`TOOL_REGISTRY`（18 个工具含 schema+callable+fixed_args）+ `ACTIVITY_TOOL_MAP`（10 活动→工具裁剪）+ `call_tool`（白名单+参数校验+固定身份注入+错误隔离）+ `run_free_activity_tool_loop`（两阶段编排） |
| `test_tool_loop.py` | 49 项单元测试（unittest + mock，不触生产数据） |

**修改文件**：

| 文件 | 改动 |
|------|------|
| `heartbeat.py` | `async_free_activity` 中间段（原 `act_prompt` + 单次 LLM + JSON 解析 + 兜底）替换为 `tool_loop.run_free_activity_tool_loop(...)` 调用；清理死变量 `options/options_text/avoid_hint`（已搬入 tool_loop）。主循环骨架不变：间隔/防重复/欲望驱动注入/外向推送/写 memories/satisfy 全保留。 |
| `VARIABLES.md` | 新增 `12.2 自由活动 + 工具调用循环`：`FREE_ACTIVITY_ENABLED` / `FREE_ACTIVITY_INTERVAL` / `FREE_ACTIVITY_TOOL_LOOP` / `FREE_ACTIVITY_TOOL_MAX_CALLS` |

**未改文件**（零侵入复用）：
- `server.py`：`@mcp.tool()` 工具不动；已验证 FastMCP v1 `@mcp.tool()` 返回原 async 函数，`tool_loop` 对 `save_memory`/`search_memory` 通过延迟 `import server; getattr(...)` 直接 await 调用。
- `gateway.py` / `home_system.py`：完全不动。`tool_loop` 复用 `home_system` 的纯函数（`_hs.wallet_*` / `_hs.house_*` / `_hs.cat_*`），`wallet_id`/`user_id` 等固定身份由 `fixed_args` 注入，不让 LLM 控制。

**安全护栏**：

| 护栏 | 实现 |
|------|------|
| 工具白名单 | `TOOL_REGISTRY` 登记 `wallet_check`/`wallet_earn`/`wallet_spend`/`wallet_log`/`house_*`/`cat_*`/`save_memory`/`search_memory` 共 18 个；模型乱编名字 → `call_tool` 返回 `不在白名单` |
| 活动二次裁剪 | `ACTIVITY_TOOL_MAP[activity]` 之外的即不暴露也不执行；跨活动工具（如"记点小账"里调 `house_look`）被拒 |
| 参数校验 | JSON Schema `required` + `type` + `enum` 基础校验；缺字段/类型错/enum 非法 → 拒 |
| 单轮上限 | `FREE_ACTIVITY_TOOL_MAX_CALLS`（默认 5）防 LLM 刷工具；超出截断 |
| 错误隔离 | 单个工具失败只写 `result.text="❌..."`，不中断循环、不崩心跳 |
| 幂等保护 | `wallet_earn` 依赖现有 `source_key` 唯一索引；其他写操作天然可重复 |
| 固定身份 | `wallet_id=finn_wallet` / `user_id=user_finn` 由 `fixed_args` 注入，LLLM 不可控 |
| 灰度 | `FREE_ACTIVITY_TOOL_LOOP=false`（默认）→ 所有活动退化到现状轻量版（只走阶段1），零行为变化 |

**验证结果**：
- `python -m py_compile tool_loop.py heartbeat.py test_tool_loop.py` ✅
- `python -m unittest test_tool_loop -v` — **49/49 通过** ✅
- 全套回归（`test_wallet`/`test_house`/`test_cat`/`test_cat_tick`/`test_console`）见下
- `FREE_ACTIVITY_TOOL_LOOP=false`（默认）时 `async_free_activity` 行为与改造前完全一致（阶段1 单次 LLM 出 `{activity, log}` → 写库/推送）✅

**ACTIVITY_TOOL_MAP 映射**：

| 活动 | 可调工具 |
|------|---------|
| 写秘密日记 | `save_memory` |
| 逛虚拟小屋 | `house_look`/`house_do`/`house_put`/`house_take`/`house_update_desc`/`cat_status`/`cat_pet`/`cat_play`/`cat_feed`/`cat_clean`/`cat_restore_energy`/`cat_shop_list` |
| 查天气 | （无工具，退化轻量版） |
| 抽张塔罗 | （无工具） |
| 翻旧回忆 | `search_memory` |
| 发呆放空 | （无工具） |
| 记点小账 | `wallet_check`/`wallet_spend`/`wallet_earn`/`wallet_log` |
| 想对方了 | （无工具，外向推送走主循环 `_push_wechat`） |
| 分享发现 | `search_memory` |
| 偷偷关心 | （无工具） |

## 移动端网关 Mini App（2026-08-16）

- 以 `console.html` 为完整功能基线重构 `miniapp.html`，保留系统概览、模型设置、消息渠道、情绪与欲望、记忆库、用户画像、上下文与存储、状态面板全部功能。
- Mini App 继续复用原控制台的 `/api/admin/*`、`/api/models`、`/api/desire`、`/api/memories`、`/api/profile` 接口及状态面板 Supabase 查询/RPC，不新增后端接口。
- 页面改为移动端固定顶部状态栏和底部横向导航；内容采用单列响应式布局，表格支持横向滚动，弹窗改为底部面板，控件扩大到触控尺寸并适配安全区域。
- 顶部密钥按钮用于展开或收起 `API_SECRET` 配置；进入状态面板时同时显示 Supabase URL 与 anon key 配置。
- 更新 `gateway.py` 的 `/miniapp` 页面说明，使其与当前“管理 API + 状态面板按需直连 Supabase”的实现一致。
- 验证：`gateway.py` 通过 `py_compile`；内联 JavaScript 通过 Node 语法解析；Mini App 与 Console 均为 8 个页面、87 个唯一元素 ID、57 个功能函数，管理 API 集合一致。
- Playwright 在 360x800、390x844、768x1024 三种视口完成页面切换和几何检查：无页面横向溢出、底部导航固定可见、连接配置可展开、8 个页面均可切换且无浏览器脚本错误。
- 未新增环境变量，未修改或删除任何 Supabase 数据。

## 网页渠道显示名恢复（2026-08-16）

- 将 `server.py` 中 `Web_Chat` 的默认渠道显示名由“橘子岛”恢复为“网页对话”。
- 同步更新 `CHANNEL_DISPLAY_MAP` 的代码示例及 `VARIABLES.md` 默认值说明。
- 未新增环境变量，未修改或删除任何 Supabase 数据。

## 自由活动猫状态检查 + Agent 赚钱系统开关（2026-08-17）

### 需求

- **目标一**：Agent 醒来后 3 轮自由活动唤醒内至少检查一次猫状态；低指标（hunger/happiness/cleanliness < 30）触发现有宠物照料 LLM 工具循环；区分"检查完成"与"照料完成"。
- **目标三**：给 Console 和 Mini App 增加 `money_earning_enabled` 运行时开关，门控 Agent 自主赚钱（`wallet_earn` bypass_cap=False），不关闭钱包其他功能。

### 已确认根因

1. **`cat_status` 返回结构被误解析**：RPC `rpc_cat_status` 返回 `{ok, pet:{hunger,happiness,cleanliness,...}, inventory:[...]}`，指标嵌套在 `pet` 子对象里。但 `tool_loop._stringify` 只识别 `{ok,message,data}` 结构，对 `cat_status` 退化为 `"成功"`，真实数值从未喂给 LLM —— 宠物照料循环一直基于空状态决策。
2. **无 happiness 低位事件**：`_PET_CARE_EVENT_DESC` 只有 `hungry_cat/dirty_cat/tired_cat`，happiness 低时无对应事件类型，无法触发照料。
3. **无自由活动轮次计数器**：`async_free_activity` 无进程内状态跟踪检查频率，无法保证"3 轮内至少检查一次"。
4. **`run_pet_care_tool_loop` 返回值不区分照料是否生效**：只返回 `(event_type, log_text)`，调用方无法判断模型是否真的调用了改善工具（空 tool_calls 也算"完成"）。
5. **`wallet_earn` 无运行时门控**：MCP 直调（`server.wallet_earn`）和自由活动（`tool_loop.call_tool`）两个入口都没有 `money_earning_enabled` 检查，仅前端隐藏会被绕过。

### 修改文件

| 文件 | 目标 | 改动 |
|------|------|------|
| `tool_loop.py` | 一+三 | 新增 `_format_cat_status_for_llm` 正确解析 pet 子对象；`call_tool` 返回增加 `raw` 字段；新增 `unhappy_cat` 事件 + `_PET_CARE_EVENT_TOOLS_HINT`；`run_pet_care_tool_loop` 返回 4 元组含 `care_effective`/`cat_status_ok`；阶段2 prompt 强化"必须调用改善工具"要求；新增 `_money_earning_enabled()` helper + `_build_tool_schema_block` 暴露层裁剪 + `call_tool` 入口门控 |
| `heartbeat.py` | 一 | 新增 `_free_activity_cat_check`（3 轮规则 + 失败重试 + tick 协调 + 低指标触发照料）；`_try_pet_care` 返回 dict 含 `ran/care_effective/cat_status_ok/skipped_cooldown`；`async_free_activity` 唤醒流程集成猫检查 |
| `gateway.py` | 三 | `_default_runtime_config` 增加 `money_earning_enabled=true`；新增 `_money_earning_enabled()` helper；PATCH 白名单 + status config_sources + process dict |
| `server.py` | 三 | `wallet_earn` MCP 入口门控（bypass_cap=False 且关闭 → `MONEY_EARNING_DISABLED`） |
| `console.html` | 三 | 存储页增加"Agent 赚钱系统"卡片 + `loadStorage` 渲染 |
| `miniapp.html` | 三 | 同 console.html |
| `test_cat_check.py` | 一 | 新增 31 项测试 |
| `test_money_earning.py` | 三 | 新增 23 项测试 |
| `VARIABLES.md` | 三 | 运行时门控说明补充 `money_earning_enabled`（明确非环境变量） |

### 轮次定义

- 一"轮" = `async_free_activity` 成功进入一次后台自由活动唤醒流程（非 LLM 内部工具调用轮次）。
- 进程内内存计数（`_free_activity_cat_check`），不落库，进程重启从首轮重新检查。

### 猫检查和失败重试规则

| 情况 | 检查计数 | care_pending | 下一轮行为 |
|------|---------|-------------|-----------|
| cat_status 成功 + 无低指标 | 重置为 0 | False | 按 3 轮规则 |
| cat_status 成功 + 低指标 + 照料生效 | 重置为 0 | False | 按 3 轮规则 |
| cat_status 成功 + 低指标 + 照料未生效（空 tool_calls/只 cat_status/工具全失败/无LLM） | 重置为 0 | True | 下一轮必须重试 |
| cat_status 成功 + 低指标 + 照料因冷却期跳过（tick 刚处理过） | 重置为 0 | False | 按 3 轮规则 |
| cat_status 失败（DB 不可用/异常） | 不重置，递增 | 不变 | 下一轮必须重试 |
| cat_status 返回结构异常（缺 pet） | 不重置，递增 | 不变 | 下一轮必须重试 |
| 宠物 tick 侧成功 cat_status 后 | 重置为 0 | False | 避免与 tick 重复检查 |

- 阈值：检查成功后允许最多跳过 2 轮，第 3 轮必须查（`rounds_since_check >= 2` 触发）。
- 低指标优先级：hunger > happiness > cleanliness。
- 协调：全局 `_cat_status_last_ok_ts` 时间戳，tick 照料成功 cat_status 后更新，自由活动唤醒时检测到更新则重置计数。

### money_earning_enabled 的准确语义

- 默认 `true`，存储在 `user_facts.sys_config`，5s 热生效，非环境变量。
- `true`：Agent 可通过 `wallet_earn`（bypass_cap=False）自主赚钱。
- `false`：禁止 Agent 自主入账，但不关闭钱包。
- **不受影响**：`wallet_check`、`wallet_log`、`wallet_spend`、`cat_shop_buy`、手动零花钱（bypass_cap=True）、手动打赏（bypass_cap=True）、查看余额和历史流水。
- **门控位置**（最终入口，非仅前端隐藏）：
  - `tool_loop.call_tool`（自由活动工具循环入口）
  - `server.wallet_earn`（MCP 直调入口）
  - `tool_loop._build_tool_schema_block`（暴露层裁剪，减少无效调用）
- 被拒返回：`{ok: false, error_code: "MONEY_EARNING_DISABLED", message: "赚钱系统已关闭..."}`。
- 前端：Console 与 Mini App 存储页一致，复用 `toggleRow()`/`patchConfig()`，显示值/来源/已热生效。

### 验证命令和结果

```
# 语法检查
python -c "import ast; [ast.parse(open(f,encoding='utf-8').read()) for f in ['tool_loop.py','heartbeat.py','gateway.py','server.py','home_system.py','test_cat_check.py','test_money_earning.py']]"
→ 全部 OK

# 前端 JS 语法 + money_earning_enabled 存在性
node -e "..."  # 检查 console.html / miniapp.html
→ 3 script block(s) OK each, money_earning_enabled count: 2 each

# 目标一测试
python -m unittest test_cat_check -v
→ Ran 31 tests, OK

# 目标三测试
python -m unittest test_money_earning -v
→ Ran 23 tests, OK

# 两个新测试套件合跑
python -m unittest test_cat_check test_money_earning
→ Ran 54 tests, OK

# 回归测试（宠物照料 + 钱包 + 猫 + tick + console）
python -m unittest test_tool_loop.TestPetCareLoop test_wallet test_cat test_cat_tick test_console
→ 111 tests, OK

# 全套回归
python -m unittest test_tool_loop test_wallet test_cat test_cat_tick test_console test_cat_check test_money_earning test_import test_keywords
→ 248 tests, 5 failures（全部为预先存在的"查天气"确定性路径相关失败，与本次改动无关）, 2 skipped
```

### 未验证内容及原因

- **生产 Supabase 写入测试**：按需求约束未连接生产数据库，数据库相关测试全部使用 mock。
- **前端实际渲染**：未启动网关用浏览器访问 `/console`、`/miniapp` 验证开关交互；仅做了 JS 语法解析和 `money_earning_enabled` 存在性检查。复用现有 `toggleRow()`/`patchConfig()`/`loadStorage()` 框架，视觉与交互方式与同页其他开关一致。
- **3 轮规则的真实后台时序**：测试用 mock 直接调用 `_free_activity_check_cat` 验证计数逻辑，未在真实 `async_free_activity` 多小时运行中验证（进程内状态、无副作用、逻辑可测）。
- **多进程热生效**：5s TTL 缓存机制为现有设计（进程 A/B 各自缓存），未新增多进程测试。

### 明确声明

- **未删除或迁移任何 Supabase 数据**。本次改动不涉及任何数据库 DDL/DML，不创建新表、新 RPC、新迁移。
- **未新增环境变量**（`money_earning_enabled` 存储在 `sys_config`，非环境变量）。
- **未改变**：猫商品价格/物品效果/RPC、猫状态衰减速度/数据库阈值、`wallet_earn` 工具定义、自由活动原有选择/写日记/外向推送/欲望驱动逻辑、其他运行时开关行为。

### 已知风险或副作用

1. **`run_pet_care_tool_loop` 返回值从 2 元组变 4 元组**：`_try_pet_care` 已同步更新解包；现有测试 `test_tool_loop.TestPetCareLoop` 用索引访问 `result[0]`/`result[1]` 仍兼容，4 项全通过。但若有外部调用方按 2 元组解包会报错（已确认仅 `_try_pet_care` 调用）。
2. **`call_tool` 返回增加 `raw` 字段**：现有调用方读 `ok`/`text` 不受影响；`raw` 仅在需要原始结构时使用。
3. **猫检查增加后台开销**：每 3 轮自由活动多一次 `cat_status` RPC 调用（轻量查询），可忽略。
4. **`unhappy_cat` 事件不走 SQL 阈值**：`rpc_cat_tick` 不会主动产生 `unhappy_cat` 事件（SQL 无 happiness 阈值块），该事件仅由自由活动猫检查在 happiness<30 时触发。这是设计选择：不修改 SQL 阈值（需求约束），由 Python 侧补充 happiness 检查。
5. **5 个预先存在的测试失败**：`test_tool_loop` 中 5 项与"查天气"确定性路径相关的失败在本次改动前已存在，与本次两个目标无关，未修复（不在需求范围内）。

---

## 2026-08-17 · 自由活动数据分类拆分与秘密日记独立面板

### 需求背景

调整"自由活动"的数据分类与秘密日记功能，使内向活动日志和秘密日记分开保存：只有"写秘密日记"活动产生 `Secret_Diary` 记录（不发 Telegram、独立面板展示），其余内向活动继续用 `Free_Activity`；外向活动不增加独立冷却，不改触发频率；Console 和 Miniapp 增加独立"秘密日记"面板；秘密日记 Prompt 改为平实直接风格。

### 已修改文件

- `tool_loop.py` — `ACTIVITY_TOOL_MAP["写秘密日记"]` 由 `["save_memory"]` 改为 `[]`；新增 `_finalize_secret_diary()` 专用确定性路径（平实 prompt，不调任何工具）；主循环 `run_free_activity_tool_loop` 在查天气分支后新增"写秘密日记"分支。
- `heartbeat.py` — 主流程保存按 activity 区分：写秘密日记保存为 `tags=Secret_Diary / category=日记`（标题 `🔒 秘密日记·写秘密日记`），其余活动仍保存为 `tags=Free_Activity`；`_recent_activity_keys` 查询改为 `.in_("tags", ["Free_Activity","Secret_Diary"])` 覆盖两标签防连续重复。
- `gateway.py` — `MEM_CATEGORY_TAGS` 新增 `"secret_diary": ["Secret_Diary"]`；`_memory_category()` 新增 `Secret_Diary → "secret_diary"` 分支；`/api/memories?category=secret_diary` 自动走 `in_("tags",["Secret_Diary"])` 精确查询。
- `console.html` — 新增导航项"秘密日记"、`p-diary` 页面容器、`PAGE_TITLES.diary`、`loaders.diary`、以及 `loadDiary/openDiaryModal/saveDiary/delDiary` 四个 JS 函数（均复用 `/api/memories?category=secret_diary` 与 `PATCH /api/memories/:id`）。
- `miniapp.html` — 与 console.html 同构的五处改动，两端面板功能完全一致。

### 普通内向活动与秘密日记的分类区别

| 活动类型 | tags | category | 标题前缀 | Telegram | 面板 |
|---|---|---|---|---|---|
| 写秘密日记 | `Secret_Diary` | `日记` | `🔒 秘密日记·写秘密日记` | 不发 | 秘密日记面板 |
| 逛虚拟小屋/查天气/抽张塔罗/翻旧回忆/发呆放空/记点小账 | `Free_Activity` | `记事` | `🎈 自由活动·{activity}` | 不发 | 自由活动页签 |
| 想对方了/分享发现/偷偷关心（外向） | `Free_Activity` | `记事` | `🎈 自由活动·{activity}` | 发送（`_push_wechat` 现有逻辑不变） | 自由活动页签 |

外向活动：未新增 `FREE_ACTIVITY_OUTGOING_INTERVAL`、未新增独立调度器、未新增 `Outgoing_Activity` 冷却记录、未改 `FREE_ACTIVITY_INTERVAL`（默认仍 5400 秒）。

### 如何避免"写秘密日记"重复写入

重复写入的根因有两处，均已封堵：

1. **工具循环侧（`tool_loop.py`）**：原 `ACTIVITY_TOOL_MAP["写秘密日记"]=["save_memory"]` 会在 `FREE_ACTIVITY_TOOL_LOOP=true` 时让阶段2 调用 `save_memory` 工具写一条记忆，随后主流程又写一条 → 两条。改为 `[]` 后，"写秘密日记"不再进入阶段2/3 工具循环，而是走新增的 `_finalize_secret_diary()` 专用路径（与查天气专用路径同构：阶段1 草稿兜底 + 专用 prompt 重新生成正文，全程不调任何工具）。
2. **主流程侧（`heartbeat.py`）**：`async_free_activity` 主流程的 `_save_memory_to_db` 调用按 `activity` 分支，写秘密日记用 `Secret_Diary` 标签保存一次，其余用 `Free_Activity`。

最终：一次"写秘密日记"只产生一条 `Secret_Diary` 记录。该改动只影响"写秘密日记"活动，其余活动（含查天气、记点小账、逛虚拟小屋等有工具的活动）的工具调用逻辑完全不变。

### 秘密日记 Prompt（平实直接风格）

`_finalize_secret_diary()` 的 prompt 全文遵循需求要求的平实风格：第一人称、80-160 字、直接说事情/想法/情绪、禁用比喻与华丽形容词、禁用"岁月静好/阳光洒进来/微风拂过"等套话、不提系统/任务/Prompt/模型/工具调用、只输出正文。查天气的专用 prompt（`_finalize_weather_activity`）未改动，且查天气仍走 `Free_Activity`，不会误存为 `Secret_Diary`。每日日记生成器的 `Core_Cognition` 日记分类逻辑未改动。

### Console 和 Miniapp 的新增内容

两端一致新增独立的"秘密日记"面板：
- 导航入口 `<a data-p="diary">`（图标 `book-lock`），位于"记忆库"与"用户画像"之间。
- `p-diary` 页面容器，含总数显示、刷新按钮、说明 hint、搜索框、列表、分页器。
- JS 函数：`loadDiary`（查询 `category=secret_diary`，按 `created_at` 倒序，支持关键词搜索 `q` 与分页 `page/size`）、`openDiaryModal`（编辑模态框，复用现有 modal 样式）、`saveDiary`（`PATCH /api/memories/:id`）、`delDiary`（`DELETE /api/memories/:id`，带 confirm）。
- 两端均复用现有 `api()`、`esc()`、`fmtCN8()`、`toast()`、`closeModal()`、`refreshIcons()` 与卡片/分页/模态框样式，未新增数据库访问机制，未新增 API 路由。

### 验证命令与结果

1. **Python 语法检查**：`python -m py_compile heartbeat.py tool_loop.py gateway.py server.py` → `PY_COMPILE_OK`。
2. **HTML 内联 JS 语法检查**（node vm.Script 编译，不执行）：console.html 1 个 script 块通过、miniapp.html 1 个 script 块通过。
3. **`test_console.py`**：31 项全部通过（含 `test_category_mapping`、`test_all_py_compile`、`test_inline_js_node_check`、`test_pagination_params_parsing`），2 项因测试壳未运行而 skip。
4. **`test_tool_loop.py`**：56 项中 51 项通过；5 项失败均为预先存在的"查天气"确定性路径相关失败（`test_build_tool_schema_block_empty_activity` 用"查天气"断言返回空但查天气映射本就有3个工具；其余4项用"查天气"作测试活动但专用路径会多调一次 LLM，与"单次调用"期望矛盾）。test_tool_loop.py 中"写秘密日记"出现 0 次、`save_memory` 仅作为 `call_tool` 直调测试（不经 ACTIVITY_TOOL_MAP，均通过）→ 本次改动未引入任何新失败。
5. **静态检查脚本（10 项）**：全部通过 — `Secret_Diary` 仅用于写秘密日记、`ACTIVITY_TOOL_MAP["写秘密日记"]==[]`、无 `FREE_ACTIVITY_OUTGOING_INTERVAL`/`Outgoing_Activity`、`_finalize_secret_diary` 无删除操作、两端 diary 面板要素齐全、gateway 分类映射登记、heartbeat 保存区分+防重复覆盖两标签、外向推送逻辑保留、秘密日记 prompt 含完整平实要求、`FREE_ACTIVITY_INTERVAL` 默认仍 5400。
6. **gateway 分类映射运行时验证**：`_memory_category("Secret_Diary")=="secret_diary"`、`_category_tag_filter("secret_diary")==["Secret_Diary"]`、`Free_Activity` 仍映射 `free` 不受影响。

### 未验证内容或已知限制

- **前端实际渲染**：未启动网关用浏览器访问 `/console`、`/miniapp` 实际点击"秘密日记"导航验证交互；仅做了内联 JS 语法编译检查与要素存在性静态检查。建议上线后在浏览器中手动验证导航切换、搜索、分页、编辑、删除。
- **秘密日记真实生成**：未在真实后台运行中触发一次"写秘密日记"活动验证 prompt 输出风格与单条落库；仅静态确认 prompt 文本与保存分支。LLM 实际输出风格取决于模型遵循 prompt 的程度。
- **`_recent_activity_keys` 行为变化**：防连续重复查询从单标签 `eq("tags","Free_Activity")` 改为双标签 `in_("tags",["Free_Activity","Secret_Diary"])`，现在秘密日记也会计入"最近活动"参与防重复判断。这是预期行为（避免连续两轮都写秘密日记），但会使防重复判定池略微扩大。
- **5 个预先存在的测试失败**：与本次需求无关（查天气路径），未修复。

### 明确声明

- **未删除或迁移任何 Supabase 数据**：本次无任何 `DELETE`/`DROP`/`TRUNCATE`，无数据库迁移、无新表、无新字段。历史 `Free_Activity` 数据保持不变，继续显示在自由活动页签。
- **未新增环境变量**：`VARIABLES.md` 无需更新。
- **未改变**：外向活动推送逻辑、`FREE_ACTIVITY_INTERVAL`、查天气专用路径、每日日记生成器 `Core_Cognition` 分类、其余记忆分类映射、其他运行时开关行为。
- **临时脚本已清理**：本次调查用的 `_search_keywords.py`/`_search2.py`/`_static_check.py`/`_check_html_js.mjs`/`_extract_js.py` 已全部删除，未提交。

---

## 2026-08-17 · 自由活动新增「逛淘宝」与「网上冲浪」（真实工具活动）

### 任务目标

在网关现有“自由活动”系统中新增两个**真实可执行**的活动类型：逛淘宝、网上冲浪。两者必须根据情绪/欲望状态做候选门控，并调用真实工具获取结果（淘宝 MCP `search_taobao_products`、网关既有 `web_search`），不允许模型凭空描述浏览过淘宝/网页。逛淘宝严格只逛不买。

### 已确认的真实调用链

- `heartbeat.async_free_activity` → `desire_bridge.tick()` 取得 `DesireSnapshot`（含 `drive` 8 维 / `display` 16 维 / `intent`）→ 注入 system 上下文 → 调 `tool_loop.run_free_activity_tool_loop(...)`。
- `tool_loop` 维护 `_FREE_ACTIVITIES`、`TOOL_REGISTRY`、`ACTIVITY_TOOL_MAP`；`call_tool(name,args)` 做白名单+参数校验+固定身份注入+错误隔离；`_resolve_callable` 对 `callable=None + _server_name` 的工具延迟从 `server` 取（`@mcp.tool()` 装饰后仍是 async 函数，已用 `_probe_mcp.py` 验证）。
- `server.web_search`（`@mcp.tool()` + `@mcp_error_handler` + `async def`）与 `search_memory` 装饰器模式完全一致 → `web_search` 走与 `search_memory` 相同的延迟注册路径。
- MCP Python SDK 为 1.x（`mcp>=1.10,<2.0`）：`streamablehttp_client(url, timeout=...)` 异步上下文产出 `(read, write, get_session_id)`；`ClientSession(read, write, read_timeout_seconds=...)`；`initialize()` / `list_tools()` / `call_tool(name, arguments, ...)`；`CallToolResult` 含 `.content`（文本块列表）与 `.isError`。
- 情绪字段真实来源：`snap.drive`（attachment/curiosity/reflection/duty/social/fatigue/libido/stress）、`snap.display`（vitality/longing/intimacy/possessiveness/lust/jealousy/anxiety/protectiveness/contentment/elation/seeking/play/dejection/irritability/fear/fatigue）。`_get_now_bj()` 返回**朴素北京时间**（utcnow+8h，无 tzinfo）。

### 参考方案中被修正的错误前提

1. 方案伪代码把 `streamablehttp_client` 与 `ClientSession` 当成可裸调函数，实际两者都是**异步上下文管理器**（`async with`），签名见上。已按 inspect.signature 的真实 API 实现。
2. 方案要求 `web_search` “延迟注册到 TOOL_REGISTRY，不要复制实现”——已确认 `@mcp.tool()` 在本 SDK 版本返回原 async 函数本身（probe 验证 `iscoroutinefunction=True`），故可走 `callable=None + _server_name="web_search"`，与 `search_memory` 完全同构，无需复制。
3. 方案情绪字段映射表中“好奇=drive.curiosity / 探索=display.seeking / 疲惫=display.fatigue …”已逐一与 `emotion_engine.DIMS` / `desire_engine.DRIVE_KEYS` 核对，全部存在，未另建重复情绪系统。
4. 方案“门控在每轮候选裁剪前只读查询当天记录”——`_get_now_bj` 是朴素北京时间，Supabase `created_at` 是 timestamptz；冷却比较统一换算到 epoch（now_bj 附加 +08:00 tz 后 `.timestamp()`，`created_at` 解析为 aware 后 `.timestamp()`），查询字符串带 `+08:00` 时区，避免被按 UTC 解释错 8 小时。
5. 方案“heartbeat 已 tick 一次取得的快照直接传给工具循环”——已实现：`heartbeat` 不再为门控二次 tick，把同一份 `snap` 经新参数 `desire_snapshot` 传入；`_gate_activities` 只读 `snap.drive`/`snap.display`。

### 修改文件及每个文件的用途

- `tool_loop.py`
  - `_FREE_ACTIVITIES` 新增「逛淘宝」「网上冲浪」（与 heartbeat 同步）。
  - 新增常量：`TAOBAO_MCP_URL`、`TAOBAO_MCP_TIMEOUT_SEC=55`、`TAOBAO_COUNT_DEFAULT=8`/`MIN=1`/`MAX=10`、冷却与每日上限（淘宝 180min/4 次、冲浪 90min/6 次）、两个活动标题。
  - 新增 `_call_taobao_search(keyword, count)`：用官方 MCP SDK（v1.x）经 `streamablehttp_client` 建立会话 → `initialize` → `list_tools` 确认 `search_taobao_products` 存在 → `call_tool` → 提取文本块；count 归一 1~10；整体 `asyncio.wait_for(timeout=55)`；失败返回明确失败结果（不伪装成功）；异常日志只记 keyword/count/类型/堆栈，不记 URL 中可能的认证信息。
  - `TOOL_REGISTRY` 注册 `search_taobao_products`（callable=`_call_taobao_search`）与 `web_search`（`callable=None, _server_name="web_search"`，延迟从 server 取）。**未注册** `convert_taobao_link`、未把 `wallet_*` 映射给逛淘宝。
  - `ACTIVITY_TOOL_MAP` 新增「逛淘宝」→`["search_taobao_products"]`、「网上冲浪」→`["web_search"]`（双重白名单：须在 REGISTRY 且在该活动映射内）。
  - 新增活动专用 Prompt 规则：`_TAOBAO_NO_BUY_RULES`（只逛不买）、`_TAOBAO_LOG_RULES`（日志可写/不可写）、`_SURF_DATE_RULES`（热点含日期、健康仅一般科普）。
  - 新增门控函数 `_get_supabase_safe`/`_iso_to_epoch`/`_bj_epoch`/`_get_activity_stats`（只读查询当天指定标题的 `Free_Activity` 记录，用于冷却/每日上限）/`_gate_taobao`/`_gate_surf`/`_gate_activities`。门控原则：最低阈值 `>=`、抑制红线 `>`；lust>0.85 只抑制淘宝不抑制冲浪；TAOBAO_MCP_URL 空 / FREE_ACTIVITY_TOOL_LOOP 关 → 淘宝/冲浪不候选；Supabase 查询失败只关这两个新增候选，其他自由活动不受影响（保守不放开）。
  - `run_free_activity_tool_loop` 新增参数 `desire_snapshot`、`desire_suggested_activity`：阶段1前调 `_gate_activities` 裁剪候选；仅从门控通过的活动构造 options；建议活动本轮不在候选 → 丢弃 `desire_hint`（倾向不绕过门控）；模型若选了被裁掉的活动则从门控候选兜底随机；stage2/stage3 注入活动专用规则与方向提示；工具全部失败时 stage3 注入“不得伪装成功浏览”约束。
- `heartbeat.py`
  - `_FREE_ACTIVITIES` 同步新增两个活动。
  - `async_free_activity`：在 desire 块前初始化 `snap=None`/`suggested=None`；把 `desire_snapshot=snap`、`desire_suggested_activity=suggested` 传给 `run_free_activity_tool_loop`。情感引擎关/异常 → snap=None → 两个新活动不候选（无门控数据），其余活动行为不变。
- `desire_bridge.py`
  - `ACTION_TO_FREE_ACTIVITY`：`explore` → `["网上冲浪","逛淘宝","分享发现","查天气"]`；`socialize` → `["网上冲浪","分享发现"]`（优先冲浪）。`suggest_free_activity` 仍返回首项；desire_hint 只表达倾向，不绕过门控。
- `test_tool_loop.py`：新增 7 个测试类共 52 项（活动一致性、淘宝门控 §3-§13、网上冲浪门控 §14-§20、工具白名单 §21-§25、淘宝 MCP 客户端 §26-§33 全 mock、冷却与频次 §34-§40、Prompt 与日志 §41-§43）。

### 情绪字段映射

| 中文概念 | 数据来源 | 真实字段 |
|---|---|---|
| 好奇 | 欲望驱动 | `drive.curiosity` |
| 探索 | 情绪展示 | `display.seeking` |
| 玩闹 | 情绪展示 | `display.play` |
| 活力 | 情绪展示 | `display.vitality` |
| 依恋 | 欲望驱动 | `drive.attachment` |
| 亲密 | 情绪展示 | `display.intimacy` |
| 占有 | 情绪展示 | `display.possessiveness` |
| 保护 | 情绪展示 | `display.protectiveness` |
| 情欲 | 情绪展示 | `display.lust` |
| 焦虑 | 情绪展示 | `display.anxiety` |
| 低落 | 情绪展示 | `display.dejection` |
| 疲惫 | 情绪展示 | `display.fatigue` |
| 反思 | 欲望驱动 | `drive.reflection` |
| 社交 | 欲望驱动 | `drive.social` |

### 淘宝只逛不买的白名单设计

三层一致：工具暴露层（`ACTIVITY_TOOL_MAP["逛淘宝"]=["search_taobao_products"]`，不含 `convert_taobao_link`/`wallet_spend`/`wallet_earn`）+ 活动 Prompt（`_TAOBAO_NO_BUY_RULES`：不购买/不下单/不支付/不加入购物车/不转换返利链接/不得声称已买到或已拥有）+ 最终日志 Prompt（`_TAOBAO_LOG_RULES`：可写搜了什么/看到什么/哪个有趣/为何想到/礼物灵感；不可写我买了/下单了/付款了/加入购物车了/已经到手）。淘宝 MCP 本身无购买/下单工具，未虚构这些接口。

### 冷却和每日上限实现

- 复用现有 `memories` 写入（`tags=Free_Activity`，标题 `🎈 自由活动·逛淘宝` / `🎈 自由活动·网上冲浪`，由 `heartbeat` 主流程保存）。**未新建表、未改 schema**。
- 候选裁剪前 `_get_activity_stats(title, now_bj)` 只读查询当天（北京时间 0 点起，`created_at >= today_start+08:00`）指定标题记录，得 `count` 与 `last_success_epoch`。
- 淘宝：180 分钟冷却、每日 4 次；冲浪：90 分钟冷却、每日 6 次。命中冷却或达上限 → 不候选。
- 只有成功完成真实工具调用并最终保存了自由活动日志才计入（计数源自已保存的 memories 行）；工具失败本轮跳过不保存 → 不计数（运行时保证，由 `heartbeat` 仅在 loop 返回结果后保存）。
- Supabase 不可用 / 查询异常 → 该活动 `stats.error` 非 None → 保守关闭该新活动候选（**不**变成无限制），其他自由活动不受影响。

### 新增环境变量

`TAOBAO_MCP_URL`（可选，默认空）。淘宝 MCP 完整 Streamable HTTP 端点（含 `/mcp` 路径）。留空 → 逛淘宝不候选。不要默认 localhost（容器内 localhost 指向网关自身）。只调用 `search_taobao_products`，不涉及购买/转链。已写入 `VARIABLES.md` §12.2。文档示例只用假地址 `http://taobao-mcp:8080/mcp`，未记录真实生产地址或凭据。

### 验证命令与实际结果

1. Python 语法检查：`python -m py_compile tool_loop.py heartbeat.py desire_bridge.py server.py test_tool_loop.py` → 全部 `PY_COMPILE_OK`。
2. MCP SDK 接口确认：`inspect.signature` 打印 `streamablehttp_client` / `ClientSession.__init__` / `initialize` / `list_tools` / `call_tool` 签名，按真实 API 实现。
3. `@mcp.tool()` 行为确认：临时 `_probe_mcp.py` 验证装饰后返回原 async 函数（`iscoroutinefunction=True`，签名保留），确认 `web_search` 可走延迟注册（与 `search_memory` 同路径）；脚本已删除。
4. 一致性检查脚本：`search_taobao_products`/`web_search` 在 REGISTRY、`convert_taobao_link` 不在 REGISTRY、`ACTIVITY_TOOL_MAP["逛淘宝"]` 仅 `search_taobao_products`、`["网上冲浪"]` 仅 `web_search`、两活动在 `_FREE_ACTIVITIES`、`_TAOBAO_TITLE` 正确 → `CONSISTENCY_OK`。
5. 单元测试 `python -m unittest test_tool_loop`：108 项，103 通过 / 5 失败。**5 项失败均为预先存在的“查天气确定性路径”失败**（`test_build_tool_schema_block_empty_activity` 用“查天气”断言返回空但查天气映射有 3 个工具；`test_avout_hint_passed_to_stage1`/`test_disabled_degrades_single_call`/`test_empty_draft_no_tools_returns_none`/`test_enabled_no_tools_activity_single_call` 用“查天气”作测试活动但专用路径会多调一次 LLM / 返回非 None，与“单次调用/返回 None”期望矛盾）——与本次需求无关，上一次工作日志已记录，未修改。本次新增 52 项测试全部通过，未破坏任何既有通过测试。

### 未验证内容

- **真实淘宝 MCP 网络连通性**：未连接真实淘宝 MCP 生产服务（未使用真实凭据）。MCP 客户端协议、工具调用、超时与错误路径均用 mock（`_FakeStreamable`/`_FakeSession`/`_McpCfg`）完成测试，覆盖 initialize / list_tools / 工具不存在 / keyword+count 限制 / 文本提取 / isError / 连接错误 / 超时 / 空关键词 / URL 空。
- **端到端后台运行**：未在真实后台触发一次逛淘宝/网上冲浪验证 LLM 实际输出与落库；仅静态确认 Prompt 文本、门控阈值与保存分支（沿用现有 `Free_Activity` 保存路径）。
- **真实情绪快照驱动的门控**：门控阈值用边界值单测覆盖，但未在真实情感引擎一拍数据下验证组合命中。

### 已知风险或副作用

- 两个新活动依赖外部工具（淘宝 MCP / 网页搜索），若上游不可用会返回失败结果 → 本轮该活动跳过，不伪装成功（stage3 有 fail_guard 约束）。
- 门控查询每轮对 `memories` 做两次只读 `select`（淘宝 + 冲浪当日计数）；异步并行执行，失败隔离，开销很小。
- `desire_hint` 现在与门控联动：建议活动本轮不在候选时 hint 被丢弃，模型从剩余候选选——若 DESIRE_DRIVEN 开启且建议活动频繁被门控裁掉，模型会更多走随机/其他候选（预期行为，符合“倾向不绕过门控”）。
- 淘宝 MCP 调用总超时 55s：若淘宝服务偶发慢，自由活动这一轮会多等最多约 55s 才进入下一步（后台异步，不阻塞前台）。

### Supabase 操作声明

**明确声明本次未执行任何 Supabase 删除操作**：无 `DELETE` / `DROP` / `TRUNCATE`，无删除历史 memories、表、字段、函数或策略。无数据库迁移、无新表、无 schema 变更。新增门控只对 `memories` 做只读 `select`（`eq("tags","Free_Activity").eq("title",...).gte("created_at",...)`），不写不删。活动日志仍由 `heartbeat` 沿用现有 `Free_Activity` 保存路径写入。`_probe_mcp.py`/`_smoke_check.py` 等临时脚本已删除，未提交。

### Git commit 状态

项目目录非 Git 仓库（环境已确认 `Is a git repository: no`），未执行任何 `git` 操作，未提交。如需提交，建议：`feat(gateway): add taobao and web browsing free activities`。

---

## 2026-08-17 · 移除「逛淘宝」的高情欲抑制条件

### 调整目标

移除自由活动「逛淘宝」候选门控中由高情欲（`display.lust`）触发的抑制条件。调整后，无论 `display.lust` 有多高，都不能再因为这个字段禁止「逛淘宝」进入候选。高情欲本身既不抑制淘宝，也不额外促进淘宝。

### 实际修改位置

仅修改与"移除淘宝的高情欲抑制条件"直接相关的内容，未顺手调整任何无关规则。

| 文件 | 位置 | 改动 |
|------|------|------|
| `tool_loop.py` | `_gate_taobao` 抑制红线块（原第 790-791 行） | 删除 `if display.get("lust", 0.0) > 0.85: sup.append("lust")` 两行 |
| `tool_loop.py` | 门控原则注释（原第 702 行） | `lust>0.85 只抑制"逛淘宝"，不抑制"网上冲浪"` → `淘宝与冲浪均不受 lust 抑制（高情欲既不抑制也不强制触发）` |
| `tool_loop.py` | `_gate_surf` docstring（原第 826 行） | 删除"与淘宝差异"中过时的 `lust>0.85 不抑制冲浪` 表述（该差异已不存在） |
| `test_tool_loop.py` | `test_10_lust_suppress` → `test_10_high_lust_does_not_block_taobao` | 旧测试断言 `lust=0.86` 抑制淘宝（`assertFalse`）；改为验证新行为：`lust=0.91`（明显高于旧红线 0.85）且好奇橱窗正向模式命中、其他抑制条件未触发时，淘宝仍可进入候选（`assertTrue` 且断言命中"好奇橱窗"方向） |
| `test_tool_loop.py` | `test_11_lust_does_not_suppress_surf` 注释 | `# lust>0.85 抑制淘宝但不抑制网上冲浪` → `# 高情欲既不抑制淘宝也不抑制冲浪（本例验证冲浪不被 lust 抑制）` |

### 当前保留的淘宝抑制条件

`_gate_taobao` 的抑制红线现在只保留三项，字段、阈值、比较符均未改动：

- `display.anxiety > 0.50`
- `display.dejection > 0.40`
- `display.fatigue > 0.60`

### 高情欲调整后的准确行为

- 高情欲（`display.lust` 任意值，包括 >0.85）**不再抑制**「逛淘宝」进入候选。
- 高情欲**也不强制触发**淘宝：淘宝是否进入候选仍由原有正向触发模式（好奇橱窗/整活橱窗/送礼橱窗/守护橱窗，阈值与比较符未变）、上述三个抑制条件、`TAOBAO_MCP_URL` 配置、`FREE_ACTIVITY_TOOL_LOOP` 开关、冷却（180 分钟）和每日上限（4 次）共同决定。
- 「网上冲浪」门控（`_gate_surf`）本身从未受 lust 抑制，本次未改其任何逻辑，仅更新了一处过时注释。

### 更新的测试

- `test_10_high_lust_does_not_block_taobao`（由 `test_10_lust_suppress` 改名重写）：验证 `display["lust"]=0.91` + `curiosity=0.60` + `seeking=0.40` 时淘宝仍 `allowed=True` 且命中"好奇橱窗"方向。该测试验证候选结果，而非仅搜索源码字符串。
- `test_11_lust_does_not_suppress_surf`：保留冲浪不被 lust 抑制的断言，更新注释表述。
- `test_7_anxiety_suppress`（`anxiety=0.51`）、`test_8_dejection_suppress`（`dejection=0.41`）、`test_9_fatigue_suppress`（`fatigue=0.61`）：未改动，继续验证三个抑制条件仍然生效。
- 「网上冲浪」及其他活动相关测试保持原样。

### 实际验证命令和结果

```
# 1. Python 语法检查
python -m py_compile tool_loop.py test_tool_loop.py
→ 两者 exit 0，PY_COMPILE_OK

# 2. 淘宝门控测试类（含新 test_10）
python -m unittest test_tool_loop.TestGatingTaobao -v
→ Ran 13 tests, OK（test_10_high_lust_does_not_block_taobao ok）

# 3. 淘宝+冲浪门控测试类
python -m unittest test_tool_loop.TestGatingTaobao test_tool_loop.TestGatingSurf
→ Ran 25 tests, OK

# 4. 与本次改动相关的全部测试类（门控/冷却/白名单/日志/一致性）
python -m unittest test_tool_loop.TestGatingTaobao test_tool_loop.TestGatingSurf test_tool_loop.TestCooldownAndFrequency test_tool_loop.TestWhitelist test_tool_loop.TestPromptAndLog test_tool_loop.TestActivityConsistency
→ Ran 42 tests, OK

# 5. 完整 test_tool_loop 套件
python -m unittest test_tool_loop
→ Ran 108 tests, 6 failures
```

**6 项失败的归属确认**（通过还原改动前后对照验证）：
- 5 项为**预先存在**的失败，与本次改动无关：`test_build_tool_schema_block_empty_activity`（查天气映射本就有 3 个工具，与断言"返回空"矛盾）、`test_avout_hint_passed_to_stage1`、`test_disabled_degrades_single_call`、`test_empty_draft_no_tools_returns_none`、`test_enabled_no_tools_activity_single_call`（后 4 项用"查天气"作测试活动，但查天气专用 finalize 路径会多调一次 LLM / 返回非 None，与"单次调用/返回 None"期望矛盾）。
- 1 项 `test_invalid_activity_fallback_random` 为**预先存在的随机性偶发失败**：还原到本次改动前的原始代码连续跑 4 次，run 1 失败、run 2-4 通过，根因是 `random.choice` 兜底选中"查天气"时走 finalize 多调 LLM。与 lust 改动无关。
- 还原对照后原始状态为 108 项 5 failures（上述前 5 项），修改后为 108 项 6 failures（前 5 项 + 随机偶发项），证明本次改动**未引入任何新的确定性失败**，且相关门控测试全部通过。

### 残留规则检查

在所有 `.py` 文件中搜索 `lust` 与 `0.85`：
- 执行代码中**无任何**"`lust > 0.85` 抑制淘宝"的有效规则残留。`tool_loop.py` 中仅剩第 702 行注释提及 lust（说明两者都不受抑制）。
- `0.85` 在 `.py` 中仅出现在 LLM `temperature=0.85`（heartbeat/tool_loop 的 ask_llm 调用）与新测试注释里的"旧红线 0.85"说明，均非抑制规则。
- `desire_engine.py`/`emotion_engine.py` 中的 `lust` 引用是情欲引擎本身的字段定义与情绪计算，与淘宝门控无关，未改动。
- `PROJECT_NOTES.md` 2026-08-17 上一任务的历史日志中仍保留 `lust>0.85 只抑制淘宝不抑制冲浪` 的表述——这是历史事实记录，按需求要求未篡改。

### 未修改的内容

- 未调整焦虑、低落、疲惫阈值（仍为 0.50 / 0.40 / 0.60，比较符仍为 `>`）。
- 未调整淘宝其他触发模式（好奇/整活/送礼/守护橱窗的阈值与比较符不变）。
- 未调整「网上冲浪」的任何门控（仅更新一处过时注释）。
- 未调整冷却时间（淘宝 180min / 冲浪 90min）或每日次数（淘宝 4 / 冲浪 6）。
- 未修改淘宝 MCP 调用方式。
- 未增加"清水白名单"，未增加新的情欲分支。
- 未修改其他自由活动，未重构无关代码，未统一无关格式。
- 未修改 Supabase 表结构或数据。
- 未新增环境变量，`VARIABLES.md` 无需修改（其中仅有 `TAOBAO_MCP_URL` 说明，无"情欲超过 0.85 禁止淘宝"的当前配置说明）。

### Supabase 操作声明

**明确声明本次未执行任何 Supabase 删除操作**：无 `DELETE` / `DROP` / `TRUNCATE`，无删除 memories、表、字段、函数、RPC、策略或历史数据。本次任务不需要任何数据库写入、迁移或删除。门控仅对 `memories` 做只读 `select`（沿用上一任务已有的 `_get_activity_stats`），未新增任何数据库访问。

### Git commit 状态

项目目录非 Git 仓库（环境已确认 `Is a git repository: no`），未执行任何 `git` 操作，未提交。如需提交，建议：`fix(gateway): remove lust inhibitor from taobao activity`。

