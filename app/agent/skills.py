"""
技能（Skills）：发现、解析与渲染。

一个技能就是一个含 `SKILL.md` 的目录（或者根目录下的一个散装 `.md`）：frontmatter 给名字
与描述，正文写给模型看的步骤、脚本用法与参考资料。**进提示词的只有名字/描述/路径**，
正文由模型用 `read` 按需读取——渐进披露，和 Pi / Claude Code 同一套取舍。

**刻意不做检索**：索引全量常驻系统提示词，匹不匹配交给模型自己判断（描述是照"什么时候用它"
写的自然语言）。代价是几十个技能的索引会占一两千 token，换来的是零额外往返、零静默漏召回，
也不需要索引与嵌入模型这类基建。

两个来源，**越靠近工作区的越优先**（同名冲突时工作区赢，落败方记一条 WARNING）：

| 来源 | 路径 |
| --- | --- |
| 全局库 | `SKILLS_DIR`（默认 `~/.my_agent/skills`） |
| 项目库 | `<工作区>/.my_agent/skills/` |

热路径（每轮拼提示词都要走一遍，一轮里还会走多次）只做 `scandir` + `stat`：文件内容按
`(mtime_ns, size)` 缓存，只有真的改了才重新读。frontmatter 用 PyYAML 解析——折叠块标量
与引号会让"手写一个极简解析器"变成静默失真的来源，而静默失真正是本项目最忌讳的失败模式。
"""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config import CONFIG_DIR, SKILLS_DIR, SKILLS_ENABLED

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
"""技能正文的固定文件名。"""

SKILLS_SUBDIR = "skills"
"""技能根下的子目录名：全局与项目库都叫这个名字。"""

SKIP_DIRS = frozenset({"node_modules"})
"""扫描时跳过的目录名（隐藏目录另外按前缀跳过）。"""

MAX_DEPTH = 6
"""往下钻的层数上限。技能目录都很浅，限一层是防病态嵌套把扫描拖成目录树遍历。"""

MAX_NAME = 64
MAX_DESCRIPTION = 1024
"""frontmatter 的长度限制（Agent Skills 标准）。超了只告警、仍然加载。"""

INDEX_WARN_BYTES = 16 * 1024
"""索引渲染后的告警线：不是截断线——静默少列几个技能比多占几百 token 糟得多。"""

_NAME_RE = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)


@dataclass(frozen=True)
class Skill:
    """一个可用技能。"""

    name: str
    description: str
    path: Path
    """`SKILL.md`（或散装 `.md`）的绝对路径。**写进提示词**，模型据此去读正文。"""
    scope: str
    """来源：`项目` 或 `全局`，只用于日志与 `/skills` 列表的展示。"""
    manual_only: bool = False
    """`disable-model-invocation: true`：不进提示词索引，只能用 `/skill:<name>` 手动调用。"""

    @property
    def base_dir(self) -> Path:
        """技能目录。正文里引用的相对路径都相对它。"""
        return self.path.parent


@dataclass(frozen=True)
class _Parsed:
    """一个候选文件的解析结果（缓存的就是它）。"""

    mtime_ns: int
    size: int
    name: str = ""
    """空串表示这个文件不该成为一个技能（没 frontmatter、解析失败、缺 description）。"""
    description: str = ""
    manual_only: bool = False


_parsed: dict[Path, _Parsed] = {}

_seen_index: dict[str, str] = {}
"""每个工作区上次记录的索引签名，用来做到"变化时记一条"而不是每轮刷屏。"""

_reported_collisions: set[tuple[str, str]] = set()
"""已经报过的重名冲突（保留的路径, 被忽略的路径）。

热路径每轮都会重新扫一遍目录，不在报过之后闭嘴就会把日志刷满，
真问题反而看不见。
"""


def _read_text(path: Path) -> str:
    """读文件内容。IO 集中在这里，探针可以替掉它数读取次数。"""
    return path.read_text(encoding="utf-8")


def _frontmatter(text: str) -> tuple[dict | None, str]:
    """
    取 frontmatter 数据；返回 (数据, 错误说明)。

    没有 frontmatter 时数据为 None、错误为空串——散装 `.md` 要靠这个区别决定"静默跳过"
    还是"告警"。
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, ""
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return None, f"frontmatter 不是合法 YAML：{exc}"
    if data is None:
        return {}, ""
    if not isinstance(data, dict):
        return None, "frontmatter 不是一个键值映射"
    return data, ""


def _name_problems(name: str) -> list[str]:
    problems = []
    if len(name) > MAX_NAME:
        problems.append(f"name 超过 {MAX_NAME} 字符（{len(name)}）")
    if not _NAME_RE.match(name):
        problems.append("name 必须是 小写字母/数字/连字符，且不以连字符开头结尾")
    return problems


def _description_problems(description: object) -> list[str]:
    if not isinstance(description, str) or not description.strip():
        return ["description 缺失或为空"]
    if len(description) > MAX_DESCRIPTION:
        return [f"description 超过 {MAX_DESCRIPTION} 字符（{len(description)}）"]
    return []


def _parse(path: Path, *, loose: bool) -> _Parsed:
    """
    解析一个候选文件，结果进缓存。

    `loose=True` 表示它是根目录下的散装 `.md`：没有合法 frontmatter 就当普通文档，
    **静默**跳过（技能根里放别的说明文件是常事，每条都告警只会淹没真问题）。
    `SKILL.md` 则是显式声明的技能文件，解析不了要告警——否则表现为"技能凭空消失"。
    """
    try:
        stat = path.stat()
    except OSError:
        return _Parsed(0, 0)

    cached = _parsed.get(path)
    if cached and cached.mtime_ns == stat.st_mtime_ns and cached.size == stat.st_size:
        return cached

    result = _Parsed(stat.st_mtime_ns, stat.st_size)
    try:
        text = _read_text(path)
    except OSError as exc:
        if not loose:
            logger.warning("技能文件读不了，已跳过 %s：%s", path, exc)
        _parsed[path] = result
        return result

    data, error = _frontmatter(text)
    if error or data is None:
        if not loose:
            logger.warning(
                "技能文件没有可用的 frontmatter，已跳过 %s%s",
                path,
                f"：{error}" if error else "（缺少 --- 包裹的元数据块）",
            )
        _parsed[path] = result
        return result

    problems = _description_problems(data.get("description"))
    if problems:
        if not loose:
            logger.warning("技能缺少 description，已跳过 %s：%s", path, "；".join(problems))
        _parsed[path] = result
        return result

    # 名字缺省时的回退：目录技能用目录名，散装文件用文件名（`pdf-tools.md` → `pdf-tools`）。
    declared = data.get("name")
    name = str(declared).strip() if declared else (path.parent.name if not loose else path.stem)
    for problem in _name_problems(name):
        logger.warning("技能 %s 的名字不合规范（仍会加载）：%s", path, problem)
    description = str(data["description"])
    for problem in _description_problems(description):
        logger.warning("技能 %s 的 description 不合规范：%s", path, problem)

    result = _Parsed(
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        name=name,
        description=description,
        manual_only=data.get("disable-model-invocation") is True,
    )
    _parsed[path] = result
    return result


def _skill_from(path: Path, parsed: _Parsed, scope: str) -> Skill:
    return Skill(
        name=parsed.name,
        description=parsed.description,
        path=path,
        scope=scope,
        manual_only=parsed.manual_only,
    )


def _scan_dir(directory: Path, scope: str, depth: int = 0) -> list[Skill]:
    """
    递归找一个目录树里的技能。

    含 `SKILL.md` 的目录**就是一个技能**，不再往里钻——技能自己的 `references/` 里
    可能还有别的 `.md`，继续扫下去会把参考资料也当成技能。
    """
    skill_file = directory / SKILL_FILE
    if skill_file.is_file():
        parsed = _parse(skill_file, loose=False)
        return [_skill_from(skill_file, parsed, scope)] if parsed.name else []
    if depth >= MAX_DEPTH:
        logger.warning("技能目录嵌套超过 %d 层，不再深入：%s", MAX_DEPTH, directory)
        return []

    found: list[Skill] = []
    for entry in _entries(directory):
        if entry.is_dir():
            found += _scan_dir(Path(entry.path), scope, depth + 1)
    return found


def _scan_root(root: Path, scope: str) -> list[Skill]:
    """扫一个技能根：子目录里的技能 + 根目录下的散装 `.md`（仅直接子级）。"""
    found: list[Skill] = []
    for entry in _entries(root):
        if entry.is_dir():
            found += _scan_dir(Path(entry.path), scope)
        elif entry.name.endswith(".md") and entry.name != SKILL_FILE:
            path = Path(entry.path)
            parsed = _parse(path, loose=True)
            if parsed.name:
                found.append(_skill_from(path, parsed, scope))
    return found


def _entries(directory: Path) -> list[os.DirEntry]:
    """列目录，排序后返回；不可读就当空。"""
    try:
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda item: item.name)
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if not entry.name.startswith(".") and entry.name not in SKIP_DIRS
    ]


def _roots(workspace: Path | None) -> list[tuple[Path, str]]:
    """按优先级给出 (技能根, 作用域)。靠前的同名技能获胜。"""
    roots: list[tuple[Path, str]] = []
    if workspace is not None:
        roots.append((Path(workspace) / CONFIG_DIR / SKILLS_SUBDIR, "项目"))
    if SKILLS_DIR:
        roots.append((Path(SKILLS_DIR), "全局"))
    return roots


def _log_index(key: str, skills: list[Skill]) -> None:
    """
    索引内容变化时记一条 INFO。

    每轮拼提示词都会走到这里，所以按签名去重：**变了才记**——"注入了什么"要能看见，
    但不能每轮都刷屏。
    """
    signature = "|".join(f"{skill.name}@{skill.path}" for skill in skills)
    if _seen_index.get(key) == signature:
        return
    _seen_index[key] = signature
    names = ", ".join(f"{skill.name}（{skill.scope}）" for skill in skills[:20])
    if len(skills) > 20:
        names += f"，…… 共 {len(skills)} 个"
    logger.info("技能索引（%s）：%s", key, names or "无")


def load_skills(workspace: str | Path | None) -> list[Skill]:
    """
    加载该工作区可用的技能，按名字排序（顺序固定，提示词前缀才不会无谓地变）。

    同名冲突保留先扫到的那个（工作区优先于全局），并把两边路径都写进日志——
    "为什么改了这个技能没生效"必须能在日志里找到答案。
    """
    if not SKILLS_ENABLED:
        return []

    root = Path(workspace) if workspace else None
    by_name: dict[str, Skill] = {}
    for directory, scope in _roots(root):
        for skill in _scan_root(directory, scope):
            existing = by_name.get(skill.name)
            if existing is not None:
                pair = (str(existing.path), str(skill.path))
                if pair not in _reported_collisions:
                    _reported_collisions.add(pair)
                    logger.warning(
                        "技能重名，保留 %s 的：%s（忽略 %s 的 %s）",
                        existing.scope,
                        existing.path,
                        scope,
                        skill.path,
                    )
                continue
            by_name[skill.name] = skill

    skills = sorted(by_name.values(), key=lambda item: item.name)
    _log_index(str(root or "应用目录"), skills)
    return skills


def find_skill(workspace: str | Path | None, name: str) -> Skill | None:
    """按名字找一个技能（含 `manual_only` 的——手动调用正是它存在的意义）。"""
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    for skill in load_skills(workspace):
        if skill.name == wanted:
            return skill
    return None


def read_skill_body(skill: Skill) -> str:
    """读技能正文。给 `/skill:<name>` 用：那里要的是**当下磁盘上**的内容，不走缓存。"""
    return _read_text(skill.path)


def render_skill_invocation(skill: Skill, args: str = "") -> str:
    """
    把一次 `/skill:<name>` 渲染成用户消息（Pi 的形状）。

    放在**用户消息**里而不是系统提示词里：技能正文是这一轮的指令，不是常驻背景；
    而且它进消息不动缓存前缀——只有这一轮变长，之后的轮次照常命中缓存。
    """
    body = read_skill_body(skill).strip()
    text = (
        f'<skill name="{_escape(skill.name)}" path="{_escape(skill.path.as_posix())}">\n'
        f"{body}\n"
        f"</skill>"
    )
    if args.strip():
        text += f"\n\nUser: {args.strip()}"
    return text


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_skills_block(skills: list[Skill]) -> str:
    """
    渲染进系统提示词的技能索引。

    只给名字/描述/路径三样（渐进披露的第一层）：描述决定了模型会不会去读正文，
    所以它是**唯一**需要常驻的部分。`manual_only` 的技能整个不出现。
    """
    visible = [skill for skill in skills if not skill.manual_only]
    if not visible:
        return ""

    lines = [
        "\n\n## 可用技能",
        "任务与某个技能的描述匹配时，用 read 读取它的 SKILL.md 全文再照做；",
        "技能里引用的相对路径以 SKILL.md 所在目录为基准解析。",
        "",
        "<available_skills>",
    ]
    for skill in visible:
        lines.append("  <skill>")
        lines.append(f"    <name>{_escape(skill.name)}</name>")
        lines.append(f"    <description>{_escape(skill.description)}</description>")
        lines.append(f"    <location>{_escape(skill.path.as_posix())}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    block = "\n".join(lines)

    if len(block.encode("utf-8")) > INDEX_WARN_BYTES:
        logger.warning(
            "技能索引已占 %d 字节（%d 个技能），每轮都会进提示词："
            "考虑合并或删掉用不上的技能",
            len(block.encode("utf-8")),
            len(visible),
        )
    return block
