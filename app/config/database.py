"""
数据库连接管理模块
----------------
使用 SQLAlchemy 管理 MySQL 连接池，提供 FastAPI 依赖注入。
"""

import logging
from collections.abc import Generator
from typing import Optional

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.config.settings import settings

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    数据库连接管理器

    管理 SQLAlchemy 引擎和会话工厂，提供连接池配置。
    支持 FastAPI 依赖注入模式。
    """

    _engine: Optional[Engine] = None
    _session_factory: Optional[sessionmaker] = None

    def __init__(self):
        self._init_engine()

    def _init_engine(self) -> None:
        """初始化 SQLAlchemy 引擎和会话工厂"""
        database_url = settings.DATABASE_URL

        self._engine = create_engine(
            database_url,
            poolclass=QueuePool,
            pool_size=10,                # 常驻连接数
            max_overflow=20,             # 最大溢出连接数
            pool_recycle=3600,           # 连接回收时间（秒）
            pool_pre_ping=True,          # 连接前检查可用性
            pool_timeout=30,             # 获取连接超时（秒）
            echo=settings.DEBUG,          # DEBUG 模式下打印 SQL
            connect_args={
                "charset": "utf8mb4",
                "autocommit": False,
            },
        )

        self._session_factory = sessionmaker(
            bind=self._engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )

        # 注册连接事件监听
        self._register_events()

        logger.info(
            "Database engine initialized: %s@%s:%d/%s",
            settings.DATABASE_USER,
            settings.DATABASE_HOST,
            settings.DATABASE_PORT,
            settings.DATABASE_NAME,
        )

    def _register_events(self) -> None:
        """注册连接池事件监听器"""

        @event.listens_for(self._engine, "connect")
        def on_connect(dbapi_connection, connection_record):
            """连接建立时设置会话参数"""
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'")
                cursor.execute("SET SESSION time_zone = '+08:00'")
                cursor.execute("SET SESSION max_execution_time = %s", (settings.QUERY_TIMEOUT_SECONDS * 1000,))
            except Exception:
                # MySQL 5.7 不支持 max_execution_time
                pass
            finally:
                cursor.close()

        @event.listens_for(self._engine, "checkout")
        def on_checkout(dbapi_connection, connection_record, connection_proxy):
            """从连接池取出连接时记录日志"""
            if settings.DEBUG:
                pool = self._engine.pool
                logger.debug(
                    "Connection checked out — pool size=%d, checked_in=%d, overflow=%d",
                    pool.size(), pool.checkedin(), pool.overflow(),
                )

    @property
    def engine(self) -> Engine:
        """获取 SQLAlchemy 引擎"""
        if self._engine is None:
            self._init_engine()
        return self._engine

    def get_session(self) -> Session:
        """创建新的数据库会话"""
        if self._session_factory is None:
            self._init_engine()
        return self._session_factory()

    def check_connection(self) -> bool:
        """测试数据库连接是否正常"""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text("SELECT 1"))
                return result.scalar() == 1
        except Exception as e:
            logger.error("Database connection check failed: %s", e)
            return False

    def dispose(self) -> None:
        """释放数据库引擎和连接池"""
        if self._engine:
            self._engine.dispose()
            logger.info("Database engine disposed.")


# 全局数据库管理器实例
db_manager = DatabaseManager()


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI 依赖注入：获取数据库会话

    用法:
        @app.get("/users")
        def get_users(db: Session = Depends(get_db)):
            ...

    会话在请求结束时自动关闭，异常时自动回滚。
    """
    session = db_manager.get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_raw_db() -> Session:
    """
    获取原始数据库会话（非依赖注入场景使用）

    返回:
        SQLAlchemy Session 实例
    """
    return db_manager.get_session()
