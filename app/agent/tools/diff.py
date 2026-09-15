"""
edit 的差异数据：给界面看的结构化 diff，模型看不到它。

三个取舍：

- **模型不需要它**：它刚做完这次替换，把 diff 塞进工具返回值只会长期占上下文——每次编辑的
  diff 都会留在历史里。Pi 同样把 diff 放进 `details`，模型收到的正文只有一行回执。
- **界面需要数据，不是文本**：图形界面要按行渲染（两列行号、增删底色、折叠的未变化段），
  丢一段 unified diff 字符串过去等于把解析工作推给前端。所以这里产出的是行列表。
- **比较用未改/已改两份文本**：`edit` 在 LF 归一化后的文本上做替换，传进来也就没有 CRLF
  差异，Windows 上不会出现"整篇重写"的假差异。
"""

import difflib

CONTEXT_LINES = 4
"""每个改动块前后各显示几行未变化内容（GitHub / unified diff 的惯例是 3~5）。"""

MAX_ROWS = 200
"""行数上限。超出就只给前面的，并如实说明还有多少行没显示。"""

MAX_INPUT_LINES = 20000
"""文件总行数上限。`SequenceMatcher` 是纯 Python 的，在两个大文件之间比对会卡住事件循环——
那时候别的工具与流式输出都在等它，宁可不给差异。"""


def build_diff(old: str, new: str) -> dict | None:
    """
    产出结构化差异；两边内容一样时返回 None。

    行列表里每行是 `{kind, old, new, text, count}`：

    | kind | 含义 | 行号 |
    | --- | --- | --- |
    | `ctx` | 未变化，作为上下文显示 | 两边都有 |
    | `del` | 删除 | 只有旧文件的行号 |
    | `add` | 新增 | 只有新文件的行号 |
    | `skip` | 被折叠的未变化段，`count` 是折叠了几行 | 不带行号 |

    行号的键名统一叫 `old` / `new`（不是 `before` / `after`）：前端要按"旧文件列 / 新文件列"
    渲染两列，名字对上更不容易接错。
    """
    old_lines = old.split("\n")
    new_lines = new.split("\n")
    if old_lines == new_lines:
        return None

    if len(old_lines) + len(new_lines) > MAX_INPUT_LINES:
        return {
            "additions": 0,
            "deletions": 0,
            "lines": [],
            "truncated": True,
            "omitted": 0,
        }

    rows: list[dict] = []
    additions = deletions = 0
    cursor = 0

    for group in difflib.SequenceMatcher(
        None, old_lines, new_lines
    ).get_grouped_opcodes(CONTEXT_LINES):
        # 组与组之间那段未变化内容被 get_grouped_opcodes 丢掉了：补一行折叠说明，
        # 否则界面上看不出"中间还有多少行没显示"。
        if group[0][1] > cursor:
            rows.append({"kind": "skip", "count": group[0][1] - cursor})
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for offset, text in enumerate(old_lines[i1:i2]):
                    rows.append(
                        {
                            "kind": "ctx",
                            "old": i1 + offset + 1,
                            "new": j1 + offset + 1,
                            "text": text,
                        }
                    )
            else:
                for offset, text in enumerate(old_lines[i1:i2]):
                    rows.append({"kind": "del", "old": i1 + offset + 1, "text": text})
                for offset, text in enumerate(new_lines[j1:j2]):
                    rows.append({"kind": "add", "new": j1 + offset + 1, "text": text})
                additions += j2 - j1
                deletions += i2 - i1
            cursor = i2

    omitted = max(0, len(rows) - MAX_ROWS)
    if omitted:
        rows = rows[:MAX_ROWS]

    return {
        "additions": additions,
        "deletions": deletions,
        "lines": rows,
        "truncated": omitted > 0,
        "omitted": omitted,
    }
