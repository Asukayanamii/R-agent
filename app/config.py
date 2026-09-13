"""环境配置。换模型服务只改 .env，不动代码。"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 显式指定 .env 位置，不用 load_dotenv() 的上溯查找：
# 上溯是按调用方文件位置找的，行为和路径解析不一致，容易踩坑。
load_dotenv(PROJECT_ROOT / ".env")

LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat").strip()


def _resolve(raw: str) -> str:
    """
    把配置里的相对路径锚定到项目根。

    否则 ./data/checkpoints.db 会随启动目录漂移：从 app/ 启动就会写到 app/data/，
    凭空多出一个库，会话看起来像"丢了"。
    """
    if not raw:
        return ""
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else (PROJECT_ROOT / path).resolve())


SQLITE_PATH = _resolve(os.getenv("SQLITE_PATH", "./data/checkpoints.db"))


def llm_configured() -> bool:
    """未配置 key 时降级为桩实现，保证接口在无模型环境下依然可调通。"""
    return bool(LLM_API_KEY)
