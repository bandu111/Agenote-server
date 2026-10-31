"""录音变更必须经全局审批，覆盖真实数据库、LangGraph 回执、冲突与重复批准。"""
from copy import deepcopy
from sqlalchemy import select
from app.modules.meetings.models.meeting import Meeting
from app.modules.execution.models import InternalTask
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.execution.tools import digest
from app.modules.meetings.services.source_changes import project_changes, change_value
from tests.test_meetings import account
from tests.test_execution_lifecycle import execute
import pytest


async def prepared_change(client, app, new_text='现在确认，取消之前的评估报告任务。'):
    headers = await account(client)
    data = {'id': 'change-test', 'revision': 1, 'capabilities': ['turns_v1', 'tool_plans_v1'],
        'startedAt': '2026-10-21T10:00:00+08:00', 'elapsed': 30000,
        'utterances': [{'id': 'old', 'speaker': '发言人 A', 'raw': '我方周三提交评估报告。', 'at': 0},
                       {'id': 'new', 'speaker': '发言人 A', 'raw': new_text, 'at': 10000}]}
    assert (await client.post('/api/v1/meetings/sync', headers=headers, json=data)).status_code == 200
    async with app.state.database.sessions() as db:
        meeting = await db.scalar(select(Meeting))
        db.add(InternalTask(id='change-task', owner_id=meeting.owner_id, meeting_id=meeting.id, version=1,
            content={'title': '提交评估报告', 'status': 'active', 'evidenceHashes': {'old': digest([data['utterances'][0]['raw'], None])}}))
        db.add(InternalTask(id='z-unrelated', owner_id=meeting.owner_id, meeting_id=meeting.id, version=1,
            content={'title': '复核客户合同', 'status': 'active', 'evidenceHashes': {'old': digest([data['utterances'][0]['raw'], None])}}))
        db.add(ProgressItem(id='change-commitment', owner_id=meeting.owner_id, meeting_id=meeting.id,
            source_key='commitment-change', kind='commitment', source_revision=1, version=1,
            content={'title': '提交评估报告', 'status': 'active', 'side': 'ours', 'evidence': {'id': 'old'}}, overrides={}, history=[]))
        await db.flush()
        # 状态和承诺 Agent 可能同时提取同一变更；必须在同一事务中去重。
        report = {'withdrawal_candidates': [{'target_id': 'old', 'evidence_ids': ['new']}],
                  'commitments': [{'origin_id': 'old', 'pending_changes': [{'evidence_ids': ['new']}]}]}
        await project_changes(db, meeting, report)
        await db.commit()
    actions = (await client.get('/api/v1/actions?includeToolPlans=true', headers=headers)).json()['data']
    return headers, data, actions


async def test_cancel_only_after_approval_and_is_idempotent(client, app):
    headers, data, actions = await prepared_change(client, app)
    assert len(actions) == 1 and actions[0]['status'] == 'pending'
    assert (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]['status'] == 'active'
    other = await account(client, 'other-change@example.com')
    assert (await execute(client, other, actions[0])).status_code == 404
    response = await execute(client, headers, actions[0])
    assert response.status_code == 200, response.text
    assert response.json()['data']['status'] == 'completed', response.text
    task = (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]
    assert task['status'] == 'cancelled' and task['version'] == 2
    unrelated = (await client.get('/api/v1/tasks', headers=headers)).json()['data'][1]
    assert unrelated['status'] == 'active' and unrelated['version'] == 1
    async with app.state.database.sessions() as db:
        commitment = await db.get(ProgressItem, 'change-commitment')
        assert commitment.overrides['status'] == 'withdrawn' and commitment.history[-1]['before']['status'] == 'active'
    assert (await execute(client, headers, actions[0])).json()['data']['status'] == 'completed'
    assert (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]['version'] == 2
    detail = (await client.get('/api/v1/meetings/change-test', headers=headers)).json()['data']
    assert detail['sourceChanges'][0]['status'] == 'confirmed'
    assert detail['utterances'][0]['raw'] == data['utterances'][0]['raw'] and not detail['utterances'][0]['withdrawn']


async def test_replace_deadline_preserves_old_history(client, app):
    headers, data, actions = await prepared_change(client, app, '现在确认，评估报告交期改为周五。')
    response = await execute(client, headers, actions[0])
    assert response.json()['data']['status'] == 'completed', response.text
    task = (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]
    assert task['status'] == 'pending' and task['deadline'].startswith('2026-10-23')
    assert task['title'] == data['utterances'][1]['raw']
    assert task['evidenceHashes'].keys() == {'new'}
    assert task['history'][0]['actor'] == 'account_owner'
    assert task['history'][0]['before']['title'] == '提交评估报告'


async def test_decline_leaves_original_task_active(client, app):
    headers, _, actions = await prepared_change(client, app)
    action = actions[0]
    response = await client.post('/api/v1/actions/' + action['id'] + '/decision', headers=headers,
        json={'fingerprint': action['fingerprint'], 'allow': False, 'deviceId': 'synthetic-device-123'})
    assert response.json()['data']['status'] == 'declined'
    assert (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]['status'] == 'active'


async def test_changed_task_version_blocks_old_approval(client, app):
    headers, _, actions = await prepared_change(client, app)
    async with app.state.database.sessions() as db:
        task = await db.get(InternalTask, 'change-task')
        task.version = 2
        await db.commit()
    response = await execute(client, headers, actions[0])
    assert response.json()['data']['status'] == 'blocked', response.text
    assert (await client.get('/api/v1/tasks', headers=headers)).json()['data'][0]['status'] == 'active'


async def test_cancelled_task_no_longer_keeps_goal_milestone_active(client, app):
    headers, _, actions = await prepared_change(client, app)
    from app.modules.execution.task_service import TaskService
    async with app.state.database.sessions() as db:
        task = await db.get(InternalTask, 'change-task')
        db.add(ProgressItem(id='bound-goal', owner_id=task.owner_id, kind='goal', source_key='manual-goal',
            source_revision=1, version=1, overrides={}, history=[], content={'title': '项目交付', 'status': 'active',
            'milestones': [{'title': '评估报告', 'status': 'pending', 'weight': 1}]}))
        task.content = {**task.content, 'goalId': 'bound-goal', 'milestoneTitle': '评估报告', 'bindingAuthorization': 'owner_confirmed'}
        await TaskService(db).project(task.owner_id)
        await db.commit()
    assert (await execute(client, headers, actions[0])).json()['data']['status'] == 'completed'
    async with app.state.database.sessions() as db:
        goal = await db.get(ProgressItem, 'bound-goal')
        assert goal.content['milestoneProjection']['milestones'][0]['status'] == 'pending'


@pytest.mark.parametrize('words', ['考虑评估报告改为周五，还没确定', '请不要取消评估报告', '评估报告取消不了', '取消另一个采购任务'])
def test_proposals_negations_and_unrelated_tasks_do_not_cancel(words):
    old = {'id': 'old', 'raw': '我方周三提交评估报告。', 'at': 0}
    new = {'id': 'new', 'raw': words, 'at': 1}
    assert change_value(old, new) is None


async def test_duplicate_tool_plans_in_same_transaction_create_one_approval(client, app):
    from app.modules.execution.service import ExecutionService
    from app.modules.actions.models import ActionRun
    headers = await account(client)
    data = {'id': 'duplicate-plans', 'revision': 1, 'capabilities': ['tool_plans_v1'],
            'startedAt': '2026-10-21T10:00:00+08:00', 'elapsed': 10000,
            'utterances': [{'id': 'u1', 'speaker': '发言人 A', 'raw': '请创建任务提交评估报告。', 'at': 0}]}
    await client.post('/api/v1/meetings/sync', headers=headers, json=data)
    plan = {'title': '创建评估报告任务', 'evidence_ids': ['u1'], 'steps': [
        {'id': 'create', 'tool': 'task_create', 'args': {'title': '提交评估报告'}}]}
    async with app.state.database.sessions() as db:
        meeting = await db.scalar(select(Meeting))
        await ExecutionService(db).project(meeting, [plan, deepcopy(plan)])
        await db.commit()
        assert len(list(await db.scalars(select(ActionRun)))) == 1
