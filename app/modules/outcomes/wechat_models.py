"""Private credentials encrypted at rest; identities never come from client claims."""
from sqlalchemy import ForeignKey, String, Integer, Text, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database.base import Base
from app.core.database.mixins import now
from datetime import datetime

class WechatBinding(Base):
    __tablename__='wechat_bindings'
    owner_id: Mapped[str]=mapped_column(ForeignKey('users.id',ondelete='CASCADE'),primary_key=True)
    version: Mapped[int]=mapped_column(Integer,default=1)
    status: Mapped[str]=mapped_column(String(30),default='pending',index=True)
    peer_hash: Mapped[str|None]=mapped_column(String(64),nullable=True,unique=True)
    encrypted: Mapped[str|None]=mapped_column(Text,nullable=True)
    state: Mapped[dict]=mapped_column(JSONB,default=dict)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,onupdate=now,index=True)
