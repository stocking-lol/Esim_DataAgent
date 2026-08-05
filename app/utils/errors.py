"""
统一异常处理模块
---------------
定义自定义异常类和全局异常处理器，确保所有 API 返回统一格式的错误响应。
"""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


# ============================================================
# 自定义异常类
# ============================================================

class AppException(Exception):
    """应用基础异常"""

    def __init__(self, code: int = 500, message: str = "Internal server error"):
        self.code = code
        self.message = message
        super().__init__(self.message)


class SecurityException(AppException):
    """SQL安全拦截异常（code: 1001）"""

    def __init__(self, message: str = "SQL安全拦截"):
        super().__init__(code=1001, message=message)


class SQLExecutionException(AppException):
    """SQL执行错误异常（code: 1002）"""

    def __init__(self, message: str = "SQL执行错误"):
        super().__init__(code=1002, message=message)


class LLMException(AppException):
    """LLM服务异常（code: 1003）"""

    def __init__(self, message: str = "LLM服务异常"):
        super().__init__(code=1003, message=message)


class DatabaseException(AppException):
    """数据库连接异常（code: 1004）"""

    def __init__(self, message: str = "数据库连接异常"):
        super().__init__(code=1004, message=message)


class AuthenticationException(AppException):
    """认证失败（code: 401）"""

    def __init__(self, message: str = "未认证"):
        super().__init__(code=401, message=message)


class AuthorizationException(AppException):
    """权限不足（code: 403）"""

    def __init__(self, message: str = "无权限"):
        super().__init__(code=403, message=message)


class NotFoundException(AppException):
    """资源不存在（code: 404）"""

    def __init__(self, message: str = "资源不存在"):
        super().__init__(code=404, message=message)


class RateLimitException(AppException):
    """请求过于频繁（code: 429）"""

    def __init__(self, message: str = "请求过于频繁"):
        super().__init__(code=429, message=message)


class BadRequestException(AppException):
    """请求参数错误（code: 400）"""

    def __init__(self, message: str = "请求参数错误"):
        super().__init__(code=400, message=message)


# ============================================================
# 全局异常处理器
# ============================================================


def _build_error_response(code: int, message: str, detail: Any = None) -> dict:
    """构建统一错误响应"""
    return {
        "code": code,
        "message": message,
        "data": detail,
    }


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """处理 AppException 及其子类"""
    return JSONResponse(
        status_code=_http_status_for_code(exc.code),
        content=_build_error_response(exc.code, exc.message),
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理请求参数验证错误（pydantic ValidationError）"""
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        errors = []
        for error in exc.errors():
            field = " -> ".join(str(loc) for loc in error["loc"])
            msg = error["msg"]
            errors.append(f"{field}: {msg}")
        detail = "; ".join(errors) if errors else str(exc)
    else:
        detail = str(exc)

    return JSONResponse(
        status_code=422,
        content=_build_error_response(400, f"参数验证失败: {detail}"),
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理所有未被捕获的异常"""
    return JSONResponse(
        status_code=500,
        content=_build_error_response(500, f"服务器内部错误: {str(exc)}"),
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理 FastAPI HTTPException"""
    from fastapi.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=_build_error_response(exc.status_code, exc.detail),
        )
    return await general_exception_handler(request, exc)


def _http_status_for_code(code: int) -> int:
    """将业务错误码映射到 HTTP 状态码"""
    if code < 1000:
        return code
    # 业务错误码统一返回 200，让前端根据 code 字段判断
    return 200
