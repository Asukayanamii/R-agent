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
| `SQLITE_PATH` | `./data/checkpoints.db` | 会话状态落盘位置，留空则仅存于进程内存 |

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

## 人工确认（Human-in-the-loop）

`app/agent/tools.py` 里 `APPROVAL_REQUIRED` 集合内的工具，执行前**逐个**征求确认：

```
agent → tools（审批 + 执行） → agent
```

agent 决定调用工具后，`tools` 节点对每个需审批的调用 `interrupt()`；前端收到 `interrupt`
事件，用户选择后调 `/chat/resume`。通过的调用执行，被拒的写入回绝的 ToolMessage，
agent 据此向用户解释。

两个要点：

- **多调用时中断逐个出现**。每次 `resume` 解决一个，响应里会带出下一个 `interrupt`，
  前端只需按同样方式再渲染一张确认卡片。
- **同一节点的多个 interrupt 复用同一个 id**，前端不能用 id 去重。

工具执行**没有**使用 LangGraph 的 `ToolNode`：它会执行 `AIMessage` 里的全部调用
（包括已被回绝的），并产生重复 `tool_call_id` 的 ToolMessage，审批拒绝会形同虚设。
执行统一放在 `app/agent/langgraph_runner.py` 的 `_run_tools`，单一执行路径。

## 调试页面

`app/static/index.html`，布局参考 Open WebUI：左侧会话列表、助手消息带头像无气泡、
用户消息右侧气泡、底部圆角输入框。功能上支持流式渲染、工具卡片折叠、人工确认卡片、
会话切换与历史恢复、浅色/深色主题切换、以及一个核对协议用的原始事件抽屉。

主题令牌集中在 CSS 顶部的 `:root[data-theme=...]`，换皮只改那一层。

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
| 加工具 | `app/agent/tools.py` 的 `TOOLS` 列表 |
| 加需审批的工具 | 把工具名加入同文件的 `APPROVAL_REQUIRED` |
| 换存储 | `app/agent/__init__.py` 里换 checkpointer（`AsyncSqliteSaver` / `AsyncPostgresSaver` / 自定义） |
| 换 Agent 实现 | 实现 `AgentRunner` 协议（`app/agent/runner.py`），在 `app/agent/__init__.py` 的工厂里替换 |
| 换事件类型 | `app/event/events.py`，前端同步加一个 `case` |
| 换前端 | `app/static/index.html` 可整体替换，事件处理逻辑可直接移植到 Next.js |
