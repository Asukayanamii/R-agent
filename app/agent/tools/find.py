"""按 glob 匹配查找文件。"""

import fnmatch

from langchain_core.tools import tool

from app.agent.tools.common import rel, resolve_path, walk_files


@tool
async def find(pattern: str, path: str = ".", max_results: int = 200) -> str:
    """
    按 glob 匹配查找文件，返回相对项目根的路径。

    - pattern 里的 * 可以跨目录，所以 "*.py" 会匹配任意层级的 py 文件
    - path 限定搜索起点
    - 结果按路径排序，超过 max_results 时截断
    """
    try:
        target = resolve_path(path)
    except ValueError as exc:
        return str(exc)
    if not target.exists():
        return f"路径不存在：{rel(target)}"

    if target.is_file():
        found = [rel(target)] if fnmatch.fnmatch(target.name, pattern) else []
        return (
            "\n".join(found) if found else f"{rel(target)} 不匹配 {pattern}"
        )

    matches: list[str] = []
    for item in walk_files(target):
        relative = item.relative_to(target).as_posix()
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(item.name, pattern):
            matches.append(rel(item))
            if len(matches) >= max_results:
                break

    if not matches:
        return f"在 {rel(target)} 下没有匹配 {pattern} 的文件"
    reached = "（已达上限，可能还有更多）" if len(matches) >= max_results else ""
    return f"匹配 {pattern}：{len(matches)} 个{reached}\n" + "\n".join(matches)
