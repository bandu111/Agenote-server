"""内部任务、目标投影、恢复工作器及受控 Hermes 工具的业务闭环。"""
import copy
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from tests.test_controlled_tools import prepared
from tests.test_meetings import account
from app.modules.actions.models import ActionRun
from app.modules.execution.tools import ControlledTools
from app.modules.execution.models import InternalTask
from app.modules.execution.planner import plan_work


async def execute(client, headers, action):
    return await client.post('/api/v1/actions/' + action['id'] + '/decision', headers=headers,
        json={'fingerprint': action['fingerprint'], 'allow': True, 'deviceId': 'synthetic-device-123'})


async def goal(client, headers):
    response = await client.post('/api/v1/progress/goals', headers=headers, json={
        'title': '测试交付', 'condition': '测试报告准备并提交', 'deadline': '2027-01-01T18:00:00+08:00',
        'milestones': [{'title': '测试报告', 'weight': 1}, {'title': '客户验收', 'weight': 1}]})
    assert response.status_code == 201, response.text
    return response.json()['data']


async def test_task_owner_confirmation_projects_only_explicit_milestone_and_version_changes_invalidate(client, app):
    headers, _, action = await prepared(client, app)
    assert (await execute(client, headers, action)).json()['data']['status'] == 'completed'
    task = (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]
    target = await goal(client, headers)
    binding = {'version': task['version'], 'goalId': target['id'], 'goalVersion': target['version'], 'milestoneTitle': '测试报告', 'note': '本人明确关联对应节点'}
    other = await account(client, 'other-task@example.com')
    path = '/api/v1/tasks/' + task['id']
    assert (await client.post(path + '/binding', headers=other, json=binding)).status_code == 404
    assert (await client.post(path + '/binding', headers=headers, json={**binding, 'milestoneTitle': '相似但不存在'})).status_code == 422
    linked = await client.post(path + '/binding', headers=headers, json=binding)
    assert linked.status_code == 200, linked.text
    task = linked.json()['data']
    goals = (await client.get('/api/v1/progress', headers=headers)).json()['data']['goals']
    assert goals[0]['milestones'][0]['status'] == 'active'
    # 文件存在不能自动完成，只有认证本人对当前版本的实际完成确认可推进。
    response = await client.post(path + '/status', headers=headers, json={'version': task['version'], 'status': 'verified', 'note': '本人已经完成报告提交'})
    assert response.status_code == 200, response.text
    assert (await client.post(path + '/status', headers=headers, json={'version': task['version'], 'status': 'verified', 'note': '重复旧版'})).status_code == 409
    current = (await client.get('/api/v1/progress', headers=headers)).json()['data']['goals'][0]
    assert [m['status'] for m in current['milestones']] == ['completed', 'pending']
    assert current['status'] != 'completed'
    assert response.json()['data']['customerAcceptance'] == 'pending'
    # 在已存资料发生版本变化后，旧完成依据不能继续推进目标。
    from app.modules.knowledge.models.library import KnowledgeLibrary
    async with app.state.database.sessions() as db:
        library = await db.scalar(select(KnowledgeLibrary))
        snapshot = copy.deepcopy(library.snapshot)
        snapshot['documents'][0]['versions'][0]['state'] = 'withdrawn'
        library.snapshot = snapshot
        await db.commit()
    task = (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]
    assert task['status'] == 'evidence_stale'
    current = (await client.get('/api/v1/progress', headers=headers)).json()['data']['goals'][0]
    assert current['milestones'][0]['status'] == 'active'


async def test_worker_resumes_approved_plan_without_duplicate_and_stops_at_retry_budget(client, app, monkeypatch):
    headers, _, action = await prepared(client, app)
    original = ControlledTools.execute
    async def failing(self, session, owner, row, tool, args, key):
        if tool == 'document_create':
            raise HTTPException(503, 'temporary_downstream_failure')
        return await original(self, session, owner, row, tool, args, key)
    monkeypatch.setattr(ControlledTools, 'execute', failing)
    result = await execute(client, headers, action)
    assert result.json()['data']['status'] == 'retryable'
    async def due():
        async with app.state.database.sessions() as db:
            row = await db.get(ActionRun, action['id'])
            row.content = {**row.content, 'nextAttemptAt': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
            await db.commit()
    for _ in range(2):
        await due()
        assert await app.state.execution_jobs.process_one()
    await due()
    assert not await app.state.execution_jobs.process_one()
    assert len((await client.get('/api/v1/tasks', headers=headers)).json()['data']) == 1
    monkeypatch.setattr(ControlledTools, 'execute', original)
    # 超过自动预算只允许本人显式重试，断点前的任务不重复创建。
    assert (await execute(client, headers, action)).json()['data']['status'] == 'completed'
    assert len((await client.get('/api/v1/tasks', headers=headers)).json()['data']) == 1


async def test_worker_blocks_changed_evidence_and_never_executes_unapproved_plan(client, app, monkeypatch):
    headers, source, action = await prepared(client, app)
    assert not await app.state.execution_jobs.process_one()
    original = ControlledTools.execute
    async def unavailable(*args, **kwargs):
        raise HTTPException(503, 'temporary_downstream_failure')
    monkeypatch.setattr(ControlledTools, 'execute', unavailable)
    assert (await execute(client, headers, action)).json()['data']['status'] == 'retryable'
    monkeypatch.setattr(ControlledTools, 'execute', original)
    source['revision'] = 2
    source['utterances'][0]['correction'] = '请不要创建任何任务'
    await client.post('/api/v1/meetings/sync', headers=headers, json=source)
    async with app.state.database.sessions() as db:
        row = await db.get(ActionRun, action['id'])
        row.content = {**row.content, 'nextAttemptAt': '2000-01-01T00:00:00+00:00'}
        await db.commit()
    assert await app.state.execution_jobs.process_one()
    assert (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data'][0]['status'] == 'blocked'
    assert (await client.get('/api/v1/tasks', headers=headers)).json()['data'] == []


async def test_hermes_task_update_uses_unique_persisted_identity_and_cannot_confirm_completion(client, app):
    headers, _, action = await prepared(client, app)
    await execute(client, headers, action)
    async with app.state.database.sessions() as db:
        row = await db.get(ActionRun, action['id'])
        owner = row.owner_id
        task = await db.scalar(select(InternalTask))
        task_id, version = task.id, task.version
    tools = ControlledTools(app.state.database)
    read = {'id': 'readtask', 'tool': 'task_read', 'args': {'query': '双端测试报告'}}
    receipt = await tools.call(owner, row, read, {})
    assert receipt['taskId'] == task_id and receipt['version'] == str(version)
    update = {'id': 'updatetask', 'tool': 'task_update', 'args': {'taskId': '${readtask.taskId}', 'version': '${readtask.version}', 'status': 'claimed_completed', 'note': '已批准记录待本人核验'}}
    first = await tools.call(owner, row, update, {'readtask': receipt})
    assert first['status'] == 'claimed_completed'
    assert await tools.call(owner, row, update, {'readtask': receipt}) == first
    from app.modules.execution.schemas import ToolPlan
    with pytest.raises(ValueError):
        ToolPlan.model_validate({'title': '违规核验', 'evidence_ids': ['u1'], 'steps': [{**update, 'args': {**update['args'], 'taskId': task_id, 'version': str(version), 'status': 'verified'}}]})
    provider = type('Provider', (), {'run': AsyncMock(return_value={'plans': []})})()
    await plan_work(provider, [{'id': 'x', 'raw': '请更新任务测试报告为进行中', 'turn_status': 'complete'}], catalog={'tasks': [receipt]})
    assert provider.run.await_count == 1
    assert provider.run.call_args.args[2]['current_account_catalog']['tasks'][0]['taskId'] == task_id


async def test_read_only_plan_runs_without_user_approval_and_write_plan_stays_pending(client, app):
    headers, _, action = await prepared(client, app)
    from app.modules.meetings.models.meeting import Meeting
    from app.modules.execution.service import ExecutionService
    async with app.state.database.sessions() as db:
        source = await db.scalar(select(Meeting))
        # 合成新来源，避免与上一份创建计划的去重键混淆。
        content = copy.deepcopy(source.content)
        content['utterances'].append({'id': 'read1', 'raw': '请检查任务双端测试报告', 'at': 10, 'speaker': '发言人 A', 'turn_status': 'complete'})
        source.content = content
        await ExecutionService(db).project(source, [{'title': '检查当前任务', 'evidence_ids': ['read1'],
            'steps': [{'id': 'check', 'tool': 'task_read', 'args': {'query': '双端测试报告'}}]}])
        await db.commit()
    assert await app.state.execution_jobs.process_one()
    values = (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data']
    read = next(v for v in values if v['title'] == '检查当前任务')
    assert read['status'] == 'completed' and read['approvalBasis'] == 'server_read_only_policy'
    assert next(v for v in values if v['id'] == action['id'])['status'] == 'pending'
    assert (await client.get('/api/v1/tasks', headers=headers)).json()['data'] == []


async def test_task_link_tool_checks_goal_version_and_unknown_titles_cannot_update(client, app):
    headers, _, action = await prepared(client, app)
    await execute(client, headers, action)
    target = await goal(client, headers)
    task = (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]
    async with app.state.database.sessions() as db:
        row = await db.get(ActionRun, action['id'])
    tools = ControlledTools(app.state.database)
    step = {'id': 'linktask', 'tool': 'task_link', 'args': {'taskId': task['id'], 'version': str(task['version']),
        'goalId': target['id'], 'goalVersion': str(target['version']), 'milestoneTitle': '测试报告', 'note': '已批准关联任务'}}
    receipt = await tools.call(row.owner_id, row, step, {})
    assert receipt['version'] == str(task['version'] + 1)
    duplicate = await tools.call(row.owner_id, row, step, {})
    assert duplicate == receipt
    unknown = await tools.call(row.owner_id, row, {'id': 'unknown', 'tool': 'task_read', 'args': {'query': '双端测试'}}, {})
    assert 'taskId' not in unknown and unknown['selectionRequired']
    step['id'] = 'stale'
    with pytest.raises(HTTPException) as caught:
        await tools.call(row.owner_id, row, step, {})
    assert caught.value.status_code == 409


def test_plan_compiles_task_version_references_before_approval():
    from app.modules.execution.schemas import ToolPlan
    from tests.test_controlled_tools import plan
    value = plan()
    value['steps'].append({'id': 'link', 'tool': 'task_link', 'args': {
        'taskId': '${task.taskId}', 'version': '${task.version}', 'goalId': 'known-goal',
        'goalVersion': '1', 'milestoneTitle': '测试报告', 'note': '明确关联'}})
    compiled = ToolPlan.model_validate(value)
    assert compiled.steps[-1].args['version'] == '${check.version}'
    # 重复校验稳定，审批与数据库持久化看到的是相同计划。
    assert ToolPlan.model_validate(compiled.model_dump()).model_dump() == compiled.model_dump()


async def test_compiled_multistep_plan_links_latest_task_version_in_real_graph(client, app):
    headers, _, action = await prepared(client, app)
    target = await goal(client, headers)
    from app.modules.execution.schemas import ToolPlan
    from app.modules.execution.tools import digest
    draft = {k: action[k] for k in ('title', 'evidence_ids', 'steps')}
    draft['steps'].append({'id': 'link', 'tool': 'task_link', 'args': {
        'taskId': '${task.taskId}', 'version': '${task.version}', 'goalId': target['id'],
        'goalVersion': str(target['version']), 'milestoneTitle': '测试报告', 'note': '明确关联'}})
    async with app.state.database.sessions() as db:
        row = await db.get(ActionRun, action['id'])
        content = {**row.content, **ToolPlan.model_validate(draft).model_dump()}
        content.pop('fingerprint')
        content['fingerprint'] = digest(content)
        row.content = content
        await db.commit()
        action = {**action, **content}
    result = await execute(client, headers, action)
    assert result.json()['data']['status'] == 'completed', result.text
    task = (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]
    assert task['goalId'] == target['id'] and task['version'] == 4
    assert task['status'] == 'evidence_verified'


async def test_non_document_task_can_record_owner_attestation_but_not_customer_acceptance(client, app):
    headers, _, action = await prepared(client, app)
    await execute(client, headers, action)
    async with app.state.database.sessions() as db:
        row = await db.scalar(select(InternalTask))
        row.content = {**row.content, 'attachments': [], 'status': 'claimed_completed'}
        await db.commit()
        task_id, version = row.id, row.version
    result = await client.post('/api/v1/tasks/' + task_id + '/status', headers=headers,
        json={'version': version, 'status': 'verified', 'note': '本人已核对实际处理结果'})
    assert result.status_code == 200, result.text
    assert result.json()['data']['verificationScope'] == 'owner_attestation'
    assert result.json()['data']['customerAcceptance'] == 'pending'
    task = (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]
    assert task['status'] == 'verified' and task['evidenceCurrent']


async def test_quoted_title_before_task_is_an_explicit_creation_command():
    provider = type('Provider', (), {'run': AsyncMock(return_value={'plans': [
        {'title': '创建评估报告任务', 'evidence_ids': ['u1'], 'steps': [
            {'id': 'create', 'tool': 'task_create', 'args': {'title': '提交评估报告'}}]}]})})()
    result = await plan_work(provider, [{'id': 'u1', 'raw': '请创建“提交评估报告”任务。'}])
    assert result and provider.run.await_count == 1
