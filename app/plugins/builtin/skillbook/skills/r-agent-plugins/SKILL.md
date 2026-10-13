---
name: r-agent-plugins
description: 给 R-agent 写插件、启用插件或改图时读它。讲清插件内核的原理（骨架 / 片段 / 锚点 / 契约 / 信任）、两种插件形态（.json 纯声明片段、.py 可编程插件）的写法与全部校验规则、插件库放哪与生效时机（回合边界）、以及"插件为什么不生效"的排查与回退。要写插件、要加一条图内链、要用 add_fragment/install_plugin、或插件没生效时用。
---

# R-agent 插件

这份技能讲三件事：**原理**（为什么这么设计）、**怎么写**（两种形态 + 校验规则）、
**怎么启用**（放哪、谁批准、什么时候生效、怎么查、怎么退）。

## 原理：一张图 = 骨架 + 挂载在锚点上的链

- **骨架**（`role="base"`，只有 `builtin/loop` 是）决定执行流程与**路由**：
  `stamp → entry → compact → pre_model → agent → post_model`，之后按有没有工具调用分流——
  有工具走 `tools → post_tools → compact`，没有走 `finish → END`。路由权**只在骨架**：
  插件写不出条件边，也改不了别的节点（换实现只能换服务）。
- **锚点**是骨架留给插件的五个坑：`entry`、`pre_model`、`post_model`、`post_tools`、`finish`。
  每个锚点在骨架里是**透传节点**：没人挂就什么都不做（不是"折叠掉"，图上一直看得见）。
- **片段**是一段**链**：挂到某个锚点上，节点按声明顺序依次执行（链尾接回锚点原来的出边）。
  节点有两种：带 `run`（自己的代码）或带 `interface`（**服务名**，实现由别的插件给）。
- **组装在回合边界生效**：插件装载/卸载/改文件只改"结构来源"，新的图在**下一个回合开始**时替换；
  正在跑的那一轮始终用旧图。**合成失败不改运行图**——坏插件不会把正在跑的会话带走。
- **一切皆插件**：协议、前端、工具箱、模型、重试、存储、沙箱策略、压缩、上下文注入……
  都是 `app/plugins/builtin/` 下的同级插件，靠服务（`ctx.provide`）与契约（`provides`/`requires`）互相认。

## 怎么写

**形态一：`.json` 纯声明片段**（不执行代码，最适合"把已有能力接进图"）：

```json
{
  "id": "workspace/research",
  "anchor": "pre_model",
  "order": 0,
  "chain": [
    {"name": "research_notes", "interface": "research", "reads": ["messages"]}
  ],
  "channels": [{"name": "plugin.workspace/research.notes"}]
}
```

- `id` 作用域内唯一；`anchor` 必须是骨架锚点之一；`chain` 非空，节点必须有 `name` 与
  `interface`（**服务名**，必须已经有插件提供它，否则装载时被挡下，报 `INTERFACE_MISSING`）。
  先调 `inspect_graph` 看 `services`：那里列出当前所有能当 `interface` 的服务名，
  `callable: true` 的才可以直接当节点实现（内核服务如 `tools`、`protocol` 不可调用）。
- 通道名只能是自己的命名空间 `plugin.<片段id>.<字段>`（内核通道 `messages`/`compaction`/
  `usage_anchor`/`harness`/`plugin_failures` 只有骨架能写）；`reducer`（合并语义）写不进 JSON。

**形态二：`.py` 可编程插件**（装载即执行代码）：

```python
from app.kernel import Schema, Service

class Notes(Service):
    def __init__(self, ctx):
        super().__init__(ctx, 'research')      # 注册服务名 → 抽象节点就能找到实现
    async def __call__(self, state):           # 服务本身可调用时，它就是节点实现
        return {}

class Config(Schema):
    enabled = Schema.boolean().default(True)

def default_config():                          # 默认值引用总体配置（.env）
    return {'enabled': True}

def apply(ctx, config=None):
    Notes(ctx)
    ctx.tools.register(my_tool)                # 挂在调用方 fiber 上，卸载自动撤销

plugin = {
    'apply': apply,
    'name': 'workspace/notes',
    'inject': ['tools'],                       # 依赖的服务（缺了会 PENDING，不报错）
    'provides': 'research:v1',                  # 对外契约；requires 对不上会**拒绝装载**
    'Config': Config,
}
```

`apply(ctx)` 里能用的接缝（都随插件卸载自动撤销）：

| 接缝 | 用途 |
| --- | --- |
| `ctx.provide(name, value)` | 提供服务（服务名就是抽象节点的 `interface`） |
| `ctx.structure.contribute(fragment)` | 贡献片段（code 里手写 `Fragment`） |
| `ctx.tools.register(tool)` | 加工具（下一次模型调用就能用，不用重建图） |
| `ctx.assets.mount(alias, dir)` | 挂静态资产（前端插件用的就是它） |
| `ctx.cleanup(fn)` / `ctx.resource.acquire(fn)` | 卸载时清理（异步清理也支持） |
| `ctx.spawn(coro)` | 后台任务（卸载时取消并等待） |
| `ctx.logger.info(...)` | 日志（级别方法：`error` / `warn` / `info` / `debug`——是 `warn` 不是 `warning`，且不支持 `%.2f` 这类精度写法） |

**校验规则**（写上就懂，装不上会给出稳定错误码）：片段不许有内部边逃逸、不许成环、插件片段不许写条件边；
节点名不许与骨架/已装载片段冲突；通道命名必须在自己命名空间里；`Config` 校验不过 = 拒绝装载。

## 怎么启用

| 放哪 | 谁批准 | 说明 |
| --- | --- | --- |
| `PLUGINS_DIR`（默认 `~/.my_agent/plugins`） | **装载即信任** | 你自己的全局库 |
| `<工作区>/.my_agent/plugins/` | 每个文件要出现在该工作区 `<工作区>/.my_agent/plugins.json` 的 `trusted` 里 | 需 `PLUGINS_WORKSPACE=1`；作用域 = 会话工作区 |

- **一个文件一个插件**：`.py`（可编程）/ `.json`（纯声明）；`_` 开头的是作者的辅助文件，不当插件。
- **写文件 ≠ 生效**：没进信任清单就不会被装载，只在报告里点"待批准"。信任清单在沙箱里是**硬阻断区**
  （读写都拒、永不提示）——agent 自己写不了它，只有人（或 `add_fragment` 批准后的服务端代码）能写。
- **生效时机**：改动（增/删/改）在**下一个回合边界**换图；批准是即时的（装载当下发生），生效仍在边界。
- **验证**（从这四处看）：启动日志里的"合成报告"（骨架/协议/参与片段/锚点挂载/待批准/失败）、
  `inspect_graph` 工具（报告 + 片段清单 + **可当 `interface` 的服务清单** + 审计）、
  审计表 `graph_version`（每次换图一行，含 `reason`）、
  `python -m app.harness`（导出当前骨架的 mermaid 图）。
- **生效时机分两类**：结构（片段）按回合边界换图；**能力类（`ctx.tools.register`、`ctx.assets.mount`、
  `ctx.provide`）装载即生效**——注册完工具，同一轮的下一次模型调用就能看到它（不用等边界）。
  注意能力类是**进程级**的：工作区插件注册的工具对所有会话可见；按工作区分域的只有结构版本。
- **回退**：删掉插件文件（回合边界回到原样），或把文件名从 `trusted` 里去掉（撤信任）。
  节点连炸三次会被**熔断**（自动摘出该片段、报告里点名），修好后改文件即重新装载。

## 常见错与对策

| 现象 | 原因 | 对策 |
| --- | --- | --- |
| 日志点"待批准" | 文件没进信任清单 | 让人批准，或写进 `<工作区>/.my_agent/plugins.json` |
| 报 `INTERFACE_MISSING` | 片段里的 `interface` 还没有服务提供 | 先装提供该服务的插件，或换服务名 |
| 报 `ANCHOR_UNKNOWN` | 锚点不是骨架声明的五个之一 | 用 `inspect_graph` 看锚点表 |
| 报 `FRAGMENT_DUPLICATE` | 同一作用域里片段 `id` 重复 | 换 `id`（不同工作区可以同名） |
| 图里看不到新链 | 还没到回合边界 / 被熔断 / 合成失败 | 看合成报告与 `graph_version` 的 `reason` |
| 报告里"未参与" | 插件 PENDING（`inject` 的服务没到） | 看它缺哪个服务名 |

深入细节（完整示例与逐条规则）：

- `references/writing-a-plugin.md`
- `references/enabling-a-plugin.md`
