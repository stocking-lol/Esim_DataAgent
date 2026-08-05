"""
多轮对话管理服务
----------------
提供对话 CRUD 操作和多轮上下文构建功能。
支持从数据库加载历史对话，拼接为 LLM 上下文，
实现"上次查的数据再按地区分组"等追问场景。

核心方法:
  - create_conversation: 创建新对话
  - get_conversation: 获取对话详情
  - list_conversations: 获取用户对话列表
  - delete_conversation: 删除对话
  - add_message: 添加消息（user/assistant）
  - get_history: 获取对话历史
  - build_context_for_agent: 构建多轮上下文文本
  - save_query_result: 保存查询结果到对话
"""

import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.config.database import get_raw_db
from app.models.conversation import Conversation, ConversationMessage

logger = logging.getLogger(__name__)

# 加载到上下文中的最大历史轮数
MAX_CONTEXT_TURNS = 5


class ConversationService:
    """多轮对话管理服务"""

    def __init__(self, db: Optional[Session] = None):
        self._db = db

    @property
    def db(self) -> Session:
        """获取数据库会话（懒加载）"""
        if self._db is None:
            self._db = get_raw_db()
        return self._db

    def create_conversation(
        self,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Conversation:
        """创建新对话

        Args:
            user_id: 用户ID
            username: 用户名
            title: 对话标题（可选，自动生成）

        Returns:
            Conversation: 创建的对话对象
        """
        conv_id = str(uuid.uuid4())
        if not title:
            title = f"对话 {datetime.now().strftime('%m-%d %H:%M')}"

        conv = Conversation(
            id=conv_id,
            user_id=user_id,
            username=username,
            title=title,
            message_count=0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self.db.add(conv)
        self.db.commit()
        self.db.refresh(conv)
        logger.info("Created conversation: id=%s, user=%s", conv_id, username)
        return conv

    def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        """获取对话详情（不含消息）"""
        return self.db.query(Conversation).filter(
            Conversation.id == conversation_id
        ).first()

    def get_conversation_with_messages(
        self, conversation_id: str
    ) -> Optional[dict]:
        """获取对话详情（含所有消息）

        Returns:
            dict: {conversation: {...}, messages: [...]}
        """
        conv = self.get_conversation(conversation_id)
        if not conv:
            return None

        messages = (
            self.db.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.created_at.asc())
            .all()
        )

        return {
            "conversation": conv.to_dict(),
            "messages": [m.to_dict() for m in messages],
        }

    def list_conversations(
        self,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        """获取用户的对话列表

        Args:
            user_id: 用户ID（可选筛选）
            username: 用户名（可选筛选）
            limit: 返回条数
            offset: 偏移量

        Returns:
            list[dict]: 对话列表
        """
        query = self.db.query(Conversation)

        if user_id is not None:
            query = query.filter(Conversation.user_id == user_id)
        elif username:
            query = query.filter(Conversation.username == username)

        query = query.order_by(desc(Conversation.updated_at))
        conversations = query.offset(offset).limit(limit).all()

        return [c.to_dict() for c in conversations]

    def delete_conversation(self, conversation_id: str) -> bool:
        """删除对话（级联删除消息）

        Returns:
            bool: 是否删除成功
        """
        conv = self.get_conversation(conversation_id)
        if not conv:
            return False

        self.db.delete(conv)
        self.db.commit()
        logger.info("Deleted conversation: %s", conversation_id)
        return True

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        generated_sql: Optional[str] = None,
        sql_status: Optional[str] = None,
        row_count: Optional[int] = None,
        execution_time_ms: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> Optional[ConversationMessage]:
        """添加消息到对话

        Args:
            conversation_id: 对话ID
            role: 消息角色 (user/assistant/system)
            content: 消息内容
            generated_sql: 生成的SQL（assistant 消息可选）
            sql_status: SQL执行状态
            row_count: 返回行数
            execution_time_ms: 执行耗时
            error_message: 错误信息

        Returns:
            ConversationMessage: 创建的消息对象，对话不存在返回 None
        """
        conv = self.get_conversation(conversation_id)
        if not conv:
            return None

        msg = ConversationMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            generated_sql=generated_sql,
            sql_status=sql_status,
            row_count=row_count,
            execution_time_ms=execution_time_ms,
            error_message=error_message,
            created_at=datetime.utcnow(),
        )
        self.db.add(msg)

        # 更新对话元数据
        conv.message_count = (conv.message_count or 0) + 1
        conv.last_message_at = datetime.utcnow()
        conv.updated_at = datetime.utcnow()

        # 如果第一条用户消息且标题是默认的，用消息内容更新标题
        if role == "user" and conv.message_count == 1:
            conv.title = content[:50] + ("..." if len(content) > 50 else "")

        self.db.commit()
        self.db.refresh(msg)
        logger.debug(
            "Added message to %s: role=%s, sql=%s",
            conversation_id, role, "yes" if generated_sql else "no",
        )
        return msg

    def get_history(
        self,
        conversation_id: str,
        limit: int = MAX_CONTEXT_TURNS * 2,
    ) -> list[ConversationMessage]:
        """获取对话历史（最近 N 条消息）

        Args:
            conversation_id: 对话ID
            limit: 最大消息数（默认最近 5 轮 = 10 条）

        Returns:
            list[ConversationMessage]: 按时间正序排列的消息列表
        """
        messages = (
            self.db.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id == conversation_id)
            .order_by(desc(ConversationMessage.created_at))
            .limit(limit)
            .all()
        )
        messages.reverse()  # 反转为正序
        return messages

    def build_context_for_agent(
        self,
        conversation_id: str,
        current_question: str,
        max_turns: int = MAX_CONTEXT_TURNS,
    ) -> str:
        """构建多轮对话上下文文本

        从数据库加载最近的对话历史，拼接为结构化文本，
        供 LLM 理解追问意图（如"改成按月统计"、"上次查的数据再按地区分组"）。

        格式示例:
            === 对话历史 ===
            [用户]: 本月各套餐销量
            [助手SQL]: SELECT p.name, COUNT(o.id) as sales ... GROUP BY p.name
            [助手]: 查询完成，共返回5行数据。

            [用户]: 其中漫游包占比多少
            === 当前问题 ===
            其中漫游包占比多少

        Args:
            conversation_id: 对话ID
            current_question: 当前用户问题
            max_turns: 最多加载的对话轮数

        Returns:
            str: 上下文文本。无历史时返回空字符串。
        """
        messages = self.get_history(
            conversation_id,
            limit=max_turns * 2,  # 每轮 2 条消息
        )

        if not messages:
            return ""

        context_parts = ["=== 对话历史 ==="]
        for msg in messages:
            if msg.role == "user":
                context_parts.append(f"[用户]: {msg.content}")
            elif msg.role == "assistant":
                if msg.generated_sql:
                    context_parts.append(f"[助手SQL]: {msg.generated_sql}")
                if msg.content:
                    # 只取前 200 字，避免上下文过长
                    summary = msg.content[:200]
                    context_parts.append(f"[助手]: {summary}")
                if msg.row_count is not None:
                    context_parts.append(f"[结果]: 返回 {msg.row_count} 行数据")

        context_parts.append("")
        context_parts.append("=== 当前问题 ===")
        context_parts.append(current_question)

        return "\n".join(context_parts)

    def save_query_turn(
        self,
        conversation_id: str,
        question: str,
        sql: str,
        data_summary: str,
        row_count: int,
        execution_time_ms: float,
        sql_status: str = "success",
        error_message: Optional[str] = None,
    ) -> None:
        """保存一轮完整的查询问答到对话历史

        一次性写入 user 消息和 assistant 消息。

        Args:
            conversation_id: 对话ID
            question: 用户问题
            sql: 生成的SQL
            data_summary: 结果摘要文本
            row_count: 返回行数
            execution_time_ms: 执行耗时(ms)
            sql_status: SQL执行状态 (success/blocked/error)
            error_message: 错误信息
        """
        # 保存用户消息
        self.add_message(
            conversation_id=conversation_id,
            role="user",
            content=question,
        )

        # 保存助手消息
        self.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=data_summary or ("查询完成" if sql_status == "success" else "查询失败"),
            generated_sql=sql,
            sql_status=sql_status,
            row_count=row_count,
            execution_time_ms=int(execution_time_ms),
            error_message=error_message,
        )

    def close(self):
        """关闭数据库会话"""
        if self._db is not None:
            self._db.close()
            self._db = None


# 全局服务实例（使用独立会话，不依赖 FastAPI 依赖注入）
conversation_service = ConversationService()
