"""
数据脱敏服务
-----------
对查询结果中的敏感字段（手机号、邮箱等）进行脱敏处理。
配置来源：security.yaml -> masking
"""

import logging
import re
from typing import Any

import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

_SECURITY_YAML = Path(__file__).resolve().parent.parent / "config" / "security.yaml"


class MaskingService:
    """
    数据脱敏服务

    支持两种模式：
    1. 按列名脱敏：查询结果中包含指定列名时自动脱敏
    2. 按内容脱敏：用正则匹配手机号、邮箱等模式
    """

    _instance: "MaskingService | None" = None
    _config: dict = None
    _column_rules: dict[str, list[str]] = {}  # table -> [columns]

    def __new__(cls) -> "MaskingService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self) -> None:
        """加载脱敏配置"""
        try:
            with open(_SECURITY_YAML, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}

            masking_cfg = self._config.get("masking", {})
            if masking_cfg.get("enabled", True):
                for entry in masking_cfg.get("fields", []):
                    table = entry.get("table", "")
                    columns = entry.get("columns", [])
                    self._column_rules[table] = columns
                logger.info("Masking service enabled: %s", self._column_rules)
            else:
                logger.info("Masking service disabled")
        except Exception as e:
            logger.error("Failed to load masking config: %s", e)

    @property
    def enabled(self) -> bool:
        return self._config.get("masking", {}).get("enabled", True)

    # ============================================================
    # 脱敏方法
    # ============================================================

    @staticmethod
    def mask_phone(value: str) -> str:
        """手机号脱敏：138****5678"""
        if not value or len(value) < 7:
            return value
        return value[:3] + "****" + value[-4:]

    @staticmethod
    def mask_email(value: str) -> str:
        """邮箱脱敏：z***@example.com"""
        if not value or "@" not in value:
            return value
        local, domain = value.split("@", 1)
        if len(local) <= 1:
            return f"*@{domain}"
        return f"{local[0]}***@{domain}"

    @staticmethod
    def mask_iccid(value: str) -> str:
        """ICCID 脱敏：8986************6789"""
        if not value or len(value) < 8:
            return value
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    @staticmethod
    def mask_imsi(value: str) -> str:
        """IMSI 脱敏：460***********"""
        if not value or len(value) < 6:
            return value
        return value[:3] + "*" * (len(value) - 3)

    @staticmethod
    def mask_generic(value: str) -> str:
        """通用脱敏：保留首尾字符"""
        if not value or len(value) <= 2:
            return "*" * len(value) if value else value
        return value[0] + "*" * (len(value) - 2) + value[-1]

    # ============================================================
    # 内容自动检测脱敏
    # ============================================================

    _PHONE_RE = re.compile(r'1[3-9]\d{9}')
    _EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

    def mask_content(self, text: str) -> str:
        """对文本内容中的敏感信息进行正则脱敏"""
        if not text or not self.enabled:
            return text

        # 手机号
        text = self._PHONE_RE.sub(
            lambda m: self.mask_phone(m.group()), text
        )
        # 邮箱
        text = self._EMAIL_RE.sub(
            lambda m: self.mask_email(m.group()), text
        )
        return text

    # ============================================================
    # 查询结果脱敏
    # ============================================================

    def mask_query_result(
        self,
        data: list[dict[str, Any]],
        columns: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """对查询结果进行脱敏处理

        Args:
            data: 查询结果行列表
            columns: 列名列表

        Returns:
            tuple: (脱敏后的数据, 脱敏的列名列表)
        """
        if not self.enabled or not data:
            return data, []

        masked_columns = set()
        sensitive_col_names = {"phone_number", "email", "iccid", "imsi",
                              "activation_code", "phone", "mobile"}

        for row in data:
            for col in list(row.keys()):
                col_lower = col.lower()

                # 按列名判断
                if col_lower in sensitive_col_names:
                    value = str(row[col]) if row[col] is not None else row[col]
                    if col_lower in ("phone_number", "phone", "mobile"):
                        row[col] = self.mask_phone(value) if value else value
                    elif col_lower == "email":
                        row[col] = self.mask_email(value) if value else value
                    elif col_lower == "iccid":
                        row[col] = self.mask_iccid(value) if value else value
                    elif col_lower == "imsi":
                        row[col] = self.mask_imsi(value) if value else value
                    elif col_lower == "activation_code":
                        row[col] = self.mask_generic(value) if value else value
                    masked_columns.add(col)

                # 按内容检测（对字符串列）
                elif isinstance(row[col], str) and len(row[col]) > 5:
                    original = row[col]
                    masked = self.mask_content(original)
                    if masked != original:
                        row[col] = masked
                        masked_columns.add(col)

        return data, list(masked_columns)

    def mask_sql_text(self, sql: str) -> str:
        """对 SQL 语句中的敏感信息脱敏（用于日志输出）"""
        if not self.enabled or not sql:
            return sql
        return self.mask_content(sql)


# 全局单例
masking_service = MaskingService()
