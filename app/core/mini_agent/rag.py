"""
Mini Agent Runtime - 语义检索层（RAG，自研混合检索）
====================================================
自研封装：复用项目 ChromaDB 训练数据（DDL/文档/SQL 示例），
把「检索 → 拼装上下文」收敛为一个稳定接口，供编排循环调用。

设计要点（面试可讲）：
  1. 与 Vanna 的差异：Vanna 在 Agent 内部自行检索；这里显式暴露
     RAGRetriever.retrieve()，让"有没有 RAG"成为可开关的对比变量
     （三路对比中 naive 路线关闭 RAG，Mini Agent 开启 RAG）。
  2. **混合检索（Hybrid Search）**：本项目 embedding 为 all-MiniLM-L6-v2
     （英文模型），对中文句子语义匹配质量差，容易出现"检索漂移"——
     短中文查询被含大量英文 SQL 关键字的 DDL 抢占相似度，而真正相关的
     中文业务文档/SQL 示例无法召回。为此自研实现关键词加权增强：
        - 向量检索拿语义候选（top-N）
        - 对 sql_examples 按 question 做字符重叠（Jaccard）打分
        - 高分项提升排名 → 解决"问题与示例一字不差却召回不了"
  3. 降级策略：ChromaDB 不可用时返回空字符串，由编排循环决定是否
     用全量 schema 提示词兜底（graceful degradation）。
"""

import logging
import re
from typing import Optional

from app.core.chroma_store import chroma_store

logger = logging.getLogger(__name__)


class RAGRetriever:
    """语义检索器：从 ChromaDB 训练数据中检索与问题相关的上下文"""

    def __init__(self, store=None, max_items: int = 3) -> None:
        self._store = store or chroma_store
        self._max_items = max_items

    @property
    def available(self) -> bool:
        return self._store.is_initialized

    # --- 主入口 ---

    def retrieve(self, question: str, max_items: Optional[int] = None) -> str:
        """检索并拼装上下文文本（混合检索：向量 + 关键词加权）

        Args:
            question: 用户自然语言问题
            max_items: 每个 collection 的检索数量上限

        Returns:
            拼装后的上下文文本；ChromaDB 不可用时返回空字符串
        """
        n = max_items or self._max_items
        try:
            if not self._store.is_initialized:
                logger.debug("RAG unavailable (store not initialized)")
                return ""
            # 取大候选池（向量召回 top-20），再做关键词重排——避免真正相关的
            # 中文示例因英文 embedding 相似度低而被挡在候选之外
            pool = max(n * 8, 20)
            results = self._store.search(question, n_results=pool)
            for coll in ("ddl", "documentation", "sql_examples"):
                records = results.get(coll, [])
                if not records:
                    continue
                reranked = self._rerank(question, records, coll, n)
                if reranked:
                    results[coll] = reranked
            context = self._format_context(results)
            if context:
                logger.debug("RAG retrieved %d chars for '%s'", len(context), question[:40])
            return context
        except Exception as e:
            logger.warning("RAG retrieve failed, degrade to empty context: %s", e)
            return ""

    def search(self, question: str, n_results: int = 3) -> dict:
        """返回结构化检索结果（供调试/可视化）"""
        try:
            if not self._store.is_initialized:
                return {}
            pool = max(n_results * 8, 20)
            results = self._store.search(question, n_results=pool)
            for coll in ("ddl", "documentation", "sql_examples"):
                records = results.get(coll, [])
                if not records:
                    continue
                reranked = self._rerank(question, records, coll, n_results)
                if reranked:
                    results[coll] = reranked
            return results
        except Exception as e:
            logger.warning("RAG search failed: %s", e)
            return {}

    # --- 混合检索核心 ---

    def _rerank(self, question: str, records: list, collection: str, n: int):
        """关键词加权重排：解决中文 embedding 检索漂移

        对每个候选记录做字符重叠（Jaccard）打分：
          - sql_examples  : 用 metadata.question 匹配（最精确）
          - documentation : 用 content 匹配
          - ddl           : 内容为英文，关键词分天然低，基本维持向量序
        当最高分超过阈值（说明存在与问题高度重叠的示例）时按分数重排，
        使"问题与示例几乎一致"的 few-shot 一定能被召回。

        Args:
            question: 用户问题
            records: 向量检索命中的候选记录（已是大候选池）
            collection: collection 名
            n: 返回数量上限

        Returns:
            重排后的记录列表；无需增强时返回 None（保持向量结果）
        """
        if not records:
            return None
        q_tokens = self._tokens(question)
        if not q_tokens:
            return None

        scored = []
        for r in records:
            if collection == "sql_examples":
                text = (r.metadata.get("question", "") or "").strip()
                contains = 1.0 if (text and (text in question or question in text)) else 0.0
            elif collection == "documentation":
                topic = (r.metadata.get("topic", "") or "").strip()
                text = topic + " " + r.content
                # contains 用主题词判断：topic 是 query 的子串 → 强相关（如"漫游"）
                contains = 1.0 if (topic and topic in question) else 0.0
            else:
                text = r.content
                contains = 0.0
            tokens = self._tokens(text)
            jac = (len(q_tokens & tokens) /
                   max(len(q_tokens | tokens), 1))
            # contains（问题包含示例问句/文档主题）是强信号，权重占一半
            scored.append((jac * 0.5 + contains * 0.5, r))

        scored.sort(key=lambda x: -x[0])
        # 仅当存在明显重叠时才重排（避免低相关项乱入）
        if scored and scored[0][0] >= 0.4:
            return [r for _, r in scored[:n]]
        return None

    @staticmethod
    def _tokens(text: str) -> set[str]:
        """中文按单字、英文/数字按词 tokenize（用于字符重叠打分）"""
        t = (text or "").lower()
        return set(re.findall(r"[\u4e00-\u9fff]|[a-z0-9_]{2,}", t))

    # --- 上下文拼装（与 chroma_store.retrieve_context 格式一致） ---

    @staticmethod
    def _format_context(results: dict, max_chars: int = 8000) -> str:
        """拼装上下文，并做总量控制

        优先级：SQL 示例（few-shot，直接影响生成）> 业务文档 > DDL（结构信息，
        可截断）。超限时从 DDL 开始丢弃，避免长上下文稀释 LLM 注意力。
        """
        parts: list[str] = []

        def _append(section: str) -> bool:
            nonlocal parts
            cur = len("\n\n".join(parts))
            if cur + len(section) > max_chars and parts:
                return False
            parts.append(section)
            return True

        sql_records = results.get("sql_examples", [])
        if sql_records:
            sec = ["## SQL 查询示例"]
            for r in sql_records:
                q = r.metadata.get("question", "")
                s = r.metadata.get("sql", "")
                if q and s:
                    sec.append(f"**Q**: {q}\n**SQL**:\n```sql\n{s}\n```")
            _append("\n".join(sec))

        doc_records = results.get("documentation", [])
        if doc_records:
            sec = ["## 业务知识文档"]
            for r in doc_records:
                topic = r.metadata.get("topic", "")
                label = f"**主题: {topic}**\n\n" if topic else ""
                sec.append(f"{label}{r.content}")
            _append("\n".join(sec))

        ddl_records = results.get("ddl", [])
        if ddl_records:
            sec = ["## 数据库表结构 (DDL)"]
            for r in ddl_records:
                table = r.metadata.get("table_name", "")
                label = f" (表: {table})" if table else ""
                sec.append(f"### {r.id}{label}\n```sql\n{r.content}\n```")
            _append("\n".join(sec))

        return "\n\n".join(parts) if parts else ""
