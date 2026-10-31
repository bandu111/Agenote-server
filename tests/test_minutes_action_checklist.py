from unittest.mock import AsyncMock
from tests.test_meetings import account, meeting


async def archive(client, app, headers, client_id='minutes-checklist'):
    source = meeting()
    source.update(id=client_id, ended=True)
    source['utterances'] = [
        {'id':'u1', 'speaker':'甲', 'raw':'乙负责提交接口评估报告。', 'at':1000},
        {'id':'u2', 'speaker':'乙', 'raw':'我负责准备回归测试用例。', 'at':2000}]
    await client.post('/api/v1/meetings/sync', headers=headers, json=source)
    app.state.meeting_analysis.agents.provider.run = AsyncMock(return_value={
        'actions': [{'text':'提交接口评估报告', 'owner':'乙', 'evidence_ids':['u1']},
                    {'text':'准备回归测试用例', 'evidence_ids':['u2']}],
        'decisions':[], 'topics':[], 'open_questions':[]})
    response = await client.post('/api/v1/meetings/'+client_id+'/archive', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()['data'], source


async def test_home_and_minutes_share_actions_completion_and_routes(client, app):
    headers = await account(client)
    other = await account(client, 'other-minutes-action@example.com')
    saved, _ = await archive(client, app, headers)
    actions = saved['details']['actions']
    assert all(a['taskId'] and a['taskVersion'] for a in actions)
    tasks = (await client.get('/api/v1/home', headers=headers)).json()['data']['tasks']
    assert {t['id'] for t in tasks} == {a['taskId'] for a in actions}
    first = tasks[0]
    assert first['sourceRoute'] == '/meetings/minutes-checklist/minutes?action='+actions[0]['id']
    url = '/api/v1/home/tasks/'+first['id']+'/complete'
    assert (await client.post(url, headers=other, json={'version':first['version']})).status_code == 404
    assert (await client.post(url, headers=headers, json={'version':99})).status_code == 409
    result = await client.post(url, headers=headers, json={'version':first['version']})
    assert result.status_code == 200, result.text
    assert result.json()['data']['completed']
    # Reload both sides; a new client gets the same state, not a local-only tick.
    restored = (await client.get('/api/v1/meetings/minutes-checklist/archive', headers=headers)).json()['data']
    assert restored['details']['actions'][0]['completed']
    assert not restored['details']['actions'][1]['completed']
    assert next(t for t in (await client.get('/api/v1/home', headers=headers)).json()['data']['tasks'] if t['id']==first['id'])['completed']
    assert (await client.post(url, headers=headers, json={'version':first['version']})).status_code == 200
    assert (await client.get('/api/v1/home', headers=other)).json()['data']['tasks'] == []
    assert app.state.meeting_analysis.agents.provider.run.await_count == 1


async def test_multiple_meetings_and_deleted_meeting_no_longer_has_tasks(client, app):
    headers = await account(client)
    await archive(client, app, headers, 'first-meeting')
    await archive(client, app, headers, 'second-meeting')
    assert len((await client.get('/api/v1/home', headers=headers)).json()['data']['tasks']) == 4
    assert (await client.request('DELETE', '/api/v1/meetings/first-meeting', headers=headers, json={'revision':1})).status_code == 200
    remaining = (await client.get('/api/v1/home', headers=headers)).json()['data']['tasks']
    assert len(remaining) == 2 and all(t['sessionId'] == 'second-meeting' for t in remaining)


async def test_new_revision_preserves_same_action_completion_but_does_not_complete_other_actions(client, app):
    headers = await account(client)
    saved, source = await archive(client, app, headers)
    action = saved['details']['actions'][0]
    await client.post('/api/v1/home/tasks/'+action['taskId']+'/complete', headers=headers, json={'version':1})
    source['revision'] = 2
    source['elapsed'] += 1000
    source['utterances'].append({'id':'u3','speaker':'甲','raw':'补充说明。','at':3000})
    assert (await client.post('/api/v1/meetings/sync', headers=headers, json=source)).status_code == 200
    assert (await client.get('/api/v1/home', headers=headers)).json()['data']['tasks'] == []
    # Same actions, reordered by the next model run, retain stable identities and completion.
    app.state.meeting_analysis.agents.provider.run.return_value['actions'].reverse()
    result = await client.post('/api/v1/meetings/minutes-checklist/archive', headers=headers)
    assert result.status_code == 200, result.text
    actions = result.json()['data']['details']['actions']
    completed = next(a for a in actions if a['taskId'] == action['taskId'])
    assert completed['completed'] and completed['taskVersion'] == 1000000002
    assert not next(a for a in actions if a['taskId'] != action['taskId'])['completed']
