"""数据库连接池、会话工厂和请求级会话依赖。"""
from collections.abc import AsyncGenerator
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.core.config import Settings, settings


class Database:
    def __init__(self, settings: Settings):
        self.engine = create_async_engine(settings.database_url.get_secret_value(),
                                          pool_pre_ping=True, echo=False, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False,
                                           class_=AsyncSession)

    async def close(self):
        await self.engine.dispose()


# 默认应用复用连接池；测试应用可注入独立数据库，避免访问开发库。
database = Database(settings)
engine = database.engine
AsyncSessionLocal = database.sessions


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    factory = request.app.state.database.sessions
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_database_connection() -> bool:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return True
