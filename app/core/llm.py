"""
LLM 服务封装
----------
封装 OpenAI 兼容接口的 LLM 服务，提供 SQL 生成、解释和纠错能力。
使用 DeepSeek-V3 作为默认模型。
"""

import json
import logging
import time
from typing import Any, Optional

from openai import AsyncOpenAI, APIError, RateLimitError, APITimeoutError

from app.config.settings import settings

logger = logging.getLogger(__name__)

# 最大重试次数
MAX_RETRIES = 3
# 重试间隔基数（秒）
RETRY_BASE_DELAY = 2.0

# DeepSeek 推荐的 SQL 生成 system prompt
SQL_GENERATION_SYSTEM_PROMPT = """你是一个专业的 SQL 查询生成助手。你需要根据用户的问题、数据库表结构(DDL)、业务文档和SQL示例，
生成准确、高效的 MySQL SQL 查询语句。

规则：
1. 只返回 SELECT 查询语句，禁止 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE 等写操作
2. 只使用给定 DDL 中存在的表和列名
3. 使用中文别名(as)使结果可读
4. 对于时间相关的查询，注意使用正确的日期函数
5. 使用 LIMIT 限制返回行数（默认不超过1000行）
6. 避免使用子查询，优先使用 JOIN
7. 只返回纯 SQL 语句，不要有任何解释、注释或 markdown 格式
8. 如果问题无法用 SQL 回答，返回一句话说明原因"""

SQL_EXPLAIN_SYSTEM_PROMPT = """你是一个 SQL 查询解释助手。请用通俗易懂的中文解释以下 SQL 查询的含义。
说明查询的目的、涉及的表、关键条件和预期结果。"""

SQL_SUMMARY_SYSTEM_PROMPT = """你是一个数据分析助手。用户提出了一个数据问题，系统执行 SQL 后返回了查询结果。
请用 1-3 句话，用通俗易懂的中文总结这些数据的核心结论（如总量、趋势、排名、占比等），
不要复述 SQL，不要罗列全部原始数据。如果数据为空，请说明未查到相关数据。"""

SQL_CORRECT_SYSTEM_PROMPT = """你是一个 SQL 调试助手。以下是生成 SQL 时出错的情况。
请根据错误信息、错误类型提示和原始问题修正 SQL。

规则：
1. 只返回修正后的纯 SQL 语句，不要有任何解释或 markdown 格式
2. 只使用数据库 DDL 中真实存在的表和列
3. 如果是表名/列名错误，请参考已有 DDL 修正
4. 如果是语法错误，请修正语法后返回
5. 保持 SELECT 查询语义与原问题一致"""


class LLMService:
    """LLM 服务封装，支持 DeepSeek-V3 / OpenAI 兼容接口"""

    def __init__(self) -> None:
        self._client: Optional[AsyncOpenAI] = None

    @property
    def client(self) -> AsyncOpenAI:
        """延迟初始化 OpenAI 客户端"""
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
                timeout=settings.LLM_TIMEOUT_SECONDS,
            )
        return self._client

    async def _call_llm(
        self,
        system_prompt: str,
        user_message: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """调用 LLM，带重试机制

        Args:
            system_prompt: 系统提示词
            user_message: 用户消息
            temperature: 温度参数，默认使用配置值
            max_tokens: 最大 token 数

        Returns:
            LLM 响应文本

        Raises:
            LLMException: LLM 调用失败
        """
        temp = temperature if temperature is not None else settings.LLM_TEMPERATURE
        tokens = max_tokens or settings.LLM_MAX_TOKENS

        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                start_time = time.perf_counter()
                logger.info(
                    "LLM call attempt %d/%d, model=%s, prompt_len=%d",
                    attempt, MAX_RETRIES, settings.LLM_MODEL, len(user_message),
                )

                response = await self.client.chat.completions.create(
                    model=settings.LLM_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=temp,
                    max_tokens=tokens,
                )

                elapsed = (time.perf_counter() - start_time) * 1000
                content = response.choices[0].message.content or ""
                usage = response.usage

                logger.info(
                    "LLM call success: elapsed=%.0fms, tokens_in=%d, tokens_out=%d",
                    elapsed,
                    usage.prompt_tokens if usage else 0,
                    usage.completion_tokens if usage else 0,
                )

                return content.strip()

            except (APITimeoutError, RateLimitError) as e:
                last_error = e
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "LLM call attempt %d failed (%s), retrying in %.1fs...",
                    attempt, type(e).__name__, delay,
                )
                if attempt < MAX_RETRIES:
                    import asyncio
                    await asyncio.sleep(delay)
                else:
                    raise LLMException(f"LLM 调用超时或限流: {e}") from e

            except APIError as e:
                last_error = e
                logger.error("LLM API error: %s", e)
                raise LLMException(f"LLM API 错误: {e}") from e

        raise LLMException(f"LLM 调用失败（已重试{MAX_RETRIES}次）: {last_error}")

    async def generate_sql(
        self,
        question: str,
        ddl: str = "",
        documentation: str = "",
        sql_examples: str = "",
    ) -> str:
        """根据自然语言问题生成 SQL 查询

        Args:
            question: 用户的自然语言问题
            ddl: 数据库 DDL 语句
            documentation: 业务文档
            sql_examples: SQL 示例（问题-SQL对）

        Returns:
            生成的 SQL 语句
        """
        context_parts = []

        if ddl:
            context_parts.append(f"## 数据库表结构 (DDL)\n{ddl}")

        if documentation:
            context_parts.append(f"## 业务文档\n{documentation}")

        if sql_examples:
            context_parts.append(f"## SQL 示例\n{sql_examples}")

        context = "\n\n".join(context_parts) if context_parts else "暂无上下文信息"

        user_message = f"{context}\n\n## 用户问题\n{question}"

        logger.info("generate_sql: question='%.100s...'", question)
        sql = await self._call_llm(SQL_GENERATION_SYSTEM_PROMPT, user_message)
        logger.info("generate_sql: generated SQL='%.200s...'", sql)
        return sql

    async def explain_sql(self, sql: str) -> str:
        """解释 SQL 查询的含义

        Args:
            sql: SQL 查询语句

        Returns:
            通俗易懂的中文解释
        """
        user_message = f"请解释以下 SQL 查询：\n\n{sql}"
        logger.info("explain_sql: SQL='%.100s...'", sql)
        explanation = await self._call_llm(SQL_EXPLAIN_SYSTEM_PROMPT, user_message)
        return explanation

    async def correct_sql(
        self,
        sql: str,
        error_message: str,
        correction_hint: str = "",
        ddl: str = "",
    ) -> str:
        """根据错误信息修正 SQL

        Args:
            sql: 原始 SQL 语句
            error_message: 数据库返回的错误信息
            correction_hint: 错误分类器给出的针对性修正提示
            ddl: 数据库表结构（可选，帮助 LLM 选择正确表/列）

        Returns:
            修正后的 SQL 语句
        """
        parts = [
            f"原始 SQL 查询：\n{sql}",
            f"执行错误信息：\n{error_message}",
        ]
        if correction_hint:
            parts.append(f"错误类型与修正建议：\n{correction_hint}")
        if ddl:
            parts.append(f"可用表结构参考（DDL）：\n{ddl}")
        parts.append("请修正以上 SQL 查询语句。")

        user_message = "\n\n".join(parts)
        logger.info("correct_sql: error='%.100s...', hint='%.50s...'",
                    error_message, correction_hint)
        corrected = await self._call_llm(SQL_CORRECT_SYSTEM_PROMPT, user_message)
        logger.info("correct_sql: corrected='%.200s...'", corrected)
        return corrected

    async def generate_summary(
        self,
        question: str,
        sql: str,
        data: list[dict[str, Any]],
        columns: list[str],
    ) -> str:
        """根据查询结果生成自然语言摘要

        Args:
            question: 用户的自然语言问题
            sql: 执行的 SQL 语句
            data: 查询结果行列表
            columns: 列名列表

        Returns:
            str: 中文摘要（1-3 句）
        """
        # 为避免上下文过大，最多取样 10 行
        sample = data[:10]
        user_message = (
            f"用户问题：{question}\n\n"
            f"执行 SQL：{sql}\n\n"
            f"返回列：{columns}\n\n"
            f"返回数据（最多 10 行）：\n{sample}\n\n"
            f"数据总行数：{len(data)}\n\n"
            f"请总结核心结论。"
        )
        summary = await self._call_llm(SQL_SUMMARY_SYSTEM_PROMPT, user_message, temperature=0.3)
        logger.info("generate_summary: '%.100s...'", summary)
        return summary


class LLMException(Exception):
    """LLM 服务异常"""

    pass


# 全局 LLM 服务实例
llm_service = LLMService()
