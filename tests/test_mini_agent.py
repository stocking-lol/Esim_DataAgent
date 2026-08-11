"""
Mini Agent Runtime 单元测试
==========================
覆盖自研 Agent 底座的核心能力：

  - extract_sql_block: LLM 输出 → 纯 SQL 提取
  - ToolRegistry: 工具注册 / 查找 / 权限
  - SqlTool dry_run: 语法 / 表 / 列三级校验 + 安全网关拦截（fail-closed）
  - 编排循环: 成功路径 / 自愈重试 / 上限终止 / 安全拦截不重试
  - NaiveNL2SQL: 非 Agent 对照组
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.mini_agent.memory import AgentMemory
from app.core.mini_agent.naive import NaiveNL2SQL
from app.core.mini_agent.rag import RAGRetriever
from app.core.mini_agent.runtime import (
    SCHEMA_DDL,
    AgentConfig,
    AgentResponse,
    MiniAgentRuntime,
    build_default_runtime,
    extract_sql_block,
)
from app.core.mini_agent.tools import SqlTool, ToolRegistry, ToolSpec


# ============================================================
# SQL 提取
# ============================================================

class TestExtractSqlBlock:
    def test_code_block(self):
        assert extract_sql_block("```sql\nSELECT name FROM plans\n```") == \
            "SELECT name FROM plans"

    def test_code_block_no_lang(self):
        assert extract_sql_block("```\nSELECT 1\n```") == "SELECT 1"

    def test_bare_sql_with_semicolon(self):
        assert extract_sql_block("SELECT name FROM plans;") == "SELECT name FROM plans"

    def test_bare_sql_no_semicolon(self):
        assert extract_sql_block("SELECT name FROM plans") == "SELECT name FROM plans"

    def test_surrounded_text(self):
        raw = "好的，以下是 SQL：\n```sql\nSELECT * FROM users\n```\n希望对你有帮助。"
        assert extract_sql_block(raw) == "SELECT * FROM users"

    def test_empty(self):
        assert extract_sql_block("") == ""
        assert extract_sql_block(None) == ""


# ============================================================
# 工具注册中心
# ============================================================

class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()

        async def _noop(**kwargs):
            return None

        reg.register(ToolSpec(name="sql_tool", description="run sql", func=_noop))
        assert reg.has("sql_tool")
        assert reg.get("sql_tool").name == "sql_tool"
        assert reg.list() == ["sql_tool"]

    def test_duplicate_register_raises(self):
        reg = ToolRegistry()

        async def _noop(**kwargs):
            return None

        reg.register(ToolSpec(name="t", description="d", func=_noop))
        with pytest.raises(ValueError):
            reg.register(ToolSpec(name="t", description="d2", func=_noop))

    def test_access_groups(self):
        reg = ToolRegistry()

        async def _noop(**kwargs):
            return None

        reg.register(ToolSpec(name="t", description="d", func=_noop,
                              access_groups=["analyst"]))
        assert reg.can_access("t", "analyst")
        assert reg.can_access("t", "admin")       # admin 恒可访问
        assert not reg.can_access("t", "viewer")
        assert not reg.can_access("unknown", "admin")


# ============================================================
# SqlTool dry_run 三级校验 + 安全网关
# ============================================================

class TestSqlToolDryRun:
    @pytest.fixture()
    def tool(self):
        return SqlTool(dry_run=True)

    def test_valid_sql_passes(self, tool):
        r = asyncio_run(tool.execute("SELECT name, price FROM plans"))
        assert r.success
        assert not r.blocked

    def test_syntax_error_retryable(self, tool):
        """网关正则兜底放行的坏 SQL → dry_run 模拟 DB 语法错误 1064（可重试）"""
        r = asyncio_run(tool.execute("SELECT name FROM plans WHERE"))
        assert not r.success
        assert r.retryable
        assert "1064" in r.error

    def test_table_not_in_whitelist_blocked(self, tool):
        """非白名单表被安全网关 fail-closed 拦截，不可重试"""
        r = asyncio_run(tool.execute("SELECT * FROM userss"))
        assert r.blocked
        assert not r.retryable
        assert "非白名单表" in r.block_reason

    def test_unknown_column_retryable(self, tool):
        """单表查询的非法裸列 → dry_run 模拟 DB 列错误 1054（可重试）"""
        r = asyncio_run(tool.execute("SELECT name, fake_col FROM plans"))
        assert not r.success
        assert r.retryable
        assert "1054" in r.error

    def test_security_gateway_blocks_information_schema(self, tool):
        """fail-closed：非白名单表被网关拦截，且不可重试"""
        r = asyncio_run(tool.execute("SELECT * FROM information_schema.tables"))
        assert r.blocked
        assert not r.retryable
        assert r.block_reason

    def test_security_gateway_blocks_write(self, tool):
        r = asyncio_run(tool.execute("DROP TABLE users"))
        assert r.blocked
        assert not r.retryable

    def test_empty_sql(self, tool):
        r = asyncio_run(tool.execute("  "))
        assert not r.success
        assert not r.retryable


# ============================================================
# 编排循环（Mock LLM，不调真实 API）
# ============================================================

class _FakeLLM:
    """可编程 Fake LLM：按调用次数返回不同结果"""

    def __init__(self, answers, errors=None):
        self.answers = list(answers)
        self.errors = list(errors or [])
        self.calls = 0

    async def generate_sql(self, question, ddl="", documentation="", sql_examples=""):
        self.calls += 1
        if self.errors:
            e = self.errors.pop(0)
            if e:
                raise e
        return self.answers[min(self.calls - 1, len(self.answers) - 1)]

    async def correct_sql(self, sql, error_message, correction_hint="", ddl=""):
        self.calls += 1
        if self.errors:
            e = self.errors.pop(0)
            if e:
                raise e
        return self.answers[min(self.calls - 1, len(self.answers) - 1)]


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


class TestMiniAgentRuntime:
    def test_success_first_try(self):
        """首次生成即成功：不触发自愈"""
        runtime = build_default_runtime(dry_run=True, use_rag=False)
        runtime.llm = _FakeLLM(answers=["SELECT name, price FROM plans"])
        resp = asyncio_run(runtime.ask("套餐价格"))
        assert resp.success
        assert resp.iterations == 1
        assert resp.retries == 0
        assert "name" in resp.sql

    def test_self_correction_loop(self):
        """第一次生成错误 → 错误反馈 → 第二次成功：自愈生效"""
        runtime = build_default_runtime(dry_run=True, use_rag=False)
        runtime.llm = _FakeLLM(answers=[
            "SELECT name, fake_col FROM plans",   # 列不存在 → 触发重试
            "SELECT name, price FROM plans",      # 修正后成功
        ])
        resp = asyncio_run(runtime.ask("套餐价格"))
        assert resp.success
        assert resp.iterations == 2
        assert resp.retries == 1
        assert len(resp.feedbacks) == 1
        assert "1054" in resp.feedbacks[0]

    def test_max_iterations_terminates(self):
        """始终报错 → 达到上限终止，不无限循环"""
        runtime = build_default_runtime(dry_run=True, use_rag=False, max_iterations=3)
        runtime.llm = _FakeLLM(answers=[
            "SELECT name, fake_col FROM plans"] * 10)
        resp = asyncio_run(runtime.ask("套餐价格"))
        assert not resp.success
        assert resp.iterations == 3
        assert "最大循环次数" in resp.error

    def test_security_block_does_not_retry(self):
        """安全拦截 → 立即终止，不进入自愈循环（避免放大风险）"""
        runtime = build_default_runtime(dry_run=True, use_rag=False)
        runtime.llm = _FakeLLM(answers=[
            "SELECT * FROM information_schema.tables",
            "SELECT name, price FROM plans",   # 若被重试将执行这条
        ])
        resp = asyncio_run(runtime.ask("测试"))
        assert resp.blocked
        assert not resp.success
        assert resp.iterations == 1
        assert resp.retries == 0

    def test_empty_sql_from_llm(self):
        runtime = build_default_runtime(dry_run=True, use_rag=False)
        runtime.llm = _FakeLLM(answers=["抱歉，我无法回答"])
        resp = asyncio_run(runtime.ask("测试"))
        assert not resp.success
        assert "未返回有效 SQL" in resp.error

    def test_llm_exception_handled(self):
        runtime = build_default_runtime(dry_run=True, use_rag=False)
        runtime.llm = _FakeLLM(answers=[], errors=[RuntimeError("API down")])
        resp = asyncio_run(runtime.ask("测试"))
        assert not resp.success
        assert "LLM 调用失败" in resp.error

    def test_memory_records_conversation(self):
        runtime = build_default_runtime(dry_run=True, use_rag=False)
        runtime.llm = _FakeLLM(answers=["SELECT name, price FROM plans"])
        asyncio_run(runtime.ask("套餐价格"))
        assert runtime.memory.size >= 1
        roles = [m["role"] for m in runtime.memory.get_history()]
        assert "assistant" in roles


# ============================================================
# 对话记忆
# ============================================================

class TestAgentMemory:
    def test_capacity_limit(self):
        mem = AgentMemory(capacity=3)
        for i in range(5):
            mem.add("user", f"msg{i}")
        assert mem.size == 3
        assert mem.get_recent(1)[0]["content"] == "msg4"

    def test_format_for_prompt(self):
        mem = AgentMemory()
        mem.add_user("问题一")
        mem.add_assistant("回答一")
        text = mem.format_for_prompt()
        assert "问题一" in text and "回答一" in text


# ============================================================
# 对照组
# ============================================================

class TestNaiveNL2SQL:
    def test_generate_returns_sql(self):
        naive = NaiveNL2SQL(llm=_FakeLLM(answers=["SELECT name, price FROM plans"]))
        sql = asyncio_run(naive.generate("套餐价格"))
        assert sql == "SELECT name, price FROM plans"

    def test_generate_handles_llm_error(self):
        naive = NaiveNL2SQL(llm=_FakeLLM(answers=[], errors=[RuntimeError("boom")]))
        sql = asyncio_run(naive.generate("套餐价格"))
        assert sql == ""


# ============================================================
# 基础资产完整性
# ============================================================

class TestAssets:
    def test_schema_ddl_has_all_tables(self):
        for t in ["operators", "users", "plans", "orders",
                  "esim_profiles", "data_usage", "roaming_packages"]:
            assert f"CREATE TABLE {t}" in SCHEMA_DDL

    def test_build_default_runtime_has_sql_tool(self):
        rt = build_default_runtime(dry_run=True)
        assert rt.sql_tool is not None
        assert rt.sql_tool.dry_run is True


# ============================================================
# RAG 混合检索（自研 hybrid：向量 + 关键词加权）
# ============================================================

class _FakeTrainingRecord:
    """模拟 TrainingRecord（仅含检索/拼装所需字段）"""

    def __init__(self, rid, coll, content, meta=None):
        self.id = rid
        self.type = coll
        self.content = content
        self.metadata = meta or {}


class _FakeStore:
    """可编程假 ChromaDB store：返回固定候选，不依赖真实向量库"""

    def __init__(self, candidates: dict):
        self._candidates = candidates
        self.is_initialized = True

    def search(self, query, n_results=5, collection_filter=None):
        out = {}
        for coll, records in self._candidates.items():
            if collection_filter and coll not in collection_filter:
                continue
            out[coll] = records[:n_results]
        return out

    def retrieve_context(self, question, max_items=5):
        return ""


def _mk_example(q, sql):
    return _FakeTrainingRecord(f"sql_{q[:4]}", "sql_examples",
                               f"问题: {q}\nSQL: {sql}",
                               {"question": q, "sql": sql})


class TestRAGHybridRetrieval:
    def test_exact_question_example_recalled_first(self):
        """问题与示例 question 完全一致 → 关键词重排排第一（修复检索漂移）"""
        exact = _mk_example(
            "查询所有已激活的用户档案",
            "SELECT * FROM esim_profiles WHERE profile_status = 'active'")
        other = _mk_example(
            "中国地区的活跃用户列表",
            "SELECT id, phone_number FROM users WHERE region = 'China'")
        store = _FakeStore({"sql_examples": [other, exact]})
        retriever = RAGRetriever(store=store)
        ctx = retriever.retrieve("查询所有已激活的用户档案", max_items=2)
        assert "esim_profiles" in ctx
        idx_exact = ctx.find("esim_profiles")
        idx_other = ctx.find("中国地区的活跃用户列表")
        assert idx_exact != -1 and idx_other != -1
        assert idx_exact < idx_other

    def test_no_overlap_keeps_vector_order(self):
        """无关键词重叠 → 不触发重排（保持向量召回顺序）"""
        a = _mk_example("套餐销量排名",
                        "SELECT p.name FROM plans p JOIN orders o")
        b = _mk_example("用户流量TOP10",
                        "SELECT u.id FROM users u JOIN data_usage d")
        store = _FakeStore({"sql_examples": [a, b]})
        retriever = RAGRetriever(store=store)
        ctx = retriever.retrieve("订单金额总和", max_items=2)
        assert ctx
        assert "套餐销量排名" in ctx

    def test_topic_substring_boosts_documentation(self):
        """文档主题是问题子串（如"漫游"）→ contains 奖励触发提升"""
        doc_roaming = _FakeTrainingRecord(
            "d1", "documentation",
            "漫游是指用户在非归属运营商网络中使用服务。",
            {"topic": "漫游"})
        doc_unrelated = _FakeTrainingRecord(
            "d2", "documentation",
            "用户离网率 = 本期流失用户数 / 期初用户数。",
            {"topic": "用户离网率"})
        store = _FakeStore({"documentation": [doc_unrelated, doc_roaming]})
        retriever = RAGRetriever(store=store)
        ctx = retriever.retrieve("查询所有处于漫游状态的套餐包", max_items=2)
        assert ctx.find("漫游是指") < ctx.find("用户离网率")

    def test_unavailable_store_degrades_to_empty(self):
        store = _FakeStore({"sql_examples": []})
        store.is_initialized = False
        retriever = RAGRetriever(store=store)
        assert retriever.retrieve("任意问题") == ""

    def test_context_truncation_keeps_examples(self):
        """超长 DDL 被截断，但 SQL 示例必须保留（few-shot 价值最高）"""
        ddl_long = _FakeTrainingRecord(
            "ddl1", "ddl", "CREATE TABLE users (" + "a INT," * 200 + ")",
            {"table_name": "users"})
        ex = _mk_example("查询所有用户", "SELECT * FROM users")
        store = _FakeStore({"ddl": [ddl_long], "sql_examples": [ex]})
        retriever = RAGRetriever(store=store)
        ctx = retriever.retrieve("查询所有用户", max_items=1)
        assert "SELECT * FROM users" in ctx
