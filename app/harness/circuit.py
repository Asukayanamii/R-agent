"""节点壳：插件节点的失败收口与熔断（设计稿 §4.2）。

**只对插件贡献的节点生效**：骨架节点（`agent`/`tools`/`compact`/`stamp`）崩溃照旧上抛——
那是内核 bug，吞掉就是掩盖真问题。

四条护栏都在壳里：

1. `GraphBubbleUp` **最先拦、原样上抛**——`interrupt()` 抛的就是它（继承 `Exception`），
   被当成"节点失败"会让审批静默退化；
2. 留痕三处：日志 WARNING + 一条前端可见的事件（映射层从 `plugin_failures` 通道取）
   + 状态里记一笔（内核通道 `plugin_failures`）；
3. 失败后**返回空更新继续跑**骨架环（跳过这一步，会话不因插件崩掉）；
4. **片段级熔断**：同一片段任一节点连续失败 3 次 → 整个片段从合成输入里摘掉
   + 报告点名（半条链比没有链更危险）。

计数是"连续"的：一次成功即清零。
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from langgraph.errors import GraphBubbleUp

__all__ = ['FailureLedger', 'FailureRecord', 'append_failures', 'fail_key', 'wrap_node']

logger = logging.getLogger(__name__)

THRESHOLD = 3
"""连续失败几次熔断（硬编码，不给配置项——§11）。"""

KEEP_RECORDS = 20
"""状态通道里最多留多少条失败记录（状态不是日志，留痕够用就行）。"""


def append_failures(left: Any, right: Any) -> list:
    """`plugin_failures` 通道的 reducer：追加并只留最近 `KEEP_RECORDS` 条。"""
    return [*(left or []), *(right or [])][-KEEP_RECORDS:]


@dataclass(frozen=True)
class FailureRecord:
    """一次插件节点失败。"""

    fragment: str
    node: str
    error: str
    at: str


def fail_key(fragment_id: str, scope: str = '') -> str:
    """熔断账本里的键：全局片段就是 id，作用域内的片段要带上前缀。

    不带前缀的话，两个工作区里同名的片段会互相带崩（一个工作区连续失败，
    另一个工作区的同名片段也被停用）。报告里照原样显示这个键——能看出是哪个工作区。
    """
    return f'{scope}|{fragment_id}' if scope else fragment_id


class FailureLedger:
    """片段级的失败账本：连续计数、熔断名单、最近的记录。"""

    def __init__(self, threshold: int = THRESHOLD) -> None:
        self.threshold = threshold
        self.counts: dict[str, int] = {}
        self.disabled: dict[str, str] = {}
        self.records: list[FailureRecord] = []
        self.revision = 0
        """熔断名单的版本号：每次新增停用 +1——重合成要看它（结构变化的一种）。"""

    def record(self, fragment_id: str, node: str, error: BaseException) -> FailureRecord:
        """记一次失败；达到阈值即把片段列入熔断（返回这条记录）。

        `fragment_id` 走 `fail_key()` 生成的**作用域限定键**（全局片段就是裸 id）。
        """
        count = self.counts.get(fragment_id, 0) + 1
        self.counts[fragment_id] = count
        record = FailureRecord(
            fragment=fragment_id,
            node=node,
            error=str(error) or type(error).__name__,
            at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
        )
        self.records = [*self.records, record][-KEEP_RECORDS:]
        if count >= self.threshold and fragment_id not in self.disabled:
            self.disabled[fragment_id] = f'连续失败 {count} 次（{node}: {record.error}）'
            self.revision += 1
            logger.warning(
                '片段 %s 因连续失败已自动停用：%s', fragment_id, self.disabled[fragment_id]
            )
        return record

    def reset(self, fragment_id: str) -> None:
        """一次成功：连续计数清零（熔断过的保持停用，修好再装载）。"""
        self.counts.pop(fragment_id, None)

    def is_disabled(self, fragment_id: str) -> bool:
        return fragment_id in self.disabled


def wrap_node(
    name: str, impl: Callable[..., Any], fragment_id: str, ledger: FailureLedger
) -> Callable[..., Any]:
    """把插件贡献的节点实现包进壳（返回的仍是一个节点函数）。"""

    async def shell(state: dict) -> Any:
        try:
            result = impl(state)
            if inspect.isawaitable(result):
                result = await result
        except GraphBubbleUp:
            # interrupt() 抛的就是它——这条必须排在最前，也不能被当成"节点失败"
            raise
        except Exception as error:  # noqa: BLE001 - 插件节点的失败一律收口
            record = ledger.record(fragment_id, name, error)
            logger.warning(
                '插件节点失败已跳过 fragment=%s node=%s：%s', fragment_id, name, record.error
            )
            return {
                'plugin_failures': [
                    {
                        'fragment': record.fragment,
                        'node': record.node,
                        'error': record.error,
                        'at': record.at,
                    }
                ]
            }
        ledger.reset(fragment_id)
        return result

    shell.__name__ = f'shell_{name}'
    return shell
