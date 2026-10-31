"""会议纪要仓储，所有访问必须包含当前账户。"""

from sqlalchemy import select
from app.modules.meetings.models.archive import MeetingArchive


class ArchiveRepository:
    def __init__(self, session):
        self.session = session

    async def get(self, owner_id, meeting_id):
        return await self.session.scalar(
            select(MeetingArchive)
            .where(
                MeetingArchive.owner_id == owner_id,
                MeetingArchive.meeting_id == meeting_id,
            )
            .execution_options(populate_existing=True)
        )

    async def save(self, row):
        self.session.add(row)
        await self.session.flush()

    async def for_meetings(self, owner_id, meeting_ids):
        if not meeting_ids:
            return {}
        rows = await self.session.scalars(
            select(MeetingArchive).where(
                MeetingArchive.owner_id == owner_id,
                MeetingArchive.meeting_id.in_(meeting_ids),
            )
        )
        return {row.meeting_id: row for row in rows}
