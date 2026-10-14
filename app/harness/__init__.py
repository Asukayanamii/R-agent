"""app/harness：装配层——把插件贡献的片段合成成一张具体的图，并管版本。

分层：``kernel``（不依赖 app.*）→ ``harness``（→ kernel、langgraph）→
``plugins``（→ harness、agent 实现）→ ``container``。

- ``fragment.py``：契约（片段/节点/边/通道的纯数据 + 校验）；
- ``composer.py``：收集 → 两段校验 → 通道合并 → 锚点插链 → 动态 schema → 编译；
- ``versions.py``：pending/current、租约、兼容判定、回收；
- ``runtime.py``：``HarnessRuntime``——LangGraphRunner 的唯一入口。
"""
