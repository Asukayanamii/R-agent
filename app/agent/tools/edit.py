"""在文件里精确替换一段文本。"""

from langchain_core.tools import tool

from app.agent.sandbox import WRITE, guard_path
from app.agent.tools.common import fail, rel
from app.exceptions import SandboxDenied


def fuzzy_span(lines: list[str], wanted: list[str]) -> tuple[int, int] | None:
    """
    忽略行尾空白的连续行匹配，返回 [start, end) 行区间。

    模型给的片段常常和文件只差行尾空白。直接判"找不到"会逼它改用 write
    整文件覆盖——那比改错一处危险得多，所以这里退一步。
    """
    target = [line.rstrip() for line in wanted]
    width = len(target)
    if width == 0 or width > len(lines):
        return None
    for start in range(len(lines) - width + 1):
        if [line.rstrip() for line in lines[start : start + width]] == target:
            return start, start + width
    return None


@tool
async def edit(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """
    在文件里精确替换一段文本。

    - old_string 必须与文件内容一致（含缩进与空行），且默认要求在文件中**唯一**
    - 确实要多处一起改时传 replace_all=true
    - 找不到完全一致的片段时，会退化为忽略行尾空白的匹配
    - 只改一小段用这个，不要用 write 整文件覆盖
    - **改已有文件就用这个**，别用 bash 的 sed -i / tee：这里保留原文件的换行风格，
      也不会把没打算改的地方顺手改掉
    """
    if not old_string:
        fail("old_string 不能为空")

    try:
        target = guard_path(path, WRITE)
    except SandboxDenied as exc:
        fail(str(exc))

    if not target.is_file():
        fail(f"{rel(target)} 不是文件")

    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"读取失败：{exc}")

    # 统一按 LF 比较，写回时再还原原文件的换行风格，
    # 否则在 Windows 上一个小改动会把整个文件的行尾翻掉。
    uses_crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    pattern = old_string.replace("\r\n", "\n")
    replacement = new_string.replace("\r\n", "\n")

    strategy = "精确匹配"
    count = text.count(pattern)

    if count == 0:
        span = fuzzy_span(text.split("\n"), pattern.split("\n"))
        if span is None:
            fail(
                f"在 {rel(target)} 里找不到 old_string。"
                "请先用 read 确认原文（注意缩进与空行），或改用 write 整体覆盖。"
            )
        start, end = span
        pattern = "\n".join(text.split("\n")[start:end])
        count = text.count(pattern)
        strategy = "忽略行尾空白匹配"

    if count > 1 and not replace_all:
        fail(
            f"old_string 在 {rel(target)} 中出现 {count} 次，无法确定改哪一处。"
            "请多带些上下文使其唯一，或传 replace_all=true 全部替换。"
        )

    updated = (
        text.replace(pattern, replacement)
        if replace_all
        else text.replace(pattern, replacement, 1)
    )
    output = updated.replace("\n", "\r\n") if uses_crlf else updated

    try:
        target.write_text(output, encoding="utf-8", newline="")
    except OSError as exc:
        fail(f"写入失败：{exc}")

    places = count if replace_all else 1
    return f"已修改 {rel(target)}（{strategy}，替换 {places} 处）"
