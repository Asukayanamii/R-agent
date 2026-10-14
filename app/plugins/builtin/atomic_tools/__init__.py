"""builtin/atomic_tools：把"一次调用做一批原子操作"的工具补上。

**为什么要有它**：工具调用是**一次一轮模型往返**——一处一改的接口，模型就会一处一返回。
真跑记录（改一份 2400 行的前端单文件）：65 次工具调用 ≈ 65 次模型往返，31.6 分钟里 91%
的时间花在往返之间；其中 `edit` 16 次、`read` 34 次，大多还是"改一处、再读一眼"。
同一个任务里，一次输出带多个动作的形态（人/上层 agent 的做法）只要个位数往返。

两个工具是同一批原语的**批量组合**（`edit` / `read` 的本体都已抽成可复用函数，不复制逻辑）：

- `edit_many(edits)`：一次改多处，可跨文件。**失败即停**，并说清"第几条、为什么、
  前面几条已应用"——批量工具最怕的是"默默只改了一半"。单文件时照样给界面画差异。
- `read_many(reads)`：一次读多段/多文件。单个失败不影响其它（读是幂等的），
  每段仍带行号与续读位置。

生效时机：能力类，**装载即生效**——下一次模型调用就能看到这两个工具，不用等回合边界。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import ToolException, tool

from app.agent.sandbox import READ, guard_path
from app.agent.tools.common import MAX_BYTES, clip_bytes, fail, rel
from app.agent.tools.diff import build_diff
from app.agent.tools.edit import apply_edit
from app.agent.tools.read import read_text
from app.common.exceptions import SandboxDenied

PLUGIN_ID = 'builtin/atomic_tools'

MAX_BATCH = 40
"""一次最多几条：几百条的批量是"写个脚本"的活，不该塞进一次工具调用。"""


def _snapshot(path: str) -> str | None:
    """批量前先留一份原文（只为"单文件时给界面画差异"，读不到就算了）。"""
    try:
        target = guard_path(path, READ)
    except SandboxDenied:
        return None
    try:
        with target.open(encoding='utf-8', newline='') as handle:
            return handle.read().replace('\r\n', '\n')
    except OSError:
        return None


@tool(response_format="content_and_artifact")
async def edit_many(edits: list[dict]) -> tuple[str, dict | None]:
    """
    一次在文件里做多处精确替换（可跨文件）——**要改多处就用这个，别一处一次调 edit**。

    `edits` 是列表，每项 `{path, old_string, new_string, replace_all?}`；按顺序应用，
    **任何一条失败就停在那里**，返回里写明"第几条、为什么、前面几条已应用"。
    匹配规则与 `edit` 完全一致（唯一性要求、行尾空白的宽松匹配、保留原换行风格）。
    """
    if not edits:
        fail("edits 不能为空")
    if len(edits) > MAX_BATCH:
        fail(f"一次最多 {MAX_BATCH} 条（收到 {len(edits)} 条）；再多请写脚本用 bash")

    paths: list[str] = []
    for item in edits:
        if not isinstance(item, dict) or not str(item.get('path') or '').strip():
            fail("每一条都要有 path；请把 edits 写成 [{path, old_string, new_string}, ...]")
        path = str(item['path']).strip()
        if path not in paths:
            paths.append(path)

    before = _snapshot(paths[0]) if len(paths) == 1 else None
    applied: list[str] = []
    for index, item in enumerate(edits, 1):
        path = str(item['path']).strip()
        try:
            message, _ = apply_edit(
                path,
                str(item.get('old_string') or ''),
                str(item.get('new_string') or ''),
                bool(item.get('replace_all', False)),
            )
        except ToolException as exc:
            fail(
                f"第 {index} 条（{path}）失败：{exc}\n"
                f"前面 {len(applied)} 条已应用：{applied}。改完剩下的可以再调一次 edit_many。"
            )
        applied.append(f"{index}. {message}")

    detail = "\n".join(applied)
    if len(paths) == 1:
        after = _snapshot(paths[0])
        diff = build_diff(before, after) if before is not None and after is not None else None
        return f"已修改 {len(applied)} 处（{paths[0]}）：\n{detail}", diff
    return f"已修改 {len(applied)} 处，涉及 {len(paths)} 个文件：\n{detail}", None


@tool
async def read_many(reads: list[dict]) -> str:
    """
    一次读多段/多个文件——**要读好几处就用这个，别一段一次调 read**。

    `reads` 是列表，每项 `{path, offset?, limit?}`（默认从第 1 行读到上限，
    与 `read` 同一套行号与续读规则）。**单个读失败不影响其它**：那一项就地写成失败原因，
    其余照常返回——读是只读操作，没必要因为一个路径写错就整批作废。
    """
    if not reads:
        fail("reads 不能为空")
    if len(reads) > MAX_BATCH:
        fail(f"一次最多 {MAX_BATCH} 段（收到 {len(reads)} 段）")

    blocks: list[str] = []
    for index, item in enumerate(reads, 1):
        if not isinstance(item, dict) or not str(item.get('path') or '').strip():
            blocks.append(f"── 第 {index} 段：缺少 path ──")
            continue
        path = str(item['path']).strip()
        try:
            blocks.append(read_text(path, int(item.get('offset') or 1), int(item.get('limit') or 2000)))
        except ToolException as exc:
            blocks.append(f"── {path}：{exc} ──")

    joined = "\n\n".join(blocks)
    clipped, dropped = clip_bytes(joined, MAX_BYTES)
    tail = f"\n（总体超出上限，后面 {dropped} 字节没给；分几次读或缩小 limit）" if dropped else ""
    return f"读了 {len(reads)} 段：\n\n{clipped}{tail}"


def apply(ctx: Any) -> None:
    ctx.tools.register(edit_many)
    ctx.tools.register(read_many)
    ctx.logger.info(
        '批量工具已注册：edit_many / read_many（一次最多 %d 条）——'
        '工具调用是一轮一次模型往返，批量版把「改多处」压成一次调用',
        MAX_BATCH,
    )


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'inject': ['tools'],
}
