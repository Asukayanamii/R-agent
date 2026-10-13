import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.web.api import chat, plugins
from app.common.config import LOG_LEVEL
from app.common.exceptions import InvalidInput, SandboxDenied
from app.container import get_assets, shutdown, startup
from app.web.handler import invalid_input, unhandled_exception
from app.harness.assets import ASSETS_STATE_ATTR, assets_router
from app.web.result.result import ErrorResult, Result

def _configure_logging() -> None:
    """装日志：第三方压到 WARNING，我们自己的 `app.*` 开到 `LOG_LEVEL`。

    **在启动时调，不在导入时调**：模块级副作用会让"只是 import 一下"的代码
    （测试、CLI、脚本）悄悄改掉全局日志行为——曾经让内核的 DEBUG 用例在收集阶段就被压没。
    """
    logging.basicConfig(
        # 第三方库的 INFO/DEBUG 是噪音（aiosqlite 连每条 SQL 和参数 blob 都打），统一压到 WARNING；
        # 我们自己的 app.* 另行开到 LOG_LEVEL。uvicorn 的 logger 自带 handler 且不向上传播，不受影响。
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,  # 覆盖 root 上可能已经被装过的配置
    )
    logging.getLogger("app").setLevel(getattr(logging, LOG_LEVEL, logging.INFO))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    await startup()
    # 资产注册表放在 app.state 上：那条动态路由按请求查表，装卸只改表、不动路由表
    setattr(app.state, ASSETS_STATE_ATTR, get_assets())
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
# 参数不合法是预期内的错误：400 + 一行日志，不打 traceback（见 handler.invalid_input）
app.add_exception_handler(InvalidInput, invalid_input)
app.add_exception_handler(SandboxDenied, invalid_input)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(plugins.router)

# `/ui` 不再静态挂载：一条查表的动态路由（未装载/卸载后都返回 404）
app.include_router(assets_router())


@app.get("/")
async def root():
    return Result.success(message="基础相应格式，code=0表示成功，code=1表示业务逻辑出错失败",data={"name":"campus-equipment-mgr测试响应对象实例"})
