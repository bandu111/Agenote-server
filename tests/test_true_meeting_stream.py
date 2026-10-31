"""真增量、半截 JSON、账户隔离、断流及先审批后慢分析的边界。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.modules.meetings.services.streaming_insights import streaming_insights, streaming_repairs
from app.modules.meetings.services.live_hints import LiveMeetingHints
from tests.test_live_meeting_hints import utterance
from tests.test_meetings import account, meeting


def test_partial_body_requires_complete_valid_evidence_metadata():
    prefix = '{"insights":[{"id":"x","kind":"risk","evidence_ids":["u"],"title":"范围变化","body":"'
    first = streaming_insights(prefix + '可能', {'u'})
    second = streaming_insights(prefix + '可能增加工作量', {'u'})
    assert first[0]['body'] == '可能' and first[0]['streaming']
    assert second[0]['body'].startswith(first[0]['body'])
    assert streaming_insights(prefix + '可能', {'another-account-source'}) == []
    assert streaming_insights('{"insights":[{"body":"不应先显示', {'u'}) == []
    assert streaming_insights(prefix.replace('"risk"', '"approved"') + '不能批准', {'u'}) == []


def test_escaped_unicode_and_json_do_not_leak_incomplete_encoding():
    prefix = '{"insights":[{"id":"x","kind":"question","evidence_ids":["u"],"title":"核对","body":"'
    assert streaming_insights(prefix + r'确认\u4e2', {'u'})[0]['body'] == '确认'
    assert streaming_insights(prefix + r'确认\ud83d', {'u'})[0]['body'] == '确认'
    assert streaming_insights(prefix + r'确认\ud83d\ude0a', {'u'})[0]['body'] == '确认😊'


def test_repair_text_prefix_and_top_level_arrays_only():
    prefix = '{"repairs":[{"target_id":"old","evidence_ids":["old","new"],"start":0,"end":0,"text":"'
    first = streaming_repairs(prefix + '功能', {'old', 'new'})
    assert first[0]['text'] == '功能' and first[0]['streaming']
    assert not streaming_repairs(prefix + '功能', {'another-owner-source'})
    assert not streaming_repairs('{"repairs":[{"text":"没有来源元数据', {'old'})
    # 正文中出现 repairs/insights 字样也不能被当成结构化来源。
    assert not streaming_repairs(json.dumps({'note': prefix}), {'old', 'new'})
    assert not streaming_insights(json.dumps({'note': '{"insights":[{"id":"fake"}]}'}), {'u'})


async def test_inline_repair_streams_before_report_finishes_without_insights():
    sources = [utterance('old', '验收就按', interrupted=True),
        utterance('new', '接回刚才的验收要求，使用功能清单。', at=10)]
    prefix = '{"repairs":[{"target_id":"old","evidence_ids":["old","new"],"start":0,"end":0,"text":"'
    finish = asyncio.Event()
    async def stream(*_):
        yield {'type': 'delta', 'text': prefix + '功能'}
        await finish.wait()
        yield {'type': 'delta', 'text': '清单"}],"insights":[]}'}
        yield {'type': 'done', 'value': json.loads(prefix + '功能清单"}],"insights":[]}')}
    service = LiveMeetingHints(SimpleNamespace(stream=stream))
    iterator = service.stream('owner', sources)
    try:
        assert not (await anext(iterator))['repairs']
        partial = await asyncio.wait_for(anext(iterator), 1)
        assert not partial['insights']
        assert not finish.is_set()
        assert partial['repairs'][0]['text'] == '功能' and partial['repairs'][0]['streaming']
        assert partial['repairs'][0]['start'] == len(sources[0]['raw'])
        finish.set()
        grown, final = await anext(iterator), await anext(iterator)
        assert grown['repairs'][0]['text'] == final['repairs'][0]['text'] == '功能清单'
        assert final['event'] == 'done' and not final['repairs'][0].get('streaming')
        assert sources[0]['raw'] == '验收就按'
    finally:
        await iterator.aclose()


async def test_model_prefix_is_visible_before_model_finishes_and_keeps_stable_card_id():
    finish = asyncio.Event()
    prefix = '{"insights":[{"id":"x","kind":"risk","evidence_ids":["u"],"title":"范围变化","body":"'
    async def stream(*_):
        yield {'type': 'delta', 'text': prefix + '可能'}
        await finish.wait()
        yield {'type': 'delta', 'text': '增加工作量"}]}'}
        yield {'type': 'done', 'value': json.loads(prefix + '可能增加工作量"}]}')}
    service = LiveMeetingHints(SimpleNamespace(stream=stream))
    iterator = service.stream('owner', [utterance('u', 'Windows 尚未批准，只能评估。')])
    try:
        assert (await anext(iterator))['event'] == 'snapshot'
        partial = await asyncio.wait_for(anext(iterator), 1)
        assert partial['insights'][0]['body'] == '可能' and partial['insights'][0]['streaming']
        assert not finish.is_set()
        finish.set()
        later = await anext(iterator)
        final = await anext(iterator)
        assert later['insights'][0]['id'] == final['insights'][0]['id'] == partial['insights'][0]['id']
        assert final['event'] == 'done' and not final['insights'][0]['streaming']
    finally:
        await iterator.aclose()
    assert not service.owners and service.capacity._value == 2


async def test_disconnected_model_marks_received_text_interrupted_and_releases_capacity():
    async def stream(*_):
        yield {'type': 'delta', 'text': '{"insights":[{"id":"x","kind":"risk","evidence_ids":["u"],"title":"核对","body":"可能'}
        raise OSError('synthetic disconnect')
    service = LiveMeetingHints(SimpleNamespace(stream=stream))
    events = [v async for v in service.stream('owner', [utterance('u', '交期改为周五。')])]
    assert events[-1]['event'] == 'done' and events[-1]['modelStatus'] == 'unavailable'
    assert events[-1]['insights'][0]['interrupted'] and not events[-1]['insights'][0]['streaming']
    assert not service.owners and service.capacity._value == 2


async def test_sse_is_authenticated_and_can_return_quote_only_without_model(client, app):
    headers = await account(client)
    app.state.live_meeting_hints = LiveMeetingHints(SimpleNamespace(stream=AsyncMock()))
    payload = {'include_model': False, 'utterances': [utterance('old', '验收就按', interrupted=True), utterance('new', '验收就按功能清单。', at=10)]}
    result = await client.post('/api/v1/meetings/live-hints', headers={**headers, 'Accept': 'text/event-stream'}, json=payload)
    assert result.status_code == 200 and 'text/event-stream' in result.headers['content-type']
    values = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
    assert values[0]['repairs'] and values[-1]['event'] == 'done'
    assert app.state.live_meeting_hints.provider.stream.await_count == 0


async def test_tool_approval_arrives_and_executes_before_slow_core_analysis(client, app):
    headers = await account(client)
    source = meeting()
    source['capabilities'] = ['tool_plans_v1']
    source['utterances'][0]['raw'] = '请创建任务提交评估报告。'
    await client.post('/api/v1/meetings/sync', headers=headers, json=source)
    ready, finish = asyncio.Event(), asyncio.Event()
    async def reply(role, *_):
        if role == 'state':
            await finish.wait()
        if role == 'controlled_tools':
            return {'plans': [{'title': '创建评估报告任务', 'evidence_ids': ['u1'], 'steps': [
                {'id': 'task', 'tool': 'task_create', 'args': {'title': '提交评估报告'}}]}]}
        return {}
    app.state.meeting_analysis.agents.provider.run = AsyncMock(side_effect=reply)
    app.state.meeting_analysis.on_actions_published = ready.set
    response = await client.post('/api/v1/meetings/phone-meeting-1/jobs', headers=headers, json={'operation': 'analysis'})
    try:
        await asyncio.wait_for(ready.wait(), 3)
        job = (await client.get('/api/v1/meetings/jobs/' + response.json()['data']['id'], headers=headers)).json()['data']
        assert job['status'] == 'running' and job['preview']['actions_prepared']
        assert not (await client.get('/api/v1/tasks', headers=headers)).json()['data']
        action = (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data'][0]
        from tests.test_execution_lifecycle import execute
        result = await execute(client, headers, action)
        assert result.json()['data']['status'] == 'completed', result.text
        assert len((await client.get('/api/v1/tasks', headers=headers)).json()['data']) == 1
    finally:
        finish.set()
        await asyncio.gather(*list(app.state.meeting_jobs.tasks))


@pytest.mark.parametrize('change_source', [False, True])
async def test_early_calendar_approval_requires_exact_current_sources(client, app, change_source):
    headers = await account(client)
    source = meeting()
    source['utterances'][0]['raw'] = '确定2030年10月23日14点到15点开进度会。'
    await client.post('/api/v1/meetings/sync', headers=headers, json=source)
    ready, finish = asyncio.Event(), asyncio.Event()
    async def reply(role, *_):
        if role == 'state': await finish.wait()
        if role == 'planner':
            return {'calendar': [{'title': '项目进度会', 'start': '2030-10-23T14:00:00+08:00',
                'end': '2030-10-23T15:00:00+08:00', 'status': 'confirmed', 'evidence_ids': ['u1']}]}
        return {}
    app.state.meeting_analysis.agents.provider.run = AsyncMock(side_effect=reply)
    app.state.meeting_analysis.on_actions_published = ready.set
    await client.post('/api/v1/meetings/phone-meeting-1/jobs', headers=headers, json={'operation': 'analysis'})
    try:
        await asyncio.wait_for(ready.wait(), 3)
        action = (await client.get('/api/v1/actions', headers=headers)).json()['data'][0]
        if change_source:
            source['revision'] = 2
            source['utterances'].append({'id': 'u2', 'speaker': '发言人 A', 'raw': '日程先不要创建。', 'at': 2000})
            await client.post('/api/v1/meetings/sync', headers=headers, json=source)
        from tests.test_execution_lifecycle import execute
        result = await execute(client, headers, action)
        if change_source:
            assert result.status_code == 409, result.text
        else:
            assert result.status_code == 200 and result.json()['data']['status'] == 'awaiting_device', result.text
    finally:
        finish.set()
        await asyncio.gather(*list(app.state.meeting_jobs.tasks))


async def test_stream_final_preserves_evidence_checked_memory_repair(client):
    old = utterance('old', '验收就按', interrupted=True)
    new = utterance('new', '刚才说的验收，就按功能清单。', at=10)
    async def stream(*_):
        yield {'type': 'done', 'value': {'repairs': [{'target_id': 'old', 'text': '功能清单',
            'evidence_ids': ['old', 'new'], 'start': 0, 'end': 0}]}}
    service = LiveMeetingHints(SimpleNamespace(stream=stream))
    events = [v async for v in service.stream('owner', [old, new])]
    assert events[-1]['event'] == 'done'
    assert events[-1]['repairs'][0]['text'] == '功能清单'

async def test_plain_suggestion_first_token_does_not_wait_for_json_or_model_finish():
    entered=asyncio.Event()
    async def stream(role,system,payload):
        assert role=='live_text'
        yield {'type':'delta','text':'建议'}
        await entered.wait()
        yield {'type':'delta','text':'确认范围。'}
        yield {'type':'done','value':{'text':'建议确认范围。'}}
    service=LiveMeetingHints(SimpleNamespace(stream=stream))
    iterator=service.stream('owner',[utterance('u','Windows 尚未批准，只能评估。')],output_protocol='text_v2')
    try:
        assert (await anext(iterator))['event']=='snapshot'
        first=await asyncio.wait_for(anext(iterator),1)
        suggestion = next(i for i in first['insights'] if i['title'] == 'AI 补充')
        assert suggestion['body']=='建议'
        assert suggestion['streaming'] and first['businessEffect']=='none'
        assert not entered.is_set()
        entered.set()
        growing,final=await anext(iterator),await anext(iterator)
        grown_suggestion = next(i for i in growing['insights'] if i['title'] == 'AI 补充')
        final_suggestion = next(i for i in final['insights'] if i['title'] == 'AI 补充')
        assert grown_suggestion['body']==final_suggestion['body']=='建议确认范围。'
        assert suggestion['id'] == grown_suggestion['id'] == final_suggestion['id']
        assert final_suggestion['inference'] and not final_suggestion['streaming']
        assert not final['repairs']
    finally: await iterator.aclose()


async def test_plain_suggestion_disconnect_preserves_prefix_without_inventing_repair():
    async def stream(*_):
        yield {'type':'delta','text':'建议先确认'}
        raise OSError('synthetic')
    service=LiveMeetingHints(SimpleNamespace(stream=stream))
    values=[v async for v in service.stream('owner',[utterance('u','Windows 尚未批准，只能评估。')],output_protocol='text_v2')]
    last=values[-1]
    suggestion = next(i for i in last['insights'] if i['title'] == 'AI 补充')
    assert suggestion['body']=='建议先确认'
    assert suggestion['interrupted'] and not suggestion['streaming']
    assert not any(i.get('issue_key') == 'memory-workflow-v1' for i in last['insights'])
    assert not last['repairs'] and last['businessEffect']=='none'
