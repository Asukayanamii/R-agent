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

from app.config import PROJECT_ROOT

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
MAX_BYTES = 50 * 1024
MAX_LINES = 2000
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


def truncate_tail(text: str, already_dropped: int = 0) -> str:
    """超限时保留尾部，并说明丢了多少——模型据此才知道要换策略。"""
    lines = text.split("\n")
    dropped_lines = 0
    if len(lines) > MAX_LINES:
        dropped_lines = len(lines) - MAX_LINES
        lines = lines[-MAX_LINES:]
    out = "\n".join(lines)

    dropped_bytes = already_dropped
    encoded = out.encode("utf-8", "ignore")
    if len(encoded) > MAX_BYTES:
        dropped_bytes += len(encoded) - MAX_BYTES
        out = encoded[-MAX_BYTES:].decode("utf-8", "ignore")

    if not (dropped_lines or dropped_bytes):
        return out

    dropped = " 与 ".join(
        part
        for part in (
            f"{dropped_lines} 行" if dropped_lines else "",
            f"{dropped_bytes} 字节" if dropped_bytes else "",
        )
        if part
    )
    return (
        f"[输出过大，已丢弃开头的 {dropped}，以下仅为末尾部分。"
        f"需要完整内容请把命令输出重定向到文件后再分段读取]\n{out}"
    )


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
    在项目根目录下执行 shell 命令，返回合并后的 stdout/stderr 与退出码。

    用法说明：
    - timeout_sec 为超时秒数，上限 600；超时会终止整个进程树，并返回此前已产生的输出
    - 不接受交互式输入，需要确认的命令请自带 -y 之类的非交互参数
    - 输出超过 50KB 或 2000 行时只保留末尾，并在结果里注明丢弃了多少
    - 工作目录是项目根目录，需要切换目录请写成 `cd 子目录 && 命令`
    """
    if not command.strip():
        return "命令为空。"

    timeout = max(1, min(int(timeout_sec), MAX_TIMEOUT))
    shell = shell_command()

    try:
        proc = await asyncio.create_subprocess_exec(
            *shell,
            command,
            cwd=str(PROJECT_ROOT),
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
        return f"无法启动 shell（{shell[0]}）：{exc}"

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

    await reader

    body = truncate_tail(
        clean(b"".join(chunks).decode("utf-8", "replace")), stats["dropped"]
    )

    if timed_out:
        tail = f"\n结束前已产生的输出：\n{body}" if body.strip() else ""
        return f"命令超过 {timeout}s 未结束，已终止整个进程树。{tail}"

    status = f"退出码 {proc.returncode}"
    if proc.returncode != 0:
        status += "（非 0，命令失败）"
    return f"{status}\n{body}" if body.strip() else status
