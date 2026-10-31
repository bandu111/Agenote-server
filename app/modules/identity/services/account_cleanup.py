"""清理注销期间仍在收尾的图检查点；仅识别言枢自身的 UUID 线程命名空间。"""
import asyncio
import logging
from sqlalchemy import text


class AccountCheckpointCleanup:
    def __init__(self, database):
        self.database, self.task = database, None

    async def sweep(self):
        async with self.database.sessions() as db:
            for table in ('checkpoint_writes', 'checkpoint_blobs', 'checkpoints'):
                if not await db.scalar(text('SELECT to_regclass(:name)'), {'name': table}):
                    continue
                # 不删除其他应用或未遵循此协议的检查点。
                await db.execute(text(f"""DELETE FROM {table} c
                  WHERE c.thread_id ~ '^(tools:)?[0-9a-f-]{{36}}:[0-9a-f-]{{36}}$'
                  AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id =
                    CASE WHEN c.thread_id LIKE 'tools:%' THEN split_part(c.thread_id, ':', 2)
                    ELSE split_part(c.thread_id, ':', 1) END)"""))
            await db.commit()

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger('yanshu.identity').warning('account_checkpoint_cleanup_failed')
            await asyncio.sleep(60)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
