"""受控计划持久化、审批和恢复；执行回执独立落库以跨进程幂等。"""
import uuid
import asyncio
from sqlalchemy import select, text
from fastapi import HTTPException
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command
from datetime import datetime, timezone, timedelta
from app.modules.actions.models import ActionRun
from app.modules.execution.schemas import ToolPlan
from app.modules.execution.tools import ControlledTools, digest, current_sources
from app.modules.execution.workflow import build_tool_workflow
from app.modules.execution.models import ToolReceipt


class ExecutionService:
    def __init__(self, session, database=None, settings=None):
        self.session, self.database, self.settings = session, database, settings

    async def project(self, meeting, candidates, source_change=False):
        if 'tool_plans_v1' not in meeting.content.get('capabilities', []): return
        sources = {u['id']: u for u in meeting.content['utterances']}
        for candidate in candidates[:3]:
            plan = ToolPlan.model_validate(candidate)
            is_change = len(plan.steps) == 1 and plan.steps[0].tool == 'source_change'
            # 模型不可直接规划此内部工具；仅允许经过来源检查的确定性服务创建。
            if any(s.tool == 'source_change' for s in plan.steps) and not (source_change and is_change): continue
            if any(i not in sources or sources[i].get('withdrawn') or sources[i].get('turn_status', 'complete') != 'complete' for i in plan.evidence_ids): continue
            import re
            words = '\n'.join(sources[i].get('correction') or sources[i]['raw'] for i in plan.evidence_ids)
            if not is_change and (not re.search(r'请|帮我|帮我们|麻烦', words) or re.search(r'不要|不需要|暂不|不必|取消|撤回|考虑|建议|如果|假如|能否|可否|请问|[？?]', words)): continue
            content = plan.model_dump()
            content['sources'] = {i: digest([sources[i]['raw'], sources[i].get('correction')]) for i in plan.evidence_ids}
            key = digest([meeting.id, content['sources'], content['steps']]) if is_change else digest([meeting.id, content['sources']])
            old = await self.session.scalar(select(ActionRun).where(ActionRun.owner_id == meeting.owner_id, ActionRun.intent_key == key))
            if old: continue
            content.update(kind='tool_plan', instruction='执行已列明的资料与任务操作，并回读核验。')
            if is_change:
                content['instruction'] = words[:300] + '；确认后更新，原记录保留。'
            content['fingerprint'] = digest(content)
            read_only = all(step['tool'] in {'knowledge_search', 'knowledge_read', 'progress_read', 'task_read'} for step in content['steps'])
            if read_only:
                content.update(approved=True, approvalBasis='server_read_only_policy', executionAttempts=0,
                    nextAttemptAt=datetime.now(timezone.utc).isoformat())
            self.session.add(ActionRun(id=str(uuid.uuid4()), owner_id=meeting.owner_id, meeting_id=meeting.id, intent_key=key, version=1, status='retryable' if read_only else 'pending', content=content))
            # 会话禁用了自动刷新；后续候选必须能查到本事务刚创建的计划，避免重复唯一键。
            await self.session.flush()

    async def advance(self, row, operation, fingerprint, value):
        if fingerprint != row.content['fingerprint']: raise HTTPException(409, 'action_changed')
        if operation != 'decision': raise HTTPException(409, 'tool_plan_receipt_is_server_only')
        if row.status in {'completed', 'declined'}: return self.output(row)
        if row.status == 'superseded': raise HTTPException(409, 'action_changed')
        if value is False:
            partial = await self.session.scalar(select(ToolReceipt.id).where(ToolReceipt.owner_id == row.owner_id, ToolReceipt.plan_id == row.id).limit(1)) is not None
            row.status = 'declined'
            row.content = {**row.content, 'stoppedAfterPartialExecution': partial}
            await self.session.commit()
            return self.output(row)
        await current_sources(self.session, row.owner_id, row)
        # 批准和有界重试计划先落库；进程在副作用后退出仍能靠工具回执恢复。
        attempts = row.content.get('executionAttempts', 0) + 1
        row.status = 'retryable'
        row.content = {**row.content, 'approved': True, 'executionAttempts': attempts,
            'nextAttemptAt': (datetime.now(timezone.utc) + timedelta(seconds=min(300, 10 * 2 ** min(attempts, 5)))).isoformat()}
        await self.session.commit()
        await self.session.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),
            {'key': f'action:{row.owner_id}:{row.id}'})
        await self.session.refresh(row)
        if row.status in {'completed', 'declined', 'superseded'}:
            return self.output(row)
        dsn = self.settings.database_url.get_secret_value().replace('postgresql+psycopg://', 'postgresql://', 1)
        async with await AsyncConnection.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row) as connection:
            saver = AsyncPostgresSaver(connection, serde=JsonPlusSerializer(pickle_fallback=False))
            graph = build_tool_workflow(saver, ControlledTools(self.database), row.owner_id, row)
            config = {'configurable': {'thread_id': 'tools:' + row.owner_id + ':' + row.id}, 'recursion_limit': 32}
            state = await graph.aget_state(config)
            try:
                if not state.values:
                    await asyncio.wait_for(graph.ainvoke({'index': 0, 'observations': {}}, config), 90)
                    state = await graph.aget_state(config)
                if state.next == ('approval',):
                    await asyncio.wait_for(graph.ainvoke(Command(resume=value is True), config), 90)
                elif value is True and state.next:
                    await asyncio.wait_for(graph.ainvoke(None, config), 90)
                elif value is False and state.next:
                    # 已执行的工具不伪装成撤销；停止尚未执行部分。
                    row.status = 'declined'
                    row.content = {**row.content, 'stoppedAfterPartialExecution': True}
                    await self.session.commit()
                    return self.output(row)
                state = await graph.aget_state(config)
                row.status = state.values.get('status', 'pending')
                row.content = {**row.content, 'result': state.values.get('observations', {})}
            except (HTTPException, ValueError, TimeoutError, OSError) as error:
                retryable = isinstance(error, (TimeoutError, OSError)) or isinstance(error, HTTPException) and (error.status_code >= 500 or error.status_code == 429)
                row.status = 'retryable' if retryable else 'blocked'
                row.content = {**row.content, 'errorCode': error.detail if isinstance(error, HTTPException) else 'tool_execution_incomplete'}
            if row.status == 'completed':
                row.content = {k: v for k, v in row.content.items() if k not in {'errorCode', 'nextAttemptAt'}}
            await self.session.commit()
            return self.output(row)

    @staticmethod
    def output(row):
        return {'id': row.id, 'version': row.version, 'status': row.status, **row.content}
