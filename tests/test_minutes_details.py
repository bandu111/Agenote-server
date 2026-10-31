from unittest.mock import AsyncMock
from app.modules.meetings.services.minutes_details import validate_details, legacy_details, enrich_actions
from tests.test_meetings import account, meeting


def test_details_drop_missing_withdrawn_and_unfinished_decisions():
    sources = [{'id': 'u1', 'at': 1000, 'raw': '接口先验证'},
               {'id': 'u2', 'at': 2000, 'raw': '取消了', 'withdrawn': True},
               {'id': 'u3', 'at': 3000, 'raw': '我们先…', 'turn_status': 'interrupted'}]
    result = validate_details({'decisions': [
        {'text': '接口先验证', 'evidence_ids': ['u1']},
        {'text': '不能留下', 'evidence_ids': ['u2']},
        {'text': '虚构结论', 'evidence_ids': ['missing']},
        {'text': '半句话不是决定', 'evidence_ids': ['u3']},
    ], 'open_questions': [{'text': '尚未说完的事项', 'evidence_ids': ['u3']}]}, sources)
    assert len(result['decisions']) == 1
    assert result['decisions'][0]['at'] == 1000
    assert result['summary'] == '接口先验证'
    assert len(result['open_questions']) == 1


def test_legacy_archive_has_real_evidence_and_no_generated_agreement():
    result = legacy_details('## 当前共识\n- 先做接口验证〔来源：u1〕\n## 待确认\n- 发布时间待确认〔来源：missing〕',
                            [{'id': 'u1', 'at': 12000, 'raw': '先做接口验证'}])
    assert result['decisions'][0]['evidence_ids'] == ['u1']
    assert result['open_questions'] == []


def test_task_binding_requires_same_meeting_evidence_and_action():
    details = {'actions': [{'text': '提交测试报告', 'evidence_ids': ['u1']}]}
    unrelated = {'id': 'other', 'kind': 'internal_task', 'version': 1, 'title': '提交测试报告',
                 'evidenceIds': ['u1'], 'sessionId': 'another-meeting', 'status': 'completed'}
    assert enrich_actions(details, [unrelated], 'm')['actions'][0].get('taskId') is None
    unrelated.update(sessionId='m', status='active', evidenceIds=['wrong'])
    assert enrich_actions(details, [unrelated], 'm')['actions'][0].get('taskId') is None


async def test_structured_archive_persisted_cached_and_account_scoped(client, app):
    headers = await account(client)
    other = await account(client, 'other-minutes@example.com')
    content = meeting(1)
    content.update(ended=True)
    content['utterances'][0]['raw'] = '周三验收通过后，周四进行灰度发布。'
    assert (await client.post('/api/v1/meetings/sync', headers=headers, json=content)).status_code == 200
    provider = app.state.meeting_analysis.agents.provider
    provider.run = AsyncMock(return_value={
        'summary': '上线计划附带验收通过的前提。',
        'topics': [{'title': '上线安排', 'text': '周四灰度发布须先通过周三验收。', 'points': ['验收前提', '灰度发布'], 'evidence_ids': ['u1']}],
        'decisions': [{'text': '验收通过后才灰度发布', 'status': 'discussed', 'evidence_ids': ['u1']}],
        'actions': [], 'open_questions': [],
    })
    path = '/api/v1/meetings/phone-meeting-1/archive'
    saved = await client.post(path, headers=headers)
    assert saved.status_code == 200, saved.text
    data = saved.json()['data']
    assert data['details']['summary'] == '上线计划附带验收通过的前提。'
    assert data['details']['decisions'][0]['status'] == 'discussed'
    assert '上线安排' in data['markdown'] and 'u1' in data['markdown']
    consensus = data['markdown'].split('## 当前共识')[1].split('## 讨论中')[0]
    assert '验收通过后才灰度发布' not in consensus
    restored = await client.get(path, headers=headers)
    assert restored.json()['data']['details'] == data['details']
    assert (await client.get(path, headers=other)).status_code == 404
    await client.post(path, headers=headers)
    assert provider.run.await_count == 1


async def test_short_meeting_keeps_originals_without_model_or_knowledge_archive(client, app):
    headers = await account(client)
    data = meeting()
    data.update(ended=True, elapsed=59999)
    await client.post('/api/v1/meetings/sync', headers=headers, json=data)
    provider = app.state.meeting_analysis.agents.provider
    provider.run = AsyncMock()
    path = '/api/v1/meetings/phone-meeting-1/archive'
    response = await client.post(path, headers=headers)
    assert response.status_code == 200
    assert response.json()['data']['reason'] == 'meeting_too_short'
    assert response.json()['data']['documentId'] is None
    assert provider.run.await_count == 0
    original = await client.get('/api/v1/meetings/phone-meeting-1', headers=headers)
    assert original.json()['data']['utterances'][0]['raw'] == data['utterances'][0]['raw']
    assert (await client.get('/api/v1/knowledge', headers=headers)).json()['data']['snapshot']['documents'] == []
