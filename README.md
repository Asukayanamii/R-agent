# my-agent

基于 LangGraph 的可扩展 Agent 后端，FastAPI 提供 SSE 流式接口，事件协议与图实现解耦。

## 环境要求

- Python 3.13（conda 环境名 `my_agent`）
- Windows / Linux / macOS 均可

## 依赖安装

推荐 conda：

```bash
conda env create -f environment.yml
conda activate my_agent
```

已有环境或使用 venv：

```bash
pip install -r requirements.txt
```

## 分层

经典三层，依赖方向单向向下，每层只做自己的事：

```
app/api/          表现层      HTTP、请求/响应 DTO、SSE 外壳
     ↓
app/service/      业务层      编排 Agent 执行、维护会话索引
     ↓
app/dao/          数据访问层  只读写 thread_index 表，不含业务规则
app/agent/        Agent 运行时 图定义、事件映射、checkpointer 访问
     ↓
app/models/       领域实体    ThreadRecord，供 dao 与 agent 共用
```

- **`app/container.py` 是组装根**：选哪个实现（有 key 走 LangGraph、无 key 降级桩）、
  连接何时开关，都属于应用装配，不放进任何一层。
- **`app/event/` 是协议模型**（wire format，含 `ThreadSummary` 等 DTO），
  下层不 import 它；dao 只认 `app/models/entities.py` 里的领域实体。
- **checkpointer 不单独抽 DAO**：那是 LangGraph 自己的存储，通过 LangGraph 的 API 访问，
  不是我们写的 SQL。只为我们自己拥有的表写 DAO。
- 改某层的实现不需要动其他层：换存储动 `container.py`，换业务规则动 `service/`，
  换 SQL 动 `dao/`。

## 配置

```bash
cp .env.example .env
```

填入 `LLM_API_KEY`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 无 | **未配置时自动降级为桩实现**，接口仍可调通 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | 百炼填 `https://dashscope.aliyuncs.com/compatible-mode/v1`，Ollama 填 `http://localhost:11434/v1` |
| `LLM_MODEL` | `deepseek-chat` | 百炼可用 `qwen-plus`，Ollama 可用 `qwen3:8b` |
| `SQLITE_PATH` | `./data/checkpoints.db` | 会话状态落盘位置。**相对路径按项目根解析**，不受启动目录影响；留空则仅存于进程内存 |

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
| GET | `/chat/history` | 读取既有会话消息，供前端恢复。走 `Result` 包装 |
| GET | `/chat/threads` | 列出既有会话，按最近更新倒序。走 `Result` 包装 |

> **会话列表为什么放在服务端**：`localStorage` 按 origin（含端口）隔离，而桌面端
> 每次启动都随机挑端口，origin 随之改变，前端自持的会话清单重启后必然是空的——
> 即便数据早已落库。因此列表以 `/chat/threads` 为唯一来源，前端只用 `localStorage`
> 记住主题偏好。
>
> **为什么还要一张索引表**：checkpointer 只提供"列出检查点"（`alist(None)`），拿不到
> 会话维度的清单——一个会话对应多条检查点。若每次列会话都枚举检查点再逐个查状态，
> 是 N+1 次查询。因此 `app/agent/thread_index.py` 维护一张 `thread_index` 表
> （thread_id / title / updated_at / pending），在写入侧随每轮对话 upsert，
> 列表接口退化成一次普通查询。表与 checkpointer 共用同一个 sqlite 文件，
> 换 Postgres 时一并迁走。索引为空而库里有会话时会自动回填一次。

`/chat/stream` 请求体：

```json
{"message": "你好", "thread_id": "可选，不传则新建"}
```

`/chat/resume` 请求体（`thread_id` 必须与中断时一致）：

```json
{"thread_id": "percall-demo", "value": "确认"}
```

两个流式接口响应格式相同，均为 `text/event-stream`，每帧 `data` 形如 `{"type": ..., "data": {...}}`：

| type | 说明 |
| --- | --- |
| `thread` | 会话握手，携带 `thread_id`，必为第一帧 |
| `text_delta` | 增量文本，累加即得完整回复 |
| `tool_start` / `tool_end` | 工具调用，通过 `id` 配对 |
| `interrupt` | 需人工确认，前端渲染确认 UI 后调 `/chat/resume` |
| `message_end` | 本条消息结束，携带 `message_id` 与 `usage` |
| `error` | 出错，`code` 与 `Result` 语义一致 |
| `done` | 流结束，必为最后一帧 |

前端只需 `switch (type)`，未知 type 静默忽略，后端新增事件不会影响已上线前端。
`error` 与 `done` 由 `app/event/stream.py` 统一收口，实现类无需产出。

## 工具

目前只有一个 `bash`，实现在 `app/agent/tools/bash.py`，设计参考 Pi（earendil-works/pi）
等开源 coding agent 的做法：

- **流式读取输出**，不等进程结束。这样命令超时被 kill 时，它此前打出的内容照样能拿回来
- **内存有滚动上限**（1MB），命令可能吐几个 G，超出的从头部丢弃并计数
- **最终输出再截断一次**（50KB / 2000 行），保留**尾部**——排错时最近的输出更有用
- **截断会明确告知模型丢了多少行/字节**，否则它会反复执行同一条命令
- **清洗 ANSI 转义与控制字符**，避免污染上下文
- **超时按进程树终止**（Windows 用 `taskkill /T`，POSIX 用进程组），只杀 shell 会把子进程留在后台
- **不接 stdin**，交互式命令会一直等输入；让它直接失败好过挂住
- **Windows 上优先用 Git Bash**（自动找 `C:\Program Files\Git\bin\bash.exe`），
  因为模型的命令是照 bash 语义写的，落到 cmd.exe 会到处不认

## 人工确认（Human-in-the-loop）

`app/agent/tools/__init__.py` 里 `APPROVAL_REQUIRED` 集合内的工具，执行前**逐个**征求确认。
**它目前是空的**——编码场景下每次执行命令都要点确认会很烦，所以默认不拦。
把 `"bash"` 加进去即可启用。

```
agent → tools（审批 + 执行） → agent
```

agent 决定调用工具后，`tools` 节点对每个需审批的调用 `interrupt()`；前端收到 `interrupt`
事件，用户选择后调 `/chat/resume`。通过的调用执行，被拒的写入回绝的 ToolMessage，
agent 据此向用户解释。

几个要点：

- **多调用时中断逐个出现**。每次 `resume` 解决一个，响应里会带出下一个 `interrupt`，
  前端只需按同样方式再渲染一张确认卡片。
- **同一节点的多个 interrupt 复用同一个 id**，前端不能用 id 去重。
- **会话卡着时不允许直接发新消息**。两种情况会被 `ChatService` 挡下：停在 `interrupt` 上
  （把待确认项重新推回前端），以及历史里有悬空的 `tool_calls`（返回 `error`，提示新建会话）。

- **会话卡着时新消息会暂存，不会硬发，也不会丢**。前端一个 FIFO 就够，不需要消息队列——
  真正的约束只有两条：同会话内有序、必须等 `interrupt` 全部消化完。那是状态机，不是投递问题。
  暂存的消息在确认处理完（服务端 `pending` 转 false）后由前端自动补发。
  服务端也做了兜底：绕过前端直接对卡住的会话发消息，会把待确认项重新推回去
  （而不是回一句"请先调用 /chat/resume"——用户面对的是界面，没法自己构造请求）。
- **打开卡住的会话会补渲染确认卡片**。`GET /chat/history` 的响应里带 `pending` 字段，
  否则侧边栏虽有红点，用户进去只见历史、无处可点。

  这条规则不能省。若在中断状态下硬发新消息，LangGraph 会**丢弃 interrupt** 并把新消息
  追加进去，于是那条没有 `ToolMessage` 回应的 `tool_calls` 永久留在历史里，之后每次调用
  模型都会被 provider 以 400 拒绝：

  > An assistant message with 'tool_calls' must be followed by tool messages
  > responding to each 'tool_call_id'.

  `GET /chat/threads` 的 `pending` 标记用的正是这个判断（`is_interrupted` 或
  `has_dangling_tool_calls`），所以被卡住的会话在侧边栏会带红点。

工具执行**没有**使用 LangGraph 的 `ToolNode`：它会执行 `AIMessage` 里的全部调用
（包括已被回绝的），并产生重复 `tool_call_id` 的 ToolMessage，审批拒绝会形同虚设。
执行统一放在 `app/agent/langgraph_runner.py` 的 `_run_tools`，单一执行路径。

## 调试页面

`app/static/index.html`，布局参考 Open WebUI：左侧会话列表、助手消息带头像无气泡、
用户消息右侧气泡、底部圆角输入框。功能上支持流式渲染、markdown、工具卡片折叠、
人工确认卡片、会话切换与历史恢复、浅色/深色主题切换、以及一个核对协议用的原始事件抽屉。

主题令牌集中在 CSS 顶部的 `:root[data-theme=...]`，换皮只改那一层。

> **浏览器端与桌面端是同一个文件**。`app/main.py` 把 `app/static` 挂在 `/ui`，
> `app/desktop.py` 的窗口也加载同一个地址，改一处两边同时生效。

**markdown 渲染**用 vendor 在 `app/static/vendor/marked.umd.js` 的 marked（MIT，v18），
**不走 CDN**：桌面端要能离线跑，内网也可能访问不到 CDN。引入时用相对路径
`./vendor/marked.umd.js`——静态目录挂在 `/ui` 下，写死绝对路径会 404。

marked 只解析、不管安全，因此输出会再过一遍白名单清洗（`sanitizeHtml`）：
不在白名单的标签脱壳成纯文本（`script`/`style`/`iframe` 因此失效），
属性只留 `href`/`src`/`alt`/`title`/`class`，`javascript:` 之类的 URL 一律剥掉。
模型输出可能被提示注入影响，直接 `innerHTML` 等于把 XSS 交给它。

## 桌面模式

把同一套前后端装进原生窗口，用于打包成桌面应用：

```bash
pip install -r requirements-desktop.txt
```

在项目根目录执行（两种写法等价）：

```bash
python run_desktop.py
python -m app.desktop
```

**必须从项目根目录启动**，否则会报 `ModuleNotFoundError: No module named 'app'`。
根目录的 `run_desktop.py` 就是为此存在的：直接运行脚本时 Python 只把**脚本所在目录**
放进 `sys.path`，所以 `python app/desktop.py` 一定会失败。

`app/desktop.py` 会在后台线程启动 uvicorn（**端口由系统分配**，不会和已在跑的 8000 冲突），
用 `/chat/history` 探活——该请求会走到 runner，成功即代表 lifespan 里的 `init_runner`
已完成——然后打开窗口。窗口关闭时后端一并退出。

**关键约束**：`/chat/stream` 的流式渲染依赖 Chromium 的 `fetch` + `ReadableStream`。
已验证 EdgeWebView2（Chromium 152）下逐帧到达；首个 `thread` 帧在 8ms 内到达、
后续 token 在 LLM 首 token 产出后陆续到达，说明宿主没有缓冲响应。因此
`app/desktop.py` 在 Windows 上**固定使用 `edgechromium` 后端**，不交给 pywebview
自动挑选，避免落到非 Chromium 内核。

打包成单个 exe 需要 PyInstaller，尚未配置。

## 命令行验证

```bash
curl -N -sS -X POST http://127.0.0.1:8000/chat/stream \
  -H "Content-Type: application/json" \
  --data-binary @payload.json
```

其中 `payload.json` 内容为 `{"message":"你好"}`。

> Windows 注意：若 PATH 中 `curl` 指向 `C:\msys64\usr\bin\curl.exe`（MSYS2 版本），
> 它处理 `@文件名` 的方式与原生版不同，会把 JSON 内容按空格拆成多个参数并报
> `unmatched close brace/bracket in URL`。请改用 `C:\Windows\System32\curl.exe`。
> PowerShell 5.1 还会吞掉内联 JSON 的双引号，因此一律用 `--data-binary @文件` 而非 `--data-raw`。

## 扩展点

| 要改什么 | 改哪里 |
| --- | --- |
| 加工具 | `app/agent/tools/` 下新建一个文件，在 `__init__.py` 的 `TOOLS` 里注册 |
| 加需审批的工具 | 把工具名加进同文件的 `APPROVAL_REQUIRED`，例如 `{"bash"}` |
| 换存储 | `app/agent/__init__.py` 里换 checkpointer（`AsyncSqliteSaver` / `AsyncPostgresSaver` / 自定义） |
| 换 Agent 实现 | 实现 `AgentRunner` 协议（`app/agent/runner.py`），在 `app/agent/__init__.py` 的工厂里替换 |
| 换事件类型 | `app/event/events.py`，前端同步加一个 `case` |
| 换前端 | `app/static/index.html` 可整体替换，事件处理逻辑可直接移植到 Next.js |
