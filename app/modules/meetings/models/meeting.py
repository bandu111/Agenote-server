"""会议文本与分析结果；原始录音不进入服务器。"""

from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database.base import Base
from app.core.database.mixins import now


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (
        UniqueConstraint("owner_id", "client_id", name="uq_meetings_owner_client"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    client_id: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(Integer)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    content: Mapped[dict] = mapped_column(JSONB)
    # 服务端识别结果独立存储，客户端正文同步不能覆盖。
    speaker_identity_revision: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    speaker_identities: Mapped[dict] = mapped_column(JSONB, default=dict, server_default='{}')
    analysis_revision: Mapped[int] = mapped_column(Integer, default=-1)
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )
