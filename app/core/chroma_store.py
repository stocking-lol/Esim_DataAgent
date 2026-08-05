"""
ChromaDB 训练数据存储层
----------------------
管理 Vanna RAG 训练数据的持久化存储，支持 DDL、业务文档、SQL 示例的向量检索。

架构：
  - 3 个 ChromaDB Collection：ddl / documentation / sql_examples
  - 使用 all-MiniLM-L6-v2 作为 embedding 模型（通过 chromadb 内置支持）
  - 持久化到本地目录，数据在服务重启后保留
"""

import json
import logging
import uuid
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.utils import embedding_functions
from chromadb.api.types import EmbeddingFunction, Documents, Embeddings

from app.config.settings import settings

logger = logging.getLogger(__name__)

# ============================================================
# 数据模型
# ============================================================

@dataclass
class TrainingRecord:
    """训练数据记录"""
    id: str
    type: str                          # "ddl" | "documentation" | "sql"
    content: str                       # 训练内容
    metadata: dict = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


# ============================================================
# 简易关键词 Embedding（备选方案）
# ============================================================

class SimpleKeywordEmbedding(EmbeddingFunction):
    """基于关键词的简易 Embedding 函数

    当 ONNX 模型下载失败时作为备选方案。
    使用简单的 TF-IDF 风格关键词匹配，不需要任何模型下载。

    原理：
    - 从所有已训练文档中提取关键词
    - 构建词袋特征向量（稀疏但有效）
    - 检索时基于关键词重叠计算相似度
    """

    def __init__(self):
        self._vocabulary: dict[str, int] = {}
        self._dim = 128  # 固定维度

    def _tokenize(self, text: str) -> dict[str, float]:
        """简单分词 + TF权重"""
        import re
        # 中英文分词：英文按空格，中文按字符+常见词
        tokens = re.findall(r'[\u4e00-\u9fff]{1,2}|[a-zA-Z_]{2,}|[0-9]+', text.lower())
        freq: dict[str, float] = {}
        for t in tokens:
            freq[t] = freq.get(t, 0) + 1
        total = len(tokens) or 1
        return {t: c / total for t, c in freq.items()}

    def __call__(self, input: Documents) -> Embeddings:
        """生成简单词袋向量"""
        result: list[list[float]] = []
        for text in input:
            tokens = self._tokenize(text)
            vec = [0.0] * self._dim
            for token, weight in tokens.items():
                idx = hash(token) % self._dim
                vec[idx] += weight
            # 归一化
            norm = max(1e-8, sum(v * v for v in vec) ** 0.5)
            result.append([v / norm for v in vec])
        return result


# ============================================================
# ChromaDB 训练存储
# ============================================================

class ChromaTrainingStore:
    """基于 ChromaDB 的训练数据存储

    管理三个 collection：
      - ddl:             数据库 DDL 语句
      - documentation:   业务文档、术语说明
      - sql_examples:    SQL 查询示例（question -> SQL 映射）

    Usage:
        store = ChromaTrainingStore()
        await store.initialize()
        store.add_ddl("CREATE TABLE users (...)")
        results = store.search("本月新增用户数", n_results=3)
    """

    COLLECTION_NAMES = ["ddl", "documentation", "sql_examples"]

    def __init__(self, persist_dir: Optional[str] = None):
        """
        Args:
            persist_dir: ChromaDB 持久化目录，默认从 settings 读取
        """
        self._persist_dir = persist_dir or settings.CHROMADB_PERSIST_DIR
        self._client: Optional[chromadb.PersistentClient] = None
        self._embedding_fn = None
        self._collections: dict[str, chromadb.Collection] = {}
        self._initialized = False

    # --- 初始化 ---

    async def initialize(self) -> None:
        """初始化 ChromaDB 客户端和 Collection

        Raises:
            RuntimeError: 初始化失败（通常是 embedding 模型下载失败）
        """
        if self._initialized:
            logger.info("ChromaTrainingStore already initialized.")
            return

        persist_path = Path(self._persist_dir)
        persist_path.mkdir(parents=True, exist_ok=True)

        logger.info("Initializing ChromaDB at: %s", self._persist_dir)

        # 创建 embedding 函数（多级 fallback）
        # 优先级: ONNX Default → SentenceTransformer → SimpleKeyword
        embedding_fn = self._create_embedding_function()
        if embedding_fn is None:
            raise RuntimeError(
                "无法创建任何 Embedding 函数。\n"
                "请手动下载 all-MiniLM-L6-v2 模型：\n"
                "  1. 访问 https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2\n"
                "  2. 下载所有文件到 ~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/\n"
                "  3. 重新启动服务"
            )
        self._embedding_fn = embedding_fn
        logger.info("Embedding function: %s", type(embedding_fn).__name__)

        try:
            # 创建 PersistentClient
            self._client = chromadb.PersistentClient(
                path=str(persist_path),
                settings=ChromaSettings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                ),
            )

            # 获取或创建 collections（使用 get_or_create 避免重复创建错误）
            for name in self.COLLECTION_NAMES:
                collection = self._client.get_or_create_collection(
                    name=name,
                    embedding_function=self._embedding_fn,
                    metadata={"hnsw:space": "cosine"},
                )
                self._collections[name] = collection
                logger.info("Collection '%s' ready (%d docs)",
                            name, collection.count())

            self._initialized = True
            logger.info("ChromaTrainingStore initialized successfully "
                        "(ddl=%d, doc=%d, sql=%d)",
                        self._collections["ddl"].count(),
                        self._collections["documentation"].count(),
                        self._collections["sql_examples"].count())

        except Exception as e:
            logger.error("Failed to initialize ChromaDB: %s", e)
            self._initialized = False
            raise RuntimeError(f"ChromaDB 初始化失败: {e}") from e

    # --- CRUD 操作 ---

    def add_ddl(self, ddl: str, table_name: str = "", metadata: Optional[dict] = None) -> str:
        """添加 DDL 训练数据

        Args:
            ddl: CREATE TABLE 等 DDL 语句
            table_name: 表名（用于 metadata）
            metadata: 额外元数据

        Returns:
            str: 记录 ID
        """
        return self._add(
            collection_name="ddl",
            content=ddl,
            metadata={"table_name": table_name, **(metadata or {})},
        )

    def add_documentation(self, documentation: str, topic: str = "",
                          metadata: Optional[dict] = None) -> str:
        """添加业务文档

        Args:
            documentation: 业务文档/术语说明
            topic: 主题标签（如 "ARPU", "Profile激活"）
            metadata: 额外元数据

        Returns:
            str: 记录 ID
        """
        return self._add(
            collection_name="documentation",
            content=documentation,
            metadata={"topic": topic, **(metadata or {})},
        )

    def add_sql_example(self, question: str, sql: str,
                        metadata: Optional[dict] = None) -> str:
        """添加 SQL 示例（question -> SQL 映射）

        Args:
            question: 自然语言问题
            sql: 对应的 SQL 查询
            metadata: 额外元数据

        Returns:
            str: 记录 ID
        """
        return self._add(
            collection_name="sql_examples",
            content=f"问题: {question}\nSQL: {sql}",
            metadata={"question": question, "sql": sql, **(metadata or {})},
        )

    def _add(self, collection_name: str, content: str,
             metadata: Optional[dict] = None) -> str:
        """通用添加方法"""
        self._ensure_initialized()

        record_id = f"{collection_name}_{uuid.uuid4().hex[:12]}"
        meta = metadata or {}
        meta["created_at"] = time.time()

        collection = self._collections[collection_name]
        collection.add(
            ids=[record_id],
            documents=[content],
            metadatas=[meta],
        )

        logger.debug("Added to '%s': id=%s (total=%d)",
                     collection_name, record_id, collection.count())
        return record_id

    def remove(self, record_type: str, record_id: str) -> bool:
        """删除指定训练数据

        Args:
            record_type: "ddl" | "documentation" | "sql"
            record_id: 记录 ID

        Returns:
            bool: 删除成功返回 True
        """
        self._ensure_initialized()

        if record_type not in self._collections:
            logger.warning("Unknown collection type: %s", record_type)
            return False

        collection = self._collections[record_type]
        try:
            collection.delete(ids=[record_id])
            logger.info("Deleted record '%s' from '%s'", record_id, record_type)
            return True
        except Exception as e:
            logger.error("Failed to delete record '%s': %s", record_id, e)
            return False

    def get_all(self, record_type: Optional[str] = None) -> list[TrainingRecord]:
        """获取所有训练数据

        Args:
            record_type: 可选过滤类型，None 表示返回全部

        Returns:
            list[TrainingRecord]: 训练数据列表
        """
        self._ensure_initialized()

        results = []

        collections_to_query = (
            [record_type] if record_type and record_type in self._collections
            else self.COLLECTION_NAMES
        )

        for name in collections_to_query:
            collection = self._collections[name]
            if collection.count() == 0:
                continue

            data = collection.get()
            for i, doc_id in enumerate(data["ids"]):
                meta = data.get("metadatas", [None])[i] or {}
                results.append(TrainingRecord(
                    id=doc_id,
                    type=name,
                    content=data["documents"][i] if data.get("documents") else "",
                    metadata=meta,
                    created_at=meta.get("created_at", 0),
                ))

        return results

    def count_by_type(self) -> dict[str, int]:
        """按类型统计训练数据数量"""
        self._ensure_initialized()
        return {name: self._collections[name].count()
                for name in self.COLLECTION_NAMES}

    # --- 向量检索 ---

    def search(self, query: str, n_results: int = 5,
               collection_filter: Optional[list[str]] = None
               ) -> dict[str, list[TrainingRecord]]:
        """检索与查询最相关的训练数据

        Args:
            query: 查询文本（自然语言问题）
            n_results: 每个 collection 返回的最大结果数
            collection_filter: 可选，限定搜索的 collection 列表

        Returns:
            dict: {collection_name: [TrainingRecord, ...]}
        """
        self._ensure_initialized()

        results: dict[str, list[TrainingRecord]] = {}
        collections_to_search = (
            collection_filter if collection_filter
            else self.COLLECTION_NAMES
        )

        for name in collections_to_search:
            if name not in self._collections:
                continue
            collection = self._collections[name]
            if collection.count() == 0:
                results[name] = []
                continue

            # 如果使用关键词 embedding，直接用关键词搜索
            if isinstance(self._embedding_fn, SimpleKeywordEmbedding):
                results[name] = self._keyword_search(query, name, n_results)
                continue

            try:
                query_results = collection.query(
                    query_texts=[query],
                    n_results=min(n_results, collection.count()),
                )

                records = []
                ids = query_results.get("ids", [[]])[0]
                docs = query_results.get("documents", [[]])[0]
                metas = query_results.get("metadatas", [[]])[0]
                distances = query_results.get("distances", [[]])[0]

                for i, doc_id in enumerate(ids):
                    meta = metas[i] if metas and i < len(metas) else {}
                    records.append(TrainingRecord(
                        id=doc_id,
                        type=name,
                        content=docs[i] if docs and i < len(docs) else "",
                        metadata=meta,
                        created_at=meta.get("created_at", 0),
                    ))

                results[name] = records
            except Exception as e:
                logger.error("Search failed for collection '%s': %s", name, e)
                results[name] = []

        return results

    def retrieve_context(self, question: str, max_items: int = 5) -> str:
        """检索相关训练数据并拼接为 LLM 上下文文本

        这是与 Vanna Agent 集成的关键方法：
        1. 从 3 个 collection 中各检索 top-N 最相关内容
        2. 拼接为结构化文本
        3. 返回给 LLM 作为上下文

        Args:
            question: 用户问题
            max_items: 每个 collection 检索的最大数量

        Returns:
            str: 拼接后的上下文文本
        """
        self._ensure_initialized()

        search_results = self.search(question, n_results=max_items)
        parts: list[str] = []

        # DDL 上下文
        ddl_records = search_results.get("ddl", [])
        if ddl_records:
            parts.append("## 数据库表结构 (DDL)")
            for r in ddl_records:
                table = r.metadata.get("table_name", "")
                label = f" (表: {table})" if table else ""
                parts.append(f"### {r.id}{label}\n```sql\n{r.content}\n```")

        # 文档上下文
        doc_records = search_results.get("documentation", [])
        if doc_records:
            parts.append("## 业务知识文档")
            for r in doc_records:
                topic = r.metadata.get("topic", "")
                label = f"**主题: {topic}**\n\n" if topic else ""
                parts.append(f"{label}{r.content}")

        # SQL 示例上下文
        sql_records = search_results.get("sql_examples", [])
        if sql_records:
            parts.append("## SQL 查询示例")
            for r in sql_records:
                q = r.metadata.get("question", "")
                s = r.metadata.get("sql", "")
                if q and s:
                    parts.append(f"**Q**: {q}\n**SQL**:\n```sql\n{s}\n```")

        context = "\n\n".join(parts) if parts else ""
        if context:
            logger.debug("Retrieved context for question '%s': %d chars",
                         question[:50], len(context))
        return context

    # --- 批量操作 ---

    def batch_add_ddl(self, ddl_list: list[str],
                      table_names: Optional[list[str]] = None) -> list[str]:
        """批量添加 DDL"""
        ids = []
        for i, ddl in enumerate(ddl_list):
            table = table_names[i] if table_names and i < len(table_names) else ""
            ids.append(self.add_ddl(ddl, table_name=table))
        return ids

    def batch_add_documentation(self, docs: list[str],
                                topics: Optional[list[str]] = None) -> list[str]:
        """批量添加业务文档"""
        ids = []
        for i, doc in enumerate(docs):
            topic = topics[i] if topics and i < len(topics) else ""
            ids.append(self.add_documentation(doc, topic=topic))
        return ids

    def batch_add_sql_examples(self, examples: list[dict]) -> list[str]:
        """批量添加 SQL 示例

        Args:
            examples: [{"question": "...", "sql": "..."}, ...]
        """
        ids = []
        for ex in examples:
            ids.append(self.add_sql_example(
                question=ex.get("question", ""),
                sql=ex.get("sql", ""),
                metadata=ex.get("metadata"),
            ))
        return ids

    def clear_all(self) -> None:
        """清空所有训练数据（危险操作，仅用于开发/重置）"""
        self._ensure_initialized()
        logger.warning("Clearing ALL training data from ChromaDB!")

        for name in self.COLLECTION_NAMES:
            if name in self._collections:
                try:
                    self._client.delete_collection(name)
                    self._collections[name] = self._client.create_collection(
                        name=name,
                        embedding_function=self._embedding_fn,
                        metadata={"hnsw:space": "cosine"},
                    )
                    logger.info("Cleared collection '%s'", name)
                except Exception as e:
                    logger.error("Failed to clear collection '%s': %s", name, e)

    def is_empty(self) -> bool:
        """检查是否没有任何训练数据"""
        self._ensure_initialized()
        return all(
            self._collections[name].count() == 0
            for name in self.COLLECTION_NAMES
        )

    # --- 资源管理 ---

    @staticmethod
    def _create_embedding_function() -> EmbeddingFunction | None:
        """创建 Embedding 函数，支持多级 fallback

        优先级:
          1. ONNX DefaultEmbeddingFunction（如已缓存）
          2. SimpleKeywordEmbedding（纯关键词，立即可用）

        SentenceTransformer 也可能触发大模型下载，因此暂不自动尝试。
        ONNX 模型正在后台下载中（~80MB），完成后重启服务即可自动切换。
        """
        import os

        # Level 1: ONNX Default（仅当已缓存时使用）
        onnx_cache = os.path.expanduser(
            "~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx"
        )
        if os.path.isdir(onnx_cache) and os.path.isfile(
            os.path.join(onnx_cache, "model.onnx")
        ):
            try:
                fn = embedding_functions.DefaultEmbeddingFunction()
                logger.info("Using ONNX DefaultEmbeddingFunction (cached)")
                return fn
            except Exception as e:
                logger.warning("Cached ONNX model load failed: %s", e)

        # Level 2: 关键词 fallback（立即可用，无需下载）
        logger.info(
            "ONNX model not cached. Using keyword-based search fallback.\n"
            "Tip: ONNX model is downloading in background automatically.\n"
            "Or manually: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"
        )
        print("[CHROMADB] INFO: Keyword-based embedding (ONNX model downloading in bg)")
        print("[CHROMADB] For better results, wait for model download or download manually:")
        print("[CHROMADB]   https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2")
        print("[CHROMADB]   Place in: ~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/")
        return SimpleKeywordEmbedding()

    def _keyword_search(
        self, query: str, collection_name: str, n_results: int
    ) -> list[TrainingRecord]:
        """关键词兜底搜索（当向量检索失效时）

        基于简单的关键词重叠评分，不依赖向量模型。
        """
        import re

        collection = self._collections[collection_name]
        data = collection.get()
        if not data.get("ids"):
            return []

        # 提取查询中的关键词
        query_lower = query.lower()
        query_tokens = set(re.findall(
            r'[\u4e00-\u9fff]{1,3}|[a-zA-Z_]{2,}|[0-9]+', query_lower
        ))

        # 对所有文档评分
        scored: list[tuple[float, TrainingRecord]] = []
        for i, doc_id in enumerate(data["ids"]):
            doc = data["documents"][i] if data.get("documents") else ""
            doc_lower = doc.lower()

            # 关键词重叠评分
            doc_tokens = set(re.findall(
                r'[\u4e00-\u9fff]{1,3}|[a-zA-Z_]{2,}|[0-9]+', doc_lower
            ))
            overlap = len(query_tokens & doc_tokens)
            jaccard = overlap / max(len(query_tokens | doc_tokens), 1)

            # 精确子串匹配加分
            substr_bonus = 1.0 if query_lower in doc_lower else 0.0

            # 部分子串匹配
            partial_bonus = sum(
                0.2 for t in query_tokens if t in doc_lower
            ) / max(len(query_tokens), 1)

            score = jaccard * 0.5 + substr_bonus * 0.3 + partial_bonus * 0.2
            if score > 0:
                meta = data.get("metadatas", [{}])[i] or {}
                scored.append((score, TrainingRecord(
                    id=doc_id,
                    type=collection_name,
                    content=doc,
                    metadata=meta,
                    created_at=meta.get("created_at", 0),
                )))

        # 按分数降序排列，取 top-N
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:n_results]]

    async def shutdown(self) -> None:
        """关闭 ChromaDB 客户端"""
        if self._client:
            logger.info("Shutting down ChromaDB client...")
            self._client = None
            self._collections.clear()
            self._initialized = False

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def _ensure_initialized(self) -> None:
        if not self._initialized or self._client is None:
            raise RuntimeError(
                "ChromaTrainingStore 未初始化，请先调用 initialize() 方法。"
                "\n提示：如果是因为 embedding 模型下载失败，请前往 HuggingFace "
                "手动下载 sentence-transformers/all-MiniLM-L6-v2 模型到本地。"
            )


# 全局单例
chroma_store = ChromaTrainingStore()
