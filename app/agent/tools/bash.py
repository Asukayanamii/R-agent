"""
bash 工具：在项目目录下执行 shell 命令。

设计参考 Pi（earendil-works/pi）等开源 coding agent 的做法：

- **流式读取输出**，而不是等进程结束——超时被 kill 时，命令此前打出的内容
  照样能拿回来。用 communicate() 会在这一刻把缓冲区整个丢掉。
- **内存有滚动上限**，命令可能吐几个 G；超出的部分从头部丢弃并计数。
- **最终输出再截断一次**（50KB / 2000 行），保留**尾部**——排错时最近的输出更有用。
- **截断要明确告知模型丢了多少**，否则它会反复执行同一条命令。
- **清洗 ANSI 转义和控制字符**，避免污染上下文。
- **超时按进程树终止**，只杀 shell 会把子进程留在后台。
- **不接 stdin**：交互式命令会一直等输入，让它直接失败比挂住好。
"""

import asyncio
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

from app.agent.runtime import current_workspace
from app.agent.tools.common import fail, truncate_tail

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
MAX_BUFFER = 1024 * 1024
READ_CHUNK = 64 * 1024

ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|[@-Z\\-_])")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

WINDOWS_BASH_PATHS = (
    Path(r"C:\Program Files\Git\bin\bash.exe"),
    Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
)


def shell_command() -> list[str]:
    """
    返回 shell 的启动命令前缀。

    Windows 上优先用 Git Bash：模型的命令是照 bash 语义写的，
    落到 cmd.exe 会到处不认（引号、管道、路径都不同）。找不到才退回 cmd。
    """
    if sys.platform != "win32":
        return ["/bin/bash", "-c"]
    for candidate in WINDOWS_BASH_PATHS:
        if candidate.exists():
            return [str(candidate), "-c"]
    return [os.environ.get("COMSPEC", "cmd.exe"), "/c"]


def clean(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


async def drain(proc: asyncio.subprocess.Process, chunks: list[bytes], stats: dict) -> None:
    """边跑边读，并把内存压在滚动窗口内。"""
    assert proc.stdout is not None
    held = 0
    while True:
        chunk = await proc.stdout.read(READ_CHUNK)
        if not chunk:
            return
        chunks.append(chunk)
        held += len(chunk)
        stats["total"] += len(chunk)
        while held > MAX_BUFFER and len(chunks) > 1:
            held -= len(chunks.pop(0))
            stats["dropped"] = stats["total"] - held


def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """终止整棵进程树。只 kill shell 本身会把子进程留在后台。"""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


@tool
async def bash(command: str, timeout_sec: int = DEFAULT_TIMEOUT) -> str:
    """
    在当前工作区下执行 shell 命令，返回合并后的 stdout/stderr 与退出码。

    用法说明：
    - timeout_sec 为超时秒数，上限 600；超时会终止整个进程树，并返回此前已产生的输出
    - 不接受交互式输入，需要确认的命令请自带 -y 之类的非交互参数
    - 输出超过 50KB 或 2000 行时只保留末尾，并在结果里注明丢弃了多少
    - 工作目录就是工作区根目录，需要切子目录请写成 `cd 子目录 && 命令`
    - **沙箱不约束本工具**：shell 能访问工作区之外的位置。别把文件工具的限制当成覆盖 bash 的边界
    """
    if not command.strip():
        fail("命令为空。")

    timeout = max(1, min(int(timeout_sec), MAX_TIMEOUT))
    shell = shell_command()
    workdir = current_workspace.get()

    if not workdir.is_dir():
        fail(f"工作区不存在或不是目录：{workdir.as_posix()}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *shell,
            command,
            cwd=str(workdir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "TERM": "dumb",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
            },
            start_new_session=sys.platform != "win32",
        )
    except OSError as exc:
        fail(f"无法启动 shell（{shell[0]}）：{exc}")

    chunks: list[bytes] = []
    stats = {"total": 0, "dropped": 0}
    reader = asyncio.create_task(drain(proc, chunks, stats))

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
    except asyncio.CancelledError:
        # 被取消（用户点了停止、连接断开）也要收干净：否则"停止"之后命令还在后台跑。
        kill_tree(proc)
        raise

    await reader

    body = truncate_tail(
        clean(b"".join(chunks).decode("utf-8", "replace")), stats["dropped"]
    )

    if timed_out:
        tail = f"\n结束前已产生的输出：\n{body}" if body.strip() else ""
        fail(f"命令超过 {timeout}s 未结束，已终止整个进程树。{tail}")

    # 非 0 退出也算"没做成"：模型与卡片都该看到失败，而不是一条普通结果。
    # 预期会返回非 0 的（grep 无匹配之类）用专门工具，别用 bash。
    failed = proc.returncode != 0
    status = f"退出码 {proc.returncode}" + ("（非 0，命令失败）" if failed else "")
    output = f"{status}\n{body}" if body.strip() else status
    if failed:
        fail(output)
    return output
