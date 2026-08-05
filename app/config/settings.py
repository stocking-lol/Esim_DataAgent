"""
应用配置管理模块
--------------
使用 pydantic-settings 从环境变量加载所有配置项。
"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    APP_NAME: str = "eSIM NL2SQL Platform"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # --- Server ---
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # --- Database (MySQL) ---
    DATABASE_HOST: str = "localhost"
    DATABASE_PORT: int = 3306
    DATABASE_USER: str = "root"
    DATABASE_PASSWORD: str = "esim_platform_pass"
    DATABASE_NAME: str = "esim_platform"

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"mysql+pymysql://{self.DATABASE_USER}:{self.DATABASE_PASSWORD}"
            f"@{self.DATABASE_HOST}:{self.DATABASE_PORT}/{self.DATABASE_NAME}"
            f"?charset=utf8mb4"
        )

    # --- ChromaDB (Vector Store for RAG) ---
    CHROMADB_PERSIST_DIR: str = str(
        Path(__file__).resolve().parent.parent.parent / "chromadb_data"
    )
    CHROMADB_COLLECTION_NAME: str = "esim_nl2sql_rag"
    CHROMADB_PORT: int = 8001

    # --- LLM Configuration (DeepSeek-V3 via OpenAI-compatible API) ---
    LLM_PROVIDER: str = "deepseek"
    LLM_MODEL: str = "deepseek-chat"
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MAX_TOKENS: int = 4096
    LLM_TEMPERATURE: float = 0.0
    LLM_TIMEOUT_SECONDS: int = 60

    # --- JWT Auth ---
    JWT_SECRET_KEY: str = "change-me-to-a-random-secret-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # --- Query Safety ---
    QUERY_TIMEOUT_SECONDS: int = 30
    MAX_QUERY_ROWS: int = 1000
    QUERY_HISTORY_DAYS: int = 90

    # --- Rate Limiting ---
    RATE_LIMIT_PER_MINUTE: int = 60
    RATE_LIMIT_BURST: int = 10

    # --- Audit ---
    AUDIT_LOG_ENABLED: bool = True

    # --- Vanna ---
    VANNA_QUERY_TEMPLATE: str = "default"

    # --- CORS ---
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    # --- Training Data ---
    AUTO_INIT_TRAINING: bool = True

    # --- Project Root ---
    @property
    def PROJECT_ROOT(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent


@lru_cache()
def get_settings() -> Settings:
    """获取配置单例（缓存）"""
    return Settings()


settings = get_settings()
