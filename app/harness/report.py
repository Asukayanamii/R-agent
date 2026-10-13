"""合成报告：由代码生成的结构说明书（设计稿 §3.2）。

报告是 ``dataclass → JSON`` 的结构化对象（不是拼字符串），一份同时供三处：
日志、``graph_updated`` 事件、``inspect_graph`` 工具（阶段 F）。
**字段全部生成**——"生效时机"、骨架与协议版本、每个挂载点的触发频率都不是手写的，
所以不存在"忘了更新报告"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ['ANCHOR_FREQUENCIES', 'CompositionReport', 'Mount']

ANCHOR_FREQUENCIES: dict[str, str] = {
    'entry': '每轮 1 次',
    'pre_model': '每次模型调用前',
    'post_model': '每次模型输出后',
    'post_tools': '每次工具执行后',
    'finish': '每轮 1 次（终结轮）',
}
"""标准锚点的触发频率描述（展示用）；自定义锚点没有条目就按"由骨架声明"呈现。"""


@dataclass(frozen=True)
class Mount:
    """一个锚点上的挂载。"""

    anchor: str
    chain: tuple[str, ...]
    """挂在这个锚点上的片段 id（按生效顺序；空 = 透传）。"""

    @property
    def frequency(self) -> str:
        return ANCHOR_FREQUENCIES.get(self.anchor, '由骨架声明')

    @property
    def empty(self) -> bool:
        return not self.chain


@dataclass(frozen=True)
class CompositionReport:
    """一次合成的完整说明书。"""

    version: int
    structure_hash: str
    base: str
    protocol: str | None
    participants: tuple[str, ...]
    not_participating: tuple[dict[str, Any], ...]
    disabled: tuple[dict[str, Any], ...]
    mounts: tuple[Mount, ...]
    warnings: tuple[str, ...]
    scope: str = ''
    """作用域：阶段 J 起 = 会话工作区；空串 = 全局（内置插件那套图）。"""
    pending_change: bool = False
    """碎片集合已经变了、但还没到回合边界：报告里的挂载链是"现状"，不是正在跑的图。"""
    effective_at: str = '能力类变更已即时生效；结构变更随本版本在本轮之后生效'

    def to_dict(self) -> dict[str, Any]:
        return {
            'version': self.version,
            'structure_hash': self.structure_hash,
            'base': self.base,
            'protocol': self.protocol,
            'participants': list(self.participants),
            'not_participating': [dict(item) for item in self.not_participating],
            'disabled': [dict(item) for item in self.disabled],
            'mounts': [
                {'anchor': item.anchor, 'chain': list(item.chain), 'frequency': item.frequency}
                for item in self.mounts
            ],
            'warnings': list(self.warnings),
            'scope': self.scope,
            'pending_change': self.pending_change,
            'effective_at': self.effective_at,
        }

    def render(self) -> str:
        """人读的多行文本（日志用）。"""
        lines = [
            f'图 v{self.version}  结构哈希 {self.structure_hash[:12]}  '
            f'骨架: {self.base}   协议: {self.protocol or "（未装载）"}',
            f'参与: {"、".join(self.participants)}',
        ]
        if self.not_participating:
            lines.append(
                '未参与: '
                + '；'.join(f'{item["id"]}（{item["reason"]}）' for item in self.not_participating)
            )
        if self.disabled:
            lines.append(
                '停用: '
                + '；'.join(f'{item["id"]}（{item["reason"]}）' for item in self.disabled)
            )
        for index, mount in enumerate(self.mounts):
            head = '挂载: ' if index == 0 else '      '
            if mount.empty:
                lines.append(f'{head}{mount.anchor:<10} ← （空，透传，{mount.frequency}）')
            else:
                lines.append(
                    f'{head}{mount.anchor:<10} ← {"、".join(mount.chain)}（{mount.frequency}）'
                )
        lines.append(f'生效时机: {self.effective_at}')
        if self.pending_change:
            lines.append('待生效: 片段集合已变，挂载链是**现状**；下一个回合边界换图')
        for index, warning in enumerate(self.warnings):
            head = '警告: ' if index == 0 else '      '
            lines.append(f'{head}{warning}')
        return '\n'.join(lines)
