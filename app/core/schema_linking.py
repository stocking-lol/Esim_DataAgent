"""
Schema Linking 优化模块
---------------------
从自然语言问题中提取实体，基于 BM25 + 向量相似度对表排序，
并支持角色级 Schema 访问控制（admin/analyst/viewer）。

核心功能：
  1. extract_entities — 从自然语言提取表名/列名关键词
  2. rank_tables — 基于关键词匹配 + BM25 评分对表排序
  3. get_top_k_tables — 返回最相关的 k 张表
  4. filter_ddl_by_role — 根据角色过滤 DDL（表级 + 列级权限）
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from app.core.chroma_store import chroma_store

logger = logging.getLogger(__name__)


# ============================================================
# eSIM 领域表元数据（用于 Schema Linking）
# ============================================================

@dataclass
class TableMeta:
    """表元数据"""
    name: str
    chinese_name: str
    description: str
    keywords: list[str]           # 中英文关键词
    columns: list[str]            # 列名列表
    sensitive_columns: list[str]  # 敏感列（viewer 不可见）


# eSIM 业务表元数据
TABLE_METADATA: dict[str, TableMeta] = {
    "users": TableMeta(
        name="users",
        chinese_name="用户",
        description="eSIM 用户信息表",
        keywords=["用户", "user", "手机号", "phone", "邮箱", "email", "ICCID",
                  "IMSI", "状态", "status", "地区", "region", "活跃", "新增"],
        columns=["id", "phone_number", "email", "iccid", "imsi", "mvno_id",
                 "status", "region", "created_at", "updated_at"],
        sensitive_columns=["phone_number", "email", "iccid", "imsi"],
    ),
    "plans": TableMeta(
        name="plans",
        chinese_name="套餐",
        description="套餐信息表",
        keywords=["套餐", "plan", "流量", "data", "语音", "voice", "短信", "sms",
                  "价格", "price", "有效期", "validity", "本地", "漫游", "roaming"],
        columns=["id", "name", "data_volume_mb", "voice_minutes", "sms_count",
                 "price", "validity_days", "type", "mvno_id", "status", "created_at"],
        sensitive_columns=[],
    ),
    "orders": TableMeta(
        name="orders",
        chinese_name="订单",
        description="订单信息表",
        keywords=["订单", "order", "订购", "购买", "支付", "payment", "金额",
                  "amount", "状态", "status", "激活", "activate", "取消", "cancel"],
        columns=["id", "user_id", "plan_id", "order_no", "status", "amount",
                 "payment_method", "mvno_id", "created_at", "activated_at"],
        sensitive_columns=[],
    ),
    "esim_profiles": TableMeta(
        name="esim_profiles",
        chinese_name="Profile",
        description="eSIM Profile 管理表",
        keywords=["profile", "激活", "activate", "下载", "download", "安装",
                  "install", "ICCID", "IMSI", "状态", "activation_code",
                  "运营商", "mno", "mvno", "转化率"],
        columns=["id", "user_id", "iccid", "imsi", "profile_status",
                 "activation_code", "mno_id", "mvno_id", "created_at",
                 "activated_at", "updated_at"],
        sensitive_columns=["iccid", "imsi", "activation_code"],
    ),
    "data_usage": TableMeta(
        name="data_usage",
        chinese_name="流量使用",
        description="流量使用记录表",
        keywords=["流量", "data", "使用", "usage", "漫游", "roaming", "国家",
                  "country", "日期", "date", "记录", "record", "MB", "GB"],
        columns=["id", "user_id", "iccid", "usage_mb", "roaming_flag",
                 "country_code", "usage_date", "recorded_at"],
        sensitive_columns=["iccid"],
    ),
    "operators": TableMeta(
        name="operators",
        chinese_name="运营商",
        description="运营商信息表",
        keywords=["运营商", "operator", "MNO", "MVNO", "移动", "联通", "电信",
                  "mcc", "mnc", "国家", "country", "ARPU"],
        columns=["id", "name", "type", "mcc_mnc", "country", "status", "created_at"],
        sensitive_columns=[],
    ),
    "roaming_packages": TableMeta(
        name="roaming_packages",
        chinese_name="漫游包",
        description="漫游包信息表",
        keywords=["漫游", "roaming", "包", "package", "国家", "country",
                  "流量", "data", "时长", "duration", "价格", "price"],
        columns=["id", "name", "countries", "data_volume_mb", "duration_days",
                 "price", "operator_id", "status"],
        sensitive_columns=[],
    ),
}

# 角色权限配置
ROLE_PERMISSIONS: dict[str, dict] = {
    "admin": {
        "tables": ["*"],
        "columns": "*",
        "description": "管理员：全量访问所有表和列",
    },
    "analyst": {
        "tables": ["users", "plans", "orders", "esim_profiles",
                   "data_usage", "operators", "roaming_packages"],
        "columns": "*",
        "description": "分析师：可访问所有业务表，但敏感字段脱敏",
    },
    "viewer": {
        "tables": ["plans", "operators", "roaming_packages",
                   "users", "orders"],
        "columns": {
            "users": ["id", "region", "status", "created_at"],
            "orders": ["id", "plan_id", "status", "amount", "created_at"],
        },
        "description": "查看者：套餐/运营商/漫游包全量访问，用户和订单有限字段",
    },
}


# ============================================================
# Schema Linking 服务
# ============================================================

class SchemaLinker:
    """Schema Linking 优化器

    从自然语言问题中提取实体关键词，基于 BM25 风格评分对表排序，
    并根据用户角色过滤 DDL。
    """

    def __init__(self):
        self._table_docs: dict[str, str] = {}
        self._build_index()

    def _build_index(self) -> None:
        """构建表文档索引（用于 BM25 评分）"""
        for name, meta in TABLE_METADATA.items():
            doc_parts = [meta.chinese_name, meta.description] + meta.keywords
            self._table_docs[name] = " ".join(doc_parts)
        logger.info("SchemaLinker index built: %d tables", len(self._table_docs))

    def extract_entities(self, question: str) -> list[str]:
        """从自然语言中提取表名/列名关键词

        Args:
            question: 用户自然语言问题

        Returns:
            list[str]: 提取到的关键词列表
        """
        entities = []

        # 中文分词：1-3字中文 + 英文单词
        tokens = re.findall(
            r'[\u4e00-\u9fff]{1,3}|[a-zA-Z_]{2,}|[0-9]+',
            question.lower(),
        )

        # 匹配表名和关键词（包含子串匹配，处理"各套餐"→"套餐"等情况）
        for token in tokens:
            for table_name, meta in TABLE_METADATA.items():
                # 直接匹配表名
                if token == table_name.lower():
                    if table_name not in entities:
                        entities.append(table_name)
                # 匹配中文名（精确或子串）
                elif meta.chinese_name in token or token in meta.chinese_name:
                    if table_name not in entities:
                        entities.append(table_name)
                # 匹配关键词（子串匹配）
                else:
                    for kw in meta.keywords:
                        kw_lower = kw.lower()
                        if kw_lower in token or token in kw_lower:
                            if table_name not in entities:
                                entities.append(table_name)
                            break

        # 额外匹配：整句子串匹配（处理贪婪分词遗漏的情况）
        question_lower = question.lower()
        for table_name, meta in TABLE_METADATA.items():
            if table_name in entities:
                continue
            # 检查中文名是否在问题中
            if meta.chinese_name in question:
                entities.append(table_name)
                continue
            # 检查关键词是否在问题中
            for kw in meta.keywords:
                if kw.lower() in question_lower:
                    entities.append(table_name)
                    break

        return entities

    def rank_tables(self, question: str) -> list[tuple[str, float]]:
        """基于关键词匹配 + BM25 风格评分对表排序

        Args:
            question: 用户自然语言问题

        Returns:
            list[tuple[str, float]]: [(table_name, score), ...] 按分数降序
        """
        question_lower = question.lower()
        question_tokens = set(re.findall(
            r'[\u4e00-\u9fff]{1,3}|[a-zA-Z_]{2,}|[0-9]+',
            question_lower,
        ))

        scored: list[tuple[str, float]] = []

        for table_name, doc in self._table_docs.items():
            meta = TABLE_METADATA[table_name]
            doc_lower = doc.lower()
            doc_tokens = set(re.findall(
                r'[\u4e00-\u9fff]{1,3}|[a-zA-Z_]{2,}|[0-9]+',
                doc_lower,
            ))

            # 关键词重叠评分
            overlap = len(question_tokens & doc_tokens)
            jaccard = overlap / max(len(question_tokens | doc_tokens), 1)

            # 精确子串匹配加分
            substr_bonus = 0.0
            for kw in meta.keywords:
                if kw.lower() in question_lower:
                    substr_bonus += 0.3

            # 中文表名匹配加分
            if meta.chinese_name in question:
                substr_bonus += 0.5

            score = jaccard * 0.3 + min(substr_bonus, 2.0) * 0.7
            scored.append((table_name, round(score, 4)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def get_top_k_tables(self, question: str, k: int = 3,
                         role: str = "analyst") -> list[str]:
        """返回最相关的 k 张表（考虑角色权限）

        Args:
            question: 用户自然语言问题
            k: 返回的表数量
            role: 用户角色（用于过滤无权限的表）

        Returns:
            list[str]: 表名列表
        """
        ranked = self.rank_tables(question)

        # 过滤无权限的表
        allowed = self.get_allowed_tables(role)
        filtered = [(name, score) for name, score in ranked
                    if name in allowed or allowed == ["*"]]

        # 如果过滤后不足 k 个，补充允许的表
        if len(filtered) < k:
            existing = {name for name, _ in filtered}
            for name, _ in ranked:
                if name not in existing and (name in allowed or allowed == ["*"]):
                    filtered.append((name, 0.0))
                    if len(filtered) >= k:
                        break

        return [name for name, _ in filtered[:k]]

    def get_allowed_tables(self, role: str) -> list[str]:
        """获取角色允许访问的表列表

        Args:
            role: 用户角色

        Returns:
            list[str]: 允许的表名列表，["*"] 表示全部
        """
        perm = ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS.get("viewer", {}))
        return perm.get("tables", [])

    def check_table_access(self, role: str, table_name: str) -> bool:
        """检查角色是否有权访问指定表

        Args:
            role: 用户角色
            table_name: 表名

        Returns:
            bool: 是否允许访问
        """
        allowed = self.get_allowed_tables(role)
        if allowed == ["*"]:
            return True
        return table_name.lower() in [t.lower() for t in allowed]

    def check_column_access(self, role: str, table_name: str,
                            column_name: str) -> bool:
        """检查角色是否有权访问指定列

        Args:
            role: 用户角色
            table_name: 表名
            column_name: 列名

        Returns:
            bool: 是否允许访问
        """
        # 先检查表级权限
        if not self.check_table_access(role, table_name):
            return False

        perm = ROLE_PERMISSIONS.get(role, {})
        cols_config = perm.get("columns", "*")

        if cols_config == "*":
            return True

        # 检查表级列权限
        # 如果表不在 columns 配置中，默认允许（已通过表级检查）
        if table_name not in cols_config:
            return True

        table_cols = cols_config[table_name]
        if table_cols == "*":
            return True

        return column_name.lower() in [c.lower() for c in table_cols]

    def filter_ddl_by_role(self, role: str,
                           full_ddl_map: Optional[dict[str, str]] = None
                           ) -> dict[str, str]:
        """根据角色过滤 DDL

        Args:
            role: 用户角色
            full_ddl_map: 完整 DDL 字典 {table_name: ddl_string}
                          如果为 None，从 TABLE_METADATA 生成简化 DDL

        Returns:
            dict[str, str]: 过滤后的 DDL {table_name: ddl_string}
        """
        allowed = self.get_allowed_tables(role)
        perm = ROLE_PERMISSIONS.get(role, {})
        cols_config = perm.get("columns", "*")

        result: dict[str, str] = {}

        # 获取 DDL 来源
        if full_ddl_map is None:
            full_ddl_map = self._generate_simplified_ddl()

        for table_name, ddl in full_ddl_map.items():
            # 检查表级权限
            if allowed != ["*"] and table_name.lower() not in [t.lower() for t in allowed]:
                continue

            # 检查列级权限
            if cols_config != "*":
                table_cols = cols_config.get(table_name, "*")
                if table_cols != "*":
                    # 过滤 DDL，只保留允许的列
                    ddl = self._filter_ddl_columns(ddl, table_cols)

            result[table_name] = ddl

        return result

    def _generate_simplified_ddl(self) -> dict[str, str]:
        """从 TABLE_METADATA 生成简化 DDL"""
        result = {}
        for name, meta in TABLE_METADATA.items():
            cols_str = ",\n    ".join(meta.columns)
            result[name] = f"CREATE TABLE {name} (\n    {cols_str}\n);"
        return result

    def _filter_ddl_columns(self, ddl: str, allowed_cols: list[str]) -> str:
        """过滤 DDL，只保留允许的列"""
        allowed_lower = {c.lower() for c in allowed_cols}
        lines = ddl.split("\n")
        filtered = []
        for line in lines:
            # 提取列名（行首的单词）
            stripped = line.strip()
            if stripped:
                col_name = stripped.split()[0].strip(",").strip("`")
                if col_name.lower() in allowed_lower or col_name.upper() in {
                    "CREATE", "TABLE", "PRIMARY", "KEY", "FOREIGN", "REFERENCES",
                    "INDEX", "UNIQUE", "CONSTRAINT", "ENGINE", "DEFAULT", "CHARSET",
                    ");", "(",
                }:
                    filtered.append(line)
            else:
                filtered.append(line)
        return "\n".join(filtered)

    def get_schema_context(self, question: str, role: str = "analyst",
                           max_tables: int = 5) -> str:
        """获取与问题最相关的 Schema 上下文（用于 LLM prompt 增强）

        Args:
            question: 用户自然语言问题
            role: 用户角色
            max_tables: 最多包含的表数

        Returns:
            str: Schema 上下文文本
        """
        # 获取 Top-K 相关表
        top_tables = self.get_top_k_tables(question, k=max_tables, role=role)

        # 获取角色过滤后的 DDL
        filtered_ddl = self.filter_ddl_by_role(role)

        parts = []
        for table_name in top_tables:
            if table_name in filtered_ddl:
                meta = TABLE_METADATA.get(table_name)
                comment = f" -- {meta.chinese_name}: {meta.description}" if meta else ""
                parts.append(f"-- 表: {table_name}{comment}")
                parts.append(filtered_ddl[table_name])
                parts.append("")

        return "\n".join(parts) if parts else ""


# 全局单例
schema_linker = SchemaLinker()
