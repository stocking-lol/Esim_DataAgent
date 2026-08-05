"""
RAG 训练管理服务
----------------
封装 ChromaDB 训练数据的增删查操作，供 API 层调用。

支持三种训练数据类型：
  - DDL:          数据库表结构（CREATE TABLE 语句）
  - Documentation: 业务知识文档（术语说明、指标定义等）
  - SQL Examples:  SQL 查询示例（自然语言问题 → SQL 映射）
"""

import logging
from typing import Optional

from app.core.chroma_store import chroma_store, TrainingRecord
from app.core.vanna_instance import vanna_manager

logger = logging.getLogger(__name__)


# ============================================================
# 输入验证
# ============================================================

def _validate_chroma_available() -> None:
    """验证 ChromaDB 训练存储是否可用"""
    if not vanna_manager.chroma_available:
        raise RuntimeError(
            "ChromaDB 训练存储不可用。可能原因：\n"
            "1. embedding 模型未下载（all-MiniLM-L6-v2）\n"
            "2. ChromaDB 初始化失败\n"
            "请前往 HuggingFace 手动下载模型，或检查日志获取详细错误信息。"
        )


# ============================================================
# 训练操作
# ============================================================

def train_ddl(ddl: str, table_name: str = "") -> dict:
    """训练一条 DDL 语句

    Args:
        ddl: 完整的 CREATE TABLE 语句
        table_name: 表名（可选，用于元数据标记）

    Returns:
        dict: {"id": "...", "type": "ddl", "content": "..."}
    """
    _validate_chroma_available()

    record_id = chroma_store.add_ddl(ddl, table_name=table_name)
    logger.info("DDL trained: id=%s, table=%s", record_id, table_name or "unknown")

    return {
        "id": record_id,
        "type": "ddl",
        "content": ddl[:300] + ("..." if len(ddl) > 300 else ""),
        "table_name": table_name,
    }


def train_documentation(documentation: str, topic: str = "") -> dict:
    """训练一条业务文档

    Args:
        documentation: 业务知识文档/术语说明
        topic: 主题标签

    Returns:
        dict: {"id": "...", "type": "documentation", "content": "..."}
    """
    _validate_chroma_available()

    record_id = chroma_store.add_documentation(documentation, topic=topic)
    logger.info("Documentation trained: id=%s, topic=%s", record_id, topic or "general")

    return {
        "id": record_id,
        "type": "documentation",
        "content": documentation[:300] + ("..." if len(documentation) > 300 else ""),
        "topic": topic,
    }


def train_sql_example(question: str, sql: str) -> dict:
    """训练一条 SQL 示例

    Args:
        question: 自然语言问题
        sql: 对应的 SQL 语句

    Returns:
        dict: {"id": "...", "type": "sql", "question": "...", "sql": "..."}
    """
    _validate_chroma_available()

    record_id = chroma_store.add_sql_example(question=question, sql=sql)
    logger.info("SQL example trained: id=%s, question='%s'",
                record_id, question[:50])

    return {
        "id": record_id,
        "type": "sql",
        "question": question,
        "sql": sql,
    }


def get_all_training_data(
    record_type: Optional[str] = None,
) -> list[dict]:
    """获取所有训练数据

    Args:
        record_type: 可选过滤 "ddl" | "documentation" | "sql"

    Returns:
        list[dict]: 训练数据列表
    """
    _validate_chroma_available()

    records = chroma_store.get_all(record_type=record_type)
    return [r.to_dict() for r in records]


def remove_training_data(record_type: str, record_id: str) -> bool:
    """删除指定训练数据

    Args:
        record_type: "ddl" | "documentation" | "sql"
        record_id: 记录 ID

    Returns:
        bool: 删除成功返回 True
    """
    _validate_chroma_available()

    success = chroma_store.remove(record_type, record_id)
    if success:
        logger.info("Training data removed: type=%s, id=%s", record_type, record_id)
    else:
        logger.warning("Failed to remove training data: type=%s, id=%s",
                       record_type, record_id)
    return success


def get_training_stats() -> dict:
    """获取训练数据统计信息

    Returns:
        dict: {"ddl": N, "documentation": N, "sql_examples": N, "total": N, "chroma_available": bool}
    """
    if not vanna_manager.chroma_available:
        return {
            "chroma_available": False,
            "ddl": 0,
            "documentation": 0,
            "sql_examples": 0,
            "total": 0,
        }

    counts = chroma_store.count_by_type()
    return {
        "chroma_available": True,
        "ddl": counts.get("ddl", 0),
        "documentation": counts.get("documentation", 0),
        "sql_examples": counts.get("sql_examples", 0),
        "total": sum(counts.values()),
    }


def clear_all_training_data() -> dict:
    """清空所有训练数据（危险操作）

    Returns:
        dict: {"cleared": True, "counts_before": {...}}
    """
    _validate_chroma_available()

    counts_before = chroma_store.count_by_type()
    chroma_store.clear_all()
    logger.warning("All training data cleared: %s", counts_before)

    return {
        "cleared": True,
        "counts_before": {k: v for k, v in counts_before.items()},
    }
