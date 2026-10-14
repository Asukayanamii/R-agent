"""片段契约的静态校验（B2）。

这一层查的是"片段自己能查清的"：边形体（逃逸/条件边/环）、名字、通道命名与写入权限；
跨片段的规则（锚点在不在骨架表里、名字冲突）单独两个函数，`wait` 语义也在用例里钉住。
"""

import json

import pytest

from app.harness.fragment import (
    ChannelSpec,
    EdgeSpec,
    Fragment,
    NodeSpec,
    ROLE_BASE,
    StructureError,
    fragment_spec,
    validate_against_base,
    validate_fragment_self,
    validate_node_names,
)


def node(name: str, **kwargs) -> NodeSpec:
    return NodeSpec(name, run=(kwargs.pop('run', None) or (lambda state: {})), **kwargs)


def make_base(anchors=('pre_model', 'post_model')) -> Fragment:
    names = ('stamp', 'compact', 'agent', 'tools', *anchors)
    return Fragment(
        id='base',
        role=ROLE_BASE,
        anchors=anchors,
        nodes=tuple(node(name) for name in names),
        edges=(EdgeSpec('agent', 'post_model'), EdgeSpec('post_model', 'tools')),
        channels=(ChannelSpec('messages'),),
    )


def chain(*names, **kwargs) -> Fragment:
    return Fragment(
        id=kwargs.pop('id', 'workspace/research'),
        anchor=kwargs.pop('anchor', 'pre_model'),
        nodes=tuple(node(name) for name in names),
        **kwargs,
    )


# ------------------------------------------------------------------ 通过路径


def test_valid_chain_passes():
    validate_fragment_self(chain('a', 'b'))
    validate_fragment_self(make_base())


def test_base_may_have_cycles_and_conditional_edges():
    """骨架自己就是环（agent ⇄ tools），也能写条件边——它代表内核语义。"""
    base = Fragment(
        id='base',
        role=ROLE_BASE,
        anchors=('post_model', 'finish'),
        nodes=tuple(node(name) for name in ('stamp', 'compact', 'agent', 'tools', 'post_model', 'finish')),
        edges=(
            EdgeSpec('START', 'stamp'),
            EdgeSpec('compact', 'agent'),
            EdgeSpec('tools', 'compact'),
            EdgeSpec('finish', 'END'),
            EdgeSpec('post_model', router=lambda state: 'END', targets={'tools': 'tools', 'END': 'END'}),
        ),
    )
    validate_fragment_self(base)


# ------------------------------------------------------------------ 结构允许集


def test_escape_edge_is_rejected():
    fragment = Fragment(
        id='workspace/research',
        anchor='pre_model',
        nodes=(node('a'),),
        edges=(EdgeSpec('a', 'agent'),),  # agent 在片段外：想回主循环 = 逃逸
    )
    with pytest.raises(StructureError) as info:
        validate_fragment_self(fragment)
    assert info.value.code == 'EDGE_ESCAPE'
    assert 'agent' in str(info.value)


def test_conditional_edge_is_rejected_for_plugin_fragments():
    fragment = Fragment(
        id='workspace/research',
        anchor='pre_model',
        nodes=(node('a'), node('b')),
        edges=(EdgeSpec('a', router=lambda state: 'b', targets={'b': 'b'}),),
    )
    with pytest.raises(StructureError) as info:
        validate_fragment_self(fragment)
    assert info.value.code == 'CONDITIONAL_EDGE'


def test_cycle_is_rejected_for_plugin_fragments():
    fragment = Fragment(
        id='workspace/research',
        anchor='pre_model',
        nodes=(node('a'), node('b')),
        edges=(EdgeSpec('a', 'b'), EdgeSpec('b', 'a')),
    )
    with pytest.raises(StructureError) as info:
        validate_fragment_self(fragment)
    assert info.value.code == 'CYCLE'
    assert 'a' in str(info.value) and 'b' in str(info.value)


def test_duplicate_node_names_rejected():
    with pytest.raises(StructureError) as info:
        validate_fragment_self(chain('a', 'a'))
    assert info.value.code == 'DUPLICATE_NODE'


def test_node_needs_exactly_one_implementation():
    with pytest.raises(StructureError) as info:
        NodeSpec('a')
    assert info.value.code == 'NODE_IMPL'
    with pytest.raises(StructureError) as info:
        NodeSpec('a', run=lambda state: {}, interface='search')
    assert info.value.code == 'NODE_IMPL'


def test_edge_shape_is_enforced():
    with pytest.raises(StructureError) as info:
        EdgeSpec('a')
    assert info.value.code == 'EDGE_SHAPE'
    with pytest.raises(StructureError) as info:
        EdgeSpec('a', target='b', router=lambda state: 'b', targets={'b': 'b'})
    assert info.value.code == 'EDGE_SHAPE'


# ------------------------------------------------------------------ 通道与写入权限


def test_plugin_cannot_write_kernel_channel():
    fragment = Fragment(
        id='workspace/research',
        anchor='pre_model',
        nodes=(NodeSpec('a', run=lambda state: {}, writes=('messages',)),),
    )
    with pytest.raises(StructureError) as info:
        validate_fragment_self(fragment)
    assert info.value.code == 'CHANNEL_RESERVED'
    assert 'messages' in str(info.value)


def test_plugin_channel_must_use_own_namespace():
    fragment = Fragment(
        id='workspace/research',
        anchor='pre_model',
        nodes=(NodeSpec('a', run=lambda state: {}, writes=('plugin.other.notes',)),),
    )
    with pytest.raises(StructureError) as info:
        validate_fragment_self(fragment)
    assert info.value.code == 'CHANNEL_NAMESPACE'
    assert 'plugin.other.notes' in str(info.value)

    # 自己命名空间下的通道合法
    ok = Fragment(
        id='workspace/research',
        anchor='pre_model',
        nodes=(NodeSpec('a', run=lambda state: {}, writes=('plugin.workspace/research.notes',)),),
        channels=(ChannelSpec('plugin.workspace/research.notes'),),
    )
    validate_fragment_self(ok)


def test_unknown_channel_name_rejected():
    fragment = Fragment(
        id='workspace/research',
        anchor='pre_model',
        channels=(ChannelSpec('notes'),),
    )
    with pytest.raises(StructureError) as info:
        validate_fragment_self(fragment)
    assert info.value.code == 'CHANNEL_NAME'


# ------------------------------------------------------------------ 跨片段规则


def test_anchor_must_be_in_base_table():
    fragment = chain('a', anchor='nowhere')
    base = make_base(anchors=('pre_model',))
    with pytest.raises(StructureError) as info:
        validate_against_base(fragment, base)
    assert info.value.code == 'ANCHOR_UNKNOWN'
    assert 'pre_model' in str(info.value)  # 报出可用锚点


def test_fragment_without_base_waits_for_loop():
    with pytest.raises(StructureError) as info:
        validate_against_base(chain('a'), None)
    assert info.value.code == 'BASE_MISSING'
    assert 'loop' in str(info.value)


def test_node_name_conflict_reported():
    with pytest.raises(StructureError) as info:
        validate_node_names(chain('agent'), taken=('agent', 'tools'))
    assert info.value.code == 'NODE_CONFLICT'
    assert 'agent' in str(info.value)


# ------------------------------------------------------------------ 快照


def test_fragment_spec_is_json_serializable():
    spec = fragment_spec(chain('a', 'b'))
    dumped = json.dumps(spec, ensure_ascii=False)
    assert '"a"' in dumped
    assert spec['role'] is None
    assert spec['anchor'] == 'pre_model'
