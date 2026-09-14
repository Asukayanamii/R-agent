"""
路径沙箱：工具执行前检查目标路径，必要时用 interrupt 征求用户授权。

分层与 Pi 的社区沙箱 pi-sandbox 一致——**进程内能拦的用检查加提示，拦不住的交给 OS**：

- `read` / `write` / `edit` / `ls` / `find` / `grep` 走这套检查
- `bash` **不在覆盖范围内**：一条 `cd /` 就出去了。真隔离只能来自 OS 或容器，
  进程内检查做不到。别把这里的限制误当成安全边界。

**信任边界是工作区**（`app.agent.runtime.current_workspace`），不是会话。
同一个工作区的所有对话共享同一份授权；换工作区就是换了一层边界。

三种结果：

1. 工作区内且未命中禁区  → 直接放行
2. 命中 deny 列表        → **硬阻断，永不提示**。绝对禁区不能靠"点允许"绕过
3. 工作区外              → **interrupt 询问**，四个选项见 `OPTIONS`

授权有三层来源，逐层查找：

| 层 | 存哪 | 生效范围 |
| --- | --- | --- |
| 本次运行 | 内存 | 本工作区，进程重启即失效 |
| 工作区 | `<工作区>/.my_agent/sandbox.json` | 本工作区，持久 |
| 全局 | `~/.my_agent/sandbox.json` | 所有工作区，持久 |

授权只存在内存与上述文件里，**agent 读不到也改不了**——边界必须对 agent 不透明，
否则它会学着去探测它。
"""

import json
import logging
import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from langgraph.types import interrupt

from app.agent.runtime import current_workspace, workspace_relative
from app.config import PROJECT_ROOT
from app.exceptions import SandboxDenied

logger = logging.getLogger(__name__)

READ = "read"
WRITE = "write"

CONFIG_DIR = ".my_agent"
CONFIG_FILE = "sandbox.json"

# 绝对禁区：命中即硬阻断，永不提示。规则按"最坏情况"设，宁可多拦。
#
# 读写列表刻意不完全相同：
# - 读没有 `.env.*`，因为 `.env.example` 这类模板是给人看和抄的
# - 写多了 `.env.*`，因为 `.env.production` 之类的真实配置不该被改
#
# `data/` 同时进两个列表：那里面是应用自己的会话库，能读就能 grep 出别的会话内容。
# `.git/` 同理——`config` 里的 remote URL 可能带 token。
_SECRETS = (".env", "*.pem", "*.key", "*id_rsa*")
DENY_READ = _SECRETS + (".git", ".git/*", "data", "data/*")
DENY_WRITE = _SECRETS + (".env.*", ".git", ".git/*", "data", "data/*")

REFUSE = "拒绝"
ALLOW_ONCE = "允许（本次运行有效）"
REMEMBER_WORKSPACE = "记住（仅此工作区）"
REMEMBER_GLOBAL = "记住（所有工作区）"
OPTIONS = (REFUSE, ALLOW_ONCE, REMEMBER_WORKSPACE, REMEMBER_GLOBAL)

MODE_TEXT = {READ: "读取", WRITE: "写入"}


@dataclass
class _Grant:
    """一组已授权的路径。"""

    read_paths: set[Path] = field(default_factory=set)
    write_paths: set[Path] = field(default_factory=set)

    def covers(self, path: Path, mode: str) -> bool:
        # 写权限隐含读权限，与 pi-sandbox 一致
        roots = self.write_paths if mode == WRITE else self.read_paths | self.write_paths
        return any(path == root or root in path.parents for root in roots)


def _workspace() -> Path:
    return current_workspace.get()


def _label(path: Path) -> str:
    return workspace_relative(path)


def _definition_file() -> Path:
    return Path.home() / CONFIG_DIR / CONFIG_FILE


def _workspace_file() -> Path:
    return _workspace() / CONFIG_DIR / CONFIG_FILE


def _load(path: Path) -> _Grant:
    """从配置文件读取已记住的授权。文件不存在或损坏都不该让工具崩掉。"""
    grant = _Grant()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return grant
    if not isinstance(raw, dict):
        return grant
    for key, target in (("allow_read", grant.read_paths), ("allow_write", grant.write_paths)):
        for item in raw.get(key) or []:
            if isinstance(item, str) and item.strip():
                target.add(Path(item).expanduser().resolve())
    return grant


def _save(path: Path, grant: _Grant) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "allow_read": sorted(item.as_posix() for item in grant.read_paths),
        "allow_write": sorted(item.as_posix() for item in grant.write_paths),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


_memory: dict[Path, _Grant] = {}
_global: _Grant | None = None


def _global_grant() -> _Grant:
    global _global
    if _global is None:
        _global = _load(_definition_file())
    return _global


def _workspace_grant(workspace: Path) -> _Grant:
    """工作区授权 = 内存里本次运行加的 + 文件里记住的。"""
    grant = _memory.get(workspace)
    if grant is None:
        grant = _load(workspace / CONFIG_DIR / CONFIG_FILE)
        _memory[workspace] = grant
    return grant


def _covers(path: Path, mode: str) -> bool:
    return _workspace_grant(_workspace()).covers(path, mode) or _global_grant().covers(
        path, mode
    )


def _denied(path: Path, mode: str) -> bool:
    patterns = DENY_WRITE if mode == WRITE else DENY_READ
    relative = _label(path)
    return any(
        fnmatch(relative, pattern) or fnmatch(path.name, pattern)
        for pattern in patterns
    )


def _remember(path: Path, mode: str, scope: str) -> None:
    """把授权写进对应层的配置文件。"""
    if scope == REMEMBER_GLOBAL:
        grant = _global_grant()
    else:
        grant = _workspace_grant(_workspace())
    (grant.write_paths if mode == WRITE else grant.read_paths).add(path)
    target = _definition_file() if scope == REMEMBER_GLOBAL else _workspace_file()
    _save(target, grant)


def absolute(raw: str) -> Path:
    """把参数路径绝对化。相对路径按**工作区**解析，不是按应用所在目录。"""
    candidate = Path(raw.strip() or ".").expanduser()
    workspace = _workspace()
    return (candidate if candidate.is_absolute() else workspace / candidate).resolve()


def guard_path(raw: str, mode: str) -> Path:
    """
    解析并检查路径。允许则返回绝对路径，否则抛 SandboxDenied。

    只有在**工作区之外**才会询问用户；工作区内的受保护路径是硬阻断。
    """
    target = absolute(raw)
    workspace = _workspace()

    if _denied(target, mode):
        logger.warning("沙箱硬阻断：%s %s", MODE_TEXT[mode], _label(target))
        raise SandboxDenied(
            f"{_label(target)} 是受保护路径，不允许{MODE_TEXT[mode]}。"
            "该限制是硬性的，无法通过授权绕过。"
        )

    if target == workspace or workspace in target.parents:
        # 工作区内：沙箱的常规路径，不打 INFO，免得把日志淹掉
        logger.debug("工作区内放行：%s %s", MODE_TEXT[mode], _label(target))
        return target

    if _covers(target, mode):
        logger.debug("命中已有授权：%s %s", MODE_TEXT[mode], _label(target))
        return target

    logger.info(
        "请求授权：%s %s（工作区 %s）",
        MODE_TEXT[mode],
        target.as_posix(),
        workspace.as_posix(),
    )
    try:
        choice = interrupt(
            {
                "prompt": (
                    f"Agent 想{MODE_TEXT[mode]}工作区之外的路径：\n{target.as_posix()}\n"
                    f"当前工作区：{workspace.as_posix()}"
                ),
                "options": list(OPTIONS),
            }
        )
    except RuntimeError as exc:
        # 不在图的执行上下文里（直接调工具、跑测试、将来的 CLI），没地方弹卡片。
        # 这种情况下只能拒绝，并且要把原因说清楚，而不是漏一个 RuntimeError 出去。
        logger.warning("不在会话中，无法征求授权，直接拒绝：%s", target.as_posix())
        raise SandboxDenied(
            f"{target.as_posix()} 在当前工作区之外，"
            "且当前不在会话中，无法征求授权，已拒绝。"
        ) from exc

    if choice == REFUSE:
        logger.info("用户拒绝：%s %s", MODE_TEXT[mode], target.as_posix())
        raise SandboxDenied(f"用户拒绝了{MODE_TEXT[mode]} {target.as_posix()}")

    if choice == REMEMBER_GLOBAL:
        _remember(target, mode, REMEMBER_GLOBAL)
        logger.info("用户授权并记住（所有工作区）：%s %s", MODE_TEXT[mode], target.as_posix())
    elif choice == REMEMBER_WORKSPACE:
        _remember(target, mode, REMEMBER_WORKSPACE)
        logger.info("用户授权并记住（本工作区）：%s %s", MODE_TEXT[mode], target.as_posix())
    else:
        # 允许（本次运行有效）：只放进内存，不落盘
        entry = _workspace_grant(workspace)
        (entry.write_paths if mode == WRITE else entry.read_paths).add(target)
        logger.info("用户授权（本次运行有效）：%s %s", MODE_TEXT[mode], target.as_posix())

    return target
