"""合成器：片段的集合 → 一张具体的 LangGraph。

流程（对应设计稿 §3）：

    收集（StructureService）→ 两段校验 → 通道合并 → 锚点插链
    → 动态拼 StateDict schema → 构建 StateGraph → 展平 → compile → GraphVersion

两条硬规则：

- **失败不改变运行图**：任何校验不通过都抛 `StructureError`，不产生新版本；
- **展平**：骨架节点、锚点、片段节点全进同一张图——检查点布局、`next` 语义、
  `as_node=` 写入路径都与现状一致（阶段 0 探针 3 已实测：旧 thread 零风险）。

锚点插链：锚点节点**永远在**（空着就是透传），挂上链以后把锚点的出边改接到链尾——
`post_model` 上挂了链，条件边的源就是链尾（探针 1 实测路由照常）。
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Iterable, Mapping, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph, add_messages

from app.kernel import Service

from app.harness.circuit import FailureLedger, fail_key, wrap_node
from app.harness.fragment import (
    END_NODE,
    KERNEL_CHANNELS,
    MESSAGES,
    PLUGIN_PREFIX,
    REQUIRED_NODES,
    REQUIRED_SEAMS,
    ROLE_BASE,
    STAMP_INTERFACE,
    START_NODE,
    ChannelSpec,
    EdgeSpec,
    Fragment,
    NodeSpec,
    StructureError,
    fragment_spec,
    validate_against_base,
    validate_fragment_self,
    validate_node_names,
)

__all__ = [
    'GraphVersion',
    'StructureService',
    'PLUGIN_SCOPE',
    'compose',
    'build_state_type',
    'render_mermaid',
]

PLUGIN_SCOPE: ContextVar[str] = ContextVar('plugin_scope', default='')
"""装载期的默认作用域：工作区插件库在调插件的 `apply` 时设上。

可编程插件（`.py`）在自己代码里 `ctx.structure.contribute(fragment)` 时不必知道
"我在哪个工作区"——片段的作用域由装载方决定。内置插件不设，落默认值 `''`（全局）。"""


class StructureService(Service):
    """结构接缝：插件在这里贡献片段（``ctx.structure``）。

    注册期校验在 ``contribute()`` 里立即执行——不通过 = 抛错 = 插件装载失败
    （文件留着，报明原因与改法）；片段登记为调用方 fiber 的 effect，
    插件卸载自动撤销，合成器的输入随之变化。

    **作用域**：片段带 ``scope``（空串 = 全局）。合成按作用域过滤，重名与节点名校验
    也按作用域分开——两个工作区可以有同名片段，工作区 A 批准的插件不进 B 的图。
    """

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx, 'structure')
        self._fragments: list[Fragment] = []
        self._revisions: dict[str, int] = {}

    # ---------------------------------------------------------------- 收集
    def revision(self, scope: str = '') -> int:
        """该作用域的结构版本号：每次贡献/撤销 +1，调用方据此判断"要不要重新合成"。"""
        return self._revisions.get(scope, 0)

    def _bump(self, scope: str) -> None:
        self._revisions[scope] = self._revisions.get(scope, 0) + 1

    def contribute(self, fragment: Fragment) -> Any:
        """贡献一个片段；返回撤销句柄（随调用方 fiber 卸载自动撤销）。

        作用域来自片段自己（`fragment.scope`），或装载期由插件库设的 `PLUGIN_SCOPE`
        ——可编程插件不必知道自己在哪个工作区。
        """
        if not fragment.scope:
            scope = PLUGIN_SCOPE.get()
            if scope:
                fragment = replace(fragment, scope=scope)
        if any(item.id == fragment.id and item.scope == fragment.scope for item in self._fragments):
            where = fragment.scope or '全局'
            raise StructureError(
                'FRAGMENT_DUPLICATE', f'片段 id 重复：{fragment.id!r}（作用域 {where}）'
            )
        base = self.base()
        validate_fragment_self(fragment)
        if fragment.role == ROLE_BASE and base is not None:
            raise StructureError('BASE_COUNT', f'已经有骨架（{base.id!r}）了，不能有两个')
        validate_against_base(fragment, base)
        validate_node_names(fragment, self.node_names(fragment.scope))

        def install() -> Callable[[], bool]:
            self._fragments.append(fragment)
            self._bump(fragment.scope)

            def withdraw() -> bool:
                if fragment in self._fragments:
                    self._fragments.remove(fragment)
                    self._bump(fragment.scope)
                    return True
                return False

            return withdraw

        return self.ctx.effect(install, f'ctx.structure.contribute({fragment.id!r})')

    def collect(self, scope: str | None = None) -> tuple[Fragment, ...]:
        """当前生效的片段（装载顺序；骨架排最前）。

        `scope=None` = 全部（报告与诊断用）；给 scope = **全局片段 + 该作用域的片段**
        （合成用）——`''` 就是全局（内置插件那套图）。
        """
        items = self._fragments
        if scope is not None:
            items = [item for item in items if not item.scope or item.scope == scope]
        return tuple(
            sorted(items, key=lambda item: 0 if item.role == ROLE_BASE else 1)
        )

    def base(self) -> Fragment | None:
        return next((item for item in self._fragments if item.role == ROLE_BASE), None)

    def node_names(self, scope: str | None = None) -> frozenset[str]:
        """节点名集合；给 scope 时只算"该作用域能看见的片段"（重名校验按作用域做）。"""
        return frozenset(
            node.name for item in self.collect(scope) for node in item.nodes
        )


@dataclass
class GraphVersion:
    """一个已编译、可直接运行的图版本（结构 + 实现表 + 编译产物）。"""

    id: int
    structure_hash: str
    spec: dict[str, Any]
    impls: dict[str, Callable[..., Any]]
    graph: Any
    warnings: list[str] = field(default_factory=list)
    created_at: str = ''
    anchors: tuple[str, ...] = ()
    base_id: str = ''

    @property
    def node_names(self) -> frozenset[str]:
        return frozenset(self.impls)


# ---------------------------------------------------------------------- 合成


def _ordered_chain(fragments: list[Fragment]) -> list[Fragment]:
    """同一锚点上的挂载顺序：先按 order，再按装载顺序（稳定排序）。"""
    return sorted(fragments, key=lambda item: item.order)


def build_state_type(channels: Mapping[str, ChannelSpec], name: str) -> type:
    """按通道表动态拼一个 TypedDict 当 StateGraph schema（探针已验证可行）。

    - 声明了 reducer 的通道用 ``Annotated[Any, reducer]``；
    - 没声明的用裸 ``Any``（last-write-wins）。
    """
    fields: dict[str, Any] = {}
    for channel in channels.values():
        if channel.reducer is not None:
            fields[channel.name] = Annotated[Any, channel.reducer]
        else:
            fields[channel.name] = Any
    return TypedDict(name, fields)


def make_stamp_node(
    *,
    version_id: int,
    structure_hash: str,
    base_id: str,
    capabilities: Callable[[], dict[str, Any]] | None = None,
) -> Callable[..., Any]:
    """内置 ``stamp`` 的实现：每轮第一个写入结构戳（§5.3）。

    戳里必须是"**这一个版本**"的 id 与哈希——那是装配时才知道的东西，
    所以骨架只声明位置（``interface="stamp"``），实现由装配器在这里注入。

    ``capabilities`` 每次执行时现取（能力集随时可变）：结构版本与能力集**分开记**，
    `toolset_updated` 事件靠它比对"这个会话上次看到的能力变了没有"。
    """

    async def stamp(state: dict) -> dict:
        harness: dict[str, Any] = {
            'version': version_id,
            'structure_hash': structure_hash,
            'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'base': base_id,
            'protocol': None,  # 协议插件（builtin/protocol）落地后填版本号
        }
        if capabilities is not None:
            harness['capabilities'] = capabilities()
        return {'harness': harness}

    return stamp


def _check_skeleton(base: Fragment) -> None:
    """骨架完整性（装配期第 7 条）：必需节点、锚点与出站接缝一个都不能缺。"""
    names = {node.name for node in base.nodes}
    missing_nodes = [name for name in REQUIRED_NODES if name not in names]
    if missing_nodes:
        raise StructureError(
            'SKELETON_NODES',
            f'骨架 {base.id!r} 缺必需节点：{"、".join(missing_nodes)}',
        )
    missing_anchors = [anchor for anchor in base.anchors if anchor not in names]
    if missing_anchors:
        raise StructureError(
            'SKELETON_ANCHORS',
            f'骨架 {base.id!r} 声明的锚点没有对应节点：{"、".join(missing_anchors)}',
        )
    missing_seams = sorted(REQUIRED_SEAMS - set(base.output_seams))
    if missing_seams:
        raise StructureError(
            'SKELETON_SEAMS',
            f'骨架 {base.id!r} 没有承诺产出这些出站接缝：{"、".join(missing_seams)}',
        )


def _merge_channels(fragments: list[Fragment]) -> dict[str, ChannelSpec]:
    """通道合并：同名通道的 reducer 必须一致（否则同一个键有两种合并语义）。"""
    merged: dict[str, ChannelSpec] = {}
    for fragment in fragments:
        for channel in fragment.channels:
            existing = merged.get(channel.name)
            if existing is None:
                merged[channel.name] = channel
                continue
            if existing.reducer is not channel.reducer:
                left = getattr(existing.reducer, '__name__', str(existing.reducer))
                right = getattr(channel.reducer, '__name__', str(channel.reducer))
                raise StructureError(
                    'REDUCER_MISMATCH',
                    f'通道 {channel.name!r} 有两种合并语义：{left} ≠ {right}',
                )
    if MESSAGES not in merged:
        raise StructureError(
            'CHANNEL_MISSING',
            f'骨架必须声明 {MESSAGES!r} 通道（add_messages 语义）',
        )
    return merged


def _channel_warnings(
    fragments: list[Fragment], channels: Mapping[str, ChannelSpec]
) -> list[str]:
    """装配成功但要明说的事：插件通道写了没人读 / 读了没人写。

    只看插件通道——内核通道由骨架自己声明并保证消费，这里要抓的是插件笔误。
    """
    warnings: list[str] = []
    writers: dict[str, list[str]] = {name: [] for name in channels}
    readers: dict[str, list[str]] = {name: [] for name in channels}
    for fragment in fragments:
        for node in fragment.nodes:
            for name in node.writes:
                writers.setdefault(name, []).append(node.name)
            for name in node.reads:
                readers.setdefault(name, []).append(node.name)
    for name in channels:
        if not name.startswith(PLUGIN_PREFIX):
            continue
        if writers.get(name) and not readers.get(name):
            warnings.append(f'通道 {name} 暂无读取者')
        if readers.get(name) and not writers.get(name):
            warnings.append(f'通道 {name} 暂无写入者')
    return warnings


def _check_connectivity(
    nodes: set[str],
    edges: list[tuple[str, str]],
    routes: list[tuple[str, list[str]]],
) -> None:
    """整图连通性（装配期第 6 条）：START 恰好一个后继、处处可达、处处能到 END。"""
    successors: dict[str, set[str]] = {name: set() for name in nodes}
    entry_targets: list[str] = []
    reaching_end: set[str] = set()
    for source, target in edges:
        if source == START_NODE:
            entry_targets.append(target)
        else:
            successors[source].add(target)
        if target == END_NODE:
            reaching_end.add(source)
    for source, targets in routes:
        successors[source].update(targets)
        if END_NODE in targets:
            reaching_end.add(source)

    if len(entry_targets) != 1:
        raise StructureError(
            'GRAPH_ENTRY',
            f'START 要恰好一个后继，现在有 {len(entry_targets)} 个：{entry_targets}',
        )

    seen = {START_NODE}
    stack = [entry_targets[0]]
    while stack:
        current = stack.pop()
        if current in seen or current == END_NODE:
            continue
        seen.add(current)
        stack.extend(successors.get(current, ()))
    unreachable = sorted(nodes - seen)
    if unreachable:
        raise StructureError('GRAPH_UNREACHABLE', f'这些节点从入口到不了：{unreachable}')

    reverse: dict[str, set[str]] = {name: set() for name in nodes}
    for source, target in edges:
        if source != START_NODE and target in reverse:
            reverse[target].add(source)
    for source, targets in routes:
        for target in targets:
            if target in reverse:
                reverse[target].add(source)
    able = set(reaching_end)
    stack = list(reaching_end)
    while stack:
        current = stack.pop()
        for source in reverse.get(current, ()):
            if source not in able:
                able.add(source)
                stack.append(source)
    dead_ends = sorted(nodes - able)
    if dead_ends:
        raise StructureError('GRAPH_DEADEND', f'这些节点到不了 END：{dead_ends}')


def compose(
    fragments: Iterable[Fragment],
    *,
    version_id: int,
    checkpointer: BaseCheckpointSaver,
    resolve_interface: Callable[[str], Any] | None = None,
    failures: FailureLedger | None = None,
    capabilities: Callable[[], dict[str, Any]] | None = None,
) -> GraphVersion:
    """把片段集合合成一个编译好的图版本；任何校验失败都抛 StructureError。

    ``failures`` 传入失败账本后，**插件贡献的节点**会被包进壳（失败收口 + 熔断）；
    骨架节点不包——内核 bug 要上抛（§4.2）。
    ``capabilities`` 是能力清单的现取值（进结构戳），与结构版本分开记。
    """
    ordered = list(fragments)
    bases = [item for item in ordered if item.role == ROLE_BASE]
    if len(bases) != 1:
        raise StructureError(
            'BASE_COUNT',
            f'需要恰好一个骨架（role="base"），现在有 {len(bases)} 个',
        )
    base = bases[0]

    # ---- 注册期规则全量重跑：直接 compose 未走 contribute 的片段也拦得住 ----
    seen_ids: set[str] = set()
    taken_names: set[str] = set()
    for fragment in ordered:
        if fragment.id in seen_ids:
            raise StructureError('FRAGMENT_DUPLICATE', f'片段 id 重复：{fragment.id!r}')
        seen_ids.add(fragment.id)
        validate_fragment_self(fragment)
        validate_against_base(fragment, base)
        validate_node_names(fragment, taken_names)
        taken_names.update(node.name for node in fragment.nodes)

    _check_skeleton(base)

    # ---- 锚点插链 ----
    mounted: dict[str, list[Fragment]] = {}
    for fragment in ordered:
        if fragment.role == ROLE_BASE:
            continue
        if fragment.edges:
            raise StructureError(
                'DAG_UNSUPPORTED',
                f'片段 {fragment.id!r} 声明了内部边：片段内 DAG 属于第二版（C2），'
                f'当前只支持节点顺序即执行顺序的链',
            )
        mounted.setdefault(fragment.anchor, []).append(fragment)

    warnings: list[str] = []
    nodes: dict[str, NodeSpec] = {node.name: node for node in base.nodes}
    owners: dict[str, str | None] = {node.name: None for node in base.nodes}
    edges: list[tuple[str, str]] = [
        (edge.source, edge.target)
        for edge in base.edges
        if not edge.is_conditional
    ]
    routes: list[tuple[str, Callable[..., Any], dict[str, str]]] = [
        (edge.source, edge.router, dict(edge.targets))
        for edge in base.edges
        if edge.is_conditional
    ]

    for anchor, chain_fragments in mounted.items():
        if len(chain_fragments) > 1:
            warnings.append(
                f'锚点 {anchor} 上挂了多个片段：'
                + '、'.join(item.id for item in _ordered_chain(chain_fragments))
                + '（按 order / 装载顺序串成一条链）'
            )
        chain: list[NodeSpec] = []
        for fragment in _ordered_chain(chain_fragments):
            for node in fragment.nodes:
                if node.name in nodes:
                    raise StructureError(
                        'NODE_CONFLICT', f'节点名与既有结构冲突：{node.name!r}'
                    )
                nodes[node.name] = node
                owners[node.name] = fail_key(fragment.id, fragment.scope)
                chain.append(node)
        if not chain:
            continue  # 只声明通道、不挂节点：合法
        head, *rest = chain
        # 锚点的出边（无条件边与条件边的源）改接到链尾
        tail_name = chain[-1].name
        edges = [
            (tail_name if source == anchor else source, target)
            for source, target in edges
        ]
        routes = [
            (tail_name if source == anchor else source, router, targets)
            for source, router, targets in routes
        ]
        # 锚点 → 链头 → … → 链尾
        edges.append((anchor, head.name))
        for previous, current in zip(chain, rest):
            edges.append((previous.name, current.name))

    # ---- 通道合并 + 动态 schema ----
    channels = _merge_channels(ordered)
    warnings.extend(_channel_warnings(ordered, channels))
    state_type = build_state_type(channels, f'ComposedState{version_id}')

    # ---- 结构快照 + 哈希（实现不进哈希，只记名字）----
    spec = {
        'fragments': [fragment_spec(item) for item in ordered],
        'nodes': sorted(nodes),
        'edges': sorted([list(edge) for edge in edges]),
        'routes': [
            {'source': source, 'targets': dict(targets)}
            for source, _router, targets in routes
        ],
        'channels': [
            {
                'name': channel.name,
                'reducer': getattr(channel.reducer, '__name__', None),
            }
            for channel in sorted(channels.values(), key=lambda item: item.name)
        ],
        'anchors': list(base.anchors),
    }
    structure_hash = hashlib.sha256(
        json.dumps(spec, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')
    ).hexdigest()

    # ---- 节点实现（自带实现 / 装配器注入 / 从服务解析）----
    stamp_spec = nodes.get('stamp')
    if stamp_spec is not None and stamp_spec.interface != STAMP_INTERFACE:
        raise StructureError(
            'STAMP_SHAPE',
            f'骨架的 stamp 节点必须声明 interface={STAMP_INTERFACE!r}'
            f'（实现由装配器注入，结构戳要带本版本的 id 与哈希）',
        )
    stamp_impl = make_stamp_node(
        version_id=version_id,
        structure_hash=structure_hash,
        base_id=base.id,
        capabilities=capabilities,
    )

    impls: dict[str, Callable[..., Any]] = {}
    for name, node in nodes.items():
        if node.run is not None:
            impls[name] = node.run
            continue
        if name == 'stamp' and node.interface == STAMP_INTERFACE:
            impls[name] = stamp_impl  # 装配器强制注入（§5.3）
            continue
        resolved = resolve_interface(node.interface) if resolve_interface else None
        if resolved is None:
            raise StructureError(
                'INTERFACE_MISSING',
                f'抽象节点 {name!r} 的实现 {node.interface!r} 暂时不可用（服务未提供）',
            )
        impls[name] = resolved

    # ---- 节点壳：插件贡献的节点包进壳（失败收口 + 熔断），骨架节点不包 ----
    if failures is not None:
        for name in list(impls):
            owner = owners.get(name)
            if owner is not None:
                impls[name] = wrap_node(name, impls[name], owner, failures)

    # ---- 图级校验 ----
    _check_connectivity(
        set(nodes),
        edges,
        [(source, list(targets.values())) for source, _, targets in routes],
    )

    # ---- 构建 + 编译 ----
    builder = StateGraph(state_type)
    for name, impl in impls.items():
        builder.add_node(name, impl)
    for source, target in edges:
        builder.add_edge(
            START if source == START_NODE else source,
            END if target == END_NODE else target,
        )
    for source, router, targets in routes:
        builder.add_conditional_edges(
            source,
            router,
            {key: (END if value == END_NODE else value) for key, value in targets.items()},
        )
    graph = builder.compile(checkpointer=checkpointer)

    return GraphVersion(
        id=version_id,
        structure_hash=structure_hash,
        spec=spec,
        impls=impls,
        graph=graph,
        warnings=warnings,
        created_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
        anchors=tuple(base.anchors),
        base_id=base.id,
    )


def render_mermaid(graph: Any) -> str:
    """把编译好的图画成 mermaid（空锚点也在图里——它们是普通节点）。"""
    return graph.get_graph().draw_mermaid()
