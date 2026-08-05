"""
查询审计日志 ORM 模型
--------------------
映射到 MySQL 中已存在的 query_audit_log 表（只读映射，不自动建表）。
"""

from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text

from app.models.conversation import Base


class QueryLog(Base):
    """查询审计日志表（只读映射）

    记录所有 NL2SQL 查询请求，包含用户信息、原始问题、生成的 SQL、
    执行状态、安全拦截情况等，用于合规审查和问题追溯。
    """

    __tablename__ = "query_audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, comment="用户ID")
    username = Column(String(100), nullable=True, comment="用户名")
    question = Column(Text, nullable=False, comment="用户原始自然语言问题")
    generated_sql = Column(Text, nullable=True, comment="LLM 生成的 SQL 语句")
    execution_status = Column(
        String(50), nullable=True, comment="执行状态: success/error/blocked"
    )
    error_message = Column(Text, nullable=True, comment="错误信息")
    execution_time_ms = Column(Integer, nullable=True, comment="执行耗时（毫秒）")
    row_count = Column(Integer, nullable=True, comment="返回行数")
    ip_address = Column(String(45), nullable=True, comment="请求来源IP")
    conversation_id = Column(String(36), nullable=True, comment="对话ID")
    security_blocked = Column(
        Integer, nullable=False, default=0, comment="是否被安全网关拦截: 0=否, 1=是"
    )
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, comment="记录创建时间"
    )

    __table_args__ = (
        Index("idx_audit_user_id", "user_id"),
        Index("idx_audit_created_at", "created_at"),
        Index("idx_audit_execution_status", "execution_status"),
    )

    def to_dict(self) -> dict:
        """转换为字典表示"""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.username,
            "question": self.question,
            "generated_sql": self.generated_sql,
            "execution_status": self.execution_status,
            "error_message": self.error_message,
            "execution_time_ms": self.execution_time_ms,
            "row_count": self.row_count,
            "ip_address": self.ip_address,
            "conversation_id": self.conversation_id,
            "security_blocked": bool(self.security_blocked) if self.security_blocked is not None else False,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
