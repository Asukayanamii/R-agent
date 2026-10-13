"""合成器测试（B5）：插链、通道合并、图级校验、stamp 注入、结构哈希。

校验用例对应计划要求的六条（越权写入 / 逃逸边 / 造新锚点 / 骨架缺节点 / 图不连通 /
必需插件缺失——最后一条在 kernel 的 test_essential，这里覆盖其余）中的合成侧。
"""

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import add_messages

from app.harness.composer import compose, render_mermaid
from app.harness.fragment import (
    ChannelSpec,
    EdgeSpec,
    Fragment,
    NodeSpec,
    StructureError,
)
from support import make_base, make_chain, noop


def compose_ok(fragments, **kwargs):
    return compose(fragments, version_id=kwargs.pop('version_id', 1), checkpointer=InMemorySaver(), **kwargs)


# ------------------------------------------------------------------ 正常路径


def test_minimal_base_composes():
    version = compose_ok([make_base()])
    assert version.node_names == frozenset(
        {'stamp', 'entry', 'compact', 'pre_model', 'agent', 'post_model', 'tools', 'post_tools', 'finish'}
    )
    assert version.base_id == 'builtin/loop'
    assert version.anchors == ('entry', 'pre_model', 'post_model', 'post_tools', 'finish')
    assert len(version.structure_hash) == 64
    json.dumps(version.spec)  # spec 必须可 JSON 化


def test_chain_is_spliced_after_anchor_and_route_source_moves():
    version = compose_ok([make_base(), make_chain('a', 'b')])
    edges = {tuple(edge) for edge in version.spec['edges']}
    assert ('post_model', 'a') in edges
    assert ('a', 'b') in edges
    assert ('post_model', 'tools') not in edges  # 锚点的出边已改接
    assert version.spec['routes'][0]['source'] == 'b'  # 条件边的源 = 链尾
    assert version.spec['routes'][0]['targets'] == {'tools': 'tools', 'END': 'finish'}


def test_empty_fragment_only_contributes_channels():
    version = compose_ok(
        [
            make_base(),
            Fragment(
                id='workspace/notes',
                anchor='pre_model',
                channels=(ChannelSpec('plugin.workspace/notes.text'),),
            ),
        ]
    )
    assert 'plugin.workspace/notes.text' in {c['name'] for c in version.spec['channels']}
    edges = {tuple(edge) for edge in version.spec['edges']}
    assert ('pre_model', 'agent') in edges  # 没有节点：锚点原样透传


def test_stamp_is_injected_with_version_identity():
    import asyncio

    version = compose_ok([make_base()])
    stamp = version.impls['stamp']
    update = asyncio.run(stamp({}))
    harness = update['harness']
    assert harness['version'] == version.id
    assert harness['structure_hash'] == version.structure_hash
    assert harness['base'] == 'builtin/loop'
    assert harness['protocol'] is None


def test_structure_hash_stable_and_sensitive():
    first = compose_ok([make_base()])
    second = compose_ok([make_base()], version_id=7)
    assert first.structure_hash == second.structure_hash  # 与版本号无关

    with_chain = compose_ok([make_base(), make_chain('a')])
    assert with_chain.structure_hash != first.structure_hash


def test_multiple_fragments_on_one_anchor_warn():
    version = compose_ok(
        [make_base(), make_chain('a', fragment_id='workspace/one'), make_chain('b', fragment_id='workspace/two')]
    )
    assert any('post_model' in warning and 'workspace/one' in warning for warning in version.warnings)
    edges = {tuple(edge) for edge in version.spec['edges']}
    assert ('a', 'b') in edges  # 串成一条链


def test_plugin_channel_without_reader_warns():
    fragment = Fragment(
        id='workspace/research',
        anchor='pre_model',
        nodes=(NodeSpec('a', run=noop, writes=('plugin.workspace/research.notes',)),),
        channels=(ChannelSpec('plugin.workspace/research.notes'),),
    )
    version = compose_ok([make_base(), fragment])
    assert any('暂无读取者' in warning for warning in version.warnings)


def test_interface_is_resolved_from_services():
    base = make_base(stamp_node=NodeSpec('stamp', interface='stamp', writes=('harness',)))
    # 把 agent 换成抽象节点
    base = Fragment(
        id=base.id,
        role=base.role,
        anchors=base.anchors,
        output_seams=base.output_seams,
        nodes=tuple(
            NodeSpec('agent', interface='search') if node.name == 'agent' else node
            for node in base.nodes
        ),
        edges=base.edges,
        channels=base.channels,
    )
    with pytest.raises(StructureError) as info:
        compose_ok([base])
    assert info.value.code == 'INTERFACE_MISSING'

    version = compose_ok([base], resolve_interface=lambda name: noop if name == 'search' else None)
    assert version.impls['agent'] is noop


# ------------------------------------------------------------------ 骨架完整性


def test_skeleton_missing_required_node_rejected():
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(drop=('tools',))])
    assert info.value.code == 'SKELETON_NODES'
    assert 'tools' in str(info.value)


def test_skeleton_anchor_without_node_rejected():
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(extra_anchors=('ghost',))])
    assert info.value.code == 'SKELETON_ANCHORS'
    assert 'ghost' in str(info.value)


def test_skeleton_missing_seams_rejected():
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(seams=())])
    assert info.value.code == 'SKELETON_SEAMS'


def test_stamp_must_be_interface_node():
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(stamp_node=NodeSpec('stamp', run=noop))])
    assert info.value.code == 'STAMP_SHAPE'


def test_two_bases_rejected():
    other = Fragment(
        id='other/loop',
        role='base',
        anchors=('entry',),
        output_seams=make_base().output_seams,
        nodes=(NodeSpec('entry', run=noop),),
    )
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(), other])
    assert info.value.code == 'BASE_COUNT'


# ------------------------------------------------------------------ 图级校验


def test_start_must_have_single_successor():
    base = make_base()
    doubled = Fragment(
        id=base.id,
        role=base.role,
        anchors=base.anchors,
        output_seams=base.output_seams,
        nodes=base.nodes,
        edges=(*base.edges, EdgeSpec('START', 'finish')),
        channels=base.channels,
    )
    with pytest.raises(StructureError) as info:
        compose_ok([doubled])
    assert info.value.code == 'GRAPH_ENTRY'


def test_unreachable_node_rejected():
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(nodes_extra=('orphan',))])
    assert info.value.code == 'GRAPH_UNREACHABLE'
    assert 'orphan' in str(info.value)


def test_dead_end_node_rejected():
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(nodes_extra=('dead',), edges_extra=(EdgeSpec('agent', 'dead'),))])
    assert info.value.code == 'GRAPH_DEADEND'
    assert 'dead' in str(info.value)


# ------------------------------------------------------------------ 其它校验


def test_node_conflict_with_base_rejected():
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(), make_chain('agent')])
    assert info.value.code == 'NODE_CONFLICT'


def test_reducer_mismatch_rejected():
    fragment = Fragment(
        id='workspace/research',
        anchor='pre_model',
        channels=(
            ChannelSpec('plugin.workspace/research.notes', reducer=add_messages),
            ChannelSpec('plugin.workspace/research.notes', reducer=None),
        ),
    )
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(), fragment])
    assert info.value.code == 'REDUCER_MISMATCH'


def test_dag_not_supported_yet():
    fragment = Fragment(
        id='workspace/research',
        anchor='pre_model',
        nodes=(NodeSpec('a', run=noop), NodeSpec('b', run=noop)),
        edges=(EdgeSpec('a', 'b'),),
    )
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(), fragment])
    assert info.value.code == 'DAG_UNSUPPORTED'


def test_escape_edge_rejected_at_compose_too():
    fragment = Fragment(
        id='workspace/research',
        anchor='pre_model',
        nodes=(NodeSpec('a', run=noop),),
        edges=(EdgeSpec('a', 'agent'),),
    )
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(), fragment])
    assert info.value.code == 'EDGE_ESCAPE'


def test_unknown_anchor_rejected_at_compose_too():
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(), make_chain('a', anchor='nowhere')])
    assert info.value.code == 'ANCHOR_UNKNOWN'


def test_channel_reserved_rejected_at_compose_too():
    fragment = Fragment(
        id='workspace/research',
        anchor='pre_model',
        nodes=(NodeSpec('a', run=noop, writes=('messages',)),),
    )
    with pytest.raises(StructureError) as info:
        compose_ok([make_base(), fragment])
    assert info.value.code == 'CHANNEL_RESERVED'


def test_mermaid_draws_empty_anchors():
    """空锚点也要画出来（docs/graph.mmd 由合成器导出）。"""
    version = compose_ok([make_base()])
    text = render_mermaid(version.graph)
    for anchor in ('entry', 'pre_model', 'post_model', 'post_tools', 'finish'):
        assert anchor in text
