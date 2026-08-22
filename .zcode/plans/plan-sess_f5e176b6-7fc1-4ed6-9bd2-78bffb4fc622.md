# 在 console + miniapp 模型设置界面加思考开关

## 决策结论（已确认）
- **开关形态**：三态 `auto` / `on` / `off`，字段名 `thinking`，缺省 `"auto"`
- **实现范围**：前端 UI + 后端注册表存储 + 实际请求注入，保存后真实生效
- **映射逻辑已查证**（见下表，基于各厂商 2025-2026 官方文档）

## 后端厂商映射逻辑（已查证修正）

新增工具函数 `_thinking_params(model_name, setting)`，按上游真实模型名自动映射。**好消息：GLM / Kimi K2.x / DeepSeek 三家同款格式，可合并判断。**

| 模型名特征（小写匹配） | `on` 注入 | `off` 注入 | 文档来源 |
|---|---|---|---|
| `deepseek` / `v4-flash` / `v4-pro` | `{"thinking":{"type":"enabled"}}` | `{"thinking":{"type":"disabled"}}` | api-docs.deepseek.com |
| `glm`（4.5+，含 4.6/4.7/5.x） | `{"thinking":{"type":"enabled"}}` | `{"thinking":{"type":"disabled"}}` | docs.bigmodel.cn |
| `kimi-k2`（含 k2.5/k2.6/k2.7） | `{"thinking":{"type":"enabled"}}` | `{"thinking":{"type":"disabled"}}` | platform.kimi.com |
| `kimi-k3` / `k3`（思考常开） | `{"reasoning_effort":"max"}` | `{"reasoning_effort":"low"}` | platform.kimi.com |
| `qwen3` / `qwq` | `{"enable_thinking":true}` | `{"enable_thinking":false}` | help.aliyun.com |
| `o1`/`o3`/`o4`/`gpt-5`（OpenAI 推理系） | `{"reasoning_effort":"medium"}` | `{"reasoning_effort":"minimal"}` | platform.openai.com |
| `claude` / 其他未知 | 不传（避免报未知参数错） | 不传 | — |

> `auto` 一律不传参，走模型默认。raw HTTP 下这些字段直接作为 JSON 顶层 key 塞进 `req_data`。
> 边界情况（GLM-5.3 不能关、K2.7-code 只能开）按通用逻辑走，碰到再单独处理。

## 改动清单（3 个文件）

### 1. `gateway.py` — 注册表 schema 扩展（3 处）

**1a. GET `/api/models` 序列化**（约 2137-2152 行）：`safe.append` 字典里加 `"thinking": m.get("thinking", "auto")`。

**1b. POST `/api/models` upsert**（约 2304-2310 行）：`entry` 字典加 `"thinking": str(payload.get("thinking", "auto")).strip() or "auto"`，校验取值 ∈ `{"auto","on","off"}`，非法降级 `"auto"`。

**1c. `_normalize_registry`**（98-150 行）：遍历 models 时把 `thinking` 规范化为三选一，非法/缺失 → `"auto"`（向后兼容旧数据）。

### 2. `gateway.py` — 新增映射函数 + 注入逻辑（2 处）

**2a. 新增 `_thinking_params(model_name, setting)`**（放在 `_resolve_model` 附近，约 237 行前）：按上表返回字典，`setting=="auto"` 或未知模型返回 `{}`。匹配逻辑用小写子串判断，优先级：deepseek > glm > kimi-k3 > kimi-k2 > qwen3 > o系 > 其他。

**2b. 主聊天流注入**（1316-1348 行附近）：
- 1316 行后补 `entry = _find_enabled_model(_load_llm_registry(), requested_model)` 拿完整 entry
- 1348 行（URL 构造完成）后、1350 行（注入上文）前，加：
  ```python
  if entry:
      _tp = _thinking_params(entry.get("model", ""), entry.get("thinking", "auto"))
      if _tp:
          req_data.update(_tp)
          _log(f"🧠 [thinking] model={entry.get('model')} setting={entry.get('thinking')} → {_tp}")
  ```
- 流式转发（1444 行 `json=req_data`）和 tool loop（1863 行 `base_payload = copy.deepcopy(req_data)`）都从 `req_data` 取参，**一次注入覆盖两条路径**，无需单独改 tool loop。

### 3. `console.html` + `miniapp.html` — 模型编辑弹窗加三态选择

两个文件改动完全一致（`openModelModal` 约 726-738 行 / 800-812 行，`saveModel` 约 740-747 行 / 814-821 行）：

**弹窗模板**在「API Key」之后、「启用」之前插入：
```html
<label>深度思考 (thinking)</label>
<div style="display:flex;gap:14px;margin-bottom:4px">
  <label style="display:inline-flex;align-items:center;gap:5px;font-size:13px">
    <input type="radio" name="mThinking" value="auto" ${(!m||m.thinking==="auto"||!m.thinking)?"checked":""} style="width:auto"/> auto 跟随默认
  </label>
  <label style="display:inline-flex;align-items:center;gap:5px;font-size:13px">
    <input type="radio" name="mThinking" value="on" ${(m&&m.thinking==="on")?"checked":""} style="width:auto"/> on 强制开启
  </label>
  <label style="display:inline-flex;align-items:center;gap:5px;font-size:13px">
    <input type="radio" name="mThinking" value="off" ${(m&&m.thinking==="off")?"checked":""} style="width:auto"/> off 强制关闭
  </label>
</div>
<div class="d" style="font-size:12px;color:var(--mute);margin-bottom:8px">后端按模型名自动映射（GLM/Kimi K2/DeepSeek→thinking.type，Qwen3→enable_thinking，o系→reasoning_effort）</div>
```

**saveModel** 的 body 加 `thinking: document.querySelector('input[name=mThinking]:checked').value`。

## 不改动的地方
- `desire_bridge.py` 的 `_thinking_extra_body` 硬编码逻辑保持不变（只管分类任务，独立路径）
- `_resolve_model` 函数签名不变（保持 4 元组），改用额外 `_find_enabled_model` 调用取 entry
- `server.py` 后台任务路径暂不改（用户需求聚焦聊天流）

## 验证方式
1. 重启网关，编辑一个 GLM 模型，看到三态选项，设为 `off` 保存，刷新确认持久化
2. 用该模型发消息，网关日志出现 `🧠 [thinking] model=glm-4.6 setting=off → {'thinking': {'type': 'disabled'}}`
3. 切 `on`，日志出现 `setting=on → {'thinking': {'type': 'enabled'}}`
4. 切 `auto`，日志不出现 thinking 行（不传参）
5. 同样验证 Kimi K2 模型（同款格式）和 Qwen3（enable_thinking 格式）

## 风险评估
- **低风险**：注册表新增字段是加法，旧数据缺 `thinking` 时 GET 返回 `"auto"`、POST 默认 `"auto"`，完全向后兼容
- **注入安全**：URL 构造后、上文注入前修改 `req_data`，不影响路由；raw HTTP 塞 JSON 顶层 key 是各厂商兼容层标准做法
- **未知模型不传参**：映射表未命中的模型三态都返回 `{}`，不会因传未知参数导致上游报错
- **三家同款格式**：GLM/Kimi K2/DeepSeek 都是 `thinking.type`，降低了映射出错概率