"""builtin/loop：骨架插件（``role="base"``）——"现在的图"本身。

    START → stamp → [entry] → compact → [pre_model] → agent → [post_model]
                                                                ├─（有 tool_calls）→ tools → [post_tools] → compact
                                                                └─（无 tool_calls）→ [finish] → END

方括号里是 5 个入站锚点：空着就是透传节点，挂上片段就把锚点的出边改接到链尾（合成器做）；
`post_model` 在路由之前，条件边的源就是它（或它上面那条链的链尾）——**每次模型输出后**都经过。

它承诺 `output_seams`（出站接缝表），协议插件依赖的是那张表，不是整张图。
**必需插件**：可替换、不可停用——换一种 loop 就是换一个插件，缺了启动就失败。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END as GRAPH_END
from langgraph.graph import add_messages
from langgraph.prebuilt import tools_condition

from app.agent.compaction.runtime import COMPACT_NODE
from app.agent.nodes import make_compact_node, make_model_node
from app.agent.tool_calls import run_tools
from app.agent.tools import TOOLS
from app.harness.fragment import (
    COMPACTION,
    END_NODE,
    HARNESS,
    MESSAGES,
    OUTPUT_SEAMS,
    ROLE_BASE,
    STAMP_INTERFACE,
    START_NODE,
    USAGE_ANCHOR,
    ChannelSpec,
    EdgeSpec,
    Fragment,
    NodeSpec,
)

PLUGIN_ID = 'builtin/loop'

ANCHORS = ('entry', 'pre_model', 'post_model', 'post_tools', 'finish')
"""入站锚点表（各锚点的触发频率见设计稿 §2.4）：换一种 loop = 换一张锚点表。"""


async def _passthrough(state: dict) -> dict:
    """空锚点的实现：透传（什么都不写）。要产出就挂片段，别改骨架。"""
    return {}


def apply(ctx: Any) -> None:
    """按注入的服务组装骨架片段，贡献给结构接缝。"""
    # 绑一次工具（与改造前一致）；阶段 D 会把 tools 抽象化，改成每次调用前对实时快照 bind。
    model_with_tools = ctx.model.bind_tools(TOOLS)

    fragment = Fragment(
        id=PLUGIN_ID,
        role=ROLE_BASE,
        anchors=ANCHORS,
        output_seams=OUTPUT_SEAMS,
        nodes=(
            # stamp 只声明位置，实现由装配器注入（结构戳要带本版本的 id 与哈希）
            NodeSpec('stamp', interface=STAMP_INTERFACE, writes=(HARNESS,)),
            NodeSpec('entry', run=_passthrough),
            NodeSpec(
                COMPACT_NODE,
                run=make_compact_node(ctx.summary_model),
                reads=(MESSAGES, USAGE_ANCHOR),
                writes=(COMPACTION, USAGE_ANCHOR),
            ),
            NodeSpec('pre_model', run=_passthrough),
            NodeSpec(
                'agent',
                run=make_model_node(model_with_tools),
                reads=(MESSAGES, COMPACTION),
                writes=(MESSAGES, USAGE_ANCHOR),
            ),
            NodeSpec('post_model', run=_passthrough),
            NodeSpec('tools', run=run_tools, reads=(MESSAGES,), writes=(MESSAGES,)),
            NodeSpec('post_tools', run=_passthrough),
            NodeSpec('finish', run=_passthrough),
        ),
        edges=(
            EdgeSpec(START_NODE, 'stamp'),
            EdgeSpec('stamp', 'entry'),
            EdgeSpec('entry', COMPACT_NODE),
            EdgeSpec(COMPACT_NODE, 'pre_model'),
            EdgeSpec('pre_model', 'agent'),
            EdgeSpec('agent', 'post_model'),
            # 路由源是 post_model（挂了链就是链尾）——路由函数一个字不动（tools_condition）；
            # 终结轮经 finish（它保证"每轮 1 次"的锚点不被漏掉）
            EdgeSpec(
                'post_model',
                router=tools_condition,
                targets={'tools': 'tools', GRAPH_END: 'finish'},
            ),
            EdgeSpec('tools', 'post_tools'),
            EdgeSpec('post_tools', COMPACT_NODE),
            EdgeSpec('finish', END_NODE),
        ),
        channels=(
            ChannelSpec(MESSAGES, reducer=add_messages),
            ChannelSpec(COMPACTION),
            ChannelSpec(USAGE_ANCHOR),
            ChannelSpec(HARNESS),
        ),
    )
    ctx.structure.contribute(fragment)
    ctx.logger.info(
        '骨架已贡献：%d 个节点，锚点 %s', len(fragment.nodes), '、'.join(ANCHORS)
    )


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'inject': ['model', 'summary_model'],
}
"""模块级导出（约定式发现认 ``plugin`` 或 ``apply``）：带名字与依赖声明。"""
