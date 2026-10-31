from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.modules.meetings.services.meeting_title import suggest_title


def test_default_title_uses_generated_topic_only_as_fallback():
    from app.modules.meetings.services.meeting_title import title_from_topics
    topics = [SimpleNamespace(title='ERP接口上线评估')]
    assert title_from_topics('录音会议', '录音会议', topics) == 'ERP接口上线评估'
    assert title_from_topics('客户周会', '客户周会', topics) == '客户周会'
    assert title_from_topics('录音会议', '接口修复安排', topics) == '接口修复安排'


def provider(result=None, error=None):
    return SimpleNamespace(settings=SimpleNamespace(qwen_workers_enabled=True,
        dashscope_api_key='test-only'), run=AsyncMock(return_value=result, side_effect=error))


def content(title='录音会议'):
    return {'title': title, 'utterances': [{'raw': '今天讨论ERP接口修复和上线安排，周三验收通过以后再决定发布。'}]}


async def test_title_uses_short_stateless_role():
    model = provider({'title': 'ERP接口修复与上线安排'})
    assert await suggest_title(model, content()) == 'ERP接口修复与上线安排'
    assert model.run.call_args.args[0] == 'meeting_title'
    assert sum(map(len, model.run.call_args.args[2]['excerpts'])) <= 1800


@pytest.mark.parametrize('result', [None, {}, {'title': '长' * 25}, {'title': '标题\n注入'}, {'title': '录音会议'}])
async def test_invalid_title_keeps_minutes_available(result):
    assert await suggest_title(provider(result), content()) == '录音会议'


async def test_failure_and_custom_titles():
    assert await suggest_title(provider(error=TimeoutError()), content()) == '录音会议'
    model = provider({'title': '不要替换手动标题'})
    assert await suggest_title(model, content('客户项目周会')) == '客户项目周会'
    model.run.assert_not_awaited()


async def test_archive_persists_title_and_old_client_cannot_restore_placeholder(client, app, monkeypatch):
    from tests.test_meetings import account, meeting
    headers = await account(client)
    source = meeting()
    source['title'] = '录音会议'
    source['utterances'][0]['raw'] = '今天讨论项目交付和验收安排，周三验证通过以后再决定是否发布。'
    source['ended'] = True
    await client.post('/api/v1/meetings/sync', headers=headers, json=source)
    service = app.state.meeting_analysis.agents.provider
    monkeypatch.setattr(service, 'settings', SimpleNamespace(qwen_workers_enabled=True, dashscope_api_key='test-only'))
    monkeypatch.setattr(service, 'run', AsyncMock(side_effect=[
        {'decisions': [], 'actions': [], 'open_questions': []},
        {'title': '项目交付与验收安排'},
    ]))
    result = await client.post(f"/api/v1/meetings/{source['id']}/archive", headers=headers)
    assert result.status_code == 200, result.text
    data = result.json()['data']
    assert data['meetingTitle'] == '项目交付与验收安排'
    assert data['title'] == '项目交付与验收安排 · 会议纪要'
    synced = await client.post('/api/v1/meetings/sync', headers=headers, json=source)
    assert synced.status_code == 200, synced.text
    assert synced.json()['data']['title'] == '项目交付与验收安排'


async def test_slow_optional_title_does_not_hold_completed_minutes(client, app, monkeypatch):
    import asyncio
    from tests.test_meetings import account, meeting
    headers = await account(client)
    source = {**meeting(), 'ended': True, 'title': '录音会议'}
    await client.post('/api/v1/meetings/sync', headers=headers, json=source)
    blocked = asyncio.Event()
    cancelled = asyncio.Event()
    async def title(*args):
        try:
            await blocked.wait()
        finally:
            cancelled.set()
    monkeypatch.setattr('app.modules.meetings.services.meeting_archive_service.suggest_title', title)
    monkeypatch.setattr(app.state.meeting_analysis.agents.provider, 'run', AsyncMock(return_value={
        'summary':'先完成接口测试。', 'topics':[{'title':'接口测试安排','text':'先测试再上线。','evidence_ids':['u1'],'points':[]}],
        'decisions':[], 'actions':[], 'open_questions':[]}))
    result = await asyncio.wait_for(client.post(f"/api/v1/meetings/{source['id']}/archive", headers=headers), timeout=2)
    assert result.status_code == 200, result.text
    assert result.json()['data']['meetingTitle'] == '接口测试安排'
    assert cancelled.is_set()
