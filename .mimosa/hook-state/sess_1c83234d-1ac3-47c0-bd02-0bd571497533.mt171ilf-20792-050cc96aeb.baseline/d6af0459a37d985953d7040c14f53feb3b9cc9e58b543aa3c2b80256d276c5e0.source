"""
统一入口 (双进程守护 · Supervisor)
====================================
拉起并守护两个完全独立的 OS 进程：

    进程 A · 消息进程   = python server.py     (GATEWAY_ROLE=message)
        └─ MCP 工具 + /v1 代理 + QQ/TG 实时收发，端口对外提供服务
    进程 B · 后台进程   = python background.py  (GATEWAY_ROLE=background)
        └─ 主动思考 / 日记 / 总结 / 提醒 / 日程 / 邮件 / 环境热同步

守护策略 (对应架构文档「不留半残状态」):
    - 任一进程退出 (无论正常还是崩溃) → 立即终止另一个 → run.py 以非零码退出
    - 交给容器层的 restart 策略 (docker compose: restart unless-stopped) 整体重启
    - 收到 SIGTERM/SIGINT (docker stop / Ctrl-C) → 优雅终止两个子进程后退出

用法:
    python run.py            # 生产/容器入口
    python server.py         # 仅进程 A (单进程兼容模式，本地调试用)
"""

import os
import sys
import signal
import subprocess

# 与 run.py 同目录，保证在任意工作目录下都能定位到脚本
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable  # 用当前解释器 (虚拟环境/容器内一致)

# 子进程句柄，供信号处理函数访问
_children: list[subprocess.Popen] = []


def _spawn(name: str, script: str, role: str) -> subprocess.Popen:
    """启动一个子进程，注入 GATEWAY_ROLE 以区分角色。"""
    env = dict(os.environ)
    env["GATEWAY_ROLE"] = role
    print(f"▶️  启动 {name}: {PYTHON} {script}  (GATEWAY_ROLE={role})")
    return subprocess.Popen([PYTHON, os.path.join(BASE_DIR, script)], env=env)


def _terminate_all(timeout: float = 10.0):
    """优雅终止所有子进程：先 SIGTERM，超时未退再 SIGKILL。"""
    for p in _children:
        if p.poll() is None:  # 仍在运行
            try:
                p.terminate()
            except Exception:
                pass
    for p in _children:
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"⚠️  子进程 PID={p.pid} 未在 {timeout}s 内退出，强制 kill")
            try:
                p.kill()
            except Exception:
                pass


def _handle_signal(signum, frame):
    print(f"\n🛑 run.py 收到信号 {signum}，正在终止所有子进程 …")
    _terminate_all()
    sys.exit(0)


def main():
    # 转发容器/终端的停止信号，做优雅收尾
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    print("=" * 62)
    print("🚪 MCP 网关 · 双进程守护启动")
    print("=" * 62)

    # 进程 A 提供对外服务，先起；进程 B 随后
    _children.append(_spawn("进程A · 消息进程", "server.py",     "message"))
    _children.append(_spawn("进程B · 后台进程", "background.py", "background"))

    # 阻塞等待：任一子进程退出即触发整体收尾
    import time
    try:
        while True:
            for p in _children:
                ret = p.poll()
                if ret is not None:
                    print(f"❗ 子进程 PID={p.pid} 已退出 (code={ret})，"
                          f"按「不留半残」策略终止另一个进程并整体退出。")
                    _terminate_all()
                    # 非零退出码 → 容器 restart 策略会整体重启
                    sys.exit(ret if ret != 0 else 1)
            time.sleep(1.0)
    except KeyboardInterrupt:
        _handle_signal(signal.SIGINT, None)


if __name__ == "__main__":
    main()
