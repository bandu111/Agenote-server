"""连续会议风险不排完整分析，来源隔离、取消及有界上下文。"""
import asyncio
from types import SimpleNamespace
import pytest
from app.modules.meetings.services.live_risks import LiveMeetingRisks, risk_context
from app.modules.meetings.services.business_guard import transcript_hashes
from tests.test_live_meeting_hints import utterance


def candidate(source):
    return {'id': 'risk-' + source['id'], 'kind': 'risk', 'title': '交期受阻',
        'body': '接口超时可能影响交付。', 'evidence_ids': [source['id']], 'inference': True}


@pytest.mark.asyncio
async def test_ten_rounds_publish_before_any_full_analysis_finishes():
    full = asyncio.Event()
    calls = []
    async def run_role(role, payload, on_partial):
        calls.append(payload)
        item = candidate(payload['utterances'][-1])
        await on_partial({'insights': [item]})
        return {'insights': [item]}
    service = LiveMeetingRisks(SimpleNamespace(run_role=run_role))
    for n in range(10):
        sources = [utterance(str(i), f'第{i}个接口超时，交付延迟{i + 1}天。', at=i * 10000) for i in range(n + 1)]
        values = [v async for v in service.stream('owner', 'meeting', sources)]
        assert values[-1]['event'] == 'done'
        assert values[-1]['insights'][0]['evidence_ids'] == [str(n)]
        assert calls[-1]['delta_ids'] == [str(n)]
        assert not full.is_set()
        assert not service.owners
    assert len(calls) == 10


@pytest.mark.asyncio
async def test_real_partial_arrives_before_model_completion_and_cancel_releases_lane():
    release, cancelled = asyncio.Event(), asyncio.Event()
    async def run_role(role, payload, on_partial):
        try:
            await on_partial({'insights': [candidate(payload['utterances'][-1])]})
            await release.wait()
            return {'insights': []}
        finally:
            cancelled.set()
    service = LiveMeetingRisks(SimpleNamespace(run_role=run_role))
    stream = service.stream('owner', 'meeting', [utterance('a', '接口超时，可能影响交付。')])
    assert (await anext(stream))['event'] == 'snapshot'
    preview = await asyncio.wait_for(anext(stream), .5)
    assert preview['event'] == 'preview' and preview['insights'][0]['streaming']
    busy = [v async for v in service.stream('owner', 'meeting', [utterance('b', '接口超时。')])]
    assert busy[-1]['modelStatus'] == 'busy'
    await stream.aclose()
    assert cancelled.is_set() and not service.owners and service.capacity._value == 2


@pytest.mark.asyncio
async def test_repeat_and_greeting_do_not_call_model_but_correction_does():
    calls = []
    async def run_role(role, payload, on_partial):
        calls.append(payload)
        return {'insights': [candidate(payload['utterances'][-1])]}
    service = LiveMeetingRisks(SimpleNamespace(run_role=run_role))
    source = utterance('a', '接口超时，可能影响交付。')
    for _ in range(2):
        values = [v async for v in service.stream('owner', 'meeting', [source])]
    assert values[-1]['modelStatus'] == 'not_needed' and len(calls) == 1
    [v async for v in service.stream('owner', 'meeting', [{**source, 'correction': '接口已经恢复，但图片上传失败。'}])]
    assert len(calls) == 2
    [v async for v in service.stream('owner', 'meeting', [utterance('b', '好，人都到齐了吧，我们开始。')])]
    assert len(calls) == 2


def test_context_bounded_and_does_not_reuse_corrected_or_withdrawn_state():
    sources = [utterance(str(i), '合同交付范围。' * 10, at=i) for i in range(100)]
    previous = {'business_guard_version': 3, 'source_hashes': transcript_hashes(sources),
        'state': [{'topic': '交付', 'value': '合同交付范围。', 'evidence_ids': ['0']}]}
    selected, context = risk_context(sources[-16:], sources, previous)
    assert len(selected) <= 32 and len(context['current_effective_state']) == 1
    assert '0' in {u['id'] for u in selected}
    selected, context = risk_context([{**sources[0], 'correction': '范围尚未批准。'}], sources, previous)
    assert context['current_effective_state'] == []
    selected, context = risk_context(sources[-16:], [{**s, 'withdrawn': s['id'] == '0'} for s in sources], previous)
    assert context['current_effective_state'] == []


@pytest.mark.asyncio
async def test_gate_isolated_by_account_and_meeting_and_never_emits_business_candidates():
    async def run_role(role, payload, on_partial):
        return {'insights': [candidate(payload['utterances'][-1])],
            'withdrawals': [{'target_id': 'a', 'evidence_ids': ['a'], 'reason': '不可执行'}]}
    service = LiveMeetingRisks(SimpleNamespace(run_role=run_role))
    for owner, meeting in [('a', 'x'), ('b', 'x'), ('a', 'y')]:
        values = [v async for v in service.stream(owner, meeting, [utterance('a', '接口超时，可能影响交付。')])]
        assert values[-1]['modelStatus'] == 'completed'
        assert values[-1]['businessEffect'] == 'none'
        assert not ({'goals', 'commitments', 'withdrawals', 'source_changes', 'action_intents'} & values[-1].keys())


async def test_endpoint_auth_input_validation_and_no_business_writes(client, app):
    from tests.test_meetings import account, meeting
    assert (await client.post('/api/v1/meetings/live-risks', json={})).status_code == 401
    owner = await account(client)
    other = await account(client, 'other-risk@example.com')
    payload = meeting()
    payload['utterances'] = [utterance('private', '私有合同原话。')]
    assert (await client.post('/api/v1/meetings/sync', headers=owner, json=payload)).status_code == 200
    seen = []
    async def run_role(role, value, on_partial):
        seen.append(value)
        return {'insights': [candidate(value['utterances'][-1])]}
    app.state.live_meeting_risks = LiveMeetingRisks(SimpleNamespace(run_role=run_role))
    response = await client.post('/api/v1/meetings/live-risks', headers=other, json={
        'meeting_id': payload['id'], 'utterances': [utterance('new', 'ERP接口超时，可能影响交付。')]})
    assert response.status_code == 200 and 'source_checked_inference_only' in response.text
    assert {u['id'] for u in seen[0]['utterances']} == {'new'}
    assert seen[0]['historical_context']['current_effective_state'] == []
    assert not (await client.get('/api/v1/progress', headers=other)).json()['data']['commitments']
    duplicate = {'utterances': [utterance('same', '接口超时。'), utterance('same', '再见。')]}
    assert (await client.post('/api/v1/meetings/live-risks', headers=other, json=duplicate)).status_code == 422
