"""所有查询均限定账户，不从客户端接受作用域。"""

from sqlalchemy import select
from app.modules.progress.models.progress_item import ProgressItem


class ProgressRepository:
    def __init__(self, session):
        self.session = session

    async def list(self, owner_id):
        return list(
            await self.session.scalars(
                select(ProgressItem)
                .where(ProgressItem.owner_id == owner_id)
                .order_by(ProgressItem.updated_at.desc())
            )
        )

    async def get(self, owner_id, item_id, *, lock=False):
        query = select(ProgressItem).where(
            ProgressItem.owner_id == owner_id, ProgressItem.id == item_id
        )
        if lock:
            query = query.with_for_update()
        return await self.session.scalar(
            query.execution_options(populate_existing=True)
        )

    async def source(self, owner_id, source_key):
        return await self.session.scalar(
            select(ProgressItem)
            .where(
                ProgressItem.owner_id == owner_id, ProgressItem.source_key == source_key
            )
            .with_for_update()
        )

    async def add(self, row):
        self.session.add(row)
        await self.session.flush()
