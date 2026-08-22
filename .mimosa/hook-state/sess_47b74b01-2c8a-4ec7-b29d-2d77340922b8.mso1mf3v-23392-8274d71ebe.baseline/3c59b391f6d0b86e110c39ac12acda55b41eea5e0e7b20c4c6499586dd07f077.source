"""
后台进程入口 (进程 B · Background Process)
==========================================
双进程架构中「没人催」的那一半：主动思考、日记、消息总结、提醒巡视、
日程小秘书、（可选）信箱巡视、环境变量热同步。

设计要点：
- 独立 OS 进程、独立事件循环、独立内存，与进程 A (server.py) 只通过 Supabase 共享状态。
- `import server` 复用其全部基础设施 (LLM 客户端 / Supabase / Pinecone / 记忆函数)，
  但**不启动** MCP HTTP 服务 (那是进程 A 的活)，因为 server.py 的服务启动都在
  `if __name__ == "__main__"` 里，import 时不会执行。
- 所有后台任务跑在同一个 asyncio 事件循环里 (heartbeat.run_background_process)，
  任一任务崩溃即整体抛出，进程非零退出 → 交给 run.py 感知并整体重启。

启动方式：
    由 run.py 拉起 (推荐)；或单独调试：GATEWAY_ROLE=background python background.py
"""

import os
import asyncio

# 自动加载 .env (本地开发)；云端由平台注入
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def main():
    # 复用 server.py 的基础设施 (仅导入，不启动其 HTTP 服务)
    import server  # noqa: F401  —— 触发 Supabase / Pinecone / LLM 客户端初始化
    from heartbeat import run_background_process

    print("=" * 62)
    print("🌙 后台进程 (进程 B) 启动中 …")
    print(f"   Supabase : {'已连接' if server.supabase else '未连接'}")
    print(f"   Pinecone : {'已启用' if server.pinecone_memory.index else '未配置'}")
    print("=" * 62)

    try:
        asyncio.run(run_background_process())
    except KeyboardInterrupt:
        print("🌙 后台进程收到中断信号，退出。")
    # 其余异常直接向上抛出 → 进程非零退出，由 run.py 整体重启


if __name__ == "__main__":
    main()
