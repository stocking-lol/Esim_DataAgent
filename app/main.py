"""
FastAPI 应用入口
---------------
eSIM NL2SQL Platform - 企业级自然语言数据查询平台
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

# 确保项目根目录在 sys.path 中
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import FastAPI
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

import logging

from app.api.v1.router import api_router
from app.config.settings import settings, get_settings
from app.core.vanna_instance import vanna_manager
from app.core.chroma_store import chroma_store
from app.utils.errors import (
    AppException,
    app_exception_handler,
    general_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)

logger = logging.getLogger("app.main")


# ============================================================
# 应用生命周期管理
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期事件"""
    # --- 启动 ---
    _print_banner()
    _print_settings()

    # 初始化 Vanna Agent
    try:
        await vanna_manager.initialize()
        print(f"[VANNA] Agent initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize Vanna Agent: %s", e)
        print(f"[VANNA] WARNING: Agent initialization failed: {e}")
        print(f"[VANNA] Query endpoints will return 503 until Agent is ready.")

    # 自动初始化训练数据
    if settings.AUTO_INIT_TRAINING:
        await _auto_init_training()

    yield

    # --- 关闭 ---
    print("[INFO] Application shutting down...")
    try:
        await vanna_manager.shutdown()
    except Exception as e:
        logger.error("Error during Vanna shutdown: %s", e)


def _print_banner() -> None:
    """打印启动横幅"""
    banner = rf"""
╔══════════════════════════════════════════════════════════════╗
║         {settings.APP_NAME:^42}           ║
║                     v{settings.APP_VERSION:<39} ║
╚══════════════════════════════════════════════════════════════╝
"""
    print(banner)


async def _auto_init_training() -> None:
    """自动初始化 eSIM 领域训练数据

    当 ChromaDB 可用且训练数据为空时，自动调用 init_training 脚本。
    失败不会阻塞应用启动，仅打印警告。
    """
    try:
        if not chroma_store.is_initialized:
            print("[TRAINING] ChromaDB not available, skipping auto-init.")
            return

        counts = chroma_store.count_by_type()
        total = sum(counts.values())
        if total > 0:
            print(f"[TRAINING] Training data exists ({total} records), skipping auto-init.")
            return

        print("[TRAINING] No training data found. Auto-initializing eSIM domain knowledge...")
        from scripts.init_training import init_all_training_data
        result = await init_all_training_data(force=False)

        if result.get("status") == "success":
            print(f"[TRAINING] Auto-init complete: {result.get('total', 0)} records")
        else:
            print(f"[TRAINING] Auto-init skipped: {result.get('message', '')}")

    except ImportError as e:
        logger.warning("Cannot import init_training module: %s", e)
        print(f"[TRAINING] WARNING: init_training module not found, skipping auto-init.")
    except Exception as e:
        logger.warning("Auto-init training failed (non-fatal): %s", e)
        print(f"[TRAINING] WARNING: Auto-init failed: {e}")


def _print_settings() -> None:
    """打印关键配置信息"""
    print(f"[CONFIG] Debug Mode:      {settings.DEBUG}")
    print(f"[CONFIG] API Prefix:      {settings.API_V1_PREFIX}")
    print(f"[CONFIG] LLM Provider:    {settings.LLM_PROVIDER}")
    print(f"[CONFIG] LLM Model:       {settings.LLM_MODEL}")
    print(f"[CONFIG] LLM Base URL:    {settings.LLM_BASE_URL}")
    print(f"[CONFIG] ChromaDB Path:   {settings.CHROMADB_PERSIST_DIR}")
    print(f"[CONFIG] Query Timeout:   {settings.QUERY_TIMEOUT_SECONDS}s")
    print(f"[CONFIG] Max Query Rows:  {settings.MAX_QUERY_ROWS}")
    print(f"[CONFIG] Database:        {settings.DATABASE_HOST}:{settings.DATABASE_PORT}/{settings.DATABASE_NAME}")


# ============================================================
# 创建 FastAPI 应用
# ============================================================

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Enterprise-grade NL2SQL data query platform for eSIM operations.",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    lifespan=lifespan,
)

# --- CORS 中间件 ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 限流中间件 ---
from app.middleware.rate_limit import RateLimitMiddleware
app.add_middleware(
    RateLimitMiddleware,
    query_limit=30,     # /api/v1/query: 30 次/分钟
    default_limit=60,   # 其他 API: 60 次/分钟
)

# --- 注册全局异常处理器 ---
app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(ValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# --- 注册 API 路由 ---
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


# ============================================================
# 健康检查
# ============================================================

@app.get("/health", tags=["Health"])
async def health_check():
    """健康检查接口"""
    return {
        "code": 200,
        "message": "success",
        "data": {
            "status": "healthy",
            "app_name": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "llm_provider": settings.LLM_PROVIDER,
        },
    }


@app.get("/", tags=["Health"])
async def root():
    """根路径"""
    return {
        "code": 200,
        "message": f"Welcome to {settings.APP_NAME}",
        "data": {"docs": "/docs", "health": "/health"},
    }


# ============================================================
# 独立运行入口
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
    )
