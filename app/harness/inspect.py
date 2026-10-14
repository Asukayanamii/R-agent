"""服务 ``graph``：把 runtime 的报告 / 片段清单 / 审计包成**给模型看的只读视图**。

`builtin/graph_tools` 的 `inspect_graph` 与排查都吃这一份；"改前/改后/依据/怎么退"里的前三样
都能从这里读到（依据在对话里，怎么退 = 删文件或撤信任）。

只读：这里的任何方法都不改图——改图只有两条路（装配根装载插件、工作区插件库的声明 + 信任）。
"""

from __future__ import annotations

from typing import Any


class GraphInfo:
    """服务 ``graph``：`report(scope)` / `spec(scope)` / `recent(scope)`。"""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def report(self, scope: str = '') -> dict:
        """当前合成报告（结构哈希、参与片段、挂载链、待批准/未参与、警告……）。"""
        return self.runtime.build_report(scope=scope).to_dict()

    def spec(self, scope: str = '') -> dict:
        """当前**运行中**版本的片段清单（节点、边、路由、通道）——"改后是什么"的原始依据。"""
        version = self.runtime.registry(scope).current
        return dict(version.spec) if version is not None else {}

    def scopes(self) -> list[str]:
        return list(self.runtime.scopes)

    async def recent(self, scope: str = '', limit: int = 10) -> list[dict]:
        """最近几次版本切换（审计表）：改了什么、为什么、什么时候。没有审计服务就给空表。"""
        service = self.runtime.ctx.get('graph_versions')
        if service is None:
            return []
        return await service.recent(scope, limit)
