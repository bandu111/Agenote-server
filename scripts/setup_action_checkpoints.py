"""显式安装官方 LangGraph PostgreSQL 检查点表，不在 Web 启动时执行建表。"""

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from app.core.config import get_settings
from app.core.event_loop import psycopg_compatible_loop_factory


async def setup():
    dsn = get_settings().database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://", 1)
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
    print("LangGraph 检查点表已准备完成")


if __name__ == "__main__":
    with asyncio.Runner(loop_factory=psycopg_compatible_loop_factory) as runner:
        runner.run(setup())
