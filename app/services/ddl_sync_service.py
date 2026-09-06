"""
DDL 同步服务
------------
把 MySQL 的真实表结构同步到 ChromaDB 的 ddl collection，消除训练知识库
与实际数据库之间的漂移。

为什么需要
----------
训练数据目前只在两个时机写入：``scripts/init_training.py``（初始化，幂等）
和管理 API（手工）。**没有任何自动机制**。已实际发生的漂移：

- ``add_performance_indexes.sql`` 新增的 6 个索引没进 ChromaDB
- 未来新增/改删列时，LLM 会编造不存在的列或漏掉新列

设计要点
--------
**以安全网关的表白名单为唯一真相源**
    ``app/config/security.yaml`` 的 ``schema_limiter.allowed_tables`` 定义了
    "用户能查哪些表"。这里直接复用它，保证"能查的表"与"有 DDL 训练的表"
    永远一致 —— 不会出现某张表允许查询却没有 DDL 导致 LLM 瞎猜的情况。
    平台自身表（app_users / conversations / query_audit_log）和只读视图
    （v_*）不在白名单内，天然被排除。

**DDL 比对必须规范化**
    ``SHOW CREATE TABLE`` 的输出包含 ``AUTO_INCREMENT=123`` 这类随数据变化的值，
    直接字符串比对会永远报漂移。比对前需要剔除这些易变片段。

**重建而非增量更新**
    DDL 是整体替换语义（先删旧记录再插新）。ChromaDB 里存的是 embedding，
    没法"改一个列"，只能整条重建。
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text

from app.config.database import db_manager

logger = logging.getLogger(__name__)

# status 取值
MISSING = "missing"   # MySQL 有、ChromaDB 没有（新增表）
ORPHAN = "orphan"     # ChromaDB 有、MySQL 没有（表已删除）
DRIFTED = "drifted"   # 两边都有但内容不同（列/索引变更）
IN_SYNC = "in_sync"


@dataclass
class DDLDrift:
    """单张表的漂移情况"""

    table: str
    status: str
    detail: str = ""
    mysql_ddl: str = ""
    chroma_id: Optional[str] = None

    @property
    def needs_sync(self) -> bool:
        return self.status in (MISSING, DRIFTED)

    def __str__(self) -> str:
        icon = {
            MISSING: "[缺失]",
            ORPHAN: "[多余]",
            DRIFTED: "[漂移]",
            IN_SYNC: "[一致]",
        }.get(self.status, "[?]")
        return f"{icon} {self.table}: {self.detail}"


@dataclass
class SyncReport:
    """一次同步的汇总报告"""

    drifts: list[DDLDrift] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def missing(self) -> list[DDLDrift]:
        return [d for d in self.drifts if d.status == MISSING]

    @property
    def orphaned(self) -> list[DDLDrift]:
        return [d for d in self.drifts if d.status == ORPHAN]

    @property
    def drifted(self) -> list[DDLDrift]:
        return [d for d in self.drifts if d.status == DRIFTED]

    @property
    def has_drift(self) -> bool:
        return any(d.needs_sync or d.status == ORPHAN for d in self.drifts)

    def to_text(self) -> str:
        lines = [
            "DDL 漂移检测报告",
            "=" * 60,
            f"检查表数: {len(self.drifts)}  "
            f"| 缺失 {len(self.missing)} | 漂移 {len(self.drifted)} "
            f"| 多余 {len(self.orphaned)}",
            "",
        ]
        for d in self.drifts:
            lines.append(f"  {d}")
        if self.applied:
            lines += ["", f"已同步 {len(self.applied)} 张表: {', '.join(self.applied)}"]
        if self.failed:
            lines += ["", "失败:"]
            for t, err in self.failed:
                lines.append(f"  [失败] {t}: {err}")
        if not self.has_drift and not self.applied:
            lines += ["", "无需同步，DDL 与 MySQL 完全一致。"]
        return "\n".join(lines)


def normalize_ddl(ddl: str) -> str:
    """规范化 DDL 文本（兜底手段，主要用于日志与人工比对）

    剔除会随数据/版本变化、但不影响 SQL 生成语义的片段：

    - ``AUTO_INCREMENT=123``：随插入行数变化，必须剔除否则永远报漂移
    - 注释（``--`` / ``/* */``）：人工维护的 DDL 常带中文注释
    - 多余空白与大小写：统一折叠

    注意：**不要用它的结果做漂移判定**。见 :func:`parse_ddl` 的说明 ——
    手写 DDL 与 MySQL ``SHOW CREATE TABLE`` 在反引号、COLLATE、DEFAULT 等
    方面存在大量等价写法差异，纯文本比对会 100% 误报。

    Args:
        ddl: 原始 DDL

    Returns:
        规范化后的字符串
    """
    if not ddl:
        return ""
    s = ddl
    # 行注释与块注释
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s)
    s = re.sub(r"#[^\n]*", " ", s)
    # AUTO_INCREMENT=数字（CREATE TABLE 的表级选项）
    s = re.sub(r"AUTO_INCREMENT\s*=\s*\d+", "", s, flags=re.I)
    # 折叠空白并统一小写
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


# 索引名：KEY / UNIQUE KEY / INDEX 后跟标识符再跟左括号。
# 要求后面必须出现 `(`，因此不会误匹配 `PRIMARY KEY (\`id\`)` 这种表级主键。
_INDEX_RE = re.compile(
    r"(?:UNIQUE\s+KEY|UNIQUE\s+INDEX|\bKEY|\bINDEX)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\(",
    re.I,
)


def parse_ddl(ddl: str) -> tuple[dict[str, str], set[str]]:
    """从 DDL 中提取结构化信息，用于语义级漂移比对

    **为什么不用文本比对**：训练库里的 DDL 是手写摘要版（无反引号、无 COLLATE、
    ``int auto_increment primary key`` 简写），而 MySQL 的 ``SHOW CREATE TABLE``
    是完整精确版（``` `id` int NOT NULL AUTO_INCREMENT``、带 COLLATE 和 DEFAULT）。
    两者语义等价但文本差异极大 —— 直接比对字符串会让**所有表都报漂移**，
    产生大量噪音，最终导致这个检测工具被人忽略。

    因此改为用 sqlglot 解析 AST 提取「列名 -> 类型」，用正则提取索引名，
    只在这两个集合有实质差异时才判定为漂移。

    Args:
        ddl: CREATE TABLE 语句

    Returns:
        ``(列名->规范化类型, 索引名集合)``。解析失败时返回 ``({}, set())``，
        调用方应视为"无法判定"
    """
    if not ddl:
        return {}, set()

    columns: dict[str, str] = {}
    try:
        import sqlglot  # noqa: PLC0415
        from sqlglot import exp  # noqa: PLC0415

        ast = sqlglot.parse_one(ddl, dialect="mysql")
        for col in ast.find_all(exp.ColumnDef):
            kind = col.args.get("kind")
            # 统一大写便于比较（MySQL 侧 INT vs 手写侧 int 应视为相同）
            columns[col.name.lower()] = (str(kind).upper() if kind else "")
    except Exception as e:
        logger.warning("sqlglot 解析 DDL 失败，退化为无法判定: %s", e)
        return {}, set()

    indexes = {m.group(1).lower() for m in _INDEX_RE.finditer(ddl)}
    return columns, indexes


class DDLSyncService:
    """DDL 漂移检测与同步

    Args:
        allowed_tables: 参与同步的表名。默认从 ``security.yaml`` 的
            ``schema_limiter.allowed_tables`` 读取
    """

    def __init__(self, allowed_tables: Optional[list[str]] = None) -> None:
        self._allowed_tables = allowed_tables
        self._chroma = None

    # --- 配置 ---

    @property
    def allowed_tables(self) -> list[str]:
        """业务表白名单（延迟加载 security.yaml）"""
        if self._allowed_tables is None:
            self._allowed_tables = self._load_allowed_tables()
        return self._allowed_tables

    @staticmethod
    def _load_allowed_tables() -> list[str]:
        """从安全策略读取允许查询的表

        复用安全网关的配置，保证"能查"与"有训练"一致。
        读取失败时返回空列表 —— 宁可不同步也不能同步错表。
        """
        try:
            from app.core.sql_security import sql_gateway  # noqa: PLC0415

            cfg = sql_gateway.sql_security_config or {}
            tables = cfg.get("schema_limiter", {}).get("allowed_tables", [])
            return [str(t) for t in tables]
        except Exception as e:
            logger.error("读取安全策略的 allowed_tables 失败: %s", e)
            return []

    # --- MySQL 侧 ---

    def fetch_mysql_ddl(self, table: str) -> Optional[str]:
        """从 MySQL 获取某张表的建表语句

        Returns:
            CREATE TABLE 语句；表不存在或查询失败返回 None
        """
        # 表名来自配置文件，但仍做白名单式校验，防止配置被改后拼出注入
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            logger.error("非法表名，跳过: %s", table)
            return None
        try:
            with db_manager.engine.connect() as conn:
                row = conn.execute(text(f"SHOW CREATE TABLE `{table}`")).fetchone()
            return row[1] if row else None
        except Exception as e:
            logger.warning("获取表 %s 的 DDL 失败: %s", table, e)
            return None

    def fetch_mysql_tables(self) -> list[str]:
        """列出 MySQL 中当前实际存在的表"""
        try:
            with db_manager.engine.connect() as conn:
                rows = conn.execute(text("SHOW TABLES")).fetchall()
            return [r[0] for r in rows]
        except Exception as e:
            logger.error("列出 MySQL 表失败: %s", e)
            return []

    # --- ChromaDB 侧 ---

    async def _get_chroma(self):
        if self._chroma is None:
            from app.core.chroma_store import chroma_store  # noqa: PLC0415

            if not chroma_store.is_initialized:
                await chroma_store.initialize()
            self._chroma = chroma_store
        return self._chroma

    async def fetch_chroma_ddl(self) -> dict[str, tuple[str, str]]:
        """读取 ChromaDB 中已有的 DDL

        Returns:
            ``{表名: (记录 id, DDL 内容)}``
        """
        store = await self._get_chroma()
        result: dict[str, tuple[str, str]] = {}
        for rec in store.get_all("ddl"):
            table = (rec.metadata or {}).get("table_name")
            if table:
                result[str(table)] = (rec.id, rec.content)
        return result

    # --- 核心：比对 ---

    async def detect(self) -> SyncReport:
        """检测漂移

        Returns:
            SyncReport，包含每张表的状态
        """
        report = SyncReport()
        allowed = self.allowed_tables
        if not allowed:
            logger.error("业务表白名单为空，拒绝执行（防止同步错误的范围）")
            return report

        mysql_tables = set(self.fetch_mysql_tables())
        chroma = await self.fetch_chroma_ddl()

        for table in allowed:
            if table not in mysql_tables:
                if table in chroma:
                    cid, _ = chroma[table]
                    report.drifts.append(
                        DDLDrift(
                            table=table,
                            status=ORPHAN,
                            detail="MySQL 中已不存在，但训练库仍有 DDL",
                            chroma_id=cid,
                        )
                    )
                else:
                    report.drifts.append(
                        DDLDrift(
                            table=table,
                            status=ORPHAN,
                            detail="MySQL 与训练库中都不存在（白名单配置可能有误）",
                        )
                    )
                continue

            mysql_ddl = self.fetch_mysql_ddl(table) or ""
            if table not in chroma:
                report.drifts.append(
                    DDLDrift(
                        table=table,
                        status=MISSING,
                        detail="训练库缺少该表的 DDL",
                        mysql_ddl=mysql_ddl,
                    )
                )
                continue

            cid, chroma_ddl = chroma[table]
            # 语义级比对：只比较「列名+类型」与「索引名」，忽略格式差异
            m_cols, m_idx = parse_ddl(mysql_ddl)
            c_cols, c_idx = parse_ddl(chroma_ddl)
            if not m_cols:
                # MySQL 侧解析失败，无法判定 —— 保守标记为漂移并说明原因
                report.drifts.append(
                    DDLDrift(
                        table=table, status=DRIFTED,
                        detail="MySQL DDL 解析失败，无法比对（建议重建）",
                        mysql_ddl=mysql_ddl, chroma_id=cid,
                    )
                )
            elif m_cols == c_cols and m_idx == c_idx:
                report.drifts.append(
                    DDLDrift(
                        table=table, status=IN_SYNC,
                        detail=f"一致（{len(m_cols)} 列 / {len(m_idx)} 索引）",
                        mysql_ddl=mysql_ddl, chroma_id=cid,
                    )
                )
            else:
                report.drifts.append(
                    DDLDrift(
                        table=table,
                        status=DRIFTED,
                        detail=self._describe_diff(c_cols, c_idx, m_cols, m_idx),
                        mysql_ddl=mysql_ddl,
                        chroma_id=cid,
                    )
                )

        # 白名单之外、但训练库里有的表（例如曾经训练过、后来移出白名单）
        for table in set(chroma) - set(allowed):
            cid, _ = chroma[table]
            report.drifts.append(
                DDLDrift(
                    table=table,
                    status=ORPHAN,
                    detail="训练库有 DDL，但不在业务表白名单内",
                    chroma_id=cid,
                )
            )

        return report

    @staticmethod
    def _describe_diff(
        c_cols: dict[str, str],
        c_idx: set[str],
        m_cols: dict[str, str],
        m_idx: set[str],
    ) -> str:
        """基于结构化信息生成人类可读的差异描述

        Args:
            c_cols / c_idx: ChromaDB 侧的列与索引
            m_cols / m_idx: MySQL 侧的列与索引
        """
        added = sorted(set(m_cols) - set(c_cols))
        removed = sorted(set(c_cols) - set(m_cols))
        changed = sorted(
            k for k in (set(m_cols) & set(c_cols))
            if m_cols[k] and c_cols[k] and m_cols[k] != c_cols[k]
        )
        idx_added = sorted(m_idx - c_idx)
        idx_removed = sorted(c_idx - m_idx)

        parts = []
        if added:
            parts.append(f"新增列 {added}")
        if removed:
            parts.append(f"删除列 {removed}")
        if changed:
            parts.append(
                "类型变更 "
                + ", ".join(f"{k}: {c_cols[k]} -> {m_cols[k]}" for k in changed)
            )
        if idx_added:
            parts.append(f"新增索引 {idx_added}")
        if idx_removed:
            parts.append(f"删除索引 {idx_removed}")
        return "；".join(parts) if parts else "结构存在差异"

    # --- 应用 ---

    async def apply(self, report: SyncReport, drop_orphans: bool = False) -> SyncReport:
        """按报告同步 DDL 到 ChromaDB

        Args:
            report: detect() 的结果
            drop_orphans: 是否删除多余（MySQL 已无 / 不在白名单）的 DDL

        Returns:
            同一个 report 对象，附带 applied / failed
        """
        store = await self._get_chroma()

        for drift in report.drifts:
            if drift.status in (MISSING, DRIFTED):
                if not drift.mysql_ddl:
                    report.failed.append((drift.table, "MySQL DDL 为空"))
                    continue
                try:
                    if drift.chroma_id:
                        store.remove("ddl", drift.chroma_id)
                    store.add_ddl(drift.mysql_ddl, table_name=drift.table)
                    report.applied.append(drift.table)
                    logger.info("已同步 DDL: %s (%s)", drift.table, drift.status)
                except Exception as e:
                    report.failed.append((drift.table, str(e)))
                    logger.error("同步 DDL 失败 %s: %s", drift.table, e)

            elif drift.status == ORPHAN and drop_orphans and drift.chroma_id:
                try:
                    store.remove("ddl", drift.chroma_id)
                    report.applied.append(f"{drift.table}(删除)")
                    logger.info("已删除多余 DDL: %s", drift.table)
                except Exception as e:
                    report.failed.append((drift.table, str(e)))

        return report
