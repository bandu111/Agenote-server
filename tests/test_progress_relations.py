"""来源关系与检索相似度隔离，任务附件必须回读有效版本。"""
import hashlib
import uuid
from sqlalchemy import select
from test_meetings import account
from test_progress import projected
from app.modules.meetings.models.meeting import Meeting
from app.modules.execution.models import InternalTask
from app.modules.execution.tools import digest
from app.modules.knowledge.models.library import KnowledgeLibrary
from app.modules.knowledge.services.knowledge_service import empty_library
from app.modules.progress.models.progress_item import ProgressItem


async def test_real_attachment_readback_does_not_link_unrelated_documents(client, app):
    headers = await account(client)
    data = await projected(client, app, headers)
    item = data['commitments'][0]
    async with app.state.database.sessions() as db:
        meeting = await db.scalar(select(Meeting).where(Meeting.client_id == 'real-progress-session'))
        source = next(u for u in meeting.content['utterances'] if u['id'] == 'c')
        snapshot = empty_library()
        snapshot['projects'][0]['allowed'] = True
        for doc_id, title, source_name in [('attached','评估报告','upload:report'),('unrelated','提交报告','upload:other')]:
            content = '评估报告正文'
            snapshot['documents'].append({'id':doc_id,'folderId':'inbox','projectId':'personal','kind':'text',
                'source':source_name,'createdAt':'2026-10-23T10:00:00+08:00','allowed':True,
                'selectedVersionId':'v1', 'versions':[{'id':'v1','number':1,'title':title,'content':content,
                    'hash':hashlib.sha256(content.encode()).hexdigest(), 'state':'current',
                    'createdAt':'2026-10-23T10:00:00+08:00','sources':[]}], 'tags':[]})
        db.add(KnowledgeLibrary(owner_id=meeting.owner_id, revision=1, snapshot=snapshot))
        task = InternalTask(id=str(uuid.uuid4()), owner_id=meeting.owner_id, meeting_id=meeting.id, version=1,
            content={'title':item['title'], 'status':'active', 'evidenceIds':['c'],
                'evidenceHashes':{'c':digest([source['raw'],source.get('correction')])},
                'attachments':[{'documentId':'attached','versionId':'v1','hash':snapshot['documents'][0]['versions'][0]['hash']}]})
        db.add(task)
        await db.commit()
    result = (await client.get('/api/v1/progress', headers=headers)).json()['data']['commitments'][0]
    assert result['attachment'] == '评估报告'
    assert {r['documentId'] for r in result['knowledgeReferences']} == {'attached'}
    assert result['evidence']['meetingTitle'] == '项目讨论'
    # 改变任务来源，不再能继续把旧附件当作该承诺的有效附件。
    async with app.state.database.sessions() as db:
        task = await db.get(InternalTask, task.id)
        task.content = {**task.content, 'evidenceHashes':{'c':'0'*64}}
        await db.commit()
    result = (await client.get('/api/v1/progress', headers=headers)).json()['data']['commitments'][0]
    assert result['attachment'] is None
    assert not result['knowledgeReferences']


async def test_legacy_candidate_goal_without_source_link_is_not_shown(client, app):
    headers = await account(client)
    data = await projected(client, app, headers)
    async with app.state.database.sessions() as db:
        meeting = await db.scalar(select(Meeting).where(Meeting.client_id == 'real-progress-session'))
        utterances = [dict(u) for u in meeting.content['utterances']]
        utterances[1]['raw'] = '我方承诺周三提交报告。'
        meeting.content = {**meeting.content, 'utterances':utterances}
        row = await db.get(ProgressItem, data['commitments'][0]['id'])
        from app.modules.meetings.services.business_guard import transcript_hashes
        row.content = {**row.content, 'sourceEvidenceHashes':{'c':transcript_hashes(utterances)['c']},
            'goalLinkBasis':'reviewed_candidate'}
        await db.commit()
    item = (await client.get('/api/v1/progress', headers=headers)).json()['data']['commitments'][0]
    assert item['goalId'] == '' and item['goalLinkBasis'] == 'unconfirmed_candidate'


async def test_reviewed_link_by_named_goal_continuation_is_kept_on_read(client, app):
    headers = await account(client)
    data = await projected(client, app, headers)
    async with app.state.database.sessions() as db:
        meeting = await db.scalar(select(Meeting).where(Meeting.client_id == 'real-progress-session'))
        utterances = [dict(u) for u in meeting.content['utterances']]
        # 原话未复述交付条件，但明确续接具名目标；写入时已接受，读回不能再丢弃。
        utterances[1]['raw'] = '继续推进移动端联调验收目标，我方承诺周三提交报告。'
        meeting.content = {**meeting.content, 'utterances':utterances}
        goal = await db.get(ProgressItem, data['goals'][0]['id'])
        goal.content = {**goal.content, 'title':'移动端联调验收', 'condition':'双端兼容性全部用例通过'}
        row = await db.get(ProgressItem, data['commitments'][0]['id'])
        from app.modules.meetings.services.business_guard import transcript_hashes
        row.content = {**row.content, 'sourceEvidenceHashes':{'c':transcript_hashes(utterances)['c']},
            'goalId':goal.id, 'goalLinkBasis':'reviewed_candidate'}
        await db.commit()
    item = (await client.get('/api/v1/progress', headers=headers)).json()['data']['commitments'][0]
    assert item['goalId'] == data['goals'][0]['id'] and item['goalLinkBasis'] == 'reviewed_candidate'
