from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.modules.identity.models.identity import AuthSession, RefreshToken, User


class IdentityRepository:
    """只读写及 flush，不在仓储层提交事务。"""
    def __init__(self, session: AsyncSession):
        self.session = session

    async def by_email(self, email: str):
        return await self.session.scalar(select(User).where(User.email == email))

    async def by_id(self, user_id: str):
        return await self.session.get(User, user_id)

    async def add(self, model):
        self.session.add(model)
        await self.session.flush()
        return model

    async def get_session(self, session_id: str, *, lock=False):
        query = select(AuthSession).where(AuthSession.id == session_id)
        if lock:
            query = query.with_for_update()
        return await self.session.scalar(query.execution_options(populate_existing=True))

    async def get_refresh(self, digest: str):
        return await self.session.scalar(select(RefreshToken).where(RefreshToken.token_hash == digest)
                                    .execution_options(populate_existing=True))
