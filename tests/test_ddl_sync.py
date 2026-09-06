"""
DDL 同步服务测试
----------------
重点是**避免误报**：训练库里是手写摘要版 DDL，MySQL 是 SHOW CREATE TABLE
精确版，两者语义等价但文本差异极大。若用字符串比对，全部 7 张表都会报漂移，
检测工具会因噪音太大而被忽略。

因此核心测试是：两种写法必须解析出相同的「列+类型」与「索引」集合。
"""

import pytest

from app.services.ddl_sync_service import (
    DRIFTED,
    IN_SYNC,
    MISSING,
    ORPHAN,
    DDLSyncService,
    parse_ddl,
)

# 手写摘要版（init_training.py 风格）：无反引号、无 COLLATE、列级 PRIMARY KEY
HANDWRITTEN_DDL = """
CREATE TABLE operators (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
  name VARCHAR(100) NOT NULL COMMENT '运营商名称',
  country_code VARCHAR(10) NOT NULL,
  status VARCHAR(20) DEFAULT 'active',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_name (name),
  INDEX idx_country (country_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# MySQL SHOW CREATE TABLE 输出：反引号、COLLATE、表级 PRIMARY KEY
MYSQL_DDL = """
CREATE TABLE `operators` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键',
  `name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '运营商名称',
  `country_code` varchar(10) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT 'active',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_name` (`name`),
  KEY `idx_country` (`country_code`)
) ENGINE=InnoDB AUTO_INCREMENT=42 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


class TestParseDDL:
    """DDL 结构化解析"""

    def test_extracts_columns_and_types(self):
        cols, _ = parse_ddl(MYSQL_DDL)
        assert set(cols) == {"id", "name", "country_code", "status", "created_at"}
        assert cols["id"] == "INT"
        assert cols["name"] == "VARCHAR(100)"

    def test_handwritten_and_mysql_are_equivalent(self):
        """最关键：两种等价写法必须解析出相同结构（否则会产生误报）"""
        h_cols, h_idx = parse_ddl(HANDWRITTEN_DDL)
        m_cols, m_idx = parse_ddl(MYSQL_DDL)
        assert h_cols == m_cols, "手写版与 MySQL 版的列定义应等价"
        assert h_idx == m_idx, "手写版与 MySQL 版的索引应等价"

    def test_index_names_extracted(self):
        _, idx = parse_ddl(MYSQL_DDL)
        assert idx == {"idx_name", "idx_country"}

    def test_primary_key_not_treated_as_index(self):
        """PRIMARY KEY (`id`) 不能被当作名为 id 的索引"""
        _, idx = parse_ddl(MYSQL_DDL)
        assert "id" not in idx
        assert "primary" not in idx

    def test_unique_key_index_captured(self):
        _, idx = parse_ddl("CREATE TABLE t (a INT, b INT, UNIQUE KEY uk_ab (a, b));")
        assert "uk_ab" in idx

    def test_empty_input(self):
        assert parse_ddl("") == ({}, set())

    def test_invalid_ddl_returns_empty(self):
        """解析失败返回空，调用方按"无法判定"处理，不能误判为无差异"""
        cols, idx = parse_ddl("这不是 DDL !!!")
        assert cols == {}
        assert idx == set()


class TestDescribeDiff:
    """差异描述"""

    def test_added_index(self):
        detail = DDLSyncService._describe_diff(
            {"a": "INT"}, set(), {"a": "INT"}, {"idx_a"}
        )
        assert "新增索引 ['idx_a']" in detail

    def test_added_and_removed_columns(self):
        detail = DDLSyncService._describe_diff(
            {"old": "INT"}, set(), {"new": "INT"}, set()
        )
        assert "新增列 ['new']" in detail
        assert "删除列 ['old']" in detail

    def test_type_change(self):
        detail = DDLSyncService._describe_diff(
            {"a": "INT"}, set(), {"a": "BIGINT"}, set()
        )
        assert "类型变更" in detail
        assert "INT -> BIGINT" in detail

    def test_no_diff_fallback(self):
        detail = DDLSyncService._describe_diff({"a": "INT"}, set(), {"a": "INT"}, set())
        assert detail == "结构存在差异"


class TestDetect:
    """漂移检测（MySQL 与 ChromaDB 均用桩替换）"""

    @staticmethod
    def _make_service(mysql_tables, mysql_ddls, chroma_ddls):
        svc = DDLSyncService(allowed_tables=list(mysql_ddls))
        svc.fetch_mysql_tables = lambda: list(mysql_tables)  # type: ignore[method-assign]
        svc.fetch_mysql_ddl = lambda t: mysql_ddls.get(t)  # type: ignore[method-assign]

        async def fake_chroma():
            return {t: (f"id-{t}", d) for t, d in chroma_ddls.items()}

        svc.fetch_chroma_ddl = fake_chroma  # type: ignore[method-assign]
        return svc

    async def test_in_sync(self):
        svc = self._make_service(
            ["t1"], {"t1": HANDWRITTEN_DDL}, {"t1": MYSQL_DDL}
        )
        report = await svc.detect()
        assert not report.has_drift
        assert report.drifts[0].status == IN_SYNC
        assert report.drifts[0].table == "t1"

    async def test_missing_ddl(self):
        svc = self._make_service(["t1"], {"t1": MYSQL_DDL}, {})
        report = await svc.detect()
        assert len(report.missing) == 1
        assert report.missing[0].status == MISSING
        assert report.has_drift

    async def test_drifted_index(self):
        """MySQL 多了索引但列相同 -> 判定为漂移而非缺失"""
        svc = self._make_service(
            ["t1"], {"t1": MYSQL_DDL}, {"t1": HANDWRITTEN_DDL.replace(
                "  INDEX idx_country (country_code)\n", ""
            )}
        )
        report = await svc.detect()
        assert len(report.drifted) == 1
        assert "idx_country" in report.drifted[0].detail

    async def test_orphan_when_table_dropped(self):
        """MySQL 已删表但训练库还有 DDL"""
        svc = self._make_service([], {"t1": MYSQL_DDL}, {"t1": MYSQL_DDL})
        report = await svc.detect()
        assert len(report.orphaned) == 1
        assert report.orphaned[0].status == ORPHAN

    async def test_not_in_whitelist_is_orphan(self):
        """训练库有 DDL 但表不在白名单 -> 提示多余"""
        svc = self._make_service(
            ["t1"], {"t1": MYSQL_DDL}, {"t1": MYSQL_DDL, "secret": "CREATE TABLE ..."}
        )
        report = await svc.detect()
        names = {d.table for d in report.orphaned}
        assert "secret" in names

    async def test_empty_whitelist_refuses_to_run(self):
        """白名单为空时必须拒绝同步，防止同步错误范围"""
        svc = DDLSyncService(allowed_tables=[])
        report = await svc.detect()
        assert report.drifts == []

    async def test_sql_injection_in_table_name_rejected(self):
        """表名来自配置，仍需校验，防止配置被改后拼出注入"""
        svc = DDLSyncService(allowed_tables=["x"])
        assert svc.fetch_mysql_ddl("users; DROP TABLE users") is None
        assert svc.fetch_mysql_ddl("us ers") is None


class TestReport:
    """报告渲染"""

    def test_to_text_includes_summary(self):
        svc = DDLSyncService(allowed_tables=[])
        report = svc._report_stub() if hasattr(svc, "_report_stub") else None
        if report is None:
            from app.services.ddl_sync_service import DDLDrift, SyncReport

            report = SyncReport(drifts=[
                DDLDrift(table="t1", status=MISSING, detail="训练库缺少该表的 DDL"),
                DDLDrift(table="t2", status=IN_SYNC, detail="一致"),
            ])
        text = report.to_text()
        assert "缺失 1" in text
        assert "t1" in text

    def test_needs_sync(self):
        from app.services.ddl_sync_service import DDLDrift

        assert DDLDrift(table="t", status=MISSING).needs_sync is True
        assert DDLDrift(table="t", status=DRIFTED).needs_sync is True
        assert DDLDrift(table="t", status=IN_SYNC).needs_sync is False
        assert DDLDrift(table="t", status=ORPHAN).needs_sync is False
