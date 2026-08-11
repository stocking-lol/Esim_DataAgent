"""
Mini Agent Runtime — 从零实现的数据 Agent 底座
================================================

背景
----
项目生产核心 NL2SQL 使用 Vanna 2.0 Agent。为论证「Agent 架构 vs 纯 LLM 直出」
的技术选型，并证明"不依赖 Vanna 也能从零实现 Agent 核心"，这里提供一个
**轻量自研 Agent Runtime**（约 1000 行），参考 Vanna 的抽象思想，但不依赖 Vanna：

  - ToolRegistry : 工具注册中心（名称/描述/访问组/回调）
  - SqlTool      : SQL 执行工具，内置安全钩子（fail-closed 网关 + RLS + 超时）
  - RAGRetriever : 语义检索（复用项目 ChromaDB 训练数据）
  - AgentMemory  : 对话记忆（环形缓冲）
  - MiniAgentRuntime : 编排循环「检索 → 生成 → 执行 → 错误反馈 → 再生成」

与 Vanna 的差异（面试要点）：
  - 更薄：只保留 NL2SQL 所需的最小闭环，无 UI/插件体系等外围。
  - 更安全：工具执行前强制过项目自研安全网关（fail-closed），Vanna 默认无此能力。
  - 可离线评估：SqlTool 支持 dry_run（sqlglot 模拟执行），不连 DB 即可跑 54 题评估。
"""

from app.core.mini_agent.tools import ToolRegistry, ToolSpec, ToolResult, SqlTool
from app.core.mini_agent.rag import RAGRetriever
from app.core.mini_agent.memory import AgentMemory
from app.core.mini_agent.runtime import (
    AgentConfig,
    AgentResponse,
    MiniAgentRuntime,
    build_default_runtime,
)
from app.core.mini_agent.naive import NaiveNL2SQL

__all__ = [
    "ToolRegistry",
    "ToolSpec",
    "ToolResult",
    "SqlTool",
    "RAGRetriever",
    "AgentMemory",
    "AgentConfig",
    "AgentResponse",
    "MiniAgentRuntime",
    "build_default_runtime",
    "NaiveNL2SQL",
]
