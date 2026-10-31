"""真实认证 API + PostgreSQL，检查会中投影、完成持久化与并发隔离。"""
import pytest
from test_meetings import account
from test_realtime_business import source, provider, run
from app.modules.home.services.task_checklist import checklist_item, fingerprint


async def test_live_delegated_todo_does_not_enter_minutes_checklist(client, app):
    headers = await account(client)
    ongoing = source()
    ongoing['startedAt'] = '2030-10-21T10:00:00+08:00'
    ongoing['utterances'] = [{'id': 'assigned', 'speaker': '发言人 A', 'speakerId': 'a',
        'speakerSeparated': True, 'at': 0, 'raw': '我方周五要提交联调测试报告，由乙负责整理。'}]
    await client.post('/api/v1/meetings/sync', headers=headers, json=ongoing)
    value = {'commitments': [{'origin_id': 'assigned', 'speaker_id': 'b',
        'action': '提交联调测试报告', 'action_quote': '提交联调测试报告',
        'task_intent': 'todo', 'deliverable_quote': '联调测试报告', 'side': 'ours',
        'events': [{'kind': 'proposed', 'evidence_ids': ['assigned'], 'summary': '安排乙提交报告', 'deadline_text': '周五'}]}]}
    async def stream(role, system, payload):
        yield {'type': 'done', 'value': value if role == 'business_realtime' else
               {'accepted': list(range(len(payload['business_candidates'])))}}
    app.state.meeting_analysis.agents.provider.stream = stream
    await run(client, headers)
    # Live business extraction remains available to the execution system,
    # but the home checklist now projects only archived minutes actions.
    assert (await client.get('/api/v1/home', headers=headers)).json()['data']['tasks'] == []
    assert (await client.get('/api/v1/tasks', headers=headers)).json()['data'] == []


async def test_live_meeting_is_not_implicitly_archived_by_home_reads(client, app):
    headers = await account(client)
    ongoing = source()
    ongoing['startedAt'] = '2030-10-21T10:00:00+08:00'
    await client.post('/api/v1/meetings/sync', headers=headers, json=ongoing)
    provider(app)
    await run(client, headers)  # 没有结束会议，也不调用完整分析。
    home = (await client.get('/api/v1/home', headers=headers)).json()['data']
    assert home['tasks'] == []
    # Re-reading a live meeting must not call the minutes generator on a GET.
    again = (await client.get('/api/v1/home', headers=headers)).json()['data']
    assert again['tasks'] == [] and again['modelCallsOnRead'] == 0


def test_undated_items_and_changed_source_not_mistaken_for_completed_work():
    item = {'kind': 'commitment', 'id': 'c', 'version': 1, 'title': '跟进接口',
            'evidence': {'sessionId': 'meeting', 'transcript': '我方负责跟进接口。'}, 'side': 'ours', 'status': 'pending', 'candidateStatus': 'committed'}
    assert checklist_item(item)['deadline'] is None
    item.update(checklistCompleted=True, checklistFingerprint=fingerprint(item))
    assert checklist_item(item)['completed']
    item['title'] = '跟进修改后的接口'
    item['evidence']['transcript'] = '我方负责跟进修改后的接口。'
    assert not checklist_item(item)['completed']
    assert checklist_item({**item, 'evidenceCurrent': False}) is None
    assert checklist_item({**item, 'reviewRejected': True}) is None
    assert checklist_item({**item, 'status': 'withdrawn'}) is None


def test_expired_task_is_hidden_at_deadline_but_original_data_is_kept():
    from datetime import datetime, timezone
    item = {'kind': 'internal_task', 'id': 't', 'version': 1, 'title': '检查报告',
            'deadline': '2030-10-30T10:00:00+08:00', 'status': 'pending'}
    assert checklist_item(item, now=datetime(2030, 10, 30, 1, 59, tzinfo=timezone.utc))
    assert checklist_item(item, now=datetime(2030, 10, 30, 2, tzinfo=timezone.utc)) is None
    assert item['status'] == 'pending'
    assert checklist_item({**item, 'deadline': None}, now=datetime(2031, 1, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize('raw,title,intent,target,accepted', [
    ('周五要提交验收报告。', '提交验收报告', 'todo', '验收报告', True),
    ('我负责修复图片上传失败问题。', '修复图片上传失败问题', 'todo', '图片上传失败问题', True),
    ('明天由我走访十二家门店，整理试点反馈。', '走访十二家门店', 'todo', '十二家门店', True),
    ('请盘点机房库存。', '盘点机房库存', 'todo', '机房库存', True),
    ('今天主要讨论三个议题。', '讨论三个议题', 'context', '三个议题', False),
    ('ERP接口偶尔超时，目前还有两个问题。', 'ERP接口偶尔超时', 'context', 'ERP接口', False),
    ('验收至少需要确认ERP能够持续运行。', '确认ERP能够持续运行', 'context', 'ERP', False),
    ('建议下周五提交预算报告，暂时还没决定。', '提交预算报告', 'proposal', '预算报告', False),
    ('如果验收通过，周四部署正式环境。', '部署正式环境', 'conditional', '正式环境', False),
    ('我方已经提交测试报告。', '提交测试报告', 'completed', '测试报告', False),
    ('下周再看看项目情况。', '再看看项目情况', 'todo', '项目情况', False),
    ('我负责处理相关工作。', '处理相关工作', 'todo', '相关工作', False),
    ('我不会提交预算报告。', '提交预算报告', 'todo', '预算报告', False),
])
def test_checklist_admission_uses_semantics_and_concrete_source(raw, title, intent, target, accepted):
    item = {'kind': 'commitment', 'id': 'semantic', 'version': 1, 'title': title,
            'taskIntent': intent, 'taskDeliverable': target, 'candidateStatus': 'proposed',
            'evidence': {'sessionId': 'meeting', 'transcript': raw}, 'status': 'pending'}
    assert (checklist_item(item) is not None) == accepted


def test_legacy_context_without_new_model_fields_is_not_a_task():
    item = {'kind': 'commitment', 'id': 'old', 'version': 1, 'status': 'pending',
            'candidateStatus': 'proposed', 'evidence': {'sessionId': 'meeting',
                'transcript': '验收至少要确认ERP能够持续运行。'}, 'title': '确认ERP能够持续运行'}
    assert checklist_item(item) is None


async def test_expired_item_cannot_be_completed_through_forged_request(client, app):
    from sqlalchemy import select
    from app.modules.progress.models.progress_item import ProgressItem
    headers = await account(client)
    ongoing = source()
    ongoing['startedAt'] = '2030-10-21T10:00:00+08:00'
    await client.post('/api/v1/meetings/sync', headers=headers, json=ongoing)
    provider(app)
    await run(client, headers)
    # Legacy completion endpoints still enforce expiry, although these rows no
    # longer populate the homepage after the switch to minutes actions.
    from app.modules.progress.services.progress_service import output
    async with app.state.database.sessions() as db:
        rows = list(await db.scalars(select(ProgressItem).where(ProgressItem.kind == 'commitment')))
        task = next(value for row in rows if (value := checklist_item({**output(row), 'kind':'commitment'})))
    async with app.state.database.sessions() as db:
        row = await db.scalar(select(ProgressItem).where(ProgressItem.id == task['id'].split(':', 1)[1]))
        row.overrides = {**row.overrides, 'deadline': '2000-01-01T00:00:00+00:00'}
        row.version += 1
        version = row.version
        await db.commit()
    assert (await client.get('/api/v1/home', headers=headers)).json()['data']['tasks'] == []
    result = await client.post('/api/v1/home/tasks/' + task['id'] + '/complete', headers=headers, json={'version': version})
    assert result.status_code == 409
    async with app.state.database.sessions() as db:
        row = await db.scalar(select(ProgressItem).where(ProgressItem.id == task['id'].split(':', 1)[1]))
        assert not row.overrides.get('checklistCompleted')
