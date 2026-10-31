"""内部任务与逐步工具回执，业务效果和回执在同一事务提交。"""
from sqlalchemy import ForeignKey, String, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database.base import Base


class InternalTask(Base):
    __tablename__ = 'internal_tasks'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey('meetings.id', ondelete='CASCADE'))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[dict] = mapped_column(JSONB)


class ToolReceipt(Base):
    __tablename__ = 'tool_receipts'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey('action_runs.id', ondelete='CASCADE'))
    input_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict] = mapped_column(JSONB)
