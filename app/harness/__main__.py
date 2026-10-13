"""画图 CLI：``python -m app.harness`` 把当前骨架画成 mermaid（空锚点也在图里）。

    python -m app.harness > docs/graph.mmd

图来自合成器（与运行时同一套片段），不是手画的，也不再从旧实现导出。
"""

from __future__ import annotations

import asyncio

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from app.harness.composer import render_mermaid
from app.harness.runtime import HarnessRuntime
from app.kernel import Context


class _DrawOnlyModel(BaseChatModel):
    """只用来装配的占位模型：画图不会调用它（loop 只对它 bind_tools）。"""

    @property
    def _llm_type(self) -> str:
        return 'draw-only'

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise NotImplementedError('画图不会调用模型')

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise NotImplementedError('画图不会调用模型')


COMMENTS = """%% 这张图由代码生成，别手改。重新生成：
%%   python -m app.harness > docs/graph.mmd
%% 图来自合成器（builtin/loop 骨架 + 当前挂载的片段），空锚点也会画出来。
%% 渲染 graph.png **不要**用 langchain 的 draw_mermaid_png：它用标准 base64 拼 URL，
%% payload 里一旦出现 '/' 就 404（实测）。走 urlsafe：
%%   b64 = base64.urlsafe_b64encode(Path("docs/graph.mmd").read_bytes()).decode()
%%   requests.get(f"https://mermaid.ink/img/{b64}?type=png&bgColor=!white").content
"""


def with_comments(mermaid: str) -> str:
    """``draw_mermaid()`` 自带 front-matter 配置块，且它只能待在文件最前——注释插在它后面。"""
    lines = mermaid.splitlines()
    head: list[str] = []
    if lines and lines[0].strip() == '---':
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == '---':
                head, lines = lines[: index + 1], lines[index + 1 :]
                break
    return '\n'.join([*head, *COMMENTS.rstrip('\n').splitlines(), *lines]) + '\n'


async def _render() -> str:
    runtime = HarnessRuntime(
        Context(),
        checkpointer=InMemorySaver(),
        model=_DrawOnlyModel(),
        summary_model=_DrawOnlyModel(),
    )
    await runtime.setup()
    return render_mermaid(runtime.versions.current.graph)


def main() -> None:
    print(with_comments(asyncio.run(_render())), end='')


if __name__ == '__main__':
    main()
