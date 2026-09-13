"""环境配置。换模型服务只改 .env，不动代码。"""

import os

from dotenv import load_dotenv

load_dotenv()

LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat").strip()

SQLITE_PATH = os.getenv("SQLITE_PATH", "./data/checkpoints.db").strip()


def llm_configured() -> bool:
    """未配置 key 时降级为桩实现，保证接口在无模型环境下依然可调通。"""
    return bool(LLM_API_KEY)
