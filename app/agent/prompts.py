"""
系统提示词。

单独一个模块是为了**破环**：图要用它（每轮拼在最前面），压缩也要用它（估算压缩后的视图
有多大），谁都不该 import 对方。提示词的改动（人格、工具选择策略）集中在这里。

拼装是**按工作区**的：项目约定（AGENTS.md）与技能索引都随工作区变，所以对外的入口是
`build_system_prompt(workspace)`；下面那个常量仍是第一段，本模块只负责拼接，不碰图、
不碰模型。
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from app.agent.agents_md import load_agents_files, render_project_context
from app.agent.skills import load_skills, render_skills_block
from app.common.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是一个在用户指定工作区里干活的编程助手。"
    "bash 与文件类工具的工作目录都是当前工作区根目录；"
    "工作区之外的位置需要用户授权才能访问。"
    "需要查看代码、跑测试或执行命令时用工具，不要编造工具返回的内容。"
    "回答用中文，简洁准确。\n"
    "\n"
    "工具选择：文件操作的活儿优先用专用工具，它们带行号、分页与截断保护，输出也更规整——\n"
    "- 读文件用 read，别用 cat / head / tail / sed -n\n"
    "- 按内容搜用 grep，按名字找用 find，看目录用 ls；别拿 bash 里的同名命令代替，"
    "更别把几条拼成一行（多件事的输出会混在一起，也没法分页）\n"
    "- 新建或整体覆盖用 write，改动片段用 edit（精确替换）；别用 echo >/tee/sed -i\n"
    "- bash 留给它真正擅长的：跑测试与构建、git、装依赖、进程与服务，"
    "以及需要管道或多步组合的活儿\n"
)


@dataclass(frozen=True)
class ContextSettings:
    """静态上下文注入的全部旋钮（由 `builtin/context` 插件的配置提供）。

    默认值 = 改造前行为；`from_app_config()` 引用总体配置（`.env`）——
    所以"改一处"的习惯（改 .env）照旧，而配置的**所有权**在插件手里。
    """

    skills_enabled: bool = True
    skills_dir: str = ''
    agents_md_enabled: bool = True
    ancestors: bool = True

    @classmethod
    def from_mapping(cls, mapping: dict | None) -> 'ContextSettings':
        """把插件配置（已过 schema 校验）归一化成设置对象。"""
        options = dict(mapping or {})
        return cls(
            skills_enabled=bool(options.get('skills_enabled', True)),
            skills_dir=str(options.get('skills_dir') or ''),
            agents_md_enabled=bool(options.get('agents_md_enabled', True)),
            ancestors=bool(options.get('ancestors', True)),
        )

    @classmethod
    def from_app_config(cls) -> 'ContextSettings':
        """引用总体配置（.env）。"""
        from app.common import config

        return cls(
            skills_enabled=config.SKILLS_ENABLED,
            skills_dir=config.SKILLS_DIR,
            agents_md_enabled=config.AGENTS_MD_ENABLED,
            ancestors=config.AGENTS_ANCESTORS,
        )


def render_static_context(
    workspace: str | None = None, settings: ContextSettings | None = None
) -> str:
    """
    静态上下文块：项目约定（AGENTS.md，外→内）＋ 可用技能索引。

    这一段由服务 `context`（builtin/context 插件）负责注入——插件卸下就不再注入，
    属于"功能退化"而不是错误。`settings` 不传则引用总体配置。
    """
    settings = settings or ContextSettings.from_app_config()
    root = Path(workspace) if workspace else PROJECT_ROOT
    parts: list[str] = []

    if settings.agents_md_enabled:
        context = render_project_context(load_agents_files(root, ancestors=settings.ancestors))
        if context:
            parts.append(context)

    if settings.skills_enabled:
        block = render_skills_block(load_skills(root, global_dir=settings.skills_dir))
        if block:
            parts.append(block)

    return "".join(parts)


def build_system_prompt(workspace: str | None = None, *, static: str | None = None) -> str:
    """
    拼出这一轮发给模型的 system 提示词。

    顺序固定：人格与工具策略 → 项目约定（AGENTS.md，外→内）→ 可用技能索引 → 当前工作区。
    **稳定内容在前、易变内容在后**（压缩摘要由 `with_summary` 追加在最末），所以同一个
    工作区里只有"约定或技能真的变了"才会动前缀，其余时候前缀缓存照常命中。

    `static` 是静态上下文块：图内由服务 `context` 提供（传进来），图外的调用
    （如压缩估算）不传，本模块自己渲染。workspace 为空表示会话没绑工作区，
    按应用所在目录处理——与沙箱的兜底值一致。
    """
    root = Path(workspace) if workspace else PROJECT_ROOT
    block = render_static_context(workspace) if static is None else static
    return "".join([SYSTEM_PROMPT, block, f"\n\n当前工作区：{root.as_posix()}"])
