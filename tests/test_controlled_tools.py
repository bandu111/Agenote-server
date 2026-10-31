"""真实数据库/检查点验证审批、失败恢复、幂等和证据边界。"""
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from tests.test_meetings import account, meeting
from app.modules.execution.schemas import ToolPlan
from app.modules.execution.tools import ControlledTools


def plan():
    return {'title': '创建测试任务并归档草稿', 'evidence_ids': ['u1'], 'steps': [
        {'id': 'task', 'tool': 'task_create', 'args': {'title': '双端测试报告'}},
        {'id': 'doc', 'tool': 'document_create', 'args': {'title': '测试方案草稿', 'content': '# AI 草稿\n待核对，不表示已经交付。'}},
        {'id': 'attach', 'tool': 'task_attach', 'args': {'taskId': '${task.taskId}', 'documentId': '${doc.documentId}', 'versionId': '${doc.versionId}', 'hash': '${doc.hash}'}},
        {'id': 'check', 'tool': 'task_verify', 'args': {'taskId': '${task.taskId}', 'documentId': '${doc.documentId}', 'versionId': '${doc.versionId}', 'hash': '${doc.hash}'}},
    ]}


async def prepared(client, app):
    headers = await account(client)
    source = meeting()
    source['capabilities'] = ['tool_plans_v1']
    source['utterances'][0]['raw'] = '请创建任务并生成测试方案文档，保留为待核对草稿。'
    await client.post('/api/v1/meetings/sync', headers=headers, json=source)
    app.state.meeting_analysis.agents.analyze = AsyncMock(return_value={'insights': [], 'repairs': [], 'withdrawals': [], 'tool_plans': [plan()]})
    response = await client.post('/api/v1/meetings/phone-meeting-1/analysis', headers=headers)
    assert response.status_code == 200, response.text
    assert (await client.get('/api/v1/actions', headers=headers)).json()['data'] == []
    actions = (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data']
    assert len(actions) == 1
    return headers, source, actions[0]


async def test_approval_executes_task_file_attachment_and_integrity_without_claiming_acceptance(client, app):
    headers, _, action = await prepared(client, app)
    assert (await client.get('/api/v1/tasks', headers=headers)).json()['data'] == []
    payload = {'fingerprint': action['fingerprint'], 'allow': True, 'deviceId': 'synthetic-device-123'}
    path = '/api/v1/actions/' + action['id'] + '/decision'
    response = await client.post(path, headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['data']['status'] == 'completed', response.text
    task = (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]
    assert task['status'] == 'evidence_verified' and task['customerAcceptance'] == 'pending'
    assert task['verificationScope'] == 'document_integrity_only'
    assert len(task['attachments']) == 1
    library = (await client.get('/api/v1/knowledge', headers=headers)).json()['data']['snapshot']
    assert len(library['documents']) == 1
    assert library['documents'][0]['fileData']
    assert (await client.post(path, headers=headers, json=payload)).json()['data']['status'] == 'completed'
    assert len((await client.get('/api/v1/tasks', headers=headers)).json()['data']) == 1
    other = await account(client, 'tool-other@example.com')
    assert (await client.get('/api/v1/tasks', headers=other)).json()['data'] == []
    assert (await client.post(path, headers=other, json=payload)).status_code == 404
    assert (await client.get('/api/v1/home', headers=headers)).status_code == 200


async def test_failed_second_step_resumes_without_duplicate_first_step(client, app, monkeypatch):
    headers, _, action = await prepared(client, app)
    original = ControlledTools.execute
    failed = False
    async def flaky(self, session, owner, row, tool, args, key):
        nonlocal failed
        if tool == 'document_create' and not failed:
            failed = True
            raise HTTPException(503, 'synthetic_temporary_failure')
        return await original(self, session, owner, row, tool, args, key)
    monkeypatch.setattr(ControlledTools, 'execute', flaky)
    payload = {'fingerprint': action['fingerprint'], 'allow': True, 'deviceId': 'synthetic-device-123'}
    path = '/api/v1/actions/' + action['id'] + '/decision'
    response = await client.post(path, headers=headers, json=payload)
    assert response.json()['data']['status'] == 'retryable', response.text
    assert len((await client.get('/api/v1/tasks', headers=headers)).json()['data']) == 1
    response = await client.post(path, headers=headers, json=payload)
    assert response.json()['data']['status'] == 'completed', response.text
    assert len((await client.get('/api/v1/tasks', headers=headers)).json()['data']) == 1


async def test_changed_source_and_decline_cannot_execute(client, app):
    headers, source, action = await prepared(client, app)
    source['revision'] = 2
    source['utterances'][0]['correction'] = '请不要创建任务。'
    assert (await client.post('/api/v1/meetings/sync', headers=headers, json=source)).status_code == 200
    response = await client.post('/api/v1/actions/' + action['id'] + '/decision', headers=headers,
        json={'fingerprint': action['fingerprint'], 'allow': True, 'deviceId': 'synthetic-device-123'})
    assert response.status_code == 409
    assert (await client.get('/api/v1/tasks', headers=headers)).json()['data'] == []
    declined = await client.post('/api/v1/actions/' + action['id'] + '/decision', headers=headers,
        json={'fingerprint': action['fingerprint'], 'allow': False, 'deviceId': 'synthetic-device-123'})
    assert declined.status_code == 200 and declined.json()['data']['status'] == 'declined'


def test_tool_schema_blocks_paths_shell_unknown_tools_and_forward_references():
    value = plan()
    value['steps'][0]['args']['path'] = '/etc/passwd'
    with pytest.raises(ValueError): ToolPlan.model_validate(value)
    value = plan(); value['steps'][0]['tool'] = 'terminal'
    with pytest.raises(ValueError): ToolPlan.model_validate(value)
    value = plan(); value['steps'][0]['args']['title'] = '${doc.text}'
    with pytest.raises(ValueError): ToolPlan.model_validate(value)
