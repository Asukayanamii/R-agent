"""builtin/sandbox：沙箱**策略**（服务 ``sandbox``）。

机制留内核（`app/agent/sandbox.py`）：判定顺序、授权文件与**硬阻断**语义、`interrupt` 通道；
这里只给策略——禁区名单、受信只读根、工作区外是否询问。卸下 = 回到默认策略（= 改造前行为）。

**配置就是它的旋钮**（`Config`）：`deny_read` / `deny_write`（glob 列表）、`trusted_read_roots`、
`ask`。默认值 = `app/agent/sandbox.py` 里的常量——安全默认，不是"空名单"。

**诚实记录**：策略经模块级 `use_policy()` 安装，因为工具（`read`/`write`/…）是模块级函数、
在 20+ 处直接调 `guard_path`。彻底服务化（策略当参数传进每个工具）留到"有第二个策略实现"时再做；
这里保证的是：安装/还原成对（`ctx.cleanup`）、卸载即回默认。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.agent import sandbox
from app.kernel import Schema, Service

PLUGIN_ID = 'builtin/sandbox'
SANDBOX_CONTRACT = 'sandbox:v1'


class Config(Schema):
    deny_read = Schema.array(Schema.string()).default(list(sandbox.DENY_READ)).description(
        '禁读 glob（命中即硬阻断、永不提示）'
    )
    deny_write = Schema.array(Schema.string()).default(list(sandbox.DENY_WRITE)).description(
        '禁写 glob（比读多 .env.* —— 真实配置不该被改）'
    )
    trusted_read_roots = Schema.array(Schema.string()).default([]).description(
        '受信只读根：读这些目录不弹授权（写仍要）'
    )
    ask = Schema.boolean().default(True).description('工作区外弹卡片询问；false = 直接拒绝')


def default_config() -> dict:
    """默认策略 = 内核里的常量（`.env` 的 SKILLS_DIR 会自动成为受信根，在沙箱那侧叠加）。"""
    policy = sandbox.SandboxPolicy()
    return {
        'deny_read': list(policy.deny_read),
        'deny_write': list(policy.deny_write),
        'trusted_read_roots': list(policy.trusted_read_roots),
        'ask': policy.ask,
    }


class SandboxService(Service):
    """服务 ``sandbox``：当前策略 + `trust(path)`（给别的插件补受信只读根）。"""

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, 'sandbox')
        self.policy = sandbox.SandboxPolicy.from_mapping(config)
        self._previous = sandbox.use_policy(self.policy)
        ctx.cleanup(lambda: sandbox.use_policy(self._previous), 'builtin/sandbox.policy')

    def trust(self, path: Any) -> None:
        """把一个目录加进受信只读根（重复加没关系）。用于随包带的技能库这类应用自己的资产。"""
        raw = str(path)
        if raw in self.policy.trusted_read_roots:
            return
        self.policy = replace(
            self.policy, trusted_read_roots=(*self.policy.trusted_read_roots, raw)
        )
        sandbox.use_policy(self.policy)


def apply(ctx: Any, config: Any = None) -> None:
    service = SandboxService(ctx, config)
    policy = service.policy
    ctx.logger.info(
        '沙箱策略已就绪：禁区 %d/%d 条，受信根 %d 个，工作区外 %s',
        len(policy.deny_read),
        len(policy.deny_write),
        len(policy.trusted_read_roots),
        '询问' if policy.ask else '直接拒绝',
    )


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'provides': SANDBOX_CONTRACT,
    'Config': Config,
}
