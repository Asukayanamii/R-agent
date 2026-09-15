"""创建或整体覆盖文件。"""

from langchain_core.tools import tool

from app.agent.sandbox import WRITE, guard_path
from app.agent.tools.common import fail, rel
from app.exceptions import SandboxDenied


@tool
async def write(path: str, content: str) -> str:
    """
    创建或整体覆盖一个文本文件。

    - 父目录不存在会自动创建
    - content 是**完整**内容，不是片段；只改一小段请用 edit，
      用 write 改片段会把其余内容整个丢掉
    - 覆盖已有文件会丢弃原有内容
    - **新建或整体覆盖就用这个**，别用 bash 的 echo >、tee、cp
    """
    try:
        target = guard_path(path, WRITE)
    except SandboxDenied as exc:
        fail(str(exc))

    if target.is_dir():
        fail(f"{rel(target)} 是目录，不能写入")

    existed = target.is_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        fail(f"写入失败：{exc}")

    size = len(content.encode("utf-8"))
    lines = len(content.splitlines())
    action = "覆盖" if existed else "创建"
    return f"已{action} {rel(target)}：{size} 字节 / {lines} 行"
