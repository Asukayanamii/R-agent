"""pytest 公共设施（项目级）。

把项目根放进 ``sys.path``：``pytest tests`` 在任何 cwd 下都能 import ``app``
（从 PyCharm 直接跑测试时 cwd 不是项目根）。内核测试自己的 anyio 支持与
日志降噪在 ``tests/kernel/conftest.py``。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
