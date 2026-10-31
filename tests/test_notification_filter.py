"""普通行动保持可审批但不产生主动推送，风险知悉不等于业务执行。"""
from unittest.mock import AsyncMock
from sqlalchemy import select
from test_meetings import account, meeting
from app.modules.meetings.models.meeting import Meeting
from app.modules.knowledge.services.document_actions import KnowledgeAssistance
from app.modules.push.services import push_jobs


async def test_only_missing_fields_and_warnings_are_push_events(client, app, monkeypatch):
    headers = await account(client)
    # 认证创建账户，业务列表保持真实数据库会话。
    assert headers
    monkeypatch.setattr(push_jobs.KnowledgeAssistance, 'notices', AsyncMock(return_value=[
        {'id': 'normal', 'state': 'pending', 'kind': 'document_action', 'fingerprint': 'a'},
        {'id': 'missing', 'state': 'pending', 'kind': 'question', 'fingerprint': 'b'},
        {'id': 'risk', 'state': 'pending', 'kind': 'warning', 'fingerprint': 'c'},
        {'id': 'read', 'state': 'allowed', 'kind': 'warning', 'fingerprint': 'd'},
    ]))
    monkeypatch.setattr(push_jobs.HomeService, 'snapshot', AsyncMock(return_value={'decisions': [
        {'id': 'action:calendar'}, {'id': 'insight:duplicate-risk'}, {'id': 'pending:goal', 'body': '交期缺失'},
    ]}))
    async with app.state.database.sessions() as session:
        events = await push_jobs.current_events(session, 'unused-owner')
    assert set(events) == {'risk', 'pending:goal'}


async def test_warning_receipt_and_source_invalidation(client, app):
    headers = await account(client)
    assert (await client.post('/api/v1/meetings/sync', headers=headers, json=meeting())).status_code == 200
    async with app.state.database.sessions() as session:
        row = await session.scalar(select(Meeting))
        owner = row.owner_id
        row.analysis_revision = row.revision
        row.analysis = {'insights': [{'id': 'risk-1', 'kind': 'risk', 'title': '验收条件未确认',
            'body': '请核对质量标准。', 'evidence_ids': ['u1']} ]}
        await session.commit()
        service = KnowledgeAssistance(session)
        notices = await service.notices(owner)
        n = next(n for n in notices if n['kind'] == 'warning')
        result = await service.decide(owner, n['id'], n['fingerprint'], True)
        assert result['status'] == 'acknowledged'
        assert next(n for n in await service.notices(owner) if n['kind'] == 'warning')['state'] == 'allowed'
        row = await session.get(Meeting, row.id)
        row.revision += 1
        await session.commit()
        assert not [n for n in await service.notices(owner) if n['kind'] == 'warning']
