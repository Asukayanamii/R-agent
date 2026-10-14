"""启动器与库位置配置：`python run_desktop.py --serve` 的参数解析、库位置一览。

打包形态（`sys.frozen`）换的是**默认落点**（用户目录），源码运行的默认值不变——
这条钉住"老用户的位置一个字节都不动"。
"""

from app.common.config import (
    APP_DIR,
    FROZEN,
    HOME_DIR,
    PROJECT_ROOT,
    USER_ENV,
    library_paths,
)
from app.desktop import _arg_value


def test_source_run_keeps_engineer_defaults():
    assert FROZEN is False
    assert APP_DIR == PROJECT_ROOT
    assert USER_ENV == HOME_DIR / '.env'


def test_arg_value_accepts_both_spellings():
    assert _arg_value(['--serve'], '--port', 0) == 0
    assert _arg_value(['--port', '8123'], '--port', 0) == 8123
    assert _arg_value(['--port=8123'], '--port', 0) == 8123
    assert _arg_value(['--port', '8123', '--serve'], '--port', 0) == 8123


def test_library_paths_lists_every_library():
    paths = library_paths()

    assert set(paths) == {'应用目录', '用户配置', '全局插件库', '全局技能库', '会话库'}
    assert paths['用户配置'] == str(USER_ENV)
    assert paths['应用目录'] == str(APP_DIR)
    for value in paths.values():
        assert isinstance(value, str)
