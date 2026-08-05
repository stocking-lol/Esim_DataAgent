"""
Day 18: 自我纠错回路 — 测试套件
--------------------------------
覆盖：
1. ErrorClassifier 错误分类（语法/表不存在/列不存在/歧义/超时/权限/未知）
2. 可重试判断 (is_retryable)
3. 纠错提示生成 (get_correction_hint)
4. LLM correct_sql 增强（错误类别提示注入）
5. execute_query_with_retry 决策逻辑（mock 掉 Vanna Agent，验证重试次数与早停）
"""

import pytest

from app.core.error_classifier import (
    ErrorCategory,
    classify_sql_error,
    get_correction_hint,
    is_retryable,
)


# ============================================================
# 1. 错误分类
# ============================================================

class TestErrorClassification:
    def test_syntax_error_by_errno(self):
        err = "(pymysql.err.ProgrammingError) (1064, \"You have an error in your SQL syntax\")"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.SYNTAX_ERROR
        assert c.mysql_errno == 1064
        assert c.retryable is True

    def test_table_not_found_by_errno(self):
        err = "pymysql.err.ProgrammingError: (1146, \"Table 'esim.usersss' doesn't exist\")"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.TABLE_NOT_FOUND
        assert c.mysql_errno == 1146

    def test_column_not_found_by_errno(self):
        err = "(1054, \"Unknown column 'phone_num' in 'field list'\")"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.COLUMN_NOT_FOUND
        assert c.mysql_errno == 1054

    def test_ambiguous_column_by_errno(self):
        err = "(1052, \"Column 'id' in field list is ambiguous\")"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.AMBIGUOUS_COLUMN
        assert c.mysql_errno == 1052

    def test_timeout_by_errno(self):
        err = "(3024, \"Query execution was interrupted, maximum statement execution time exceeded\")"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.TIMEOUT
        assert c.mysql_errno == 3024

    def test_permission_denied_by_errno(self):
        err = "(1142, \"SELECT command denied to user 'x'@'localhost' for table 'users'\")"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.PERMISSION_DENIED
        assert c.mysql_errno == 1142

    def test_syntax_error_by_keyword(self):
        err = "You have an error in your SQL syntax near 'FROM' at line 1"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.SYNTAX_ERROR

    def test_table_not_found_by_keyword(self):
        err = "Table 'esim.missing_table' doesn't exist"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.TABLE_NOT_FOUND

    def test_column_not_found_by_keyword(self):
        err = "Unknown column 'xyz' in 'where clause'"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.COLUMN_NOT_FOUND

    def test_timeout_by_keyword(self):
        err = "Query execution was interrupted (max_execution_time exceeded)"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.TIMEOUT

    def test_permission_by_keyword(self):
        err = "Access denied for user 'readonly'@'%' to database 'esim'"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.PERMISSION_DENIED

    def test_unknown_error(self):
        err = "something weird happened with no recognizable pattern"
        c = classify_sql_error(err)
        assert c.category == ErrorCategory.UNKNOWN
        assert c.retryable is False

    def test_empty_error(self):
        c = classify_sql_error("")
        assert c.category == ErrorCategory.UNKNOWN
        assert c.retryable is False


# ============================================================
# 2. 可重试判断
# ============================================================

class TestRetryableDecision:
    def test_retryable_categories(self):
        for err in [
            "(1064, \"syntax\")",
            "(1146, \"table\")",
            "(1054, \"column\")",
            "(1052, \"ambiguous\")",
        ]:
            assert is_retryable(err) is True

    def test_non_retryable_categories(self):
        for err in [
            "(3024, \"timeout\")",
            "(1142, \"access denied\")",
            "(2003, \"can't connect\")",
            "some random error",
        ]:
            assert is_retryable(err) is False


# ============================================================
# 3. 纠错提示
# ============================================================

class TestCorrectionHint:
    def test_syntax_hint(self):
        hint = get_correction_hint("(1064, \"syntax\")")
        assert "语法" in hint

    def test_table_hint(self):
        hint = get_correction_hint("(1146, \"table\")")
        assert "表名" in hint

    def test_column_hint(self):
        hint = get_correction_hint("(1054, \"column\")")
        assert "列名" in hint

    def test_ambiguous_hint(self):
        hint = get_correction_hint("(1052, \"ambiguous\")")
        assert "别名" in hint or "歧义" in hint

    def test_no_hint_for_unknown(self):
        hint = get_correction_hint("random error")
        assert hint == ""


# ============================================================
# 4. LLM correct_sql 增强
# ============================================================

class TestLLMCorrect:
    @pytest.mark.asyncio
    async def test_correct_sql_includes_hint(self, mocker):
        from app.core import llm as llm_module
        svc = llm_module.LLMService()

        captured = {}

        async def fake_call(system_prompt, user_message, temperature=None, max_tokens=None):
            captured["msg"] = user_message
            return "SELECT 1"

        mocker.patch.object(svc, "_call_llm", side_effect=fake_call)

        await svc.correct_sql(
            sql="SELECT * FROM userss",
            error_message="(1146, \"Table 'userss' doesn't exist\")",
            correction_hint="表名不存在。请只使用真实存在的表名。",
            ddl="CREATE TABLE users (id INT);",
        )
        msg = captured["msg"]
        assert "userss" in msg
        assert "错误类型与修正建议" in msg
        assert "表名不存在" in msg
        assert "CREATE TABLE users" in msg


# ============================================================
# 5. execute_query_with_retry 决策逻辑
# ============================================================

class TestRetryLoop:
    """使用 mock 替换 execute_query，验证重试决策"""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """清空查询缓存，避免跨测试命中缓存导致重试逻辑被短路"""
        from app.services.query_cache import query_cache
        query_cache.clear()

    @pytest.mark.asyncio
    async def test_no_retry_on_success(self, mocker):
        from app.services import query_service as qs
        from app.services.query_service import QueryResult

        good = QueryResult(question="q", sql="SELECT 1", data=[{"1": 1}], row_count=1)

        async def fake_execute(**kwargs):
            return good

        mocker.patch.object(qs, "execute_query", side_effect=fake_execute)
        result = await qs.execute_query_with_retry(question="q", max_retries=2)
        assert result.retry_count == 0
        assert result.error is None

    @pytest.mark.asyncio
    async def test_retry_on_retryable_error_then_success(self, mocker):
        from app.services import query_service as qs
        from app.services.query_service import QueryResult

        bad = QueryResult(question="q", sql="SELECT * FROM userss", error="(1146, \"Table 'userss' doesn't exist\")")
        good = QueryResult(question="q", sql="SELECT * FROM users", data=[], row_count=0)

        states = {"n": 0}
        async def fake_execute(**kwargs):
            states["n"] += 1
            if states["n"] == 1:
                return bad
            return good

        mocker.patch.object(qs, "execute_query", side_effect=fake_execute)
        result = await qs.execute_query_with_retry(question="q", max_retries=2)
        assert result.retry_count == 1
        assert result.error is None
        assert "userss" in result.corrections[0]

    @pytest.mark.asyncio
    async def test_no_retry_on_non_retryable(self, mocker):
        from app.services import query_service as qs
        from app.services.query_service import QueryResult

        blocked = QueryResult(question="q", blocked=True, block_reason="权限不足")
        async def fake_execute(**kwargs):
            return blocked

        mocker.patch.object(qs, "execute_query", side_effect=fake_execute)
        result = await qs.execute_query_with_retry(question="q", max_retries=2)
        assert result.retry_count == 0
        assert result.blocked is True

    @pytest.mark.asyncio
    async def test_stop_after_max_retries(self, mocker):
        from app.services import query_service as qs
        from app.services.query_service import QueryResult

        bad = QueryResult(question="q", sql="SELECT bad", error="(1054, \"Unknown column\")")
        async def fake_execute(**kwargs):
            return bad

        mocker.patch.object(qs, "execute_query", side_effect=fake_execute)
        result = await qs.execute_query_with_retry(question="q", max_retries=2)
        # 首次 + 2 次重试 = 3 次尝试，retry_count = 2
        assert result.retry_count == 2
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_no_retry_on_timeout(self, mocker):
        from app.services import query_service as qs
        from app.services.query_service import QueryResult

        timeout = QueryResult(question="q", error="(3024, \"max_execution_time exceeded\")")
        async def fake_execute(**kwargs):
            return timeout

        mocker.patch.object(qs, "execute_query", side_effect=fake_execute)
        result = await qs.execute_query_with_retry(question="q", max_retries=2)
        assert result.retry_count == 0
        assert result.error is not None
