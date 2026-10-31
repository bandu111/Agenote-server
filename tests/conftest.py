import asyncio
import os
import sys
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.config import Settings, get_settings
from app.main import create_app


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.WindowsSelectorEventLoopPolicy() if sys.platform == "win32" else asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="session", autouse=True)
def migrate_test_database():
    values = dotenv_values(ROOT / ".env")
    url = os.environ.get("TEST_DATABASE_URL") or values.get("TEST_DATABASE_URL")
    # 防止测试误清真实开发库或外部业务数据库。
    if not url or make_url(url).database != "yanshu_auth_test":
        raise RuntimeError("Explicit isolated yanshu_auth_test database required")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from app.core.event_loop import psycopg_compatible_loop_factory
    async def setup_checkpoints():
        async with AsyncPostgresSaver.from_conn_string(url.replace("postgresql+psycopg://", "postgresql://", 1)) as saver:
            await saver.setup()
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(setup_checkpoints())
    yield url
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous
    get_settings.cache_clear()


@pytest.fixture
async def app(migrate_test_database):
    settings = Settings(database_url=migrate_test_database, app_env="test",
                        jwt_secret="unit-test-signing-key-only-not-for-production-0123456789",
                        auth_requests_per_minute=1000, jpush_app_key=None, jpush_master_secret=None)
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        async with application.state.database.sessions() as db:
            for name in ("refresh_tokens", "auth_sessions", "users"):
                await db.execute(text(f"DELETE FROM {name}"))
            for name in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await db.execute(text(f"DELETE FROM {name}"))
            await db.commit()
        yield application


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as instance:
        yield instance
