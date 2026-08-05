"""
用户 ORM 模型
-------------
定义 app_users 表，用于平台用户认证与授权。
表名使用 app_users 以避免与业务 users 表冲突。
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String
from sqlalchemy.orm import declarative_base

from app.models.conversation import Base


class User(Base):
    """平台用户表

    存储用户认证信息和角色分配，支持基于 MVNO 的行级安全（RLS）。
    """

    __tablename__ = "app_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), nullable=False, unique=True, comment="用户名（唯一）")
    email = Column(String(255), nullable=False, unique=True, comment="邮箱（唯一）")
    hashed_password = Column(String(255), nullable=False, comment="bcrypt 哈希密码")
    role = Column(
        String(20),
        nullable=False,
        default="analyst",
        comment="角色: admin / analyst / viewer",
    )
    mvno_id = Column(Integer, nullable=True, comment="关联 MVNO ID（用于 RLS 行级安全）")
    is_active = Column(Boolean, nullable=False, default=True, comment="是否激活")
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        comment="创建时间",
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        comment="更新时间",
    )

    # 索引
    __table_args__ = (
        Index("idx_app_users_username", "username"),
        Index("idx_app_users_email", "email"),
        Index("idx_app_users_role", "role"),
        Index("idx_app_users_mvno_id", "mvno_id"),
        Index("idx_app_users_is_active", "is_active"),
    )

    def to_dict(self, include_sensitive: bool = False) -> dict:
        """转换为字典

        Args:
            include_sensitive: 是否包含敏感字段（hashed_password）

        Returns:
            dict: 用户信息字典
        """
        data = {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "mvno_id": self.mvno_id,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_sensitive:
            data["hashed_password"] = self.hashed_password
        return data

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username='{self.username}', role='{self.role}')>"
