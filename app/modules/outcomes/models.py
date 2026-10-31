"""Separate from executable actions: a report must never become a calendar action."""
from sqlalchemy import ForeignKey, String, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database.base import Base

class OutcomeRun(Base):
    __tablename__ = 'outcome_runs'
    __table_args__ = (UniqueConstraint('owner_id','intent_key',name='uq_outcome_owner_intent'),)
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id',ondelete='CASCADE'),index=True)
    meeting_id: Mapped[str | None] = mapped_column(ForeignKey('meetings.id',ondelete='CASCADE'),index=True,nullable=True)
    intent_key: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer,default=1)
    status: Mapped[str] = mapped_column(String(40),default='queued',index=True)
    content: Mapped[dict] = mapped_column(JSONB)
