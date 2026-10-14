"""片段契约：插件对图结构的贡献形状（纯数据，可 JSON 化）。

一个片段 = **一个挂点（锚点）+ 一段受约束的内部结构**。约束靠机制，不靠措辞：

- **路由写不出来**：契约里没有"条件边"字段——只有 `role="base"` 的骨架能写
  （路由是内核语义；换路由 = 换一个 loop 插件，而不是让插件自己分流）；
- **嵌套写不出来**：片段只能声明自己的节点与内部边，边不许越出片段（逃逸 = 拒绝）；
- **环写不出来**：插件片段只允许链 / DAG（"再来一轮"写在节点代码里，不进图）；
- **改内核节点写不出来**：没有"替换别人节点"的字段——换实现只能走 DI（换服务）。

校验分两段执行（见 composer）：这里的 `validate_fragment_self` 只查"片段自身"，
跨片段的规则（锚点是否在骨架锚点表里、节点名冲突、通道 reducer 一致性、整图连通性）
在合成期查——**失败不改变运行图**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    'StructureError',
    'NodeSpec',
    'EdgeSpec',
    'ChannelSpec',
    'Fragment',
    'ROLE_BASE',
    'START_NODE',
    'END_NODE',
    'MESSAGES',
    'COMPACTION',
    'USAGE_ANCHOR',
    'HARNESS',
    'PLUGIN_FAILURES',
    'KERNEL_CHANNELS',
    'PLUGIN_PREFIX',
    'REQUIRED_NODES',
    'STAMP_INTERFACE',
    'OUTPUT_SEAMS',
    'REQUIRED_SEAMS',
    'validate_fragment_self',
    'validate_against_base',
    'validate_node_names',
    'fragment_spec',
    'fragment_from_dict',
]


class StructureError(Exception):
    """结构契约被违反。``code`` 是稳定标识，供用例与报告引用；message 给人看。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


ROLE_BASE = 'base'
"""骨架片段的角色：恰好一个，提供锚点表与出站接缝表，可替换不可停用。"""

START_NODE = 'START'
END_NODE = 'END'
"""片段边里的两个哨兵点：只有 role="base" 的骨架能碰。"""

MESSAGES = 'messages'
COMPACTION = 'compaction'
USAGE_ANCHOR = 'usage_anchor'
HARNESS = 'harness'
PLUGIN_FAILURES = 'plugin_failures'
"""节点壳的失败留痕（§4.2）：哪个片段、哪一步、异常原文；由壳写、映射层读。"""
KERNEL_CHANNELS = (MESSAGES, COMPACTION, USAGE_ANCHOR, HARNESS, PLUGIN_FAILURES)
"""内核通道：只有骨架节点能写（校验器强制），插件通道一律 ``plugin.<片段id>.<字段>``。"""

PLUGIN_PREFIX = 'plugin.'

REQUIRED_NODES = ('stamp', 'compact', 'agent', 'tools')
"""骨架必需节点：缺一个就拒绝装配（§3.1 第 7 条）。"""

STAMP_INTERFACE = 'stamp'
"""``stamp`` 节点的实现名：骨架只声明它的位置（interface），
实现由**装配器注入**——结构戳必须带"当前这个版本"的 id 与哈希，装配器才知道（§5.3）。"""

OUTPUT_SEAMS = (
    'thread',
    'text_delta',
    'message_end',
    'tool_start',
    'tool_end',
    'interrupt',
    'compact',
    'done',
)
"""内核已知的全部出站接缝（协议插件按这张表映射成 wire 形状）。"""

REQUIRED_SEAMS = frozenset(OUTPUT_SEAMS)
"""骨架必须承诺产出的最小集合：输出点随内核版本走，不随插件变化。"""


@dataclass(frozen=True)
class NodeSpec:
    """片段里的一个节点：图里固定的是名字与位置，实现要么自带、要么从服务解析。"""

    name: str
    run: Callable[..., Any] | None = None
    interface: str | None = None
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    impl_id: str = ''

    def __post_init__(self) -> None:
        if (self.run is None) == (self.interface is None):
            raise StructureError(
                'NODE_IMPL',
                f'节点 {self.name!r} 需要 run（自带实现）与 interface（抽象节点）恰有一个',
            )


@dataclass(frozen=True)
class EdgeSpec:
    """片段内部的一条边（无条件 / 条件两种形态）。

    - 无条件边：``source → target``；
    - 条件边：``source`` 之后按 ``router(state)`` 的返回值分流，``targets`` 是
      "返回值 → 目标"；目标是片段内节点名，或哨兵 ``"END"``。
      **只有 role="base" 的骨架能写条件边**——路由权不交给插件。
    """

    source: str
    target: str | None = None
    router: Callable[..., Any] | None = None
    targets: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.router is None:
            if self.target is None or self.targets is not None:
                raise StructureError('EDGE_SHAPE', f'无条件边必须是 source → target：{self.source!r}')
        elif self.target is not None or not self.targets:
            raise StructureError('EDGE_SHAPE', f'条件边需要 router + targets：{self.source!r}')

    @property
    def is_conditional(self) -> bool:
        return self.router is not None


@dataclass(frozen=True)
class ChannelSpec:
    """片段声明的一条状态通道。

    ``reducer=None`` 表示 last-write-wins（现有 ``compaction`` / ``usage_anchor`` 的语义）。
    同名通道的 reducer 必须一致（合成期校验）。
    """

    name: str
    reducer: Callable[..., Any] | None = None
    default: Any = None


@dataclass(frozen=True)
class Fragment:
    """一份结构贡献。"""

    id: str
    anchor: str | None = None
    nodes: tuple[NodeSpec, ...] = ()
    edges: tuple[EdgeSpec, ...] = ()
    channels: tuple[ChannelSpec, ...] = ()
    order: int = 0
    role: str | None = None
    output_seams: tuple[str, ...] = ()
    anchors: tuple[str, ...] = ()
    """仅 role="base"：它提供的锚点表（入站锚点 + 触发频率由骨架自己声明）。"""
    scope: str = ''
    """作用域：空串 = 全局（内置插件那套图）；非空 = 会话工作区（工作区插件库）。

    作用域只影响"谁能看见它"：合成按 `scope` 过滤（全局片段人人可见），
    校验（重名/节点名）也按作用域分开做——两个工作区可以有同名片段。"""


def _names(value: Any, fragment_id: str, node_name: str, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise StructureError(
            'DECL_FIELD', f'{fragment_id} 的节点 {node_name} 的 {field} 必须是字符串数组'
        )
    return tuple(value)


def fragment_from_dict(
    payload: Mapping[str, Any], *, source: str = '', scope: str = ''
) -> Fragment:
    """
    把**纯数据**（JSON 声明）翻成 Fragment——插件库里的 `.json` 形态（§5.1）。

    数据能表达的只有"位置与形状"：节点必须是**抽象节点**（`interface`，实现由服务解析），
    内部边写不出来（片段本来就是"节点顺序即执行顺序"的链，§3.1），条件边/角色/出站接缝
    都写不出来（路由与内核语义是骨架与代码的事）。形状不对就抛 `StructureError`——
    合成期的两段校验照旧兜底。

    形状::

        {"id": "workspace/research", "anchor": "pre_model", "order": 0,
         "chain": [{"name": "notes", "interface": "research",
                    "reads": ["messages"], "writes": ["plugin.workspace/research.notes"]}],
         "channels": [{"name": "plugin.workspace/research.notes"}]}
    """
    where = source or '声明'
    if not isinstance(payload, Mapping):
        raise StructureError('DECL_SHAPE', f'{where} 必须是 JSON 对象')

    fragment_id = payload.get('id')
    if not isinstance(fragment_id, str) or not fragment_id.strip():
        raise StructureError('DECL_ID', f'{where} 缺少字符串 id')

    anchor = payload.get('anchor')
    if not isinstance(anchor, str) or not anchor.strip():
        raise StructureError('DECL_ANCHOR', f'{fragment_id} 缺少 anchor（挂点）')

    order = payload.get('order', 0)
    if isinstance(order, bool) or not isinstance(order, int):
        raise StructureError('DECL_ORDER', f'{fragment_id} 的 order 必须是整数')

    chain = payload.get('chain')
    if not isinstance(chain, list) or not chain:
        raise StructureError('DECL_CHAIN', f'{fragment_id} 的 chain 必须是非空数组')

    nodes: list[NodeSpec] = []
    for entry in chain:
        if not isinstance(entry, Mapping):
            raise StructureError('DECL_NODE', f'{fragment_id} 的 chain 项必须是对象')
        node_name = entry.get('name')
        interface = entry.get('interface')
        if not isinstance(node_name, str) or not node_name.strip():
            raise StructureError('DECL_NODE', f'{fragment_id} 的 chain 项缺少字符串 name')
        if not isinstance(interface, str) or not interface.strip():
            raise StructureError(
                'DECL_INTERFACE',
                f'{fragment_id} 的节点 {node_name} 必须给 interface：'
                f'纯声明片段不执行代码，实现只能来自服务',
            )
        nodes.append(
            NodeSpec(
                name=node_name,
                interface=interface,
                reads=_names(entry.get('reads'), fragment_id, node_name, 'reads'),
                writes=_names(entry.get('writes'), fragment_id, node_name, 'writes'),
            )
        )

    channels: list[ChannelSpec] = []
    for entry in payload.get('channels') or ():
        if not isinstance(entry, Mapping):
            raise StructureError('DECL_CHANNEL', f'{fragment_id} 的 channels 项必须是对象')
        channel_name = entry.get('name')
        if not isinstance(channel_name, str) or not channel_name.strip():
            raise StructureError('DECL_CHANNEL', f'{fragment_id} 的通道缺少字符串 name')
        if 'reducer' in entry:
            raise StructureError(
                'DECL_REDUCER',
                f'{fragment_id} 的通道 {channel_name} 声明了 reducer：合并语义是代码，写不进数据',
            )
        channels.append(ChannelSpec(name=channel_name, default=entry.get('default')))

    return Fragment(
        id=fragment_id,
        anchor=anchor,
        order=order,
        nodes=tuple(nodes),
        channels=tuple(channels),
        scope=scope,
    )


def _channel_owner_ok(fragment: Fragment, name: str, *, what: str) -> None:
    """通道名必须落在这两种之一：内核通道（仅骨架）或自己的 plugin.<id>.* 命名空间。"""
    if name in KERNEL_CHANNELS:
        if fragment.role != ROLE_BASE:
            raise StructureError(
                'CHANNEL_RESERVED',
                f'片段 {fragment.id!r} 不能{what}内核通道 {name!r}——那是骨架的通道',
            )
        return
    if name.startswith(PLUGIN_PREFIX):
        if not name.startswith(f'{PLUGIN_PREFIX}{fragment.id}.'):
            raise StructureError(
                'CHANNEL_NAMESPACE',
                f'片段 {fragment.id!r} 不能{what} {name!r}：插件通道必须在自己命名空间下，'
                f'即 {PLUGIN_PREFIX}{fragment.id}.<字段>',
            )
        return
    raise StructureError(
        'CHANNEL_NAME',
        f'非法通道名 {name!r}：只能是内核通道或 {PLUGIN_PREFIX}<片段id>.<字段>',
    )


def _has_cycle(nodes: Iterable[str], edges: Iterable[EdgeSpec]) -> list[str] | None:
    """有向环检测（无条件边与条件边的目标都算连接）；返回环上的一个节点路径或 None。"""
    adjacency: dict[str, list[str]] = {name: [] for name in nodes}
    for edge in edges:
        if edge.is_conditional:
            adjacency.setdefault(edge.source, []).extend(
                target for target in (edge.targets or {}).values() if target in adjacency
            )
        elif edge.target in adjacency:
            adjacency.setdefault(edge.source, []).append(edge.target)

    state: dict[str, int] = {}
    path: list[str] = []

    def walk(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for neighbor in adjacency.get(node, ()):
            if state.get(neighbor) == 1:
                return [*path[path.index(neighbor) :], neighbor]
            if state.get(neighbor) is None:
                found = walk(neighbor)
                if found is not None:
                    return found
        path.pop()
        state[node] = 2
        return None

    for name in adjacency:
        if state.get(name) is None:
            found = walk(name)
            if found is not None:
                return found
    return None


def validate_fragment_self(fragment: Fragment) -> None:
    """片段自身的静态校验（不依赖其它片段，注册期即可执行）。

    查：角色/挂点形状、节点重名、边形体（逃逸 / 条件边越权 / 环）、通道命名与写入权限。
    """
    if not fragment.id:
        raise StructureError('FRAGMENT_ID', '片段必须有 id')
    if fragment.role not in (None, ROLE_BASE):
        raise StructureError('FRAGMENT_ROLE', f'未知角色 {fragment.role!r}（只认 {ROLE_BASE!r}）')

    is_base = fragment.role == ROLE_BASE
    if is_base:
        if fragment.anchor is not None:
            raise StructureError('BASE_ANCHOR', f'骨架 {fragment.id!r} 不挂在锚点上')
        if not fragment.anchors:
            raise StructureError('BASE_ANCHORS', f'骨架 {fragment.id!r} 没有声明锚点表')
    else:
        if not fragment.anchor:
            raise StructureError('FRAGMENT_ANCHOR', f'插件片段 {fragment.id!r} 必须声明锚点')

    names = [node.name for node in fragment.nodes]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise StructureError('DUPLICATE_NODE', f'片段 {fragment.id!r} 内节点重名：{duplicates}')

    internal = set(names)
    sentinels = {START_NODE, END_NODE} if is_base else set()
    for edge in fragment.edges:
        if edge.is_conditional and not is_base:
            raise StructureError(
                'CONDITIONAL_EDGE',
                f'片段 {fragment.id!r} 写了条件边（{edge.source!r}）：路由是内核语义，'
                f'插件片段只允许无条件边',
            )
        endpoints = [edge.source]
        endpoints.extend((edge.target,) if edge.target else (edge.targets or {}).values())
        for endpoint in endpoints:
            if endpoint in sentinels:
                continue
            if endpoint not in internal:
                raise StructureError(
                    'EDGE_ESCAPE',
                    f'片段 {fragment.id!r} 的边端点 {endpoint!r} 不在片段内部——'
                    f'边不许逃逸（想回 agent 重问 = 把路由权交给插件，不允许）',
                )

    if not is_base:
        cycle = _has_cycle(names, fragment.edges)
        if cycle is not None:
            raise StructureError(
                'CYCLE',
                f'片段 {fragment.id!r} 里有环：{" → ".join(cycle)}——'
                f'"再来一轮"写在节点代码里，不进图',
            )

    for channel in fragment.channels:
        _channel_owner_ok(fragment, channel.name, what='声明')

    for node in fragment.nodes:
        for written in node.writes:
            _channel_owner_ok(fragment, written, what='写')


def validate_against_base(fragment: Fragment, base: Fragment | None) -> None:
    """跨片段规则之一：挂点必须在骨架声明的锚点表里（骨架不在 = 报"等待 loop 就绪"）。"""
    if fragment.role == ROLE_BASE:
        return
    if base is None:
        raise StructureError(
            'BASE_MISSING',
            f'片段 {fragment.id!r} 等待 loop 就绪：当前没有骨架，锚点表还无从对照',
        )
    if fragment.anchor not in base.anchors:
        raise StructureError(
            'ANCHOR_UNKNOWN',
            f'片段 {fragment.id!r} 的锚点 {fragment.anchor!r} 不在骨架锚点表里；'
            f'可用锚点：{"、".join(base.anchors)}',
        )


def validate_node_names(fragment: Fragment, taken: Iterable[str]) -> None:
    """跨片段规则之一：节点名不许与骨架或已装载片段冲突。"""
    conflicts = sorted(set(node.name for node in fragment.nodes) & set(taken))
    if conflicts:
        raise StructureError(
            'NODE_CONFLICT',
            f'片段 {fragment.id!r} 的节点名已被占用：{conflicts}',
        )


def fragment_spec(fragment: Fragment) -> dict[str, Any]:
    """可 JSON 化的结构快照（实现只记名字，不进快照本身）。"""

    def name_of(value: Any) -> str | None:
        return getattr(value, '__name__', None) if value is not None else None

    return {
        'id': fragment.id,
        'role': fragment.role,
        'anchor': fragment.anchor,
        'order': fragment.order,
        'anchors': list(fragment.anchors),
        'output_seams': list(fragment.output_seams),
        'nodes': [
            {
                'name': node.name,
                'run': name_of(node.run),
                'interface': node.interface,
                'reads': list(node.reads),
                'writes': list(node.writes),
                'impl_id': node.impl_id,
            }
            for node in fragment.nodes
        ],
        'edges': [
            {
                'source': edge.source,
                'target': edge.target,
                'router': name_of(edge.router),
                'targets': dict(edge.targets) if edge.targets else None,
            }
            for edge in fragment.edges
        ],
        'channels': [
            {
                'name': channel.name,
                'reducer': name_of(channel.reducer),
                'default': channel.default,
            }
            for channel in fragment.channels
        ],
    }
