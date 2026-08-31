"""
Prometheus 自定义业务指标
-------------------------
定义 NL2SQL 平台的核心业务指标，供 /metrics 端点暴露。

指标列表：
  - nl2sql_query_total:           查询总数（按状态分类）
  - nl2sql_query_duration_seconds: 查询耗时分布
  - nl2sql_security_blocked_total: 安全拦截次数（按原因分类）
  - nl2sql_correction_total:      自我修正次数（按是否成功分类）
  - nl2sql_query_accuracy:        查询准确率（Gauge）
  - nl2sql_active_users:          活跃用户数（Gauge）
"""

import logging
import time
from typing import Optional

from fastapi import Request, Response
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.routing import Mount

logger = logging.getLogger(__name__)


# ============================================================
# 自定义业务指标定义
# ============================================================

# 查询总数计数器（按状态：success / error / blocked）
nl2sql_query_total = Counter(
    "nl2sql_query_total",
    "Total number of NL2SQL queries by status",
    ["status"],
)

# 查询耗时直方图（秒）
nl2sql_query_duration_seconds = Histogram(
    "nl2sql_query_duration_seconds",
    "NL2SQL query duration in seconds",
    ["endpoint"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0, 60.0),
)

# 安全拦截计数器（按拦截原因）
nl2sql_security_blocked_total = Counter(
    "nl2sql_security_blocked_total",
    "Total number of queries blocked by security rules",
    ["reason"],
)

# 自我修正计数器（按是否成功）
nl2sql_correction_total = Counter(
    "nl2sql_correction_total",
    "Total number of self-correction attempts by success",
    ["success"],
)

# 审计写入失败计数器（坑⑪：审计失败不再静默）
nl2sql_audit_failed_total = Counter(
    "nl2sql_audit_failed_total",
    "Total number of failed audit log writes",
)

# 查询准确率（瞬时值）
nl2sql_query_accuracy = Gauge(
    "nl2sql_query_accuracy",
    "NL2SQL query accuracy rate (0.0 - 1.0)",
)

# 活跃用户数（瞬时值）
nl2sql_active_users = Gauge(
    "nl2sql_active_users",
    "Number of active users in the current window",
)


# ============================================================
# 指标便捷操作类
# ============================================================

class MetricsRecorder:
    """
    业务指标记录器

    提供简洁的 API 供 service 层和中间件调用，
    封装 label 设置和错误处理逻辑。
    """

    @staticmethod
    def record_query(status: str, duration_seconds: float, endpoint: str = "/api/v1/query") -> None:
        """记录一次查询

        Args:
            status: 查询状态 - success / error / blocked
            duration_seconds: 查询耗时（秒）
            endpoint: 查询端点路径
        """
        nl2sql_query_total.labels(status=status).inc()
        nl2sql_query_duration_seconds.labels(endpoint=endpoint).observe(duration_seconds)

    @staticmethod
    def record_security_block(reason: str) -> None:
        """记录一次安全拦截

        Args:
            reason: 拦截原因（如 sql_injection, forbidden_keyword, ddl_operation 等）
        """
        nl2sql_security_blocked_total.labels(reason=reason).inc()

    @staticmethod
    def record_correction(success: bool) -> None:
        """记录一次自我修正

        Args:
            success: 修正是否成功
        """
        nl2sql_correction_total.labels(success=str(success).lower()).inc()

    @staticmethod
    def set_query_accuracy(accuracy: float) -> None:
        """设置当前查询准确率

        Args:
            accuracy: 准确率值（0.0 - 1.0）
        """
        nl2sql_query_accuracy.set(max(0.0, min(1.0, accuracy)))

    @staticmethod
    def set_active_users(count: int) -> None:
        """设置当前活跃用户数

        Args:
            count: 活跃用户数量
        """
        nl2sql_active_users.set(max(0, count))

    @staticmethod
    def record_audit_failed() -> None:
        """记录一次审计写入失败（坑⑪）"""
        nl2sql_audit_failed_total.inc()


# 全局单例
metrics = MetricsRecorder()


# ============================================================
# 指标采集中间件
# ============================================================

# 需要采集业务指标的路径
_METRICS_PATHS = ("/api/v1/query", "/api/v1/query/stream")


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    业务指标采集中间件

    拦截 /api/v1/query 请求，自动采集查询耗时和状态。
    安全拦截和自我修正等细粒度指标由 service 层主动调用
    MetricsRecorder 记录。
    """

    def __init__(self, app):
        super().__init__(app)
        logger.info("MetricsMiddleware initialized")

    def _should_collect(self, path: str) -> bool:
        """判断请求路径是否需要采集指标"""
        return any(path == p or path.startswith(p) for p in _METRICS_PATHS)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        # 非查询路径直接放行
        if not self._should_collect(path):
            return await call_next(request)

        # GET 请求（如 /query/status）不采集业务指标
        if request.method == "GET":
            return await call_next(request)

        # 记录开始时间
        start_time = time.perf_counter()

        # 执行请求
        try:
            response = await call_next(request)
        except Exception:
            # 请求异常，记录 error 状态
            duration = time.perf_counter() - start_time
            metrics.record_query(status="error", duration_seconds=duration, endpoint=path)
            raise

        # 计算耗时
        duration = time.perf_counter() - start_time

        # 根据响应状态码判断查询状态
        status = self._determine_status(response.status_code)

        # 如果响应体中包含安全拦截标记，则状态为 blocked
        # （此处仅基于 HTTP 状态码做初步判断，精确的 blocked 状态
        #   由 service 层调用 metrics.record_security_block 补充记录）
        if response.status_code == 200:
            # 尝试从 response 中读取业务状态码
            blocked = getattr(request.state, "_security_blocked", False)
            if blocked:
                status = "blocked"

        metrics.record_query(status=status, duration_seconds=duration, endpoint=path)

        return response

    @staticmethod
    def _determine_status(http_status: int) -> str:
        """根据 HTTP 状态码判断查询状态

        Args:
            http_status: HTTP 响应状态码

        Returns:
            str: success / error
        """
        if http_status >= 400:
            return "error"
        return "success"


# ============================================================
# Prometheus ASGI 应用挂载
# ============================================================

def setup_prometheus(app) -> None:
    """
    将 Prometheus 指标端点挂载到 FastAPI 应用

    在 /metrics 路径下暴露 prometheus_client 收集的所有指标。
    应在应用创建后、路由注册前调用。

    Args:
        app: FastAPI 应用实例
    """
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)
    logger.info("Prometheus metrics endpoint mounted at /metrics")
