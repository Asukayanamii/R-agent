"""按正则搜索文件内容。"""

import fnmatch
import re

from langchain_core.tools import tool

from app.agent.sandbox import READ, guard_path
from app.agent.tools.common import fail, looks_binary, rel, walk_files
from app.exceptions import SandboxDenied


@tool
async def grep(
    pattern: str,
    path: str = ".",
    glob: str = "",
    ignore_case: bool = False,
    max_results: int = 200,
) -> str:
    """
    在文件内容里按正则搜索，返回 `文件:行号: 内容`。

    - pattern 是正则表达式
    - path 限定搜索起点（可以是单个文件）
    - glob 可按文件名过滤，例如 "*.py"
    - ignore_case 忽略大小写；命中行数超过 max_results 时截断
    - 跳过 .git、__pycache__、node_modules 等目录与二进制文件
    - **按内容搜就用这个**，别用 bash 的 grep/rg：这里结果带文件与行号、自动跳过噪音目录、
      超限会告诉你截断了，也不会把几段搜索的输出混成一片
    """
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        fail(f"正则无效：{exc}")

    try:
        target = guard_path(path, READ)
    except SandboxDenied as exc:
        fail(str(exc))
    if not target.exists():
        fail(f"路径不存在：{rel(target)}")

    candidates = [target] if target.is_file() else walk_files(target)

    hits: list[str] = []
    reached_limit = False
    for item in candidates:
        if glob and not fnmatch.fnmatch(item.name, glob):
            continue
        if looks_binary(item):
            continue
        try:
            text = item.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{rel(item)}:{number}: {line.strip()[:200]}")
                if len(hits) >= max_results:
                    reached_limit = True
                    break
        if reached_limit:
            break

    if not hits:
        return f"没有匹配 {pattern} 的内容"
    reached = "（已达上限，可能还有更多）" if reached_limit else ""
    return f"匹配 {pattern}：{len(hits)} 行{reached}\n" + "\n".join(hits)
