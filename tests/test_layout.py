"""分层守卫：**目录顺序 = 依赖方向**（设计稿 §7）。

扫描 ``app/**/*.py`` 的 import，按允许矩阵断言：

    kernel / common   什么都不 import（app 内）
    harness  → kernel
    agent    → kernel、harness、common
    plugins  → kernel、harness、agent、common
    web      → 上面全部 + 自己；内部再守一次 api → service 单向

（存储那层已经不在这里了：阶段 K2 把它拆成了 `builtin/store` 插件。）

入口与装配根（`main.py` / `desktop.py` / `container.py` / `harness/__main__.py`）豁免——
它们的职责就是"什么都知道"。例外表只有一条：api 经装配根取服务（服务定位器），写在
`EXCEPTIONS` 里并注明理由。

约束靠机制，不靠措辞：这条写进用例，比在文档里写十句"不许反向依赖"管用。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'app'

ALLOWED = {
    'kernel': (),
    'common': (),
    'harness': ('kernel',),
    'agent': ('kernel', 'harness', 'common'),
    'plugins': ('kernel', 'harness', 'agent', 'common'),
    'web': ('kernel', 'harness', 'agent', 'plugins', 'common'),
}

ALLOWED_WEB = {
    'api': ('api', 'service', 'result', 'constant'),
    'service': ('service', 'result', 'constant'),
    'handler': ('handler', 'result', 'constant'),
    'result': ('result', 'constant'),
    'constant': ('constant',),
}

ENTRIES = ('main.py', 'desktop.py', 'container.py', 'harness/__main__.py')

EXCEPTIONS = {
    # 服务定位器：api 经装配根拿服务（get_service / get_protocol），不自己建依赖。
    # 装配根是唯一的全局句柄；改错地方这条会红。
    ('web/api/chat.py', 'container'),
}


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
    return found


def _scan() -> tuple[list[str], list[str], int]:
    """返回（跨层违规、web 内部违规、扫描文件数）。"""
    across: list[str] = []
    inner: list[str] = []
    scanned = 0
    for path in sorted(ROOT.rglob('*.py')):
        if '__pycache__' in path.parts:
            continue
        scanned += 1
        relative = path.relative_to(ROOT).as_posix()
        if relative in ENTRIES:
            continue
        source = relative.split('/')[0] if '/' in relative else None
        for lineno, target in _imports(path):
            parts = target.split('.')
            if parts[0] != 'app' or len(parts) < 2:
                continue
            layer = parts[1]
            if (relative, layer) in EXCEPTIONS:
                continue
            if source is None:
                across.append(f'{relative}:{lineno} import {target}——顶层只放入口与装配根')
                continue
            if layer == source:
                if source == 'web':
                    inner.extend(_web_inner(relative, parts, lineno))
                continue
            if layer not in ALLOWED[source]:
                allowed = '、'.join(ALLOWED[source]) or '（无 app 内依赖）'
                across.append(
                    f'{relative}:{lineno} import {target}——{source}/ 只允许依赖 {allowed}'
                )
    return across, inner, scanned


def _web_inner(relative: str, parts: list[str], lineno: int) -> list[str]:
    sub = relative.split('/')[1]
    if len(parts) < 3 or sub not in ALLOWED_WEB:
        return []
    target_sub = parts[2]
    if target_sub in ALLOWED_WEB[sub] or target_sub not in ALLOWED_WEB:
        return []
    allowed = '、'.join('web/' + item for item in ALLOWED_WEB[sub])
    return [f'{relative}:{lineno} import {".".join(parts)}——web/{sub}/ 只允许依赖 {allowed}']


def test_layers_are_one_way():
    """内核侧单向向上：kernel → harness → agent → plugins；web 只向下，common 谁都不依赖。"""
    across, _, scanned = _scan()

    assert scanned > 60, f'扫描到的文件太少（{scanned}），路径算错了？'
    assert not across, '依赖方向被破坏：\n' + '\n'.join(across)


def test_web_three_layers_order():
    """经典三层：api → service → dao 单向。"""
    _, inner, _ = _scan()

    assert not inner, 'web 内部的层级被破坏：\n' + '\n'.join(inner)
