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
        security_blocked: bool = False,
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
            security_blocked: 是否被安全网关拦截
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
                             execution_time_ms, row_count, ip_address,
                             conversation_id, security_blocked)
                        VALUES
                            (:user_id, :username, :question, :generated_sql,
                             :execution_status, :error_message,
                             :execution_time_ms, :row_count, :ip_address,
                             :conversation_id, :security_blocked)
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
                        "conversation_id": conversation_id,
                        "security_blocked": 1 if security_blocked else 0,
                    },
                )
                db.commit()
                logger.debug("Audit log saved: status=%s, rows=%d", execution_status, row_count)
            finally:
                db.close()
        except Exception as e:
            # 审计日志失败不应影响主流程
            logger.error("Failed to write audit log: %s", e, exc_info=True)

    def get_log_by_id(self, log_id: int) -> Optional[dict]:
        """根据 ID 获取单条审计日志

        Args:
            log_id: 审计日志ID

        Returns:
            dict | None: 审计日志详情，不存在时返回 None
        """
        try:
            db: Session = get_raw_db()
            try:
                result = db.execute(
                    text("""
                        SELECT id, user_id, username, question, generated_sql,
                               execution_status, error_message, execution_time_ms,
                               row_count, ip_address, conversation_id,
                               security_blocked, created_at
                        FROM query_audit_log
                        WHERE id = :log_id
                    """),
                    {"log_id": log_id},
                )
                row = result.fetchone()
                if row is None:
                    return None
                return _row_to_dict(row)
            finally:
                db.close()
        except Exception as e:
            logger.error("Failed to get audit log by id=%s: %s", log_id, e)
            return None

    def get_logs_filtered(
        self,
        user_id: Optional[int] = None,
        status: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """分页查询审计日志（支持多条件过滤）

        Args:
            user_id: 按用户ID过滤
            status: 按执行状态过滤 (success/error/blocked)
            start_date: 起始日期 (YYYY-MM-DD 或 ISO datetime)
            end_date: 截止日期 (YYYY-MM-DD 或 ISO datetime)
            page: 页码（从1开始）
            page_size: 每页条数

        Returns:
            dict: {"items": [...], "total": int, "page": int, "page_size": int, "total_pages": int}
        """
        try:
            db: Session = get_raw_db()
            try:
                conditions: list[str] = []
                params: dict = {}

                if user_id is not None:
                    conditions.append("user_id = :user_id")
                    params["user_id"] = user_id

                if status:
                    conditions.append("execution_status = :status")
                    params["status"] = status

                if start_date:
                    conditions.append("created_at >= :start_date")
                    params["start_date"] = start_date

                if end_date:
                    conditions.append("created_at <= :end_date")
                    params["end_date"] = end_date

                where_clause = ""
                if conditions:
                    where_clause = " WHERE " + " AND ".join(conditions)

                # 查询总数
                count_sql = f"SELECT COUNT(*) FROM query_audit_log{where_clause}"
                total = db.execute(text(count_sql), params).scalar() or 0

                # 分页查询
                offset = (page - 1) * page_size
                params["limit"] = page_size
                params["offset"] = offset

                list_sql = f"""
                    SELECT id, user_id, username, question, generated_sql,
                           execution_status, error_message, execution_time_ms,
                           row_count, ip_address, conversation_id,
                           security_blocked, created_at
                    FROM query_audit_log
                    {where_clause}
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                """
                result = db.execute(text(list_sql), params)
                rows = result.fetchall()
                items = [_row_to_dict(r) for r in rows]

                total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0

                return {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                }
            finally:
                db.close()
        except Exception as e:
            logger.error("Failed to get filtered audit logs: %s", e)
            return {
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "total_pages": 0,
            }

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


def _row_to_dict(row) -> dict:
    """将数据库行转换为字典

    列顺序需与 SELECT 语句一致:
    id, user_id, username, question, generated_sql,
    execution_status, error_message, execution_time_ms,
    row_count, ip_address, conversation_id,
    security_blocked, created_at
    """
    return {
        "id": row[0],
        "user_id": row[1],
        "username": row[2],
        "question": row[3],
        "generated_sql": row[4],
        "execution_status": row[5],
        "error_message": row[6],
        "execution_time_ms": row[7],
        "row_count": row[8],
        "ip_address": row[9],
        "conversation_id": row[10],
        "security_blocked": bool(row[11]) if row[11] is not None else False,
        "created_at": row[12].isoformat() if row[12] else None,
    }
