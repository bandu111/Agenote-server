"""Evidence verification, identity boundaries and durable queue uniqueness."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from app.modules.outcomes.professions import classify_local, CATEGORIES
from app.modules.outcomes.jobs import intent, ready, enqueue
from app.modules.outcomes.engine import analyze
from app.modules.outcomes.router import channel_status
from app.modules.meetings.models.meeting import Meeting
from app.modules.meetings.models.archive import MeetingArchive
from app.modules.outcomes.models import OutcomeRun
from tests.test_auth import register, bearer

def test_classification_conservative_and_no_role_restriction():
    assert len(CATEGORIES) == 13
    assert classify_local('Flutter 开发工程师') == 'engineering'
    assert classify_local('产品经理兼开发工程师') is None
    assert classify_local('销售总监') == 'sales_business'
    assert classify_local('未知职业') is None
    assert classify_local('growthacker') is None

def test_job_key_and_ready_gate():
    assert intent('m', 1) == intent('m', 1)
    assert intent('m', 1) != intent('m', 2)
    assert intent('m', 1) != intent('m', 1, 'explicit-request')
    m = SimpleNamespace(deleted=False, revision=1, content={'ended': False, 'utterances': [1]})
    a = SimpleNamespace(revision=1, details={'summary': 'test'})
    assert not ready(m, a)
    m.content['ended'] = True
    assert ready(m, a)
    a.revision = 2
    assert not ready(m, a)
    assert channel_status()['qr'] is None and (not channel_status()['canSend'])

async def test_engine_rejects_fabricated_citation():
    provider = SimpleNamespace(run=AsyncMock(side_effect=[{'worthwhile': True, 'question': 'risk', 'queries': [], 'analysis_goal': '验证接口风险', 'metrics': ['接口错误率'], 'required_sources': ['接口日志'], 'methods': ['证据审查'], 'deliverable': '技术验证计划', 'value_reason': '确定下一步验证方法'}, {'title': 'report', 'summary': 'test', 'findings': [{'text': 'invented', 'certainty': 'hypothesis', 'evidence_ids': ['other-user-evidence']}], 'next_steps': [], 'data_gaps': ['缺少调用日志'], 'notify': True, 'analysis_goal': '验证接口风险', 'metrics': [{'name': '接口错误率', 'status': 'missing', 'missing_data': '缺少调用日志'}], 'methods': [{'name': '证据审查', 'tool': 'evidence_review', 'status': 'performed', 'detail': '比对会议原话'}], 'sections': [{'title': '验证顺序', 'content': '先收集日志，再验证接口风险。', 'evidence_ids': ['other-user-evidence']}], 'notification_summary': '接口风险需补充日志验证'}]))
    with pytest.raises(ValueError, match='invalid_evidence_reference'):
        await analyze(provider, None, 'owner', {'evidence': {'utterance:a': {'text': 'real'}}})

async def test_profession_and_reports_are_account_scoped(client, app):
    one = await register(client)
    two = await register(client, 'second@example.com')
    h = bearer(one)
    h2 = bearer(two)
    value = await client.post('/api/v1/assistant/profession/classify', headers=h, json={'raw': 'Flutter 开发工程师'})
    assert value.status_code == 200 and value.json()['data']['source'] == 'dictionary'
    assert value.json()['data']['confirmed'] is False
    saved = await client.post('/api/v1/assistant/profession', headers=h, json={'raw': 'Flutter 开发工程师', 'categoryId': 'engineering', 'version': 1})
    assert saved.status_code == 200
    assert (await client.get('/api/v1/assistant/wechat', headers=h2)).json()['data']['profession'] is None
    assert (await client.post('/api/v1/assistant/wechat/qr', headers=h)).status_code == 503
    async with app.state.database.sessions() as session:
        from app.modules.identity.models.identity import User
        owner = await session.scalar(select(User).where(User.email == 'yan@example.com'))
        meeting = Meeting(id='outcome-meeting', owner_id=owner.id, client_id='client-meeting', revision=1, content={'ended': True, 'utterances': [{'id': 'u', 'raw': '真实证据', 'at': 1}]})
        session.add(meeting)
        await session.flush()
        archive = MeetingArchive(id='outcome-archive', meeting_id=meeting.id, owner_id=owner.id, revision=1, title='meeting', markdown='test', details={'summary': 'real'})
        session.add(archive)
        await session.flush()
        first = await enqueue(session, meeting, archive)
        second = await enqueue(session, meeting, archive)
        assert first.id == second.id
        await session.commit()
        run_id = first.id
    assert (await client.get('/api/v1/assistant/outcomes/' + run_id, headers=h)).status_code == 200
    assert (await client.get('/api/v1/assistant/outcomes/' + run_id, headers=h2)).status_code == 404
    assert (await client.get('/api/v1/assistant/outcomes', headers=h2)).json()['data'] == []
    denied = await client.post('/api/v1/assistant/meetings/outcome-meeting/analyze', headers=h2, json={'requestId': 'test-explicit-request', 'question': 'read other owner'})
    assert denied.status_code == 404

async def test_worker_persists_report_and_does_not_rerun_finished_job(client, app):
    from app.modules.identity.models.identity import User
    from app.modules.outcomes.jobs import OutcomeJobs
    from app.core.database.mixins import now
    from datetime import timedelta
    one = await register(client)
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User).where(User.email == 'yan@example.com'))
        owner.profile = {**(owner.profile or {}), 'outcome_preferences': {'autoAnalyze': True, 'enabledAt': (now() - timedelta(minutes=1)).isoformat()}}
        meeting = Meeting(id='worker-meeting', owner_id=owner.id, client_id='worker-client', revision=1, content={'ended': True, 'utterances': [{'id': 'u', 'raw': '接口延迟增加，需要验证日志。', 'at': 1000}]})
        session.add(meeting)
        await session.flush()
        archive = MeetingArchive(id='worker-archive', meeting_id=meeting.id, owner_id=owner.id, revision=1, title='接口验证', markdown='text', details={'summary': '延迟需要排查'})
        session.add(archive)
        await session.flush()
        row = await enqueue(session, meeting, archive)
        key = row.id
        await session.commit()
    provider = SimpleNamespace(run=AsyncMock(side_effect=[{'worthwhile': True, 'question': '是否版本引入延迟', 'queries': [], 'analysis_goal': '验证接口风险', 'metrics': ['接口错误率'], 'required_sources': ['接口日志'], 'methods': ['证据审查'], 'deliverable': '技术验证计划', 'value_reason': '确定下一步验证方法'}, {'title': '延迟验证计划', 'summary': '尚无日志，不能确认根因。', 'findings': [{'text': '延迟与版本关系仍需验证。', 'certainty': 'hypothesis', 'evidence_ids': ['utterance:u']}], 'next_steps': ['提供变更前后日志'], 'data_gaps': ['缺少真实响应耗时记录'], 'notify': True, 'analysis_goal': '验证接口风险', 'metrics': [{'name': '接口错误率', 'status': 'missing', 'missing_data': '缺少调用日志'}], 'methods': [{'name': '证据审查', 'tool': 'evidence_review', 'status': 'performed', 'detail': '比对会议原话'}], 'sections': [{'title': '验证顺序', 'content': '先收集日志，再验证接口风险。', 'evidence_ids': ['utterance:u']}], 'notification_summary': '接口风险需补充日志验证'}]))
    worker = OutcomeJobs(app.state.database, provider)
    assert await worker.process_one()
    assert not await worker.process_one()
    assert provider.run.await_count == 2
    async with app.state.database.sessions() as session:
        saved = await session.get(OutcomeRun, key)
        assert saved.status == 'succeeded' and saved.content['attempts'] == 1
        assert saved.content['result']['data_gaps']
        assert saved.content['delivery']['status'] == 'not_sent'
    assert not await OutcomeJobs(app.state.database, provider).process_one()

async def test_worker_recovers_expired_lease_and_bounds_retries(client, app):
    from app.modules.identity.models.identity import User
    from app.modules.outcomes.jobs import OutcomeJobs
    from app.core.database.mixins import now
    from datetime import timedelta
    await register(client)
    async with app.state.database.sessions() as session:
        user = await session.scalar(select(User).where(User.email == 'yan@example.com'))
        meeting = Meeting(id='recover-meeting', owner_id=user.id, client_id='recover-client', revision=1, content={'ended': True, 'utterances': [{'id': 'u', 'raw': '需要排查真实风险', 'at': 1}]})
        session.add(meeting)
        await session.flush()
        archive = MeetingArchive(id='recover-archive', meeting_id=meeting.id, owner_id=user.id, revision=1, title='meeting', markdown='test', details={'summary': '真实问题'})
        session.add(archive)
        await session.flush()
        row = await enqueue(session, meeting, archive, request_key='explicit-request', request='排查风险')
        key = row.id
        row.status = 'running'
        row.content = {**row.content, 'attempts': 1, 'leaseUntil': (now() - timedelta(seconds=1)).isoformat()}
        await session.commit()
    provider = SimpleNamespace(run=AsyncMock(side_effect=TimeoutError()))
    worker = OutcomeJobs(app.state.database, provider)
    assert await worker.process_one()
    async with app.state.database.sessions() as session:
        row = await session.get(OutcomeRun, key)
        assert row.status == 'retryable' and row.content['attempts'] == 2
        assert row.content['error'] == 'TimeoutError'
        row.content = {**row.content, 'nextAt': (now() - timedelta(seconds=1)).isoformat()}
        await session.commit()
    assert await worker.process_one()
    assert not await worker.process_one()
    async with app.state.database.sessions() as session:
        row = await session.get(OutcomeRun, key)
        assert row.status == 'failed' and row.content['attempts'] == 3

async def test_automatic_scan_only_completed_current_new_meetings(client, app):
    from app.modules.identity.models.identity import User
    from app.modules.outcomes.jobs import OutcomeJobs
    from app.core.database.mixins import now
    from datetime import timedelta
    await register(client)
    stamp = now()
    async with app.state.database.sessions() as session:
        user = await session.scalar(select(User).where(User.email == 'yan@example.com'))
        user.profile = {**(user.profile or {}), 'outcome_preferences': {'autoAnalyze': True, 'enabledAt': (stamp - timedelta(minutes=10)).isoformat()}}
        for name, ended, started, revision in [('eligible', True, stamp - timedelta(minutes=5), 1), ('active', False, stamp - timedelta(minutes=5), 1), ('old-checkbox-update', True, stamp - timedelta(days=1), 1), ('stale-archive', True, stamp - timedelta(minutes=5), 2)]:
            meeting = Meeting(id=name, owner_id=user.id, client_id=name, revision=revision, content={'ended': ended, 'started_at': started.isoformat(), 'elapsed_ms': 60000, 'utterances': [{'id': 'u', 'raw': '待验证的问题', 'at': 1}]})
            session.add(meeting)
            await session.flush()
            session.add(MeetingArchive(id='a-' + name, meeting_id=name, owner_id=user.id, revision=1, title='会议', markdown='真实归档', details={'summary': '需要分析的问题'}, updated_at=stamp))
        await session.commit()
    worker = OutcomeJobs(app.state.database, SimpleNamespace())
    await worker.scan()
    await worker.scan()
    async with app.state.database.sessions() as session:
        rows = list(await session.scalars(select(OutcomeRun)))
        assert len(rows) == 1 and rows[0].meeting_id == 'eligible'

async def test_readonly_tool_identity_is_not_taken_from_plan(monkeypatch):
    owners = []

    class Search:

        def __init__(self, session):
            pass

        async def search(self, owner, *args, **kwargs):
            owners.append(owner)
            return []
    monkeypatch.setattr('app.modules.outcomes.engine.UnifiedSearch', Search)
    provider = SimpleNamespace(run=AsyncMock(side_effect=[{'worthwhile': True, 'question': 'risk', 'queries': ['接口'], 'analysis_goal': '验证接口风险', 'metrics': ['接口错误率'], 'required_sources': ['接口日志'], 'methods': ['证据审查'], 'deliverable': '技术验证计划', 'value_reason': '确定下一步验证方法'}, {'title': 'report', 'summary': 'test', 'findings': [{'text': '需验证', 'certainty': 'hypothesis', 'evidence_ids': ['a']}], 'next_steps': [], 'data_gaps': ['缺少日志'], 'notify': True, 'analysis_goal': '验证接口风险', 'metrics': [{'name': '接口错误率', 'status': 'missing', 'missing_data': '缺少调用日志'}], 'methods': [{'name': '证据审查', 'tool': 'evidence_review', 'status': 'performed', 'detail': '比对会议原话'}], 'sections': [{'title': '验证顺序', 'content': '先收集日志，再验证接口风险。', 'evidence_ids': ['a']}], 'notification_summary': '接口风险需补充日志验证'}]))
    value = await analyze(provider, None, 'authenticated-owner', {'evidence': {'utterance:a': {'text': 'real'}}})
    assert owners == ['authenticated-owner']
    assert value['findings'][0]['evidence_ids'] == ['utterance:a']

async def test_pdf_requires_owner_and_keeps_evidence(client, app, monkeypatch, tmp_path):
    from app.modules.identity.models.identity import User
    from app.modules.outcomes import pdf_report
    monkeypatch.setattr(pdf_report, 'ROOT', tmp_path)
    first = await register(client)
    second = await register(client, 'second@example.com')
    async with app.state.database.sessions() as session:
        owner = await session.scalar(select(User.id).where(User.email == 'yan@example.com'))
        session.add(Meeting(id='pdf-meeting', owner_id=owner, client_id='pdf-client', revision=1, content={'ended': True}))
        await session.flush()
        session.add(OutcomeRun(id='pdf-report', owner_id=owner, meeting_id='pdf-meeting', intent_key='pdf-intent', version=1, status='succeeded', content={'kind': 'role_outcome', 'result': {'title': '接口验证报告', 'summary': '尚不能确认根因', 'findings': [{'text': '需要核对调用日志', 'certainty': 'hypothesis', 'evidence_ids': ['u']}], 'next_steps': ['验证接口'], 'data_gaps': ['缺少日志'], 'evidence': {'u': {'text': '先验证再上线', 'at': 1000}}}}))
        await session.commit()
    path = '/api/v1/assistant/outcomes/pdf-report/pdf'
    assert (await client.get(path, headers=bearer(second))).status_code == 404
    response = await client.get(path, headers=bearer(first))
    assert response.status_code == 200 and response.content.startswith(b'%PDF-')
    assert 'no-store' in response.headers['cache-control']
    from pypdf import PdfReader
    from io import BytesIO
    content = ''.join((p.extract_text() for p in PdfReader(BytesIO(response.content)).pages))
    assert '接口验证报告' in content and '先验证再上线' in content and ('待验证假设' in content)
