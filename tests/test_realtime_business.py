"""业务小批次的真实数据库投影、统一校验、连续发言及审批边界。"""
import asyncio
import json
from copy import deepcopy
import pytest
from tests.test_meetings import account


async def test_action_item_publishes_before_state_and_model_end_and_survives_new_speech(client, app):
    headers = await account(client)
    initial = source()
    await client.post('/api/v1/meetings/sync', headers=headers, json=initial)
    entered, release = asyncio.Event(), asyncio.Event()
    async def stream(role, system, payload):
        if role == 'business_realtime':
            yield {'type': 'delta', 'text': json.dumps({'tool_plans': draft()['tool_plans']})[:-1]}
            entered.set()
            await release.wait()
            yield {'type': 'done', 'value': draft()}
        else:
            yield {'type': 'done', 'value': {'accepted': list(range(len(payload['business_candidates'])))}}
    app.state.meeting_analysis.agents.provider.stream = stream
    work = asyncio.create_task(run(client, headers))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        actions = (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data']
        assert len(actions) == 1 and actions[0]['status'] == 'pending'
        assert not work.done()
        initial['revision'] = 2
        initial['utterances'].append({'id': 'next', 'speaker': '发言人 B', 'raw': '接下来讨论测试环境。', 'at': 3000})
        await client.post('/api/v1/meetings/sync', headers=headers, json=initial)
        action = actions[0]
        result = await client.post('/api/v1/actions/'+action['id']+'/decision', headers=headers,
            json={'allow': True, 'fingerprint': action['fingerprint'], 'deviceId': 'synthetic-device-123'})
        assert result.status_code == 200, result.text
        assert result.json()['data']['status'] == 'completed', result.text
        assert len((await client.get('/api/v1/tasks', headers=headers)).json()['data']) == 1
    finally:
        release.set()
        await work


async def test_withdrawal_is_prepared_during_extraction_but_cancels_only_after_owner_approval(client, app):
    headers = await account(client)
    initial = source()
    await client.post('/api/v1/meetings/sync', headers=headers, json=initial)
    provider(app)
    await run(client, headers)
    task_plan = (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data'][0]
    from tests.test_execution_lifecycle import execute
    assert (await execute(client, headers, task_plan)).status_code == 200
    initial['revision'] = 2
    initial['utterances'].append({'id': 'cancel', 'speaker': '发言人 A',
        'raw': '现在确认，撤回之前提交评估报告的任务。', 'at': 5000})
    await client.post('/api/v1/meetings/sync', headers=headers, json=initial)
    entered, release = asyncio.Event(), asyncio.Event()
    value = {'withdrawals': [{'target_id': 't', 'evidence_ids': ['cancel'], 'reason': '本人确认撤回'}]}
    async def stream(*_):
        yield {'type': 'delta', 'text': json.dumps(value)[:-1]}
        entered.set()
        await release.wait()
        yield {'type': 'done', 'value': value}
    app.state.meeting_analysis.agents.provider.stream = stream
    work = asyncio.create_task(run(client, headers, 2))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        actions = (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data']
        cancel = next(a for a in actions if a['title'].startswith('撤回'))
        assert not work.done()
        tasks = (await client.get('/api/v1/tasks', headers=headers)).json()['data']
        assert tasks[0]['status'] != 'cancelled'
        result = await execute(client, headers, cancel)
        assert result.status_code == 200, result.text
        assert result.json()['data']['status'] == 'completed', result.text
        assert (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]['status'] == 'cancelled'
    finally:
        release.set()
        await work


@pytest.mark.parametrize('change', ['取消之前的安排。', '刚才记录需要改为其他日期。'])
async def test_new_unreviewed_change_still_blocks_old_approval(client, app, change):
    headers = await account(client)
    initial = source()
    await client.post('/api/v1/meetings/sync', headers=headers, json=initial)
    provider(app)
    await run(client, headers)
    action = (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data'][0]
    initial['revision'] = 2
    initial['utterances'].append({'id': 'change', 'speaker': '发言人 A', 'raw': change, 'at': 3000})
    await client.post('/api/v1/meetings/sync', headers=headers, json=initial)
    result = await client.post('/api/v1/actions/'+action['id']+'/decision', headers=headers,
        json={'allow': True, 'fingerprint': action['fingerprint'], 'deviceId': 'synthetic-device-123'})
    assert result.status_code == 409
    assert not (await client.get('/api/v1/tasks', headers=headers)).json()['data']


def source(revision=1):
    return {'id': 'realtime-test', 'revision': revision, 'capabilities': ['turns_v1', 'tool_plans_v1'],
        'startedAt': '2026-10-21T10:00:00+08:00', 'elapsed': 30000, 'utterances': [
            {'id': 'g', 'speaker': '发言人 A', 'speakerId': 'a', 'speakerSeparated': True,
                'raw': '本期交付目标是完成双端测试，交期2030年10月30日。', 'at': 0},
            {'id': 'c', 'speaker': '发言人 B', 'speakerId': 'b', 'speakerSeparated': True,
                'raw': '对方承诺周五提供测试账号。', 'at': 1000},
            {'id': 't', 'speaker': '发言人 A', 'speakerId': 'a', 'speakerSeparated': True,
                'raw': '请创建任务提交评估报告。', 'at': 2000}]}


def draft():
    return {'goals': [{'origin_id': 'g', 'title': '完成双端测试', 'condition': '完成双端测试',
        'deadline_text': '2030年10月30日', 'evidence_ids': ['g']}],
        'commitments': [{'origin_id': 'c', 'speaker_id': 'b', 'action': '提供测试账号', 'action_quote': '提供测试账号',
            'side': 'theirs', 'side_evidence_ids': ['c'], 'events': [{'kind': 'committed', 'evidence_ids': ['c'],
                'summary': '对方提供账号', 'deadline_text': '周五'}]}],
        'state': [{'topic': '交付', 'value': '完成双端测试', 'status': 'confirmed', 'evidence_ids': ['g']}],
        'tool_plans': [{'title': '创建评估报告任务', 'evidence_ids': ['t'], 'steps': [
            {'id': 'create', 'tool': 'task_create', 'args': {'title': '提交评估报告'}}]}]}


def provider(app, *, accepted=True, wait=None):
    async def stream(role, system, payload):
        if role == 'business_realtime':
            yield {'type': 'done', 'value': draft()}
        else:
            assert role == 'business_verify'
            if wait is not None: await wait.wait()
            yield {'type': 'done', 'value': {'reviews': [{
                'id': c['id'], 'digest': c['digest'], 'accepted': accepted, 'reason': '原话核对'}
                for c in payload['business_candidates']]}}
    app.state.meeting_analysis.agents.provider.stream = stream


async def run(client, headers, revision=1):
    result = await client.post('/api/v1/meetings/realtime-test/realtime', headers=headers, json={'revision': revision})
    assert result.status_code == 200, result.text
    return [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]


async def test_realtime_updates_goals_their_commitment_home_and_pending_notice_without_full_analysis(client, app):
    headers = await account(client)
    await client.post('/api/v1/meetings/sync', headers=headers, json=source())
    provider(app)
    values = await run(client, headers)
    assert values[-1]['event'] == 'done' and values[-1]['business_updated'], values
    snapshot = (await client.get('/api/v1/progress', headers=headers)).json()['data']
    assert snapshot['goals'][0]['title'] == '完成双端测试'
    commitment = snapshot['commitments'][0]
    assert commitment['side'] == 'theirs' and commitment['status'] == 'pending'
    assert commitment['authorizationStatus'] == 'pending'
    home = (await client.get('/api/v1/home', headers=headers)).json()['data']
    assert home['rows'][0]['text'] == '完成双端测试'
    actions = (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data']
    assert len(actions) == 1 and actions[0]['status'] == 'pending'
    assert not (await client.get('/api/v1/tasks', headers=headers)).json()['data']
    assert not app.state.meeting_jobs.tasks
    # 同版重试复用结果，不重复创建目标、承诺或审批。
    assert (await run(client, headers))[-1]['cached']
    assert len((await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data']) == 1
    other = await account(client, 'other-realtime@example.com')
    assert (await client.post('/api/v1/meetings/realtime-test/realtime', headers=other, json={'revision': 1})).status_code == 404


async def test_unified_review_rejection_never_writes_goals_or_commitments(client, app):
    headers = await account(client)
    await client.post('/api/v1/meetings/sync', headers=headers, json=source())
    provider(app, accepted=False)
    assert (await run(client, headers))[-1]['business_updated']
    snapshot = (await client.get('/api/v1/progress', headers=headers)).json()['data']
    assert not snapshot['goals'] and not snapshot['commitments'] and not snapshot['pending']


@pytest.mark.parametrize('correction', [False, True])
async def test_new_speech_does_not_starve_current_batch_but_correction_blocks_old_results(client, app, correction):
    headers = await account(client)
    original = source()
    await client.post('/api/v1/meetings/sync', headers=headers, json=original)
    release = asyncio.Event()
    provider(app, wait=release)
    work = asyncio.create_task(run(client, headers))
    await asyncio.sleep(.1)
    newer = deepcopy(original)
    newer['revision'] = 2
    if correction:
        newer['utterances'][0]['correction'] = '本期目标尚未批准。'
    else:
        newer['utterances'].append({'id': 'next', 'speaker': '发言人 A', 'raw': '我们继续讨论预算。', 'at': 3000})
    assert (await client.post('/api/v1/meetings/sync', headers=headers, json=newer)).status_code == 200
    release.set()
    events = await work
    snapshot = (await client.get('/api/v1/progress', headers=headers)).json()['data']
    if correction:
        assert events[-1]['event'] == 'error' and not snapshot['goals']
    else:
        assert events[-1]['event'] == 'done' and snapshot['goals']
        home = (await client.get('/api/v1/home', headers=headers)).json()['data']
        assert home['rows'], home
        # 原来源未变且只追加普通发言时，旧审批仍能执行。
        action = (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data'][0]
        from tests.test_execution_lifecycle import execute
        assert (await execute(client, headers, action)).status_code == 200


async def test_next_speech_keeps_home_but_source_correction_invalidates_realtime_snapshot(client, app):
    headers = await account(client)
    original = source()
    await client.post('/api/v1/meetings/sync', headers=headers, json=original)
    provider(app)
    await run(client, headers)
    original['revision'] = 2
    original['utterances'].append({'id': 'next', 'speaker': '发言人 A', 'raw': '继续讨论。', 'at': 3000})
    await client.post('/api/v1/meetings/sync', headers=headers, json=original)
    assert (await client.get('/api/v1/home', headers=headers)).json()['data']['rows'][0]['text'] == '完成双端测试'
    original['revision'] = 3
    original['utterances'][0]['correction'] = '原目标尚未批准。'
    await client.post('/api/v1/meetings/sync', headers=headers, json=original)
    home = (await client.get('/api/v1/home', headers=headers)).json()['data']
    assert not any(r['text'] == '完成双端测试' and r['label'] == '待核对' for r in home['rows'])
    snapshot = (await client.get('/api/v1/progress', headers=headers)).json()['data']
    assert not snapshot['goals']
    assert any(p['kind'] == 'goal' and '原话已变更' in p['reason'] for p in snapshot['pending'])


async def test_compact_review_binds_only_given_indices_and_rejects_out_of_range(client, app):
    headers = await account(client)
    await client.post('/api/v1/meetings/sync', headers=headers, json=source())
    async def stream(role, *_):
        yield {'type': 'done', 'value': draft() if role == 'business_realtime' else {'accepted': [999]}}
    app.state.meeting_analysis.agents.provider.stream = stream
    values = await run(client, headers)
    assert values[-1]['event'] == 'error'
    assert not (await client.get('/api/v1/progress', headers=headers)).json()['data']['goals']

@pytest.mark.parametrize('accept', [True, False])
async def test_candidates_visible_before_review_but_cannot_authorize_and_rejected_preview_disappears(client, app, accept):
    headers = await account(client)
    await client.post('/api/v1/meetings/sync', headers=headers, json=source())
    entered, release = asyncio.Event(), asyncio.Event()
    async def stream(role, system, payload):
        if role == 'business_realtime':
            yield {'type': 'done', 'value': draft()}
        else:
            entered.set()
            await release.wait()
            yield {'type': 'done', 'value': {'accepted': list(range(len(payload['business_candidates']))) if accept else []}}
    app.state.meeting_analysis.agents.provider.stream = stream
    work = asyncio.create_task(run(client, headers))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        progress = (await client.get('/api/v1/progress', headers=headers)).json()['data']
        goal, commitment = progress['goals'][0], progress['commitments'][0]
        assert goal['reviewPending'] and commitment['reviewPending']
        assert not work.done()
        response = await client.post('/api/v1/progress/commitments/' + commitment['id'] + '/status',
            headers=headers, json={'version': commitment['version'], 'status': 'planned', 'note': '不可提前批准'})
        assert response.status_code == 409
        assert (await client.get('/api/v1/home', headers=headers)).json()['data']['rows']
        assert (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data']
        assert not (await client.get('/api/v1/tasks', headers=headers)).json()['data']
    finally:
        release.set()
    events = await work
    assert any(e['event'] == 'preview' and e['business_updated'] for e in events)
    progress = (await client.get('/api/v1/progress', headers=headers)).json()['data']
    if accept:
        assert progress['goals'][0]['id'] == goal['id']
        assert not progress['goals'][0]['reviewPending']
    else:
        assert not progress['goals'] and not progress['commitments']

async def test_complete_candidate_is_published_while_model_is_still_generating(client, app):
    headers = await account(client)
    await client.post('/api/v1/meetings/sync', headers=headers, json=source())
    entered, release = asyncio.Event(), asyncio.Event()
    async def stream(role, system, payload):
        if role == 'business_realtime':
            value = draft()
            # 模型只完成了前两组字段，后面的承诺和行动还未生成。
            prefix = json.dumps({'state': value['state'], 'goals': value['goals']},ensure_ascii=False)[:-1]
            yield {'type':'delta','text':prefix}
            entered.set()
            await release.wait()
            yield {'type':'done','value':value}
        else:
            yield {'type':'done','value':{'accepted':list(range(len(payload['business_candidates'])))}}
    app.state.meeting_analysis.agents.provider.stream=stream
    work=asyncio.create_task(run(client,headers))
    try:
        await asyncio.wait_for(entered.wait(),3)
        snapshot=(await client.get('/api/v1/progress',headers=headers)).json()['data']
        assert snapshot['goals'][0]['reviewPending'] is True
        assert not work.done()
        assert not snapshot['commitments']
        home=(await client.get('/api/v1/home',headers=headers)).json()['data']
        assert home['rows']
    finally: release.set()
    events=await work
    assert any(e.get('extracting') for e in events)
    assert events[-1]['event']=='done'
    assert not (await client.get('/api/v1/progress',headers=headers)).json()['data']['goals'][0]['reviewPending']


async def test_unchanged_candidate_reuses_review_but_source_change_does_not(client, app):
    headers=await account(client)
    initial=source()
    await client.post('/api/v1/meetings/sync',headers=headers,json=initial)
    calls=[]
    async def stream(role, system, payload):
        calls.append(role)
        yield {'type':'done','value':draft() if role=='business_realtime' else {'accepted':list(range(len(payload['business_candidates'])))}}
    app.state.meeting_analysis.agents.provider.stream=stream
    await run(client,headers)
    initial['revision']=2
    initial['utterances'].append({'id':'extra','speaker':'发言人 A','raw':'继续讨论。','at':3000})
    await client.post('/api/v1/meetings/sync',headers=headers,json=initial)
    await run(client,headers,2)
    assert calls.count('business_verify')==1
    initial['revision']=3
    initial['utterances'][1]['correction']='对方承诺周五提供测试账号，具体范围待核对。'
    await client.post('/api/v1/meetings/sync',headers=headers,json=initial)
    await run(client,headers,3)
    assert calls.count('business_verify')==2


def test_partial_business_json_is_not_a_candidate_and_nested_text_cannot_inject_items():
    from app.modules.meetings.services.realtime_stream import complete_items
    value=draft()
    incomplete=json.dumps({'goals':value['goals']},ensure_ascii=False)
    assert not complete_items(incomplete[:incomplete.index('condition')+8])
    nested=json.dumps({'notes':json.dumps({'goals':value['goals']})})
    assert not complete_items(nested)

async def test_failed_partial_extraction_retries_unverified_sources_instead_of_treating_them_as_processed(client, app):
    headers=await account(client)
    await client.post('/api/v1/meetings/sync',headers=headers,json=source())
    async def fail(role,system,payload):
        yield {'type':'delta','text':json.dumps({'goals':draft()['goals']},ensure_ascii=False)}
        raise ValueError('synthetic_stream_failure')
    app.state.meeting_analysis.agents.provider.stream=fail
    assert (await run(client,headers))[-1]['event']=='error'
    assert not (await client.get('/api/v1/progress',headers=headers)).json()['data']['goals']
    async def recover(role,system,payload):
        if role=='business_realtime':
            assert set(payload['new_utterance_ids'])=={'g','c','t'}
            yield {'type':'done','value':draft()}
        else:
            yield {'type':'done','value':{'accepted':list(range(len(payload['business_candidates'])))}}
    app.state.meeting_analysis.agents.provider.stream=recover
    assert (await run(client,headers))[-1]['event']=='done'
    assert (await client.get('/api/v1/progress',headers=headers)).json()['data']['goals']
