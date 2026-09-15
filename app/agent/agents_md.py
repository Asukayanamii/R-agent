"""
项目约定（AGENTS.md / CLAUDE.md）：分层发现，注入系统提示词。

同一目录里只取一个文件，优先级 `AGENTS.override.md` → `AGENTS.md` → `CLAUDE.md`：
override 顶掉同目录另外两个，其他目录照常层叠（这是 Pi 的规则，用来给"这个仓库里临时
换成另一套约定"留个口子，而不用去改大家都提交的 AGENTS.md）。

加载顺序是**外 → 内**，越靠近工作区越靠后：

1. 全局：`~/.my_agent/AGENTS.md`
2. 祖先链：从工作区根向上，到**含 `.git` 的目录为止**（收下它再停）
3. 工作区根

顺序本身就是信号——通用的在前、具体的在后，离得近的那份更占优，不需要模型自己揣摩
谁覆盖谁。向上遍历可以用 `AGENTS_ANCESTORS=0` 关掉（只读全局与工作区根）。

**内容直接进提示词**，不像技能那样按需读取：约定是每轮都该生效的短文本，让模型自己判断
"要不要读"只会漏。代价是它每轮都占 token，所以单文件超过 `MAX_BYTES` 会截断并标注。
"""

import logging
from pathlib import Path

from app.config import AGENTS_ANCESTORS, CONFIG_DIR, PROJECT_ROOT

logger = logging.getLogger(__name__)

CANDIDATES = ("AGENTS.override.md", "AGENTS.md", "CLAUDE.md")
"""单个目录里的候选文件，靠前的优先。"""

MAX_BYTES = 64 * 1024
"""单个约定文件的注入上限。约定是给人写短指令用的，超过这个量级多半放错了地方。"""

MAX_FILES = 8
"""最多注入几份约定（全局 + 祖先链）。纯粹的保险丝：真撞上说明目录层级有问题。"""

CACHE: dict[Path, tuple[int, int, str]] = {}
"""`路径 → (mtime_ns, size, 正文)`。热路径只 stat，内容没变不重读。"""


def _read_text(path: Path) -> str:
    """读文件内容。IO 集中在这里，探针可以替掉它数读取次数。"""
    return path.read_text(encoding="utf-8")


def _pick(directory: Path) -> Path | None:
    """取该目录里优先级最高的那一个约定文件。"""
    for name in CANDIDATES:
        candidate = directory / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _content(path: Path) -> str | None:
    """读约定文件（带缓存与截断）。读不了返回 None，不让它把这一轮对话带崩。"""
    try:
        stat = path.stat()
    except OSError:
        return None

    cached = CACHE.get(path)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2] or None

    try:
        raw = _read_text(path)
    except OSError as exc:
        logger.warning("约定文件读不了，已跳过 %s：%s", path, exc)
        return None

    if len(raw.encode("utf-8")) > MAX_BYTES:
        # 截断必须说出来（沿用工具输出那条约定），否则表现是"约定莫名少了一半"
        raw = raw.encode("utf-8")[:MAX_BYTES].decode("utf-8", "ignore")
        raw += f"\n\n[已截断：文件超过 {MAX_BYTES // 1024}KB，只注入前半部分]"
        logger.warning("约定文件超过 %dKB，已截断注入：%s", MAX_BYTES // 1024, path)

    CACHE[path] = (stat.st_mtime_ns, stat.st_size, raw)
    return raw


def _chain(start: Path) -> list[Path]:
    """
    从工作区根往上的目录链，**外 → 内**排列。

    遇到含 `.git` 的目录就收下它然后停：那是仓库边界，再往上（用户主目录、盘根）
    跟这个项目没关系了。`.git` 可能是目录也可能是文件（worktree / submodule），
    所以用 `exists()` 而不是 `is_dir()`。
    """
    if not AGENTS_ANCESTORS:
        return [start]

    directories = [start]
    current = start
    while not (current / ".git").exists():
        parent = current.parent
        if parent == current:  # 盘根
            break
        current = parent
        directories.append(current)
    return list(reversed(directories))


def load_agents_files(workspace: str | Path | None = None) -> list[tuple[Path, str]]:
    """
    按层加载约定文件，返回 [(路径, 正文)]（外 → 内）。

    路径用解析后的绝对路径去重：工作区正好在全局目录里、或者祖先链与全局指向同一个文件时，
    同一份约定只该注入一次。
    """
    root = Path(workspace) if workspace else PROJECT_ROOT
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    for directory in (Path.home() / CONFIG_DIR, *_chain(root)):
        if len(found) >= MAX_FILES:
            logger.warning("约定文件超过 %d 份，后面的不再注入：%s", MAX_FILES, directory)
            break
        path = _pick(directory)
        if path is None:
            continue
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        content = _content(path)
        if content is None or not content.strip():
            continue
        seen.add(key)
        found.append((path, content))

    if found:
        logger.debug(
            "注入项目约定 %d 份：%s", len(found), "、".join(str(p) for p, _ in found)
        )
    return found


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_project_context(files: list[tuple[Path, str]]) -> str:
    """
    渲染成一段提示词。

    路径写进标签里：模型与用户都能看出"这条约定来自哪一份文件"，而多份约定冲突时
    （外层说用 A、内层说用 B）这也是唯一的线索。
    """
    if not files:
        return ""

    lines = ["\n\n## 项目约定"]
    for path, content in files:
        lines.append(f'<project_instructions path="{_escape(path.as_posix())}">')
        lines.append(content.strip())
        lines.append("</project_instructions>")
    return "\n".join(lines)
