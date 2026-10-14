"""把 `tests/harness` 放进 ``sys.path``：发现器用例要复用那套"真骨架 + 可合成"夹具
（``make_ready_runtime`` / ``support``）。两个目录各是一套独立测试根，这里显式搭桥。
"""

import sys
from pathlib import Path

_HARNESS = Path(__file__).resolve().parents[1] / 'harness'
if str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))
