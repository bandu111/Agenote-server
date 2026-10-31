import asyncio
from logging.config import fileConfig
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.config import get_settings
from app.core.database.base import Base
from app.core.event_loop import psycopg_compatible_loop_factory
import app.core.database.model_registry  # noqa: F401 统一注册 ORM 元数据

config = context.config
fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_sync(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(get_settings().database_url.get_secret_value(), hide_parameters=True)
    async with engine.connect() as connection:
        await connection.run_sync(run_sync)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=get_settings().database_url.get_secret_value(), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(online())
