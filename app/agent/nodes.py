"""agent 的图节点实现：模型调用与压缩检查。

节点本身很薄——真正的活儿在 `tool_calls` / `compaction.runtime` / `prompts` 里，这里只负责
把它们接成"图上的一个节点"该有的形状：读状态、调服务、回写更新。

独立成模块是因为**图的组装已经不在这里**：结构由 `app/harness` 从 `builtin/loop` 插件的
骨架片段合成，节点实现作为普通 callable 交给片段声明。`RECURSION_LIMIT` / `log_step_limit`
也留在这里——它们描述的是"这一轮图能跑多长"，和节点同属 agent 运行时。
"""

import logging
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.config import get_config

from app.agent.compaction.policy import with_summary
from app.agent.compaction.runtime import compact_once
from app.agent.messages import usable_compaction
from app.agent.prompts import build_system_prompt
from app.agent.retry import ainvoke_with_retry

logger = logging.getLogger(__name__)

RECURSION_LIMIT = 10_000
"""单轮图的 superstep 上限（LangGraph 的 recursion_limit）。

LangGraph 逼着必须给一个有限值（默认只有 25），但这个值不该成为"复杂任务跑一半挂掉"的原因：

- 我们的图一个工具回合要花 3 步（`tools → compact → agent`），25 步只够 7 个回合；
- 主流 coding agent 基本不设上限（Pi 的 agent loop 是 `while (true)`，只在模型不再调工具/
  出错/用户取消时退出；Codex CLI 同理；Cline 把 max requests 直接删了）；
- LangGraph 自己新版默认已是 10007，`create_agent` 用 9999。

所以取 10000（约 3300 个工具回合），当成"真出现死循环时最后兜底"，而不是日常约束。
**真正的停止手段是用户点停止**（与手动停止走同一条收尾路径）。
"""


def log_step_limit(thread_id: str, exc: Exception) -> None:
    """
    撞上 superstep 上限时补一条日志。

    这个上限高到正常任务碰不到（见 RECURSION_LIMIT），真撞上基本就是死循环：
    错误本身照旧原样上抛，日志里说清"多少步、可能是循环"，别让人对着一个数字发愣。
    """
    logger.warning(
        "本轮撞上图的 superstep 上限 thread=%s：%s（上限 %d，"
        "正常任务碰不到——多半是工具反复失败把模型困在循环里）",
        thread_id,
        exc,
        RECURSION_LIMIT,
    )


def make_compact_node(summary_model: BaseChatModel, settings: Any = None):
    """构造压缩检查节点：超水位就把中段摘要掉；不超（或压不动）就原样放行，返回空更新。

    `settings` 由 `builtin/compaction` 插件的配置提供（不传则引用总体配置）。
    """

    async def compact_context(state: dict) -> dict:
        config = get_config()
        thread_id = (config.get("configurable") or {}).get("thread_id") or "-"
        return await compact_once(
            state,
            summary_model=summary_model,
            thread_id=thread_id,
            settings=settings,
            config=config,
        )

    return compact_context


def make_model_node(model: BaseChatModel, registry: Any, context: Any = None):
    """
    构造模型调用节点。节点本身无状态：每轮都把 system prompt 重新拼在历史前面，
    历史来自 checkpointer。

    **工具集每次调用前从快照重建绑定**（§4.1）——实测 0.1ms、请求体与复用一致（阶段 0 探针 5），
    所以运行时加/减工具不需要重建图：下一次模型调用就能看到新能力集。

    静态上下文块（项目约定 + 技能索引）来自服务 `context`（可选插件）：没装就不注入。

    压缩过的会话发的是"摘要 + 首条用户消息 + 切点起的原文"（只压视图，原文还在检查点里）。
    `usage_anchor` 记的是这次请求 provider 实报的 input tokens 与"覆盖到第几条**状态里的**
    消息"——之后追加多少条，下一轮就只粗估那几条。
    """

    async def call_model(state: dict):
        messages = state["messages"]
        compaction = state.get("compaction") or {}
        config = get_config()
        # 静态上下文（项目约定 + 技能索引）随工作区变，所以每轮按当前工作区重拼。
        # 同一工作区内它是稳定的：变了才动前缀缓存，见 prompts.build_system_prompt。
        workspace = (config.get("configurable") or {}).get("workspace")
        static = context.static(workspace) if context is not None else ""
        prompt = build_system_prompt(workspace, static=static)
        if usable_compaction(compaction, len(messages)):
            prompt = with_summary(prompt, compaction["summary"])
            messages = [messages[0], *messages[compaction["from_index"] :]]
        elif compaction:
            logger.warning(
                "压缩锚点越界，本次按全量发送 from_index=%s 共%d条",
                compaction.get("from_index"),
                len(messages),
            )

        model_with_tools = model.bind_tools(list(registry.snapshot()))
        response = await ainvoke_with_retry(
            model_with_tools,
            [{"role": "system", "content": prompt}, *messages],
            config=config,
            thread_id=(config.get("configurable") or {}).get("thread_id") or "-",
            label="模型调用",
        )
        meta = getattr(response, "usage_metadata", None) or {}
        if not meta:
            return {"messages": [response]}
        return {
            "messages": [response],
            "usage_anchor": {
                "tokens": meta.get("input_tokens", 0),
                "upto": len(state["messages"]),
            },
        }

    return call_model
