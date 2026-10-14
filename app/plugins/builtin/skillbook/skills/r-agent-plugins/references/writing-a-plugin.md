# 写一个插件

## 先选形态

- **只改结构（接线）** → `.json` 纯声明片段：把**已有服务**接成一条链。不执行代码，最安全。
- **要有新能力（服务/工具/资产/后台任务）** → `.py` 可编程插件。

两种形态在同一个插件库里混放没问题；`_` 开头的是辅助文件（不会被当插件）。

## `.json` 逐字段

```json
{
  "id": "workspace/research",
  "anchor": "pre_model",
  "order": 0,
  "chain": [
    {"name": "fetch_docs", "interface": "fetch", "reads": ["messages"]},
    {"name": "summarize",  "interface": "summarize", "writes": ["plugin.workspace/research.notes"]}
  ],
  "channels": [{"name": "plugin.workspace/research.notes", "default": null}]
}
```

| 字段 | 规则 |
| --- | --- |
| `id` | 必填，作用域内唯一（不同工作区可以同名） |
| `anchor` | 必填，骨架锚点之一：`entry` / `pre_model` / `post_model` / `post_tools` / `finish` |
| `order` | 选填，同一锚点上多片段时的排序（小在前） |
| `chain` | 必填非空；**顺序就是执行顺序**；每项必须有 `name` 与 `interface` |
| `interface` | **服务名**：已经有插件 `ctx.provide('fetch', ...)` 才能装载；先 `inspect_graph` 看 `services`（`callable: true` 的才能直接当节点） |
| `reads` / `writes` | 选填，声明用到的通道（`reads` 不强制，`writes` 会被校验命名空间） |
| `channels` | 选填，声明自己的通道；**不能写 `reducer`**（合并语义是代码） |

装载前会**预检**：JSON 合法 → 形状合法 → `interface` 有实现。任何一条不过：文件按名字点名报错、
图一动不动（不会把整张合成打回）。

## `.py` 骨架

```python
"""我的插件：给 pre_model 加一步检索。"""

from app.kernel import Schema, Service

class Fetch(Service):
    """服务 `fetch`：抽象节点的实现就是它（可调用 → 直接当节点用）。"""

    def __init__(self, ctx):
        super().__init__(ctx, 'fetch')

    async def __call__(self, state: dict) -> dict:
        return {}      # 节点写状态：只能写自己声明的通道

def apply(ctx, config=None):
    Fetch(ctx)                                   # 注册服务
    ctx.logger.info('fetch 已就绪')               # 级别：error / warn / info / debug

plugin = {'apply': apply, 'name': 'workspace/fetch', 'inject': ['tools']}
```

要点：

- `plugin` 是**约定入口**（没有它装载失败）：`apply` 必有；`name` 建议 `作用域/名字`；
  `inject` 声明依赖的服务（缺了 = PENDING，不报错、不装载，报告里点名）；`Config` 声明可配置项。
- `apply` 里注册的东西**都挂在当前 fiber** 上：插件卸载（或热重载、或工作区被 LRU 回收）时自动撤销。
  别在模块顶层注册——那不在任何 fiber 上，卸不掉。
- `Config` 用 `Schema` 声明（`Schema.string()/integer()/boolean()/array(...)/object({...})`），
  配上 `default_config()` 返回默认值（通常引用 `.env` 的总体配置）。**校验不过 = 拒绝装载**。
  只声明 `Config` 不给 `default_config()` 也行：那时吃 schema 里的 `.default(...)`；
  必填字段既没默认值又没人给配置 = 拒绝装载（不是"悄悄给个 None"）。
  两个入口都吃这份配置：内置插件由装配根调 `default_config()`，插件库里的文件由发现器调。
- 需要异步清理（连接、任务）用 `ctx.cleanup(fn)` 或 `ctx.resource.acquire(open_fn)`；
  需要后台任务用 `ctx.spawn(coro)`——两者卸载时都会收干净。
- 想贡献**片段**（图里多一条链）就在 `apply` 里 `ctx.structure.contribute(Fragment(...))`；
  工作区插件不必自己填 `scope`，装载方会带上（`PLUGIN_SCOPE`）。
- 想要**自己的前端**：`ctx.assets.mount('panel', <目录绝对路径>)` → 页面在 `/panel/…`
  （目录里默认找 `index.html`）。一个进程可同时挂多个 alias；内置调试前端占 `/ui`，别去抢。
  资产是**进程级**的（不按工作区分域）。

## 片段与通道的硬规则

- 片段是**链 / DAG**，不许有环；边不许逃逸出片段（想回 `agent` 重问 = 抢路由权，不允许）；
- **条件边只有骨架能写**（`role="base"`），插件片段只能写无条件边——当前合成器只支持"节点顺序即执行顺序"；
- 通道名：内核通道 `messages` / `compaction` / `usage_anchor` / `harness` / `plugin_failures`
  只有骨架能写；插件只能写 `plugin.<片段id>.<字段>`；
- 节点名全局唯一（同作用域内），与骨架或既有片段冲突会被拒；
- "再来一轮"写在节点**代码**里（循环、重试），不要试图在图里连回去。

## 装上去之后

1. 看日志里的**合成报告**：参与片段、每个锚点的挂载链、待批准、失败原因、生效时机；
2. 用 `inspect_graph` 工具或审计表 `graph_version` 看"现在是什么、上一次为什么换"；
3. 结构变化**下一个回合边界**生效——同一轮里看不到自己的新链（刻意的）；
4. **改实现 ≠ 换图**：节点实现在合成那一刻**按值冻结**进图，运行期不按名字回查。改"只提供服务"的
   `.py`（节点用 `interface` 指向它）只会重载模块、**不会重新合成**——注册表里是新服务，
   已合成进图的节点仍跑旧实现（静默分叉）。要让新实现上车：动一下片段集合（改一下引用它的
   `.json`，哪怕只改一个字段）或等别的结构变化，再过**一个回合边界**。片段自带的 `run` 没有这个
   问题（文件一变片段重挂，下次合成就是新的）；结构哈希只记名字、不含实现，实现替换即使搭上了
   换图也不会发 `graph_updated` 通知。
