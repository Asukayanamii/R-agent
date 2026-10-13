# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 my-agent（R-agent）整体打成可运行目录。

    pip install pyinstaller
    pyinstaller --clean --noconfirm packaging/r-agent.spec

产出 `dist/r-agent/r-agent.exe`：

    r-agent.exe            # 桌面窗口（需要 pywebview，见 requirements-desktop.txt）
    r-agent.exe --serve    # 只跑后端；浏览器打开日志里那个 /ui/ 地址
    r-agent.exe --serve --port 8123

打包后要有数件必须显式带上：内置前端的静态资产、内置技能库——它们是**包内数据**，
PyInstaller 只自动收集 `.py`，漏了的表现恰好是"前端 404 / 技能索引为空"。
运行时的可写文件（会话库、插件库、用户配置）都不在安装目录里，见 app/common/config。
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent
"""spec 放在 `packaging/` 下，所以项目根是它的上一级。"""

STATIC = ROOT / 'app' / 'plugins' / 'builtin' / 'webui' / 'static'
SKILLS = ROOT / 'app' / 'plugins' / 'builtin' / 'skillbook' / 'skills'

a = Analysis(  # noqa: F821 - PyInstaller 在 spec 命名空间里注入这些名字
    [str(ROOT / 'run_desktop.py')],
    pathex=[str(ROOT)],
    binaries=[],
    # 数据文件按"包内相对路径"摆放：插件用 `Path(__file__).parent / 'static'` 找它们
    datas=[
        (str(STATIC), 'app/plugins/builtin/webui/static'),
        (str(SKILLS), 'app/plugins/builtin/skillbook/skills'),
    ],
    hiddenimports=[
        # uvicorn 的协议/事件循环实现是运行时挑的，静态分析看不见
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # 存储：异步 sqlite 的 checkpointer 是字符串路径导入的
        'langgraph.checkpoint.sqlite.aio',
        'aiosqlite',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython'],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='r-agent',
    debug=False,
    strip=False,
    upx=False,
    # 桌面模式想要"没有黑框"就改成 console=False（这次留控制台：日志就是这个应用的界面）
    console=True,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='r-agent',
)
