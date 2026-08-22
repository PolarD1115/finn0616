# Zeabur 部署指南

> 本文档描述如何在 Zeabur 平台上部署 Finn0616 MCP 网关。

---

## 快速部署

1. 在 Zeabur 控制台创建新项目
2. 关联 GitHub 仓库
3. 配置环境变量（见下方）
4. 部署命令：`python server.py`

---

## 环境变量

详见 [VARIABLES.md](VARIABLES.md)。最小可运行配置：

```env
PORT=10000
API_SECRET=your-random-secret
CHAT_API_KEY=sk-xxxxxxxx
CHAT_MODEL_NAME=abab6.5s-chat
```

---

## 数据库迁移

所有迁移均为非破坏性、向后兼容：

```bash
# 阶段 1：小屋/小钱包基础 schema
migrations/20240811_001_home_system_schema.sql

# 阶段 2：小钱包 RPC
migrations/20240811_002_wallet_rpc.sql

# 阶段 3：有状态小屋 RPC
migrations/20240811_003_house_rpc.sql

# 阶段 4：小满猫商店 RPC
migrations/20240811_004_cat_rpc.sql

# 阶段 5：后台 tick + 自动收入
migrations/20240811_005_cat_tick.sql
```

迁移特点：
- 幂等可重复执行
- 无 DELETE / DROP / TRUNCATE
- 旧数据完全保留
