from types import SimpleNamespace
from unittest.mock import AsyncMock
from sqlalchemy import select
from app.modules.identity.models.identity import User
from app.modules.meetings.models.meeting import Meeting
from app.modules.meetings.models.archive import MeetingArchive
from app.modules.outcomes.jobs import OutcomeJobs
from tests.test_auth import register, bearer

async def test_owned_questions_archive_queue_and_app_fallback(client, app):
    one=await register(client)
    two=await register(client,'question-other@example.com')
    headers=bearer(one)
    async with app.state.database.sessions() as db:
        owner=await db.scalar(select(User).where(User.email=='yan@example.com'))
        m=Meeting(id='question-api-meeting',owner_id=owner.id,client_id='question-api',revision=1,
            content={'ended':True,'utterances':[{'id':'u1','raw':'接口状态延迟十秒','speaker':'B','at':12000}]})
        db.add(m);await db.flush()
        db.add(MeetingArchive(id='question-api-archive',meeting_id=m.id,owner_id=owner.id,
            revision=1,title='接口讨论',markdown='已保存纪要',details={'summary':'接口状态延迟'}))
        await db.commit()
    path='/api/v1/meetings/question-api/questions'
    payload={'baseVersion':0,'mutationId':'first','content':{'atMs':10000,'text':'影响多久？','sourceIds':['u1']}}
    saved=await client.post(path+'/q1/save',headers=headers,json=payload)
    assert saved.status_code==200,saved.text
    assert saved.json()['data']['analysis']['message']=='正在分析'
    repeat=await client.post(path+'/q1/save',headers=headers,json=payload)
    assert repeat.json()['data']['version']==1
    assert (await client.get(path,headers=bearer(two))).status_code==404
    assert (await client.post(path+'/q1/save',headers=headers,json={**payload,'mutationId':'stale'})).status_code==409
    fake=SimpleNamespace(run=AsyncMock(return_value={'question':'影响多久？','text':'延迟十秒',
        'coverage':'explicit','evidence':[{'id':'utterance:u1','quote':'延迟十秒'}]}))
    assert await OutcomeJobs(app.state.database,fake).process_one()
    result=(await client.get(path,headers=headers)).json()['data'][0]
    assert result['analysis']['state']=='answered'
    assert result['content']['status']=='pending'
    assert result['analysis']['answers'][0]['evidence'][0]['atMs']==12000
