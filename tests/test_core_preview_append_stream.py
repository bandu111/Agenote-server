"""新发言追加不能吞掉正文流；原来源纠错必须使旧流停止。"""
import asyncio
import pytest
from tests.test_meetings import account, meeting
from app.modules.meetings.services.streaming_insights import streaming_insights


def test_memory_card_streams_before_json_finishes():
    prefix = '{"insights":[{"id":"m","kind":"stitch","title":"验收接回","evidence_ids":["u1"],"body":"'
    first = streaming_insights(prefix + '接', {'u1'})
    second = streaming_insights(prefix + '接着刚才的验收', {'u1'})
    assert first[0]['body'] == '接' and first[0]['streaming']
    assert second[0]['body'].startswith(first[0]['body'])
    assert streaming_insights(prefix + '接', {'other-owner'}) == []


@pytest.mark.parametrize('correction', [False, True])
async def test_append_keeps_core_deltas_but_correction_stops_old_preview(client, app, correction):
    headers = await account(client)
    await client.post('/api/v1/meetings/sync', headers=headers, json=meeting())
    started, proceed, published, finish = [asyncio.Event() for _ in range(4)]
    async def analyze(utterances, context=None, on_core_ready=None, on_actions_ready=None):
        started.set()
        await proceed.wait()
        value = {'insights': [{'id': 'stream', 'kind': 'risk', 'title': '验收待确认',
            'body': '验', 'evidence_ids': ['u1'], 'streaming': True}],
            'repairs': [], 'withdrawals': [], 'state': [], 'agents_completed': []}
        await on_core_ready(value)
        published.set()
        await finish.wait()
        return value
    app.state.meeting_analysis.agents.analyze = analyze
    response = await client.post('/api/v1/meetings/phone-meeting-1/jobs', headers=headers, json={'operation': 'analysis'})
    job_id = response.json()['data']['id']
    try:
        await asyncio.wait_for(started.wait(), 3)
        updated = meeting(2)
        if correction:
            updated['utterances'][0]['correction'] = '重新确认的原话'
        else:
            updated['utterances'].append({'id': 'u2', 'raw': '继续讨论预算。', 'speaker': '发言人 B', 'at': 2000})
        # Flutter 长会议使用增量入口；完整同步保留原有取消旧任务规则。
        updated['baseRevision'] = 1
        assert (await client.post('/api/v1/meetings/sync-delta', headers=headers, json=updated)).status_code == 200
        proceed.set()
        await asyncio.wait_for(published.wait(), 3)
        job = (await client.get('/api/v1/meetings/jobs/'+job_id, headers=headers)).json()['data']
        insights = (job.get('preview') or {}).get('insights', [])
        if correction:
            assert not insights
        else:
            assert insights[0]['body'] == '验' and insights[0]['streaming']
            assert job['status'] == 'running' and job['result'] is None
    finally:
        proceed.set()
        finish.set()
        await asyncio.gather(*list(app.state.meeting_jobs.tasks))
