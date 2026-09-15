"""列出目录内容。"""

from datetime import datetime

from langchain_core.tools import tool

from app.agent.sandbox import READ, guard_path
from app.agent.tools.common import MAX_LINES, fail, rel
from app.exceptions import SandboxDenied


@tool
async def ls(path: str = ".") -> str:
    """
    列出目录内容，目录在前，附带文件大小与修改时间。

    - path 省略时列出工作区根目录
    - 条目过多时截断
    - **看目录就用这个**，别用 bash 的 ls：这里目录排在前面、带大小与修改时间
    """
    try:
        target = guard_path(path, READ)
    except SandboxDenied as exc:
        fail(str(exc))

    if not target.exists():
        fail(f"路径不存在：{rel(target)}")
    if not target.is_dir():
        fail(f"{rel(target)} 不是目录，请用 read")

    try:
        entries = sorted(
            target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
        )
    except OSError as exc:
        fail(f"读取目录失败：{exc}")

    if not entries:
        return f"{rel(target)} 是空目录"

    limit = MAX_LINES
    shown = entries[:limit]
    rows = []
    for entry in shown:
        if entry.is_dir():
            rows.append(f"目录  {entry.name}/")
            continue
        try:
            stat = entry.stat()
            stamp = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            rows.append(f"文件  {stat.st_size:>10}  {stamp}  {entry.name}")
        except OSError:
            rows.append(f"文件  {'?':>10}  {'?':<16}  {entry.name}")

    suffix = ""
    if len(entries) > limit:
        suffix = f"\n…共 {len(entries)} 项，仅显示前 {limit} 项"
    return f"{rel(target)}/\n" + "\n".join(rows) + suffix
