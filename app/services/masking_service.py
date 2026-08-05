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

    Day 17 增强：
    3. 基于角色的脱敏控制（admin 豁免）
    4. 多种脱敏模式（mask_middle / mask_all_except_last4 / mask_all / mask_email）
    5. 配置驱动的脱敏规则（security.yaml -> data_masking）
    """

    _instance: "MaskingService | None" = None
    _config: dict = None
    _column_rules: dict[str, list[str]] = {}  # table -> [columns]
    _data_masking_rules: dict[str, dict] = {}  # column_name -> {pattern, roles_exempt}
    _role_policy: dict[str, str] = {}  # role -> "none" | "sensitive"

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

            # 基础脱敏配置
            masking_cfg = self._config.get("masking", {})
            if masking_cfg.get("enabled", True):
                for entry in masking_cfg.get("fields", []):
                    table = entry.get("table", "")
                    columns = entry.get("columns", [])
                    self._column_rules[table] = columns
                logger.info("Masking service enabled: %s", self._column_rules)
            else:
                logger.info("Masking service disabled")

            # Day 17: 列级脱敏规则
            data_masking_cfg = self._config.get("data_masking", {})
            if data_masking_cfg.get("enabled", True):
                for rule in data_masking_cfg.get("rules", []):
                    col = rule.get("column", "").lower()
                    pattern = rule.get("pattern", "mask_middle")
                    roles_exempt = rule.get("roles_exempt", [])
                    self._data_masking_rules[col] = {
                        "pattern": pattern,
                        "roles_exempt": roles_exempt,
                    }
                self._role_policy = data_masking_cfg.get("role_policy", {
                    "admin": "none",
                    "analyst": "sensitive",
                    "viewer": "sensitive",
                })
                logger.info("Data masking rules loaded: %d columns, policies: %s",
                           len(self._data_masking_rules), self._role_policy)
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
        role: str = "analyst",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """对查询结果进行脱敏处理（基于角色）

        Args:
            data: 查询结果行列表
            columns: 列名列表
            role: 用户角色 (admin/analyst/viewer)

        Returns:
            tuple: (脱敏后的数据, 脱敏的列名列表)
        """
        if not self.enabled or not data:
            return data, []

        # admin 角色不脱敏
        if self._is_role_exempt(role):
            return data, []

        masked_columns = set()

        for row in data:
            for col in list(row.keys()):
                col_lower = col.lower()
                value = row[col]

                # None 或非字符串值跳过内容检测
                if value is None:
                    continue

                # 按列名规则脱敏
                if col_lower in self._data_masking_rules:
                    rule = self._data_masking_rules[col_lower]
                    pattern = rule.get("pattern", "mask_middle")
                    str_value = str(value)
                    row[col] = self._apply_pattern(str_value, pattern)
                    masked_columns.add(col)

                # 旧版按列名匹配（兼容）
                elif col_lower in ("phone_number", "phone", "mobile"):
                    row[col] = self.mask_phone(str(value)) if value else value
                    masked_columns.add(col)
                elif col_lower == "email":
                    row[col] = self.mask_email(str(value)) if value else value
                    masked_columns.add(col)
                elif col_lower == "iccid":
                    row[col] = self.mask_iccid(str(value)) if value else value
                    masked_columns.add(col)
                elif col_lower == "imsi":
                    row[col] = self.mask_imsi(str(value)) if value else value
                    masked_columns.add(col)
                elif col_lower == "activation_code":
                    row[col] = self.mask_generic(str(value)) if value else value
                    masked_columns.add(col)

                # 按内容检测（对字符串列）
                elif isinstance(value, str) and len(value) > 5:
                    original = value
                    masked = self.mask_content(original)
                    if masked != original:
                        row[col] = masked
                        masked_columns.add(col)

        return data, list(masked_columns)

    def _is_role_exempt(self, role: str) -> bool:
        """检查角色是否豁免脱敏"""
        policy = self._role_policy.get(role, "sensitive")
        return policy == "none"

    def _apply_pattern(self, value: str, pattern: str) -> str:
        """根据脱敏模式应用脱敏

        Args:
            value: 原始值
            pattern: 脱敏模式 (mask_middle / mask_all_except_last4 / mask_all / mask_email)

        Returns:
            str: 脱敏后的值
        """
        if not value:
            return value

        if pattern == "mask_middle":
            # 138****5678
            if len(value) <= 7:
                return self.mask_generic(value)
            return value[:3] + "****" + value[-4:]

        elif pattern == "mask_all_except_last4":
            # ************1234
            if len(value) <= 4:
                return "*" * len(value)
            return "*" * (len(value) - 4) + value[-4:]

        elif pattern == "mask_all":
            # ****
            return "*" * min(len(value), 20)

        elif pattern == "mask_email":
            # z***@example.com
            return self.mask_email(value)

        else:
            return self.mask_generic(value)

    def mask_sql_text(self, sql: str) -> str:
        """对 SQL 语句中的敏感信息脱敏（用于日志输出）"""
        if not self.enabled or not sql:
            return sql
        return self.mask_content(sql)


# 全局单例
masking_service = MaskingService()
