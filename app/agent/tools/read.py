"""读取文本文件，带行号，超长时保留开头并给出续读位置。"""

from langchain_core.tools import tool

from app.agent.tools.common import (
    MAX_LINES,
    looks_binary,
    rel,
    resolve_path,
    truncate_head,
)


@tool
async def read(path: str, offset: int = 1, limit: int = MAX_LINES) -> str:
    """
    读取项目内的文本文件，返回带行号的内容。

    - offset 是起始行号（从 1 开始），limit 是最多读取的行数
    - 内容过长时只保留开头，并在末尾提示下次该用哪个 offset
    - 二进制文件会被拒绝，那种情况请改用 bash
    - 返回的行号前缀仅供定位，不要写进 edit 的匹配文本里
    """
    try:
        target = resolve_path(path)
    except ValueError as exc:
        return str(exc)

    if not target.exists():
        return f"文件不存在：{rel(target)}"
    if target.is_dir():
        return f"{rel(target)} 是目录，请用 ls 查看"
    if looks_binary(target):
        return f"{rel(target)} 看起来是二进制文件，read 只处理文本"

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"读取失败：{exc}"

    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(offset))
    if start > total:
        return f"{rel(target)} 共 {total} 行，起始行 {start} 超出范围"

    window = lines[start - 1 : start - 1 + max(1, int(limit))]
    width = len(str(start + len(window) - 1))
    numbered = "\n".join(
        f"{start + index:>{width}}\t{line}" for index, line in enumerate(window)
    )

    consumed = start - 1 + len(window)
    more = f"续读用 offset={consumed + 1}" if consumed < total else ""
    header = f"{rel(target)}（共 {total} 行，显示 {start}-{consumed}）"
    return f"{header}\n{truncate_head(numbered, more)}"
