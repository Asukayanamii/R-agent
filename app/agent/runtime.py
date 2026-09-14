"""
工具执行期的运行上下文。

工作区（也就是信任边界）由调用方按每次运行传入，但工具函数拿不到 RunnableConfig，
所以由 `_run_tools` 在调用工具前把它放进 ContextVar，沙箱从这里读。

用 ContextVar 而不是全局变量：它是 async-safe 的，多个会话并发跑不会串。
"""

from contextvars import ContextVar
from pathlib import Path

from app.config import PROJECT_ROOT

current_workspace: ContextVar[Path] = ContextVar(
    "current_workspace", default=PROJECT_ROOT
)


def workspace_relative(path: Path) -> str:
    """
    展示用：相对当前工作区的路径，统一用正斜杠。

    Windows 的反斜杠在模型输出里容易被当成转义符，也不跨平台，
    所以对外一律用 POSIX 分隔符；回传时 Path 两种都认。

    工具的输出与沙箱的拒绝信息都用它，避免同一个路径在两边长得不一样。
    """
    workspace = current_workspace.get()
    try:
        return path.relative_to(workspace).as_posix() or "."
    except ValueError:
        return path.as_posix()
