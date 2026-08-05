"""
审计日志服务
-----------
记录所有 NL2SQL 查询到 query_audit_log 表，支持合规审查和问题追溯。
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config.database import get_raw_db
from app.config.settings import settings

logger = logging.getLogger(__name__)


class AuditService:
    """查询审计日志服务"""

    def log_query(
        self,
        question: str,
        generated_sql: Optional[str] = None,
        execution_status: str = "success",
        error_message: Optional[str] = None,
        execution_time_ms: int = 0,
        row_count: int = 0,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        ip_address: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        """记录一条查询审计日志

        Args:
            question: 用户原始自然语言问题
            generated_sql: LLM 生成的 SQL 语句
            execution_status: 执行状态 (success/error/blocked)
            error_message: 错误信息
            execution_time_ms: 执行耗时（毫秒）
            row_count: 返回行数
            user_id: 用户ID
            username: 用户名
            ip_address: 请求来源IP
            conversation_id: 对话ID
        """
        if not settings.AUDIT_LOG_ENABLED:
            return

        try:
            db: Session = get_raw_db()
            try:
                db.execute(
                    text("""
                        INSERT INTO query_audit_log
                            (user_id, username, question, generated_sql,
                             execution_status, error_message,
                             execution_time_ms, row_count, ip_address)
                        VALUES
                            (:user_id, :username, :question, :generated_sql,
                             :execution_status, :error_message,
                             :execution_time_ms, :row_count, :ip_address)
                    """),
                    {
                        "user_id": user_id,
                        "username": username,
                        "question": question[:65535],
                        "generated_sql": (generated_sql or "")[:65535],
                        "execution_status": execution_status,
                        "error_message": (error_message or "")[:65535] if error_message else None,
                        "execution_time_ms": execution_time_ms,
                        "row_count": row_count,
                        "ip_address": ip_address,
                    },
                )
                db.commit()
                logger.debug("Audit log saved: status=%s, rows=%d", execution_status, row_count)
            finally:
                db.close()
        except Exception as e:
            # 审计日志失败不应影响主流程
            logger.error("Failed to write audit log: %s", e, exc_info=True)

    def get_recent_logs(
        self,
        limit: int = 50,
        status: Optional[str] = None,
        offset: int = 0,
    ) -> list[dict]:
        """获取最近的审计日志

        Args:
            limit: 返回条数
            status: 按状态过滤
            offset: 偏移量

        Returns:
            list[dict]: 审计日志列表
        """
        try:
            db: Session = get_raw_db()
            try:
                query = """
                    SELECT id, user_id, username, question, generated_sql,
                           execution_status, error_message, execution_time_ms,
                           row_count, ip_address, created_at
                    FROM query_audit_log
                """
                params: dict = {"limit": limit, "offset": offset}

                if status:
                    query += " WHERE execution_status = :status"
                    params["status"] = status

                query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"

                result = db.execute(text(query), params)
                rows = result.fetchall()
                return [
                    {
                        "id": r[0],
                        "user_id": r[1],
                        "username": r[2],
                        "question": r[3],
                        "generated_sql": r[4],
                        "execution_status": r[5],
                        "error_message": r[6],
                        "execution_time_ms": r[7],
                        "row_count": r[8],
                        "ip_address": r[9],
                        "created_at": r[10].isoformat() if r[10] else None,
                    }
                    for r in rows
                ]
            finally:
                db.close()
        except Exception as e:
            logger.error("Failed to get audit logs: %s", e)
            return []

    def get_stats(self) -> dict:
        """获取审计统计信息"""
        try:
            db: Session = get_raw_db()
            try:
                # 总查询数
                total = db.execute(
                    text("SELECT COUNT(*) FROM query_audit_log")
                ).scalar() or 0

                # 按状态分组
                status_rows = db.execute(
                    text("""
                        SELECT execution_status, COUNT(*) as cnt
                        FROM query_audit_log
                        GROUP BY execution_status
                    """)
                ).fetchall()
                status_counts = {r[0]: r[1] for r in status_rows}

                # 平均执行时间
                avg_time = db.execute(
                    text("""
                        SELECT COALESCE(AVG(execution_time_ms), 0)
                        FROM query_audit_log
                        WHERE execution_status = 'success'
                    """)
                ).scalar() or 0

                # 最近24小时查询数
                recent = db.execute(
                    text("""
                        SELECT COUNT(*) FROM query_audit_log
                        WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                    """)
                ).scalar() or 0

                return {
                    "total_queries": total,
                    "by_status": status_counts,
                    "avg_execution_time_ms": round(avg_time, 2),
                    "queries_last_24h": recent,
                }
            finally:
                db.close()
        except Exception as e:
            logger.error("Failed to get audit stats: %s", e)
            return {"total_queries": 0, "by_status": {}, "avg_execution_time_ms": 0}


# 全局单例
audit_service = AuditService()
