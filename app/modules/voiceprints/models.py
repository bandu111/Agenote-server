"""Encrypted features only: registration audio never persists in the database."""
from datetime import datetime
from sqlalchemy import ForeignKey, String, Integer, Boolean, LargeBinary, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database.base import Base
from app.core.database.mixins import now


class VoiceprintProfile(Base):
    __tablename__ = 'voiceprint_profiles'
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    template: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    model_version: Mapped[str] = mapped_column(String(150), default='')
    key_id: Mapped[str] = mapped_column(String(50), default='')
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    device: Mapped[dict] = mapped_column(JSONB, default=dict)


class VoiceprintEnrollment(Base):
    __tablename__ = 'voiceprint_enrollments'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    base_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default='recording')
    # Each slot has its own generation: late computation cannot overwrite a re-recording.
    samples: Mapped[dict] = mapped_column(JSONB, default=dict)
    candidate: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    model_version: Mapped[str] = mapped_column(String(150))
    key_id: Mapped[str] = mapped_column(String(50))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class VoiceprintAudit(Base):
    __tablename__ = 'voiceprint_audits'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    action: Mapped[str] = mapped_column(String(40))
    consent_version: Mapped[str] = mapped_column(String(40))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
