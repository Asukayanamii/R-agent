"""技能索引：文件读写的现实（BOM）、坏文件的可见性。

真跑里抓到过：Windows 上 `Set-Content -Encoding utf8` 写出来的 `SKILL.md` 带 BOM，
首行不是 `---` → frontmatter 解析失败 → 技能"凭空消失"。产品的行为是对的（**告警而不静默**），
但 BOM 是白拿的容错，读的时候顺手吃掉。
"""

import logging
from pathlib import Path

from app.agent.skills import load_skills, render_skills_block

GOOD = """\
---
name: demo-report
description: 把材料整理成三段式报告。用户说"用 demo-report"时用它。
---

# demo-report

正文里的第一句里放一个只属于正文的标记：BODY-ONLY-TOKEN。
"""


def write_skill(root: Path, name: str, text: str, *, bom: bool = False) -> Path:
    """按真实布局摆：技能在 `<工作区>/.my_agent/skills/<名字>/SKILL.md`。"""
    directory = root / '.my_agent' / 'skills' / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'SKILL.md'
    path.write_text(text, encoding='utf-8-sig' if bom else 'utf-8')
    return path


def test_bom_is_tolerated(tmp_path):
    """带 BOM 的 SKILL.md 照样能装（否则 Windows 用户第一次写技能就会"看不见"）。"""
    write_skill(tmp_path, 'demo-report', GOOD, bom=True)

    skills = {skill.name for skill in load_skills(tmp_path, global_dir='', extra_dirs=())}

    assert skills == {'demo-report'}


def test_bad_frontmatter_is_skipped_with_a_warning(tmp_path, caplog):
    """真坏的还是坏：跳过 + 告警（这条行为是"技能凭空消失"的唯一线索，不能静默）。"""
    write_skill(tmp_path, 'broken', '# 没有 frontmatter\n')

    with caplog.at_level(logging.WARNING, logger='app.agent.skills'):
        skills = list(load_skills(tmp_path, global_dir='', extra_dirs=()))

    assert skills == []
    assert any('没有可用的 frontmatter' in record.getMessage() for record in caplog.records)


def test_index_lists_name_description_and_path(tmp_path):
    """进提示词的只有名字/描述/路径——正文靠模型按需 read。"""
    path = write_skill(tmp_path, 'demo-report', GOOD)

    block = render_skills_block(list(load_skills(tmp_path, global_dir='', extra_dirs=())))

    assert '<name>demo-report</name>' in block
    assert '<description>' in block
    assert path.as_posix() in block
    assert 'BODY-ONLY-TOKEN' not in block  # 正文不进索引（靠模型按需 read）
