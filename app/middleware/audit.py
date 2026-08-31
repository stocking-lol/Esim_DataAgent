"""
审计中间件
---------
拦截所有 /api/v1/query 请求，自动记录审计日志。

记录内容：
  - 用户信息（user_id, username, 从 JWT 提取）
  - 原始问题（从请求体 question 字段）
  - 安全检查结果（从响应 code 判断是否被安全拦截）
  - 最终 SQL 和执行结果（从响应体提取）
  - 客户端 IP、对话ID

写入方式：异步线程池执行，不阻塞主请求流程。
使用 audit_service 进行实际的日志写入。
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = logging.getLogger(__name__)

# 仅对这些路径进行审计记录
_AUDIT_PATHS = ("/api/v1/query", "/api/v1/query/stream")


class AuditMiddleware(BaseHTTPMiddleware):
    """
    查询审计中间件

    拦截 /api/v1/query 和 /api/v1/query/stream 请求，
    在请求完成后异步记录审计日志。

    注意：query_service 内部已有 service 层审计调用（_audit_log），
    本中间件作为 HTTP 层补充，捕获更完整的请求上下文（IP、用户信息等）。
    若 service 层已记录，则中间件不再重复写入（通过 request.state 标记判断）。
    """

    def __init__(self, app):
        super().__init__(app)
        logger.info("AuditMiddleware initialized")

    def _should_audit(self, path: str) -> bool:
        """判断请求路径是否需要审计"""
        return any(path == p or path.startswith(p) for p in _AUDIT_PATHS)

    def _get_client_ip(self, request: Request) -> str:
        """获取客户端真实 IP（支持代理转发）"""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
        return request.client.host if request.client else "unknown"

    def _extract_user_info(self, request: Request) -> dict:
        """从 Authorization 头中解析用户信息"""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return {}
        try:
            from app.core.auth import JWTManager

            token = auth_header[7:]
            payload = JWTManager.verify_token(token)
            return {
                "user_id": int(payload.get("sub", 0)),
                "username": payload.get("username", ""),
            }
        except Exception:
            return {}

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        # 非查询路径直接放行
        if not self._should_audit(path):
            return await call_next(request)

        # GET /query/status 不需要审计
        if request.method == "GET":
            return await call_next(request)

        # 读取请求体（需缓存以便后续端点使用）
        body_bytes = await request.body()
        request._body = body_bytes

        # 解析请求体
        question: Optional[str] = None
        conversation_id: Optional[str] = None
        try:
            if body_bytes:
                body = json.loads(body_bytes)
                question = body.get("question")
                conversation_id = body.get("conversation_id")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

        # 提取用户信息和 IP
        user_info = self._extract_user_info(request)
        ip_address = self._get_client_ip(request)

        # 标记：service 层是否已写入审计日志
        request.state._audit_logged_by_service = False

        # 执行请求
        response = await call_next(request)

        # 如果 service 层已记录审计日志，则中间件不再重复写入
        if getattr(request.state, "_audit_logged_by_service", False):
            return response

        # 从响应中提取审计信息
        audit_data = await self._extract_audit_from_response(response)

        # 异步写入审计日志（不阻塞响应返回）
        asyncio.create_task(
            self._write_audit_log(
                question=question or "",
                conversation_id=conversation_id,
                user_info=user_info,
                ip_address=ip_address,
                audit_data=audit_data,
            )
        )

        return response

    async def _extract_audit_from_response(self, response: Response) -> dict:
        """从响应中提取审计所需信息

        Returns:
            dict: 包含 execution_status, error_message, generated_sql,
                  execution_time_ms, row_count, security_blocked 等字段
        """
        audit_data = {
            "execution_status": "success",
            "error_message": None,
            "generated_sql": None,
            "execution_time_ms": 0,
            "row_count": 0,
            "security_blocked": False,
        }

        # 坑④ 修复：SSE 流式响应不得整体缓冲读取，否则首包延迟=整流时长。
        # 注意：BaseHTTPMiddleware 包装后的 response.media_type 可能为 None，
        # 必须用 Content-Type 头判断，否则会漏判并整体消费流。
        content_type = (response.headers.get("content-type") or "").lower()
        if "text/event-stream" in content_type:
            return audit_data

        try:
            # 读取响应体
            response_body = b""
            async for chunk in response.body_iterator:
                response_body += chunk
            # 重置 body_iterator 以便响应正常返回
            response.body_iterator = _async_iter([response_body])

            if not response_body:
                return audit_data

            data = json.loads(response_body)
            resp_code = data.get("code", 200)
            resp_data = data.get("data", {})

            # 判断安全拦截
            if resp_code == 1001 or resp_data.get("blocked"):
                audit_data["execution_status"] = "blocked"
                audit_data["security_blocked"] = True
                audit_data["error_message"] = data.get("message", "")
            elif resp_code != 200:
                audit_data["execution_status"] = "error"
                audit_data["error_message"] = data.get("message", "")
            else:
                audit_data["execution_status"] = "success"

            # 提取 SQL 和执行信息
            audit_data["generated_sql"] = resp_data.get("sql")
            audit_data["execution_time_ms"] = int(
                resp_data.get("execution_time_ms", 0)
            )
            audit_data["row_count"] = resp_data.get("row_count", 0)

            # 如果有错误信息字段
            if resp_data.get("error"):
                audit_data["error_message"] = resp_data["error"]
                if audit_data["execution_status"] == "success":
                    audit_data["execution_status"] = "error"

        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as e:
            logger.debug("Failed to parse response body for audit: %s", e)
        except Exception as e:
            logger.warning("Unexpected error extracting audit data: %s", e)

        return audit_data

    async def _write_audit_log(
        self,
        question: str,
        conversation_id: Optional[str],
        user_info: dict,
        ip_address: str,
        audit_data: dict,
    ) -> None:
        """异步写入审计日志（静默失败）"""
        try:
            from app.services.audit_service import audit_service

            audit_service.log_query(
                question=question,
                generated_sql=audit_data.get("generated_sql"),
                execution_status=audit_data.get("execution_status", "success"),
                error_message=audit_data.get("error_message"),
                execution_time_ms=audit_data.get("execution_time_ms", 0),
                row_count=audit_data.get("row_count", 0),
                user_id=user_info.get("user_id"),
                username=user_info.get("username"),
                ip_address=ip_address,
                conversation_id=conversation_id,
                security_blocked=audit_data.get("security_blocked", False),
            )
        except Exception as e:
            logger.error("Audit middleware failed to write log: %s", e, exc_info=True)
            try:
                from app.middleware.metrics import metrics
                metrics.record_audit_failed()
            except Exception:
                pass


async def _async_iter(data: list[bytes]):
    """将字节列表转换为异步迭代器（用于重置 response.body_iterator）"""
    for chunk in data:
        yield chunk
