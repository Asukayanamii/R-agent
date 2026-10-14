"""builtin/webui：可选插件——调试前端（静态资产），alias ``/ui``。

界面类接缝（§2.5）：资产走 ``ctx.assets.mount()``，**表驱动**——装载即 200、卸载即 404，
全程不动 Starlette 的路由表。它是**可选插件**：卸下只丢 ``/ui``，``/chat/*`` 照常
（需要更复杂的界面时，换一份 webui 插件目录即可，内核零改动）。

它依赖协议的形状表（``requires: "protocol:v1"``）——前端认识的 type 与协议里的事件类型
是一份约定；协议 bump 到 v2 而这里没跟，会被**拒绝装载**并报明原因。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

PLUGIN_ID = 'builtin/webui'
ALIAS = '/ui'
STATIC_DIR = Path(__file__).parent / 'static'


def apply(ctx: Any) -> None:
    """把前端资产挂到 ``/ui``（随插件卸载自动撤销）。"""
    ctx.assets.mount(ALIAS, STATIC_DIR)
    ctx.logger.info('前端已挂载：%s → %s', ALIAS, STATIC_DIR)


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'requires': 'protocol:v1',
}
