"""会议结束后的私有纪要文档，不包含录音和设备路径。"""

from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database.base import Base
from app.core.database.mixins import now


class MeetingArchive(Base):
    __tablename__ = "meeting_archives"
    __table_args__ = (
        UniqueConstraint("meeting_id", name="uq_meeting_archive_meeting"),
    )
    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE")
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    speaker_identity_revision: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    title: Mapped[str] = mapped_column(String(240))
    markdown: Mapped[str] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )
