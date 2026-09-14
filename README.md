# my-agent

基于 LangGraph 的可扩展 Agent 后端，FastAPI 提供 SSE 流式接口，事件协议与图实现解耦。

## 环境要求

- Python 3.13（conda 环境名 `my_agent`）
- Windows / Linux / macOS 均可

## 依赖安装

```bash
conda env create -f environment.yml
conda activate my_agent
```

已有环境或使用 venv：`pip install -r requirements.txt`。
桌面模式额外需要 `pip install -r requirements-desktop.txt`。

## 配置

```bash
cp .env.example .env
```

填入 `LLM_API_KEY`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 无 | **没配就没法对话**：发消息会直接收到一条 `error` 事件说明原因。会话列表、历史、浏览目录、删除会话都不依赖模型，照常可用 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | 百炼填 `https://dashscope.aliyuncs.com/compatible-mode/v1`，Ollama 填 `http://localhost:11434/v1` |
| `LLM_MODEL` | `deepseek-chat` | 百炼可用 `qwen-plus`，Ollama 可用 `qwen3:8b` |
| `SQLITE_PATH` | `./data/checkpoints.db` | 会话状态落盘位置。相对路径按项目根解析，不受启动目录影响；留空则仅存于进程内存 |
| `AGENT_STUB` | `0` | 置 `1` 时用桩实现（复读 + 假的工具卡片 / 人工确认），用于没有模型时调前端交互。**不是兜底**，只认显式开关 |
| `LOG_LEVEL` | `INFO` | 日志级别，约定见下面「日志」。排查问题时改 `DEBUG` 重启 |

## 启动

```bash
python -m uvicorn app.main:app --reload --port 8000
```

- 调试页面：<http://127.0.0.1:8000/ui>
- 接口文档：<http://127.0.0.1:8000/docs>

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/chat/stream` | 发起一轮对话，SSE 流 |
| POST | `/chat/resume` | 带着用户选择，从 `interrupt` 处继续，SSE 流 |
| GET | `/chat/history` | 读取既有会话消息，供前端恢复 |
| GET | `/chat/threads` | 列出既有会话，按最近更新倒序；一并给出默认工作区 |
| DELETE | `/chat/threads/{id}` | 删除会话，硬删；不存在的会话静默通过（幂等） |
| PUT | `/chat/workspace` | 把会话绑定到工作区，即沙箱的信任边界（新会话诞生时写一次） |
| GET | `/chat/browse` | 列出目录，供挑选工作区 |

非流式接口走 `Result` 包装（`{code, message, data}`，`code=0` 为成功）。

**异常有两个出口，各自兜住全部异常**：流式在 `app/event/stream.py` 补一帧 `error` + `done`，
非流式由 `app/handler/` 的全局处理器包成 500 + `Result`。两边都返回异常原文（堆栈只进服务端日志），
所以上游（模型服务、数据库）出错既不会带走进程，前端也总能拿到明确的结束标记。

**停止一轮 = 断开这条流。** 点停止（或按 `Esc`）会中止当前请求，服务端在取消里做收尾：
补索引行、给没拿到结果的工具调用补一条"这一轮被中断，未取得结果。"的结果——所以停完的会话
**还能接着用**，不会变成只能新建的坏死会话。手动停止与意外断开（刷新页面、断网、服务挂了）
走的是同一条路、同一个标记「已中断」，界面不做区分。

运行中再发消息是**排队**：Enter 不打断当前工作，本轮结束后自动发出（和 Claude Code 一致）。
运行中不允许切会话/删会话——先停止或等它结束。

`/chat/stream` 请求体：

```json
{"message": "你好", "thread_id": "可选，不传则新建"}
```

`/chat/resume` 请求体（`thread_id` 必须与中断时一致）：

```json
{"thread_id": "demo", "value": "拒绝"}
```

### 事件协议

两个流式接口响应均为 `text/event-stream`，每帧 `data` 形如 `{"type": ..., "data": {...}}`：

| type | 说明 |
| --- | --- |
| `thread` | 会话握手，携带 `thread_id`，必为第一帧 |
| `text_delta` | 增量文本，累加即得完整回复 |
| `tool_start` / `tool_end` | 工具调用，通过 `id` 配对。**工具抛异常时也会补一帧 `tool_end`（`ok=false`）**——LangGraph 那时只发 `on_tool_error`，不补的话卡片会一直停在"运行中" |
| `interrupt` | 需人工确认，前端渲染确认 UI 后调 `/chat/resume` |
| `message_end` | 本条消息结束，携带 `message_id` 与 `usage` |
| `error` | 出错，`code` 与 `Result` 语义一致 |
| `done` | 流结束，必为最后一帧 |

前端只需 `switch (type)`，**未知 type 静默忽略**，后端新增事件不会影响已上线前端。
`error` 与 `done` 由 `app/event/stream.py` 统一收口，实现类无需产出。

两个前端实现时要知道的约定：**多调用时中断逐个出现**，每次 `resume` 解决一个、
响应里会带出下一个 `interrupt`；**同一节点的多个 interrupt 复用同一个 id**，
所以不能用 id 给卡片去重。**等确认的工具不会再有 `tool_end`**（它停在那里等人点），
前端收到 `interrupt` 时应当把这一轮里还没收尾的工具卡片标成"未完成"。

## 工具

八个，参考 Pi（earendil-works/pi）的设计，一个工具一个文件：

| 工具 | 作用 | 要点 |
| --- | --- | --- |
| `read` | 读文本文件 | 带行号；`offset`/`limit` 分页；超长保留开头并给出续读 offset；拒绝二进制 |
| `write` | 创建或整体覆盖 | 自动建父目录；参数是**完整内容**而非片段 |
| `edit` | 精确替换片段 | 默认要求 `old_string` 唯一；找不到时退化为忽略行尾空白匹配；保留原文件换行风格 |
| `ls` | 列目录 | 目录在前，带大小与修改时间 |
| `find` | 按 glob 找文件 | `*` 跨目录，结果排序 |
| `grep` | 按正则搜内容 | 支持 glob 过滤与忽略大小写；跳过噪音目录与二进制文件 |
| `bash` | 执行命令 | 流式读取、内存有上限、超时杀进程树、Windows 优先用 Git Bash |
| `get_current_time` | 取当前时间 | 八种格式（`iso` 默认 / `timestamp` / `date` / `time` / `human` / `cn` / `rfc` / `full`），支持 IANA 命名时区 |

**所有工具的输出都有上限**（50KB / 2000 行）。`read` 保留开头并提示续读位置；
`bash` / `grep` / `find` / `ls` 保留尾部或计数。截断时会**明确告知丢了多少**——
不告诉模型的话它会反复执行同一条命令。

**成功与失败靠返回值区分**：返回字符串 = 做成了（包括"没有匹配"这类空结果），
`common.fail()` 抛 `ToolException` = 没做成（文件不存在、沙箱拒绝、命令非零退出、参数不合法……）。
两者模型收到的话一样，区别在 `ToolMessage.status`——界面把卡片标成"失败"、
历史恢复出来也还是"失败"，而不是含混的"完成"。

**同一轮的多个调用并行执行**：工具都是 async，重叠的是它们的等待（子进程、文件 IO），
同步段仍只有一份——这是事件循环上的并发，不是线程池。其中任一调用要求人工确认（中断）时，
其余调用会被取消：那一整轮本来就要重跑，留着它们跑完只会留下没人认领的副作用。
被取消的调用不发 `tool_end`，它的卡片由确认卡片收成"未完成"。

## 工作区

**信任边界是工作区，不是会话。** 同一个工作区的所有对话共享同一份沙箱授权。

工作区在库里是实体：`workspace` 表存路径（`path_key` 归一化后判重，同一目录不会因为大小写
写法不同裂成两个分组），`thread_workspace` 表存会话归属。**归属不参与索引重建**——
`thread_index` 被重建后分组照旧。

- **每个会话都有且只有一个工作区。** 没挑过的时候就是应用所在目录——它和用户自己挑的
  工作区一视同仁：同一个实体、同一种分组、同一条绑定路径，侧边栏里没有单独的「应用目录」分组
  （`GET /chat/threads` 会把它作为 `default_workspace` 一并返回）
- 选目录 = **开一条绑定该目录的新对话**，绝不改动已打开的会话；「新对话」沿用此刻所在的工作区
- 绑定发生在第一条消息发出之前（会话诞生时）。只选了目录却没说话，库里不会留下任何东西
- 归属一旦定下就不会被别的动作改掉，只有删除会话才会清掉
- 选目录的方式：**桌面端弹 Windows 原生目录选择器**（pywebview 的 `FileDialog.FOLDER`）；
  浏览器里没有这个能力，退回页内浏览面板（走 `GET /chat/browse`）
- 侧边栏**按工作区分组**，组内再按今天/昨天/更早分。组头可折叠（状态记在 localStorage），
  悬停时露出的 `+` 直接在这个工作区里开新会话；目录已不存在的组会标出来、不给加号
- `bash` 的 cwd 与六个文件工具的路径基准**都是工作区根目录**，两边必须一致
- **只有用户能改工作区**——agent 若能改自己的边界，边界就不存在了

> `GET /chat/browse` 可以列举任意目录，这是刻意的：沙箱限制的是 agent，不是用户。
> 本地单用户部署没问题，但要把 API 暴露出去，**必须先给它加鉴权或删掉**。

## 路径沙箱

六个文件工具的路径都过 `app/agent/sandbox.py` 的守卫：

| 情况 | 行为 |
| --- | --- |
| 工作区内、未命中禁区 | 直接放行 |
| 命中绝对禁区 | **硬阻断，永不提示** |
| 工作区之外 | **弹确认卡片询问** |

绝对禁区：`.env`、`*.pem`、`*.key`、`*id_rsa*`、`.git/`、`data/`。

询问时四个选项，对应三层授权来源：

| 选项 | 存哪 | 生效范围 |
| --- | --- | --- |
| 拒绝 | 不存 | — |
| 允许（本次运行有效） | 内存 | 本工作区，进程重启即失效 |
| 记住（仅此工作区） | `<工作区>/.my_agent/sandbox.json` | 本工作区，持久 |
| 记住（所有工作区） | `~/.my_agent/sandbox.json` | 所有工作区，持久 |

授权粒度是**被问到的那一个路径本身**：同意一个文件不等于同意整个目录。

> **bash 不受沙箱约束。** 它的 cwd 跟随工作区，但 shell 一条 `cd /` 就出去了——
> 进程内检查对它无效。别把这层限制当成覆盖 bash 的安全边界。

## 人工确认

`app/agent/tools/__init__.py` 里 `APPROVAL_REQUIRED` 内的工具，执行前逐个征求确认。
**它目前是空的**——编码场景下每条命令都点确认会很烦。把 `"bash"` 加进去即可启用。

```
agent → tools（审批 + 执行） → agent
```

被拒的调用写入回绝的 ToolMessage，agent 据此向用户解释。

**会话卡住时的行为**：停在 `interrupt` 上时不允许直接发新消息。前端会把新消息
暂存并在确认处理完后自动补发；打开卡住的会话会补渲染确认卡片，不会让你无处可点。
被卡住的会话在侧边栏带红点。

**运行中发的消息走同一个暂存队列**：不打断当前这一轮，等它结束后自动发出。

## 删除会话

侧边栏每行的垃圾桶按钮，或 `DELETE /chat/threads/{id}`。**硬删，没有归档态**。

一次删除动四处，**服务端前两步的顺序不能反**：

| 顺序 | 对象 | 不这么做会怎样 |
| --- | --- | --- |
| 1 | checkpointer 的 `checkpoints` + `writes` | — |
| 2 | `thread_index` 里的行 | 反过来的话，中途失败会留下「索引没了但检查点还在」的会话，下次索引重建又把它枚举回来 |
| 3 | `thread_workspace` 里的绑定行 | 离开索引行就不可见，所以放最后删；中途失败也只剩一条谁都不会读的孤立记录 |
| 4 | 前端暂存的新消息 | 删的正好是当前打开的会话时，不清掉暂存会在下一轮对话里被补发出去 |

**沙箱授权不删。** 授权按工作区归属，同一个工作区的其他对话还在用它——
跟着会话一起删授权，会把别的对话连坐。开源的 OpenHands 删除接口是同一套判断：
只有当没有其他会话共享同一个沙箱时才回收沙箱资源。

> 另一个容易踩的坑是「两个生命周期」。Claude Code 的 `claude rm` 只删掉正在跑的
> 会话，转写文件仍可 resume；`/clear` 更是完全不算删除。所以「删除」必须先定义
> 清楚删的是哪一个概念——这里删的是会话本身，不留后路。

## 调试页面

`app/static/index.html`，布局参考 Open WebUI：左侧会话列表**先按工作区、再按今天/昨天/更早**
两层分组（组头可折叠，悬停露出「在此工作区新建」）、助手消息带头像无气泡、
用户消息右侧气泡（24px 圆角，与 Open WebUI 的 `rounded-3xl` 一致）、
底部圆角输入框。支持流式渲染、markdown、工具卡片折叠、人工确认卡片、工作区切换
（选目录即开一条绑定该目录的新对话，不搬动既有会话）、
会话切换与历史恢复、删除会话、浅色/深色主题切换，以及一个核对协议用的原始事件抽屉。

消息悬停时浮出操作行（复制 + 用量），最后一条常显；图标用 Heroicons，与 Open WebUI 同源。
破坏性操作走自绘弹窗而非原生 `confirm`——原生弹窗样式不受控，桌面端的 WebView 里还可能被拦。

主题令牌集中在 CSS 顶部的 `:root[data-theme=...]`，换皮只改那一层。

> 浏览器端与桌面端是**同一个文件**。`app/main.py` 把 `app/static` 挂在 `/ui`，
> `app/desktop.py` 的窗口也加载同一个地址，改一处两边同时生效。

## 桌面模式

```bash
python run_desktop.py
```

**必须从项目根目录启动**，否则会报 `ModuleNotFoundError: No module named 'app'`。
根目录的 `run_desktop.py` 就是为此存在的。

窗口端口由系统分配，不会和已在跑的 8000 冲突；窗口关闭时后端一并退出。

## 命令行验证

```bash
curl -N -sS -X POST http://127.0.0.1:8000/chat/stream \
  -H "Content-Type: application/json" \
  --data-binary @payload.json
```

其中 `payload.json` 内容为 `{"message":"你好"}`。

> **Windows**：若 PATH 中 `curl` 指向 MSYS2 版本，`@文件名` 会被错误处理；
> 请用 `C:\Windows\System32\curl.exe`。PowerShell 5.1 会吞掉内联 JSON 的引号，
> 所以用 `--data-binary @文件` 而不是 `--data-raw`。

## 分层

经典三层，依赖方向单向向下：

```
app/api/          表现层      HTTP、请求体校验、SSE 外壳
     ↓
app/service/      业务层      编排 Agent 执行、维护会话索引与工作区
     ↓
app/dao/          数据访问层  只读写自有表：thread_index / workspace / thread_workspace
app/agent/        Agent 运行时 图定义、事件映射、沙箱、工具
     ↓
app/models/       领域实体    ThreadRecord / WorkspaceRecord，层间交换用
```

- `app/container.py` 是**组装根**：选哪个实现、连接何时开关，都属于应用装配
- `app/handler/` 是**全局异常处理**：非流式那半边的兜底出口（流式在 `app/event/stream.py`）
- `app/event/` 是**协议模型**（wire format）：前端能看见的一切形状。判据只有一条——
  **要不要跨 HTTP 边界**：要 → 这里是 pydantic，供 OpenAPI；只在层间流转 → 放 `app/models/`，
  dataclass 不带校验。agent 层直接产出数据面事件，业务层把实体映射成列表项与回执，
  表现层只负责包 `Result`；**dao 不 import 它**（存储层不该知道协议）。
  请求体是入站的 HTTP 校验，留在 api 层，不进 `app/event/`
- `app/exceptions/` 放自定义异常（`SandboxDenied`、`InvalidInput`）
- checkpointer **不单独抽 DAO**：那是 LangGraph 自己的存储，不是我们写的 SQL

**`app/` 下每个关注点都是一级包**，不放散落的同名模块——`app/config/`、`app/result/`、
`app/constant/` 都一样。包内的模块名**别和包同名**，否则会出现 `app.exceptions.exceptions`
这种双重叠词；所以"默认名字必然等于包名"的那几个（`config`、`exceptions`、`handler`）直接把
代码写在 `__init__.py` 里，其余一律具名模块（`api/chat.py`、`event/events.py`、`dao/workspace_dao.py`）。

> `app/config/__init__.py` 的 `PROJECT_ROOT` 是**按目录层级数出来的**，挪文件必须同步改。
> 它算错不会报错，只会让相对路径的配置静默写到别处去，所以那里有启动断言兜底。

## 日志

级别由 `LOG_LEVEL` 控制（默认 `INFO`），约定：

| 级别 | 记什么 | 例子 |
| --- | --- | --- |
| `ERROR` | 需要人去处理的失败 | 未处理的异常、对话流异常（都带堆栈） |
| `WARNING` | 降级与拒绝 | 没配 key、沙箱硬阻断、工具被拒绝、目录不存在 |
| `INFO` | 主线里程碑 | 每轮对话起止、工具调用与用时、等待确认、绑定工作区、删除会话、启动迁移 |
| `DEBUG` | 细节 | 每次写库、路径检查放行、历史与扫描条数、前端关键转场 |

排查问题时把 `LOG_LEVEL` 改成 `DEBUG` 重启即可。带 `thread=` 的行能对上具体会话；
两个异常出口（流式 / 非流式）都先写日志再回响应。

> 级别只作用于 `app.*`：第三方库统一压在 `WARNING`，否则 `aiosqlite` 会把每条 SQL
> 和参数 blob 都打出来，自己人的日志反而找不到。uvicorn 的访问日志不受影响。

> 错误信息就是异常原文，不做翻译——面向程序员的项目，原文比客套话有用。
> 前端也会把 `error` 帧与关键转场打到浏览器 console（前缀 `[my-agent]`）。

## 扩展点

| 要改什么 | 改哪里 |
| --- | --- |
| 加工具 | `app/agent/tools/` 下新建文件，在 `__init__.py` 的 `TOOLS` 里注册 |
| 加需审批的工具 | 把工具名加进同文件的 `APPROVAL_REQUIRED` |
| 换存储 | `app/container.py` 里换 checkpointer |
| 换 Agent 实现 | 实现 `AgentRunner` 协议（`app/agent/runner.py`），在容器里替换 |
| 换事件类型 | `app/event/events.py`，前端同步加一个 `case` |
| 换前端 | `app/static/index.html` 可整体替换，事件处理逻辑可直接移植到 Next.js |
