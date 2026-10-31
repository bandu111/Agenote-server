"""会议仓储统一限定账户，避免跨账户读取或更新。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.modules.meetings.models.meeting import Meeting


class MeetingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, owner_id: str, client_id: str, *, lock: bool = False, include_deleted: bool = False):
        query = select(Meeting).where(
            Meeting.owner_id == owner_id, Meeting.client_id == client_id
        )
        if not include_deleted:
            query = query.where(Meeting.deleted.is_(False))
        if lock:
            query = query.with_for_update()
        return await self.session.scalar(
            query.execution_options(populate_existing=True)
        )

    async def list(self, owner_id: str):
        rows = await self.session.scalars(
            select(Meeting)
            .where(Meeting.owner_id == owner_id, Meeting.deleted.is_(False))
            .order_by(Meeting.updated_at.desc())
            .limit(200)
        )
        return list(rows)

    async def add(self, meeting: Meeting):
        self.session.add(meeting)
        await self.session.flush()
