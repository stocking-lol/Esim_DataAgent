"""
认证服务
--------
提供用户注册、认证、查询、角色管理等业务逻辑。
使用 SQLAlchemy session 操作 app_users 表。
"""

import logging
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from app.config.database import db_manager
from app.models.user import User
from app.utils.crypto import hash_password, verify_password

logger = logging.getLogger(__name__)

# 允许的角色
VALID_ROLES = {"admin", "analyst", "viewer"}


class AuthService:
    """认证服务单例"""

    def _get_session(self):
        """获取数据库会话"""
        return db_manager.get_session()

    # ========================================================
    # 用户注册
    # ========================================================

    def register_user(
        self,
        username: str,
        email: str,
        password: str,
        role: str = "analyst",
        mvno_id: Optional[int] = None,
    ) -> User:
        """注册新用户

        Args:
            username: 用户名
            email: 邮箱
            password: 明文密码
            role: 角色（admin/analyst/viewer），默认 analyst
            mvno_id: 关联 MVNO ID（可选，用于 RLS）

        Returns:
            User: 创建的用户对象

        Raises:
            ValueError: 用户名/邮箱已存在，或角色无效
        """
        if role not in VALID_ROLES:
            raise ValueError(f"无效的角色: {role}，允许的角色: {VALID_ROLES}")

        session = self._get_session()
        try:
            # 检查用户名和邮箱是否已存在
            existing = (
                session.query(User)
                .filter(
                    or_(User.username == username, User.email == email)
                )
                .first()
            )
            if existing:
                if existing.username == username:
                    raise ValueError(f"用户名已存在: {username}")
                raise ValueError(f"邮箱已被注册: {email}")

            user = User(
                username=username,
                email=email,
                hashed_password=hash_password(password),
                role=role,
                mvno_id=mvno_id,
                is_active=True,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            logger.info("用户注册成功: username=%s, role=%s", username, role)
            return user
        except ValueError:
            session.rollback()
            raise
        except IntegrityError:
            session.rollback()
            raise ValueError(f"用户名或邮箱已存在: {username} / {email}")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ========================================================
    # 用户认证
    # ========================================================

    def authenticate_user(self, username: str, password: str) -> Optional[User]:
        """验证用户名密码

        Args:
            username: 用户名
            password: 明文密码

        Returns:
            User: 认证成功的用户对象，失败返回 None
        """
        session = self._get_session()
        try:
            user = (
                session.query(User)
                .filter(User.username == username, User.is_active == True)  # noqa: E712
                .first()
            )
            if not user:
                return None
            if not verify_password(password, user.hashed_password):
                return None
            return user
        except Exception:
            logger.exception("认证查询异常: username=%s", username)
            return None
        finally:
            session.close()

    # ========================================================
    # 用户查询
    # ========================================================

    def get_user_by_id(self, user_id: int) -> Optional[User]:
        """根据 ID 获取用户

        Args:
            user_id: 用户 ID

        Returns:
            User 或 None
        """
        session = self._get_session()
        try:
            return session.query(User).filter(User.id == user_id).first()
        except Exception:
            logger.exception("查询用户异常: user_id=%s", user_id)
            return None
        finally:
            session.close()

    def get_user_by_username(self, username: str) -> Optional[User]:
        """根据用户名获取用户

        Args:
            username: 用户名

        Returns:
            User 或 None
        """
        session = self._get_session()
        try:
            return session.query(User).filter(User.username == username).first()
        except Exception:
            logger.exception("查询用户异常: username=%s", username)
            return None
        finally:
            session.close()

    def list_users(
        self,
        role_filter: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """分页查询用户列表

        Args:
            role_filter: 按角色过滤（可选）
            page: 页码（从 1 开始）
            page_size: 每页条数

        Returns:
            dict: {"users": [...], "total": int, "page": int, "page_size": int}
        """
        session = self._get_session()
        try:
            query = session.query(User)
            if role_filter:
                query = query.filter(User.role == role_filter)

            total = query.count()
            offset = (page - 1) * page_size
            users = (
                query.order_by(User.id.asc())
                .offset(offset)
                .limit(page_size)
                .all()
            )

            return {
                "users": [u.to_dict() for u in users],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        except Exception:
            logger.exception("查询用户列表异常")
            raise
        finally:
            session.close()

    # ========================================================
    # 用户管理
    # ========================================================

    def update_user_role(self, user_id: int, new_role: str) -> User:
        """更新用户角色

        Args:
            user_id: 用户 ID
            new_role: 新角色（admin/analyst/viewer）

        Returns:
            User: 更新后的用户对象

        Raises:
            ValueError: 角色无效或用户不存在
        """
        if new_role not in VALID_ROLES:
            raise ValueError(f"无效的角色: {new_role}，允许的角色: {VALID_ROLES}")

        session = self._get_session()
        try:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                raise ValueError(f"用户不存在: id={user_id}")

            user.role = new_role
            session.commit()
            session.refresh(user)
            logger.info("用户角色更新: user_id=%s, new_role=%s", user_id, new_role)
            return user
        except ValueError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def deactivate_user(self, user_id: int) -> User:
        """停用用户（软删除）

        Args:
            user_id: 用户 ID

        Returns:
            User: 停用后的用户对象

        Raises:
            ValueError: 用户不存在
        """
        session = self._get_session()
        try:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                raise ValueError(f"用户不存在: id={user_id}")

            user.is_active = False
            session.commit()
            session.refresh(user)
            logger.info("用户已停用: user_id=%s, username=%s", user_id, user.username)
            return user
        except ValueError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def update_email(self, user_id: int, new_email: str) -> User:
        """更新用户邮箱（用户自助修改）

        Args:
            user_id: 用户 ID
            new_email: 新邮箱

        Returns:
            User: 更新后的用户对象

        Raises:
            ValueError: 邮箱已被占用或用户不存在
        """
        session = self._get_session()
        try:
            # 检查邮箱是否被其他用户占用
            existing = (
                session.query(User)
                .filter(User.email == new_email, User.id != user_id)
                .first()
            )
            if existing:
                raise ValueError(f"邮箱已被占用: {new_email}")

            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                raise ValueError(f"用户不存在: id={user_id}")

            user.email = new_email
            session.commit()
            session.refresh(user)
            logger.info("用户邮箱更新: user_id=%s, new_email=%s", user_id, new_email)
            return user
        except ValueError:
            session.rollback()
            raise
        except IntegrityError:
            session.rollback()
            raise ValueError(f"邮箱已被占用: {new_email}")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# 全局单例
auth_service = AuthService()
