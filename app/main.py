import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import chat
from app.config import LOG_LEVEL
from app.container import shutdown, startup
from app.handler import unhandled_exception
from app.result.result import ErrorResult, Result

logging.basicConfig(
    # 第三方库的 INFO/DEBUG 是噪音（aiosqlite 连每条 SQL 和参数 blob 都打），统一压到 WARNING；
    # 我们自己的 app.* 另行开到 LOG_LEVEL。uvicorn 的 logger 自带 handler 且不向上传播，不受影响。
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,  # 覆盖 root 上可能已经被装过的配置
)
logging.getLogger("app").setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await startup()
    yield
    await shutdown()


app = FastAPI(
    title="my-agent",
    description="my-agent后端",
    version="0.0.1",
    lifespan=lifespan,
    # 所有接口都可能走到下面注册的全局处理器，错误形状在文档里也写出来
    responses={500: {"model": ErrorResult, "description": "未处理的异常"}},
)

app.add_exception_handler(Exception, unhandled_exception)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)

app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")


@app.get("/")
async def root():
    return Result.success(message="基础相应格式，code=0表示成功，code=1表示业务逻辑出错失败",data={"name":"campus-equipment-mgr测试响应对象实例"})
