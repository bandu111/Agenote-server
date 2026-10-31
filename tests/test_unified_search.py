"""统一检索的真实数据库边界与匹配评分，不依赖模型生成测试期望。"""
import uuid
from copy import deepcopy
import pytest
from sqlalchemy import select
from app.modules.search.relevance import relevance
from app.modules.search.service import UnifiedSearch
from app.modules.meetings.models.meeting import Meeting
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.execution.models import InternalTask
from app.modules.knowledge.services.meeting_context import MeetingKnowledgeContext
from tests.test_meetings import account, meeting
from tests.test_knowledge import snapshot


@pytest.mark.parametrize('query,target,expected', [('Android','android',100),('Android','iOS',0),('Android iOS','Android',55),('测试','测试报告',85),('Android iOS','Android iOS Windows',94),('','测试',None),('C++','C#',0)])
def test_relevance_meaning(query,target,expected):
    assert relevance(query,target)==expected


async def test_search_is_unified_owner_scoped_and_revocation_aware(client,app):
    first=await account(client)
    second=await account(client,'other-search@example.com')
    owner=(await client.get('/api/v1/auth/me',headers=first)).json()['data']['id']
    data=meeting();data['utterances'][0].update(raw='Android 双端测试尚未批准新增 Windows 范围。',turnStatus='complete')
    assert (await client.post('/api/v1/meetings/sync',headers=first,json=data)).status_code==200
    library=snapshot()
    assert (await client.post('/api/v1/knowledge/sync',headers=first,json={'revision':0,'snapshot':library})).status_code==200
    async with app.state.database.sessions() as db:
        m=await db.scalar(select(Meeting).where(Meeting.owner_id==owner))
        for kind in ('goal','commitment'):
            db.add(ProgressItem(id=str(uuid.uuid4()),owner_id=owner,meeting_id=m.id,source_key=kind,kind=kind,
                content={'title':'Android 双端测试','status':'planned','condition':'交付 Android 测试报告','projectId':'p1'}))
        db.add(InternalTask(id='task',owner_id=owner,meeting_id=m.id,content={'title':'Android 测试任务','status':'pending'}))
        await db.commit()
    response=await client.post('/api/v1/search',headers=first,json={'query':'Android','limit':50})
    assert response.status_code==200,response.text
    hits=response.json()['data']
    assert {'goal','commitment','task','document','utterance'} <= {h['kind'] for h in hits}
    assert all(0<h['matchPercent']<=100 and h['matchMethod']=='lexical_relevance_v1' for h in hits)
    voice=next(h for h in hits if h['kind']=='utterance')
    assert voice['original']==data['utterances'][0]['raw'] and 'utterance=u1' in voice['path']
    assert (await client.post('/api/v1/search',headers=second,json={'query':'Android'})).json()['data']==[]
    library['documents'][0]['versions'][0]['state']='retracted'
    await client.post('/api/v1/knowledge/sync',headers=first,json={'revision':1,'snapshot':library})
    assert (await client.post('/api/v1/search',headers=first,json={'query':'Android','kinds':['document']})).json()['data']==[]
    assert (await client.post('/api/v1/search',headers=first,json={'query':'Android','owner':owner})).status_code==422


async def test_old_meeting_recalled_beyond_recent_history_and_agent_acl(client,app):
    headers=await account(client)
    owner=(await client.get('/api/v1/auth/me',headers=headers)).json()['data']['id']
    async with app.state.database.sessions() as db:
        db.add(Meeting(id=str(uuid.uuid4()),owner_id=owner,client_id='old',revision=1,
            content={'title':'最早合同', 'utterances':[{'id':'old-u','raw':'火星探测器交付不得提前取消','speaker':'发言人 A','turn_status':'complete'}]}))
        for i in range(205):
            db.add(Meeting(id=str(uuid.uuid4()),owner_id=owner,client_id='new-'+str(i),revision=1,content={'title':'无关日常','utterances':[]}))
        await db.commit()
        service=UnifiedSearch(db)
        hits=await service.search(owner,'火星探测器',kinds=['utterance'])
        assert any(h['utteranceId']=='old-u' for h in hits)
        assert await service.search(owner,'火星探测器',kinds=['utterance'],agent=True,current_session='another')==[]
        current=await service.search(owner,'火星探测器',kinds=['utterance'],agent=True,current_session='old')
        assert current[0]['utteranceId']=='old-u'


async def test_delta_sync_is_idempotent_and_cannot_rewrite_original(client):
    headers=await account(client)
    first=meeting()
    initial=await client.post('/api/v1/meetings/sync',headers=headers,json=first)
    assert initial.status_code==200 and initial.json()['data']['deltaSupported']
    delta={**first,'revision':2,'baseRevision':1,'elapsed':2000,
        'utterances':[{'id':'u2','raw':'还没说完，交期是','speaker':'发言人 A','at':1500,'turnStatus':'pending'}]}
    for _ in range(2):
        response=await client.post('/api/v1/meetings/sync-delta',headers=headers,json=delta)
        assert response.status_code==200,response.text
        assert response.json()['data']['acceptedUtteranceIds']==['u2']
        stored=(await client.get('/api/v1/meetings/'+first['id'],headers=headers)).json()['data']
        assert [u['id'] for u in stored['utterances']]==['u1','u2']
    wrong={**delta,'revision':3,'baseRevision':1}
    assert (await client.post('/api/v1/meetings/sync-delta',headers=headers,json=wrong)).status_code==409
    forged={**delta,'revision':3,'baseRevision':2,'utterances':[{**first['utterances'][0],'raw':'伪造原话'}]}
    assert (await client.post('/api/v1/meetings/sync-delta',headers=headers,json=forged)).status_code==409
    other=await account(client,'delta-other@example.com')
    assert (await client.post('/api/v1/meetings/sync-delta',headers=other,json=delta)).status_code==404


async def test_long_meeting_over_two_thousand_fragments_can_sync_incrementally(client):
    headers=await account(client)
    value=meeting();value['title']='Android 会议';value['utterances']=[{'id':'u'+str(i),'raw':'尚未批准新增范围','speaker':'发言人 A','at':i} for i in range(2100)]
    response=await client.post('/api/v1/meetings/sync',headers=headers,json=value)
    assert response.status_code==200,response.text
    patch={**value,'revision':2,'baseRevision':1,'elapsed':3000,'utterances':[{'id':'last','raw':'现在仍按原范围执行','speaker':'发言人 A','at':2500}]}
    result=await client.post('/api/v1/meetings/sync-delta',headers=headers,json=patch)
    assert result.status_code==200 and result.json()['data']['acceptedUtteranceIds']==['last']
    assert len(result.content)<350
    stored=(await client.get('/api/v1/meetings/'+value['id'],headers=headers)).json()['data']
    assert len(stored['utterances'])==2101
    no_fake_hits=await client.post('/api/v1/search',headers=headers,json={'query':'Android','kinds':['utterance']})
    assert no_fake_hits.json()['data']==[]
