"""合成器测试的公共件：与 builtin/loop 同形的**假骨架** + 假片段。

不依赖 app.agent 与真实模型——这里要测的是合成机制本身；真骨架的回归在 golden 用例里。
"""

from __future__ import annotations

from langgraph.graph import add_messages

from app.harness.fragment import (
    ChannelSpec,
    EdgeSpec,
    Fragment,
    NodeSpec,
    OUTPUT_SEAMS,
    ROLE_BASE,
    START_NODE,
    END_NODE,
    HARNESS,
    MESSAGES,
    COMPACTION,
    STAMP_INTERFACE,
    USAGE_ANCHOR,
)

DEFAULT_ANCHORS = ('entry', 'pre_model', 'post_model', 'post_tools', 'finish')


async def noop(state: dict) -> dict:
    return {}


def fake_router(state: dict) -> str:
    """假路由：返回 'tools' 或 'END'（与 tools_condition 的形态一致）。"""
    return 'tools'


def make_base(
    *,
    anchors=DEFAULT_ANCHORS,
    extra_anchors=(),
    drop=(),
    nodes_extra=(),
    edges_extra=(),
    seams=OUTPUT_SEAMS,
    stamp_node=None,
) -> Fragment:
    """一个功能上与 builtin/loop 同形的假骨架。"""
    base_nodes = {
        'stamp': stamp_node
        or NodeSpec('stamp', interface=STAMP_INTERFACE, writes=(HARNESS,)),
        'compact': NodeSpec(
            'compact', run=noop, reads=(MESSAGES, USAGE_ANCHOR), writes=(COMPACTION, USAGE_ANCHOR)
        ),
        'agent': NodeSpec(
            'agent', run=noop, reads=(MESSAGES, COMPACTION), writes=(MESSAGES, USAGE_ANCHOR)
        ),
        'tools': NodeSpec('tools', run=noop, reads=(MESSAGES,), writes=(MESSAGES,)),
    }
    for anchor in anchors:
        base_nodes[anchor] = NodeSpec(anchor, run=noop)
    for name in nodes_extra:
        base_nodes[name] = NodeSpec(name, run=noop)
    nodes = tuple(
        node for name, node in base_nodes.items() if name not in drop
    )
    edges = [
        EdgeSpec(START_NODE, 'stamp'),
        EdgeSpec('stamp', 'entry'),
        EdgeSpec('entry', 'compact'),
        EdgeSpec('compact', 'pre_model'),
        EdgeSpec('pre_model', 'agent'),
        EdgeSpec('agent', 'post_model'),
        EdgeSpec(
            'post_model',
            router=fake_router,
            targets={'tools': 'tools', END_NODE: 'finish'},
        ),
        EdgeSpec('tools', 'post_tools'),
        EdgeSpec('post_tools', 'compact'),
        EdgeSpec('finish', END_NODE),
        *edges_extra,
    ]
    edges = [
        edge
        for edge in edges
        if edge.source not in drop
        and edge.target not in drop
        and not (edge.is_conditional and any(v in drop for v in (edge.targets or {}).values()))
    ]
    return Fragment(
        id='builtin/loop',
        role=ROLE_BASE,
        anchors=(*anchors, *extra_anchors),
        output_seams=tuple(seams),
        nodes=nodes,
        edges=tuple(edges),
        channels=(
            ChannelSpec(MESSAGES, reducer=add_messages),
            ChannelSpec(COMPACTION),
            ChannelSpec(USAGE_ANCHOR),
            ChannelSpec(HARNESS),
        ),
    )


def make_chain(*names, anchor='post_model', fragment_id='workspace/research', **kwargs) -> Fragment:
    return Fragment(
        id=fragment_id,
        anchor=anchor,
        nodes=tuple(NodeSpec(name, run=noop) for name in names),
        **kwargs,
    )
