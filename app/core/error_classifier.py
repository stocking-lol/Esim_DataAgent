"""
SQL 错误分类器
-------------

将数据库返回的错误信息归类为可识别的类别，用于驱动自我纠错回路决策：
- 哪些是「可重试」（表名/列名错误、语法错误），重试修正 SQL 有望成功
- 哪些是「不可重试」（权限拒绝、安全拦截、超时）

支持从错误消息中提取 MySQL 错误码 (errno) 进行精确匹配。
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ErrorCategory(str, Enum):
    """SQL 错误类别"""

    SYNTAX_ERROR = "syntax_error"          # 语法错误 (1064)
    TABLE_NOT_FOUND = "table_not_found"    # 表不存在 (1146)
    COLUMN_NOT_FOUND = "column_not_found"  # 列不存在 (1054)
    AMBIGUOUS_COLUMN = "ambiguous_column"  # 列歧义 (1052)
    TIMEOUT = "timeout"                    # 查询超时 (3024)
    PERMISSION_DENIED = "permission_denied"  # 权限不足 (1142/1143)
    DUPLICATE_ENTRY = "duplicate_entry"    # 唯一键冲突 (1062)
    CONNECTION = "connection_error"        # 连接错误
    UNKNOWN = "unknown"                    # 未知错误


# MySQL 错误码 -> 类别
_MYSQL_ERRNO_MAP: dict[int, ErrorCategory] = {
    1064: ErrorCategory.SYNTAX_ERROR,
    1146: ErrorCategory.TABLE_NOT_FOUND,
    1054: ErrorCategory.COLUMN_NOT_FOUND,
    1052: ErrorCategory.AMBIGUOUS_COLUMN,
    3024: ErrorCategory.TIMEOUT,
    3026: ErrorCategory.TIMEOUT,
    1142: ErrorCategory.PERMISSION_DENIED,
    1143: ErrorCategory.PERMISSION_DENIED,
    1062: ErrorCategory.DUPLICATE_ENTRY,
    2003: ErrorCategory.CONNECTION,
    2006: ErrorCategory.CONNECTION,
    2013: ErrorCategory.CONNECTION,
}

# 兜底：基于关键字的正则匹配（当无法提取 errno 时）
_KEYWORD_PATTERNS: list[tuple[re.Pattern, ErrorCategory]] = [
    (re.compile(r"you have an error in your sql syntax", re.IGNORECASE),
     ErrorCategory.SYNTAX_ERROR),
    (re.compile(r"table\s+['\"]?[\w.]*['\"]?\s*(doesn't exist|does not exist|not exists)",
                re.IGNORECASE),
     ErrorCategory.TABLE_NOT_FOUND),
    (re.compile(r"unknown table", re.IGNORECASE), ErrorCategory.TABLE_NOT_FOUND),
    (re.compile(r"unknown column", re.IGNORECASE), ErrorCategory.COLUMN_NOT_FOUND),
    (re.compile(r"column\s+['\"]?[\w.]*['\"]?\s*(doesn't exist|does not exist|not known)",
                re.IGNORECASE),
     ErrorCategory.COLUMN_NOT_FOUND),
    (re.compile(r"ambiguous column", re.IGNORECASE), ErrorCategory.AMBIGUOUS_COLUMN),
    (re.compile(r"column.*ambiguous", re.IGNORECASE), ErrorCategory.AMBIGUOUS_COLUMN),
    (re.compile(r"max_execution_time", re.IGNORECASE), ErrorCategory.TIMEOUT),
    (re.compile(r"query execution was interrupted", re.IGNORECASE), ErrorCategory.TIMEOUT),
    (re.compile(r"lock wait timeout", re.IGNORECASE), ErrorCategory.TIMEOUT),
    (re.compile(r"access denied|permission|not allowed", re.IGNORECASE),
     ErrorCategory.PERMISSION_DENIED),
    (re.compile(r"duplicate entry", re.IGNORECASE), ErrorCategory.DUPLICATE_ENTRY),
    (re.compile(r"lost connection|can't connect|connection.*refused", re.IGNORECASE),
     ErrorCategory.CONNECTION),
]

# 可被自我纠错回路重试的类别
RETRYABLE_CATEGORIES: set[ErrorCategory] = {
    ErrorCategory.SYNTAX_ERROR,
    ErrorCategory.TABLE_NOT_FOUND,
    ErrorCategory.COLUMN_NOT_FOUND,
    ErrorCategory.AMBIGUOUS_COLUMN,
}


@dataclass
class ErrorClassification:
    """错误分类结果"""

    category: ErrorCategory
    mysql_errno: Optional[int] = None
    message: str = ""
    retryable: bool = False

    def __post_init__(self) -> None:
        self.retryable = self.category in RETRYABLE_CATEGORIES


def _extract_mysql_errno(error_message: str) -> Optional[int]:
    """从错误消息中提取 MySQL 错误码 (errno)

    常见格式：
        (pymysql.err.ProgrammingError) (1064, "You have an error...")
        ERROR 1146 (42S02): Table 'x' doesn't exist
        (1146, "...")
    """
    if not error_message:
        return None
    # 匹配 (errno, "...") 或 ERROR errno
    patterns = [
        re.compile(r"\((\d{4}),\s*"),          # (1064,
        re.compile(r"ERROR\s+(\d{4})", re.IGNORECASE),  # ERROR 1146
        re.compile(r"\[Errno\s*(\d{4})\]"),    # [Errno 1064]
    ]
    for pat in patterns:
        m = pat.search(error_message)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def classify_sql_error(error_message: str) -> ErrorClassification:
    """对 SQL 错误消息进行分类

    Args:
        error_message: 数据库或驱动返回的错误信息

    Returns:
        ErrorClassification: 分类结果（含是否可重试）
    """
    if not error_message or not error_message.strip():
        return ErrorClassification(
            category=ErrorCategory.UNKNOWN,
            message="(empty error)",
            retryable=False,
        )

    # 1. 优先基于 MySQL errno 精确匹配
    errno = _extract_mysql_errno(error_message)
    if errno and errno in _MYSQL_ERRNO_MAP:
        category = _MYSQL_ERRNO_MAP[errno]
        return ErrorClassification(
            category=category,
            mysql_errno=errno,
            message=error_message,
        )

    # 2. 否则基于关键字正则兜底匹配
    for pattern, category in _KEYWORD_PATTERNS:
        if pattern.search(error_message):
            return ErrorClassification(
                category=category,
                mysql_errno=errno,
                message=error_message,
            )

    # 3. 无法识别
    return ErrorClassification(
        category=ErrorCategory.UNKNOWN,
        mysql_errno=errno,
        message=error_message,
    )


def is_retryable(error_message: str) -> bool:
    """判断错误是否可被子自我纠错回路重试

    Args:
        error_message: 错误消息

    Returns:
        bool: True 表示可重试
    """
    return classify_sql_error(error_message).retryable


# 针对各类别给 LLM 的修正提示
_CORRECTION_HINTS: dict[ErrorCategory, str] = {
    ErrorCategory.SYNTAX_ERROR: (
        "重点检查 SQL 语法：括号是否匹配、关键字拼写是否正确、"
        "字符串是否用单引号包裹、聚合函数使用是否正确。"
    ),
    ErrorCategory.TABLE_NOT_FOUND: (
        "表名不存在。请只使用数据库 DDL 中真实存在的表名，"
        "注意大小写和下划线，不要臆造表名。"
    ),
    ErrorCategory.COLUMN_NOT_FOUND: (
        "列名不存在。请只使用对应表中真实存在的列名，"
        "注意 JOIN 后列需要带表别名前缀（如 u.id）。"
    ),
    ErrorCategory.AMBIGUOUS_COLUMN: (
        "列名存在歧义（多表中都有该列）。请为 SELECT 中的列显式添加表别名前缀。"
    ),
}


def get_correction_hint(error_message: str) -> str:
    """根据错误类别返回给 LLM 的修正提示语

    Args:
        error_message: 错误消息

    Returns:
        str: 修正提示
    """
    classification = classify_sql_error(error_message)
    return _CORRECTION_HINTS.get(classification.category, "")
