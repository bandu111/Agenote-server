"""保存原型快照、LlamaIndex 节点和导入状态，索引不代替权限判断。"""

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database.base import Base


class KnowledgeLibrary(Base):
    __tablename__ = "knowledge_libraries"
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=0)
    snapshot: Mapped[dict] = mapped_column(JSONB)


class DeletedMeetingKnowledge(Base):
    """A user removed this meeting's document; automatic archival must not recreate it."""
    __tablename__ = 'deleted_meeting_knowledge'
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(160), primary_key=True)


class KnowledgeNode(Base):
    __tablename__ = "knowledge_nodes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[str] = mapped_column(String(160), index=True)
    version_id: Mapped[str] = mapped_column(String(160))
    source_hash: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    node: Mapped[dict] = mapped_column(JSONB)


class KnowledgeIndexState(Base):
    __tablename__ = "knowledge_index_states"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "document_id", "version_id", name="uq_knowledge_index_version"
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[str] = mapped_column(String(160))
    version_id: Mapped[str] = mapped_column(String(160))
    source_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40))
