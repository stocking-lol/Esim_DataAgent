"""
Mini Agent Runtime - 纯 LLM 直出对照组（Non-Agent）
===================================================
三路对比中的「非 Agent」基线：**单次 LLM 调用**生成 SQL。

设计（与 Agent 路线的差异点，面试可讲）：
  - 无检索（RAG）：全量 schema 硬塞进 prompt；
  - 无工具：不执行 SQL、无安全钩子（只产出文本）；
  - 无自愈：出错即失败，不重试。

这对应业界最常见的「调 LLM 写 SQL」做法——用它做对照组，
才能量化「Agent 化（检索 + 工具 + 自愈）」带来的增量价值。
"""

import logging
from typing import Optional

from app.core.llm import LLMService, llm_service
from app.core.mini_agent.runtime import SCHEMA_DDL, extract_sql_block

logger = logging.getLogger(__name__)


class NaiveNL2SQL:
    """纯 LLM 直出 NL2SQL（Non-Agent 对照组）"""

    def __init__(self, llm: Optional[LLMService] = None) -> None:
        self.llm = llm or llm_service

    async def generate(self, question: str) -> str:
        """单次调用生成 SQL（无检索/无工具/无自愈）

        Returns:
            生成的 SQL；失败返回空字符串
        """
        try:
            raw = await self.llm.generate_sql(
                question,
                ddl=SCHEMA_DDL,      # 全量 schema 硬塞（上下文固定，不随问题变化）
                documentation="",
                sql_examples="",
            )
            return extract_sql_block(raw)
        except Exception as e:
            logger.error("Naive NL2SQL failed for '%s': %s", question[:50], e)
            return ""
