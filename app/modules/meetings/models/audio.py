"""账户隔离的整场录音分卷和分块；注销和会议删除时级联清理。"""
from sqlalchemy import ForeignKey, String, Integer, BigInteger, LargeBinary, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database.base import Base


class MeetingAudio(Base):
    __tablename__ = 'meeting_audio'
    __table_args__ = (UniqueConstraint('meeting_id', 'sha256', name='uq_meeting_audio_hash'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey('meetings.id', ondelete='CASCADE'), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    offset: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String(16), default='uploading')
    metadata_json: Mapped[dict] = mapped_column(JSONB)


class MeetingAudioChunk(Base):
    __tablename__ = 'meeting_audio_chunks'
    audio_id: Mapped[str] = mapped_column(ForeignKey('meeting_audio.id', ondelete='CASCADE'), primary_key=True)
    offset: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
