"""索引查询和资料查询统一限定认证账户。"""

import hashlib
from sqlalchemy import select, delete, text
from app.modules.knowledge.models.library import (
    KnowledgeLibrary,
    KnowledgeNode,
    KnowledgeIndexState,
)


class KnowledgeRepository:
    def __init__(self, session):
        self.session = session

    async def library(self, owner, *, lock=False):
        if lock:
            key = int.from_bytes(
                hashlib.sha256(f"knowledge:{owner}".encode()).digest()[:8],
                "big",
                signed=True,
            )
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
            )
        query = select(KnowledgeLibrary).where(KnowledgeLibrary.owner_id == owner)
        if lock:
            query = query.with_for_update()
        return await self.session.scalar(
            query.execution_options(populate_existing=True)
        )

    async def nodes(self, owner):
        return list(
            await self.session.scalars(
                select(KnowledgeNode).where(KnowledgeNode.owner_id == owner)
            )
        )

    async def states(self, owner):
        return list(
            await self.session.scalars(
                select(KnowledgeIndexState).where(KnowledgeIndexState.owner_id == owner)
            )
        )

    async def purge(self, owner, ids):
        for model in [KnowledgeNode, KnowledgeIndexState]:
            statement = delete(model).where(model.owner_id == owner)
            if ids:
                statement = statement.where(model.document_id.not_in(ids))
            await self.session.execute(statement)
