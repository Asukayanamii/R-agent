"""
上下文压缩的运行时：判定 + 调摘要模型 + 写状态。

图里的 `compact` 节点与"用户主动压缩"（`POST /chat/compact`）都走 `compact_once`，
判定只写一遍。零件的算法在 `policy`，这里只管时序、日志与状态写入。
"""

import logging
from datetime import datetime, timezone
from time import monotonic

from langchain_core.language_models import BaseChatModel

from app.agent.compaction.policy import (
    TAG,
    estimate,
    estimate_view,
    in_cooldown,
    message_tokens,
    note_failure,
    note_success,
    plan_cut,
    rough_tokens,
    serialize,
    summary_block_tokens,
    summary_input_chars,
    summary_request,
    trim_middle,
)
from app.agent.messages import text_of
from app.agent.prompts import build_system_prompt
from app.agent.retry import ainvoke_with_retry
from app.config import (
    COMPACT_AT,
    COMPACT_ENABLED,
    COMPACT_KEEP_TOKENS,
    LLM_CONTEXT_WINDOW,
)

logger = logging.getLogger(__name__)

COMPACT_NODE = "compact"
"""压缩节点的名字。事件翻译层靠 on_chain_end 里的这个节点名认出"本轮写了压缩记录"，
写成常量，改名时两边一起改——不然压缩事件会静默消失。"""


def compaction_floor(state: dict) -> int:
    """
    上次压缩的切点：本次只摘要它之后的内容。

    已经摘要过的部分仍留在消息列表里（我们不删消息），但**不能再送进摘要器**——否则同一段
    内容会被反复摘要，摘要被越改越长。实测踩到过：两次切点相同的压缩把视图从 439 推到 463，
    等于白花一次调用还更占上下文。Pi 的规则也是从上次的保留边界开始。
    """
    compaction = state.get("compaction") or {}
    index = compaction.get("from_index")
    messages = state.get("messages") or []
    if isinstance(index, int) and 1 <= index < len(messages):
        return index
    return 1


async def compact_once(
    state: dict,
    *,
    summary_model: BaseChatModel,
    thread_id: str,
    force: bool = False,
    config: dict | None = None,
) -> dict:
    """
    跑一次压缩判定与摘要，返回状态更新；没压就返回空字典。

    `force=True`（手动触发）跳过阈值、开关与冷却：用户点了就是要压，跟自动那套闸门无关。
    """
    if not force and not COMPACT_ENABLED:
        return {}

    messages = state["messages"]
    # 提示词要和 call_model 里真正发出去的是同一份：它现在含项目约定与技能索引，
    # 不带上就会拿"不含静态上下文的旧视图"比"含它的新视图"，估算偏小。
    workspace = ((config or {}).get("configurable") or {}).get("workspace")
    prompt = build_system_prompt(workspace)
    trigger = int(LLM_CONTEXT_WINDOW * COMPACT_AT)
    before = estimate(messages, state.get("usage_anchor"))
    if not state.get("usage_anchor"):
        # 没有锚点时粗估的是"消息"，而 system 提示词也是要发出去的：不补上它，
        # 后面的净收益校验就是拿"不含 system 的旧视图"比"含 system 的新视图"，
        # 短会话会被永远判成"没收益"（实测踩到过）。
        before += rough_tokens(prompt)
    if not force and before < trigger:
        logger.debug("未到压缩线 thread=%s 估算=%d 触发线=%d", thread_id, before, trigger)
        return {}
    if not force and in_cooldown(thread_id):
        logger.debug("压缩在冷却中，跳过 thread=%s", thread_id)
        return {}

    floor = compaction_floor(state)
    cut = plan_cut(messages, COMPACT_KEEP_TOKENS, floor=floor)
    if cut is None:
        logger.debug("没有值得摘要的中段 thread=%s 共%d条", thread_id, len(messages))
        return {}

    previous = (state.get("compaction") or {}).get("summary", "")
    body = trim_middle(
        serialize(messages[floor:cut]), summary_input_chars(LLM_CONTEXT_WINDOW)
    )
    started = monotonic()
    try:
        response = await ainvoke_with_retry(
            summary_model,
            summary_request(previous, body),
            config=config,
            thread_id=thread_id,
            label="摘要调用",
            tags=[TAG],
        )
    except Exception as exc:
        # 压不动不是会话的错：本轮照旧发全量，退避之后再试
        note_failure(thread_id)
        logger.warning("摘要失败，本轮不压缩 thread=%s：%s", thread_id, exc)
        return {}

    summary = text_of(response).strip()
    # 净收益校验用同口径比较：被替换掉的中段 vs 顶替它的摘要块。拿"provider 实报的前值"
    # 去比"粗估的后值"会因两边误差方向不同而误判（实测：CJK 内容下会把划算的压缩判成不划算）。
    replaced = sum(message_tokens(item) for item in messages[floor:cut])
    if not summary or summary_block_tokens(summary) >= replaced:
        note_failure(thread_id)
        logger.warning(
            "摘要没带来净收益，不写入 thread=%s 中段=%d 摘要块=%d",
            thread_id,
            replaced,
            summary_block_tokens(summary),
        )
        return {}

    kept = [messages[0], *messages[cut:]]
    after = estimate_view(prompt, summary, kept)

    note_success(thread_id)
    count = (state.get("compaction") or {}).get("count", 0) + 1
    logger.info(
        "上下文已压缩 thread=%s 第%d次（%s）估算 %d → %d tokens，"
        "保留首条与第 %d 条起（共 %d 条），用时 %.2fs",
        thread_id,
        count,
        "手动" if force else "自动",
        before,
        after,
        cut,
        len(messages),
        monotonic() - started,
    )
    return {
        "compaction": {
            "summary": summary,
            "from_index": cut,
            "tokens_before": before,
            "tokens_after": after,
            "count": count,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        # 锚点按新视图重置：否则这一轮要是被中断、resume 回来重跑 compact，
        # 会拿着压缩前的旧数字又压一次（同一份内容压两遍，白花钱）。
        "usage_anchor": {"tokens": after, "upto": len(messages)},
    }
