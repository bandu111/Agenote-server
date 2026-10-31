"""验证慢职责隔离、真实增量、审查拒绝与缓存失效，而非只检查调用次数。"""
import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.core.config import Settings
from app.integrations.hermes.provider import HermesProvider
from app.modules.meetings.services.meeting_agents import MeetingAgents
from app.modules.meetings.services.incremental_analysis import changed_sources, review_binding, role_sources
from app.modules.meetings.services.business_guard import transcript_hashes, fingerprint, guarded_report
from app.modules.meetings.services.meeting_jobs import MeetingJobs
from app.modules.meetings.services.decision_questionnaire import DecisionQuestionnaire
from app.integrations.hermes.request_slots import RequestSlots
from app.modules.meetings.services.progressive_review import ProgressiveReview
from tests.test_commitment_agent import source, candidate, event


def insight():
    return {'id': 'risk1', 'kind': 'risk', 'title': '范围待确认', 'body': 'Windows尚未批准。',
        'evidence_ids': ['u1'], 'inference': True}


async def test_slow_forecast_cannot_block_verified_risk_or_commitment_publication():
    release, seen_risk, seen_commitment = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def reply(role, prompt, payload):
        if role == 'forecast':
            await release.wait()
            return {}
        if role == 'risk':
            return {'insights': [insight()]}
        if role == 'commitment':
            return {'commitments': [candidate([event('committed', 'u1')])]}
        if role == 'verify':
            if payload['business_candidates']:
                return {'reviews': [{**{k: c[k] for k in ['id', 'digest']}, 'accepted': True}
                    for c in payload['business_candidates']]}
            return payload['candidates'][0]
        return {}
    async def publish(value):
        if any(i['kind'] == 'risk' for i in value.get('insights', [])):
            seen_risk.set()
    async def actions(value):
        if value.get('commitments'):
            seen_commitment.set()
    agent = MeetingAgents(SimpleNamespace(run=AsyncMock(side_effect=reply)))
    task = asyncio.create_task(agent.analyze([source('u1', 'Windows范围尚未批准，我方负责提交评估报告。')],
        on_core_ready=publish, on_actions_ready=actions))
    try:
        await asyncio.wait_for(seen_risk.wait(), 2)
        await asyncio.wait_for(seen_commitment.wait(), 2)
        assert not task.done()
    finally:
        release.set()
        result = await task
    assert result['commitments'] and result['insights']


async def test_rejected_business_candidate_never_published_as_progress():
    async def reply(role, prompt, payload):
        if role == 'commitment':
            return {'commitments': [candidate([event('committed', 'u1')])]}
        if role == 'verify':
            return {'reviews': [{**{k: c[k] for k in ['id', 'digest']}, 'accepted': False}
                for c in payload['business_candidates']]}
        return {}
    publish = AsyncMock()
    result = await MeetingAgents(SimpleNamespace(run=AsyncMock(side_effect=reply))).analyze(
        [source('u1', '我方负责提交评估报告。')], on_actions_ready=publish)
    assert result['commitments'] == []
    assert not any(c.args[0].get('commitments') for c in publish.await_args_list)


async def test_real_text_prefix_arrives_before_role_finishes_and_is_not_business_state():
    received, release = asyncio.Event(), asyncio.Event()
    class Provider:
        async def stream(self, role, prompt, payload):
            if role == 'risk':
                text = json.dumps({'insights': [insight()]}, ensure_ascii=False)
                body_start = text.index('Windows') + 3
                # 元数据先到；body 还没生成完，模拟模型真实分块。
                ordered = {'id': 'r', 'kind': 'risk', 'title': '范围待确认',
                    'evidence_ids': ['u1'], 'inference': True, 'body': 'Windows尚未批准。'}
                text = json.dumps({'insights': [ordered]}, ensure_ascii=False)
                cut = text.index('Windows') + 3
                yield {'type': 'delta', 'text': text[:cut]}
                await release.wait()
                yield {'type': 'delta', 'text': text[cut:]}
                yield {'type': 'done', 'value': {'insights': [ordered]}}
            else:
                yield {'type': 'done', 'value': {}}
        async def run(self, role, prompt, payload):
            if role == 'verify':
                return payload['candidates'][0]
            return {}
    async def publish(value):
        if any(i['body'] == 'Win' for i in value['insights']):
            assert value['preview_kind'] == 'source_checked_inference_only'
            assert not value['goals'] and not value['commitments']
            received.set()
    task = asyncio.create_task(MeetingAgents(Provider()).analyze(
        [source('u1', '合同范围尚未批准。')], on_core_ready=publish))
    try:
        await asyncio.wait_for(received.wait(), 2)
        assert not task.done()
    finally:
        release.set()
        await task


async def test_two_background_network_waits_leave_one_realtime_slot(monkeypatch):
    provider = HermesProvider(Settings())
    release, ready = asyncio.Event(), asyncio.Event()
    active = []
    async def stream(role, *args, **kwargs):
        # 与生产一致：只把冷启动放入串行区，网络等待在区外。
        async with provider._startup_capacity:
            active.append(role)
        if len(active) == 3:
            ready.set()
        await release.wait()
        yield {'type': 'done', 'value': {}}
    monkeypatch.setattr(provider, '_stream_unbounded', stream)
    async def collect(role):
        return [e async for e in provider._stream_once(role, '', {})]
    tasks = [asyncio.create_task(collect(r)) for r in ['risk', 'memory', 'forecast', 'live']]
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert set(active) == {'risk', 'memory', 'live'}
    finally:
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.parametrize('field,value', [('correction', '不要提交'), ('speaker_id', 'b'),
    ('speaker_revision', 2), ('turn_status', 'interrupted'), ('withdrawn', True)])
def test_source_correction_or_identity_change_invalidates_cache(field, value):
    raw = [source('u1', '提交评估报告。')]
    hashes = transcript_hashes(raw)
    raw[0][field] = value
    assert changed_sources(raw, {'previous_analysis': {'source_hashes': hashes}}) == raw


def test_review_binding_changes_for_auth_documents_and_explicit_changes_but_not_smalltalk():
    raw = [source('u1', '我方负责提交评估报告。')]
    c = candidate([event('committed', 'u1')])
    original = review_binding(c, raw, {})
    assert review_binding(c, [*raw, source('u2', '休息一下。')], {}) == original
    assert review_binding(c, [*raw, source('u2', '请不要提交报告。')], {}) != original
    assert review_binding(c, raw, {'knowledge_revision': 2}) != original
    assert review_binding(c, raw, {'progress': [{'version': 2}]}) != original


def test_incremental_context_keeps_old_commitment_evidence_without_repeating_50_turns():
    raw = [source('u1', '我方负责提交评估报告。')]
    raw += [source('u'+str(i), '继续讨论。') for i in range(2, 52)]
    context = {'previous_analysis': {'source_hashes': transcript_hashes(raw[:-1]),
        'commitments': [candidate([event('committed', 'u1')])]}}
    selected = role_sources(raw, context)
    assert raw[0] in selected and raw[-1] in selected
    assert len(selected) < 10


async def test_questionnaire_fallback_is_instant_and_can_be_enriched_later():
    provider = SimpleNamespace(run=AsyncMock(return_value={'questions': []}))
    gap = {'id': 'g', 'origin_id': 'u1', 'field': 'deadline', 'subject': '提交报告',
        'fallback': '何时提交？', 'evidence_ids': ['u1']}
    draft = await DecisionQuestionnaire(provider).build([gap], allow_model=False)
    assert draft['deferred'] and draft['questions']
    provider.run.assert_not_awaited()
    await DecisionQuestionnaire(provider).build([gap], previous=draft)
    provider.run.assert_awaited_once()


async def test_job_events_publish_immediately_and_enforce_owner_scope():
    jobs = MeetingJobs(None, None)
    job = {'id': 'j', 'owner_id': 'owner', 'revision': 1, 'operation': 'analysis',
        'status': 'running', 'result': None, 'error': None, 'updated': 0}
    jobs.jobs['j'] = job
    assert [v async for v in jobs.watch('other', 'j')] == []
    watcher = jobs.watch('owner', 'j')
    assert (await anext(watcher))['data']['status'] == 'running'
    pending = asyncio.create_task(anext(watcher))
    await asyncio.sleep(0)
    job['preview'] = {'insights': [insight()]}
    jobs.signal(job)
    assert (await asyncio.wait_for(pending, 1))['data']['preview']['insights']
    job['status'] = 'succeeded'
    job['updated'] = time.monotonic()
    jobs.signal(job)
    assert (await anext(watcher))['data']['status'] == 'succeeded'
    await watcher.aclose()


@pytest.mark.parametrize('role', ['state', 'commitment'])
async def test_complete_business_item_reviewed_and_published_while_model_still_generating(role):
    release, published = asyncio.Event(), asyncio.Event()
    raw = '本期交付Android与iOS应用，我方负责提交评估报告。'
    item = ({'origin_id': 'u1', 'title': '双端交付', 'condition': '交付Android与iOS应用',
        'evidence_ids': ['u1']} if role == 'state' else candidate([event('committed', 'u1')]))
    field = 'goals' if role == 'state' else 'commitments'
    class Provider:
        async def stream(self, current, prompt, payload):
            if current == role:
                text = json.dumps({field: [item]}, ensure_ascii=False)
                yield {'type': 'delta', 'text': text[:-2]}
                await release.wait()
                yield {'type': 'delta', 'text': text[-2:]}
                yield {'type': 'done', 'value': {field: [item]}}
            else:
                yield {'type': 'done', 'value': {}}
        async def run(self, current, prompt, payload):
            if current == 'verify':
                return {'reviews': [{**{k: c[k] for k in ['id', 'digest']}, 'accepted': True}
                    for c in payload['business_candidates']]}
            return {}
    async def publish(value):
        if value.get(field):
            published.set()
    task = asyncio.create_task(MeetingAgents(Provider()).analyze([source('u1', raw)], on_actions_ready=publish))
    try:
        await asyncio.wait_for(published.wait(), 2)
        assert not task.done()
    finally:
        release.set()
        result = await task
    assert result[field]


async def test_pending_review_precedes_new_forecast_and_cancelled_waiter_does_not_leak():
    slots = RequestSlots(1)
    order = []
    async def use(name, priority):
        async with slots.slot(priority):
            order.append(name)
    async with slots.slot():
        forecast = asyncio.create_task(use('forecast', 8))
        cancelled = asyncio.create_task(use('cancelled', 0))
        review = asyncio.create_task(use('verify', 0))
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
    await asyncio.gather(forecast, review)
    assert order == ['verify', 'forecast']
    assert not slots.waiters and slots.active == 0


async def test_persisted_goal_metadata_does_not_break_incremental_review_reuse():
    goal = {'origin_id': 'u1', 'title': '双端交付', 'condition': '交付Android与iOS应用', 'evidence_ids': ['u1']}
    async def reply(role, prompt, payload):
        if role == 'state' and payload['delta_ids'] == ['u1']:
            return {'goals': [goal]}
        if role == 'verify':
            return {'reviews': [{**{k: c[k] for k in ['id', 'digest']}, 'accepted': True}
                for c in payload['business_candidates']]}
        return {}
    provider = SimpleNamespace(run=AsyncMock(side_effect=reply))
    agent = MeetingAgents(provider)
    raw = [source('u1', '本期交付Android与iOS应用。')]
    first = guarded_report(await agent.analyze(raw), raw)
    assert first['goals'][0]['authorization_status'] == 'pending'
    provider.run.reset_mock()
    raw.append(source('u2', '大家先休息一下。'))
    second = await agent.analyze(raw, {'previous_analysis': first})
    assert second['goals']
    assert [c.args[0] for c in provider.run.await_args_list] == ['state']


async def test_current_prefix_replaces_old_hint_body_with_same_evidence_and_title():
    snapshots = []
    async def publish(value):
        snapshots.append(value)
    review = ProgressiveReview(None, [source('u1', 'Windows范围尚未批准。')], {}, publish)
    review.parts['previous'] = {'insights': [insight()]}
    await review.partial('risk', {'insights': [{**insight(), 'body': 'Win', 'streaming': True}]})
    assert snapshots[-1]['insights'][0]['body'] == 'Win'
    assert snapshots[-1]['insights'][0]['streaming']
