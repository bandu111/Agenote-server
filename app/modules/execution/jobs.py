"""只恢复已批准的受控计划，不自行扩大工具范围或绕过审批。"""
import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select, text
from fastapi import HTTPException
from app.modules.actions.models import ActionRun
from app.modules.execution.service import ExecutionService


class ExecutionJobs:
    def __init__(self, database, settings):
        self.database, self.settings, self.task = database, settings, None

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def loop(self):
        while True:
            try:
                worked = await self.process_one()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger('yanshu.execution').warning('execution_retry_failed', extra={'error_type': type(error).__name__})
                worked = False
            if not worked:
                await asyncio.sleep(3)

    async def process_one(self):
        async with self.database.sessions() as session:
            # 有界数据库筛选，多个进程通过相同行动锁领取；不恢复未批准或拒绝的计划。
            rows = list(await session.scalars(select(ActionRun).where(ActionRun.status == 'retryable',
                ActionRun.content['kind'].astext == 'tool_plan', ActionRun.content['approved'].as_boolean().is_(True),
                ActionRun.content['executionAttempts'].as_integer() < 3,
                ActionRun.content['nextAttemptAt'].astext <= datetime.now(timezone.utc).isoformat()).limit(20)))
            for row in rows:
                locked = await session.scalar(text('SELECT pg_try_advisory_xact_lock(hashtext(:key))'),
                    {'key': f'action:{row.owner_id}:{row.id}'})
                if not locked:
                    continue
                await session.refresh(row)
                if row.status != 'retryable' or row.content.get('approved') is not True:
                    continue
                if row.content.get('executionAttempts', 0) >= 3 or row.content['nextAttemptAt'] > datetime.now(timezone.utc).isoformat():
                    continue
                try:
                    await ExecutionService(session, self.database, self.settings).advance(row, 'decision', row.content['fingerprint'], True)
                except HTTPException as error:
                    row.status = 'blocked'
                    row.content = {**row.content, 'errorCode': str(error.detail)}
                    await session.commit()
                return True
            return False
