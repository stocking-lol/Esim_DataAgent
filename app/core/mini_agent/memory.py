"""
Mini Agent Runtime - 对话记忆层
===============================
自研实现：环形缓冲的对话记忆，记录 user/assistant/tool 三类消息，
供多轮会话与评估复盘使用。

设计要点：
  - 容量上限（默认 20 条），防止上下文无限膨胀（面试可讲：Agent 记忆
    必须限制容量，否则 token 成本与延迟都会失控）；
  - 与 Vanna AgentMemory 的差异：这里只做纯内存实现，无持久化，
    生产可替换为 Redis/数据库版本（接口一致）。
"""

import logging
import time
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AgentMemory:
    """环形缓冲对话记忆"""

    def __init__(self, capacity: int = 20) -> None:
        self._capacity = max(1, capacity)
        self._messages: deque[dict[str, Any]] = deque(maxlen=self._capacity)

    def add(self, role: str, content: str, metadata: Optional[dict] = None) -> None:
        """记录一条消息

        Args:
            role: "user" | "assistant" | "tool"
            content: 消息内容
            metadata: 附加元数据（如 SQL、耗时）
        """
        self._messages.append({
            "role": role,
            "content": content,
            "ts": time.time(),
            "metadata": metadata or {},
        })
        logger.debug("Memory add: role=%s len=%d", role, len(self._messages))

    def add_user(self, content: str) -> None:
        self.add("user", content)

    def add_assistant(self, content: str, metadata: Optional[dict] = None) -> None:
        self.add("assistant", content, metadata)

    def add_tool(self, content: str, metadata: Optional[dict] = None) -> None:
        self.add("tool", content, metadata)

    def get_history(self) -> list[dict[str, Any]]:
        """返回全部消息（按时间顺序）"""
        return list(self._messages)

    def get_recent(self, n: int = 5) -> list[dict[str, Any]]:
        """返回最近 n 条消息"""
        items = list(self._messages)
        return items[-n:]

    def format_for_prompt(self, n: int = 5) -> str:
        """把最近 n 条消息格式化为 prompt 片段（多轮会话上下文）"""
        parts = []
        for msg in self.get_recent(n):
            prefix = {"user": "用户", "assistant": "助手", "tool": "工具"}.get(
                msg["role"], msg["role"])
            parts.append(f"{prefix}: {msg['content']}")
        return "\n".join(parts)

    def clear(self) -> None:
        self._messages.clear()

    @property
    def size(self) -> int:
        return len(self._messages)

    @property
    def capacity(self) -> int:
        return self._capacity
