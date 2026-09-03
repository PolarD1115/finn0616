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

### Phase 5 — 旧 Pinecone assistant 混合向量隔离（2026-08-25）
**性质**：召回结果隔离，不是数据删除。旧向量仍保留在 Pinecone 中。

**生产现象**：日志显示 `legacy=4 v2=1 assistant_format=4` 和 `legacy=5 v2=0 assistant_format=5`，旧 assistant 混合向量仍大量进入模型上下文。

**实际修改**：

| 文件 | 修改 | 目的 |
|------|------|------|
| `server.py` | 新增 `_is_assistant_format()` 判断函数：检测 memory 文本中的旧角色分隔格式（`assistant:` 行首或 `\| assistant:`） | 识别旧混合向量 |
| `server.py` | 新增 `_filter_recalled_memories()` 过滤函数：过滤含 `assistant:` 格式的结果，保留 v2 安全结果和 legacy user-only 结果 | 召回结果隔离 |
| `server.py` | `search()` 在日志统计后、返回前调用 `_filter_recalled_memories()` | 所有调用方统一覆盖 |
| `test_memory_phase3.py` | 更新 `test_k_old_mixed_vector_returned` → `test_k_old_mixed_vector_filtered`：旧混合向量现在被过滤而非返回 | Phase 5 兼容 |
| `test_legacy_isolation_phase5.py` | 新增 25 个专项测试 | 过滤/保留/顺序/脱敏/回归 |

**过滤规则**：
- 过滤：memory 文本含 `assistant:` 角色分隔格式的结果（无论 v2 还是 legacy）
- 保留：v2 user 结果、legacy user-only 结果、curated memory（不含 `assistant:` 格式）
- 不匹配普通包含 "assistant" 单词的文本
- 不修改原始 result 列表

**过滤位置**：`search()` 内部，在 `_log_pinecone_recall()` 统计后、返回前。所有调用方（网页/TG/QQ/search_memory MCP 工具）自动覆盖。

**统计日志**：`🧠 记忆隔离 source=web_user input=5 kept=1 filtered_legacy_assistant=4 v2=1 legacy=4`

**空结果降级**：过滤后无安全结果时返回 `[]`，上下文使用现有"无相关深层记忆"占位，不暴露过滤机制。

**召回行为保护**：没有增加 query，没有改变 top_k、filter、namespace。结果相对顺序保持。不设置 score 阈值。

**本阶段是召回隔离，不是数据删除。旧向量仍保留在 Pinecone 中。**

**测试结果**：专项 25/25 通过；Phase 3 33/33 通过；Phase 4 20/20 通过；Phase 3.8 28/28 通过；Phase 4.1 20/20 通过；安全 17/17 通过

**Supabase 操作声明**：未修改 Supabase schema 或数据
**Pinecone 操作声明**：未操作真实 Pinecone，未删除旧向量，未修改 Pinecone query 参数

### Phase 4.1 — 修复记忆正文日志泄露与空消息上游 400（2026-08-24）
**性质**：修复生产日志确认的两个问题。

**生产现象**：
1. `search_memory` 工具结果完整正文（含用户记忆、旧 AI 回复）被打印到 Zeabur 日志
2. 上游拒绝空 assistant/空文本块，返回 400 错误（客户端带 79/81 条历史时触发）

**已确认根因**：
- `tool_loop.py:1494/1681/1879` 使用 `print(f"... {res['text'][:60]}")` 打印工具结果，`search_memory` 返回值含记忆正文
- 客户端历史消息中存在空 assistant 消息（content 为空/纯空白），上游 API 拒绝

**实际修改**：

| 文件 | 修改 | 解决的问题 |
|------|------|----------|
| `tool_loop.py` | 新增 `_safe_tool_log_text()` 函数，对 `search_memory` 工具只返回"OK（返回 N 条记忆，正文已隐藏）"，其他工具保留原截断行为；替换 3 个日志打印点 | 记忆正文日志泄露 |
| `gateway.py` | 新增 `_sanitize_outgoing_messages()` 函数：删除空 user/assistant 消息（保留带 tool_calls 的 assistant 和带 tool_call_id 的 tool）；删除多模态数组中的空白 text part；不删除 system 消息；不原地修改输入 | 上游 400 空消息错误 |
| `gateway.py` | 流式路径和 tool loop 路径在上游 `requests.post` 前调用 `_sanitize_outgoing_messages()` | 覆盖所有上游发送路径 |
| `test_sanitize_phase41.py` | 新增 20 个专项测试 | 日志脱敏、空消息清洗、tool_calls 保留、多模态、不原地修改、回归 |

**日志脱敏规则**：
- search_memory 成功 → `OK（返回 N 条记忆，正文已隐藏）`
- search_memory 失败 → `FAIL（错误正文已隐藏）`
- 其他工具 → 保留原有 60 字符截断行为
- 日志不包含：用户正文、记忆正文、Base64、user_id、vector ID、API Key

**空消息清洗规则**：
- 删除：content 为空/纯空白的 user/assistant 消息（无 tool_calls）
- 保留：带 tool_calls 的 assistant 消息（即使 content 为空）
- 保留：带 tool_call_id 的 tool 消息（即使 content 为空）
- 保留：system 消息（即使 content 为空）
- 删除：多模态数组中的空白 text part
- 保留：有效 image_url/file/audio/video 等媒体 part
- 不原地修改输入列表（返回新列表）
