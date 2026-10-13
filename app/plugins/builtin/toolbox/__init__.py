"""builtin/toolbox：必需插件——服务 ``tools``：工具注册表 + 能力快照。

两条约定（§4.1）：

- **能力集与结构版本分开记**：增删工具不改图，只改快照——模型节点每次调用前用新快照
  重建绑定，执行节点每次调用现查注册表。`digest` 是能力清单的稳定摘要（跨进程可比），
  供 `toolset_updated` 事件判断"变没变"；
- **注册即 effect**：`ctx.tools.register(...)` 挂在调用方 fiber 上，插件卸载自动撤销。

**对外声明事件** ``tools/pre-execute``：每个调用执行前广播一次（观测/审计的挂点）。
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable

from app.agent.tool_calls import make_tools_node
from app.agent.tools import TOOLS
from app.kernel import Service

PLUGIN_ID = 'builtin/toolbox'


class Toolbox(Service):
    """服务 ``tools``：图上 `tools` 节点的实现与能力快照都从这里来。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx, 'tools')
        self._tools: dict[str, Any] = {tool.name: tool for tool in TOOLS}
        self.revision = 0
        self._node: Callable[..., Any] | None = None

    # ---------------------------------------------------------------- 注册
    def register(self, tool: Any) -> Any:
        """注册一个工具（随调用方 fiber 卸载自动撤销）；同名**顶替**，卸载时还原被顶替的那个。

        顶替要能回退：插件可以"升级"一个内置工具（同名注册、行为是超集），卸载它就该回到
        原来那个实现——否则撤掉插件的那一刻工具会凭空消失，而"注册即 effect"承诺的是
        **回到注册前的状态**，不是"删掉这个名字"。
        """

        def install() -> Callable[[], bool]:
            previous = self._tools.get(tool.name)
            self._tools[tool.name] = tool
            self.revision += 1

            def withdraw() -> bool:
                if self._tools.get(tool.name) is not tool:
                    return False
                if previous is not None:
                    self._tools[tool.name] = previous
                else:
                    del self._tools[tool.name]
                self.revision += 1
                return True

            return withdraw

        return self.ctx.effect(install, f'ctx.tools.register({tool.name!r})')

    def unregister(self, name: str) -> bool:
        """移除一个工具；返回是否真的移除了。"""
        if name not in self._tools:
            return False
        del self._tools[name]
        self.revision += 1
        return True

    # ---------------------------------------------------------------- 查询
    def get(self, name: str) -> Any:
        return self._tools.get(name)

    def snapshot(self) -> tuple[Any, ...]:
        """当前能力快照（按名字排序，绑定与查找都用它）。"""
        return tuple(self._tools[name] for name in sorted(self._tools))

    def names(self) -> list[str]:
        return sorted(self._tools)

    @property
    def digest(self) -> str:
        """能力清单摘要：只随"有哪些工具"变，跨进程稳定（`toolset_updated` 比对它）。"""
        return hashlib.sha256(','.join(sorted(self._tools)).encode('utf-8')).hexdigest()[:12]

    # ---------------------------------------------------------------- 审批与拦截点
    def requires_approval(self, name: str) -> bool:
        """这次调用要不要先人工确认——**现取**审批策略（收紧类变更即时生效）。"""
        approvals = self._service_ctx.get('approvals')
        return approvals is not None and approvals.requires(name)

    def pre_execute(self, call: dict) -> None:
        """执行前广播（观测/审计挂点）。"""
        self._service_ctx.emit('tools/pre-execute', call)

    # ---------------------------------------------------------------- 抽象节点
    @property
    def node(self) -> Callable[..., Any]:
        """服务暴露成图上的 `tools` 节点（抽象节点的实现从服务解析）。"""
        if self._node is None:
            self._node = make_tools_node(self)
        return self._node


def apply(ctx: Any) -> None:
    toolbox = Toolbox(ctx)
    ctx.logger.info('工具箱已就绪：%d 个工具，能力摘要 %s', len(toolbox.names()), toolbox.digest)


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
}
