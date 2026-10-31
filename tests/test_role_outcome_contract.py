from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.modules.outcomes.role_policy import role_lens,LENSES
from app.modules.outcomes.engine import analyze

EVIDENCE={'utterance:u':{'text':'客户反馈新版本体验下降，续费率降低，交付延期；研发怀疑接口性能。'}}

def plan(lens):
    return {'worthwhile':True,'question':lens['objective'],'analysis_goal':lens['objective'],
        'metrics':lens['candidateMetrics'][:2],'required_sources':lens['sourceTypes'][:2],
        'methods':lens['candidateMethods'][:1],'deliverable':lens['deliverableExamples'][0],
        'value_reason':'针对当前会议问题确定验证和行动顺序','queries':[]}

def report(lens):
    return {'title':lens['deliverableExamples'][0],'summary':lens['objective'],
        'analysis_goal':lens['objective'],
        'metrics':[{'name':lens['candidateMetrics'][0],'status':'missing','missing_data':lens['sourceTypes'][0]+'尚未提供'}],
        'methods':[{'name':'会议证据审查','tool':'evidence_review','status':'performed','detail':'审查当前会议原话'}],
        'sections':[{'title':lens['deliverableExamples'][0]+'工作草案','content':lens['objective']+'；先补充'+lens['sourceTypes'][0],
            'evidence_ids':['utterance:u']}],
        'findings':[{'text':lens['objective']+'尚需进一步证据','certainty':'hypothesis','evidence_ids':['utterance:u']}],
        'next_steps':['补充'+lens['sourceTypes'][0]],'data_gaps':[lens['sourceTypes'][0]+'未提供'],
        'notify':True,'notification_summary':lens['objective']+'；尚缺'+lens['sourceTypes'][0]}

@pytest.mark.parametrize('category',[c for c in LENSES if c!='other'])
async def test_all_roles_receive_distinct_problem_metrics_sources_and_deliverable(category):
    lens=role_lens({'raw':'当前职业','categoryId':category,'confirmed':True})
    provider=SimpleNamespace(run=AsyncMock(side_effect=[plan(lens),report(lens)]))
    result=await analyze(provider,None,'owner',{'profession':{'raw':'当前职业','categoryId':category,'confirmed':True},
        'evidence':EVIDENCE,'request':'请额外分析成本影响'})
    first=provider.run.call_args_list[0].args[2]
    second=provider.run.call_args_list[1].args[2]
    assert first['role_lens']['categoryId']==category
    assert first['request']=='请额外分析成本影响'
    assert second['analysis_plan']['required_sources']==lens['sourceTypes'][:2]
    assert result['metrics'][0]['name']==lens['candidateMetrics'][0]
    assert result['sections'][0]['content'].endswith(lens['sourceTypes'][0])
    assert result['completion']=='partial' and result['schema']==2

@pytest.mark.parametrize('fault',['fake_metric','fake_compute','missing_sources','bad_section_reference'])
async def test_rejects_title_only_or_unperformed_analysis(fault):
    lens=role_lens({'raw':'Flutter 开发工程师'})
    p=plan(lens);r=report(lens)
    if fault=='fake_metric':r['metrics']=[{'name':'错误率','status':'quoted','value':'30%',
        'quote':'错误率为30%','evidence_ids':['utterance:u']}]
    if fault=='fake_compute':r['methods']=[{'name':'SQL统计','tool':'sql','status':'performed','detail':'已计算续费率'}]
    if fault=='missing_sources':p['required_sources']=[]
    if fault=='bad_section_reference':r['sections'][0]['evidence_ids']=['other-owner-evidence']
    provider=SimpleNamespace(run=AsyncMock(side_effect=[p,r]))
    with pytest.raises(ValueError):await analyze(provider,None,'owner',{'profession':{'raw':'Flutter 开发工程师'},'evidence':EVIDENCE})

async def test_no_extra_value_does_not_make_report():
    provider=SimpleNamespace(run=AsyncMock(return_value={'worthwhile':False}))
    result=await analyze(provider,None,'owner',{'evidence':EVIDENCE})
    assert result['skipped'] is True and provider.run.await_count==1

async def test_uncited_claim_is_a_gap_not_a_finding():
    lens=role_lens({'raw':'销售经理'})
    result=report(lens)
    result['findings'].append({'text':'流失集中在大客户，尚无名单核实','certainty':'unverified','evidence_ids':[]})
    provider=SimpleNamespace(run=AsyncMock(side_effect=[plan(lens),result]))
    value=await analyze(provider,None,'owner',{'profession':{'raw':'销售经理'},'evidence':EVIDENCE})
    assert len(value['findings'])==1
    assert any('尚无依据' in gap for gap in value['data_gaps'])

def test_role_differences_not_just_title():
    lenses=[role_lens({'categoryId':c,'confirmed':True}) for c in ['engineering','sales_business','product_design','project_delivery']]
    for key in ['objective','candidateMetrics','sourceTypes','candidateMethods','deliverableExamples']:
        assert len({repr(l[key]) for l in lenses})==4

async def test_archive_revision_change_does_not_send_second_automatic_report(client,app):
    from tests.test_auth import register
    from sqlalchemy import select
    from app.modules.identity.models.identity import User
    from app.modules.meetings.models.meeting import Meeting
    from app.modules.meetings.models.archive import MeetingArchive
    from app.modules.outcomes.jobs import enqueue
    await register(client)
    async with app.state.database.sessions() as session:
        user=await session.scalar(select(User).where(User.email=='yan@example.com'))
        meeting=Meeting(id='one-auto-meeting',owner_id=user.id,client_id='one-auto',revision=1,
            content={'ended':True,'utterances':[{'id':'u','raw':'需要验证风险','at':0}]})
        session.add(meeting);await session.flush()
        archive=MeetingArchive(id='one-auto-archive',meeting_id=meeting.id,owner_id=user.id,revision=1,
            title='test',markdown='test',details={'summary':'test'})
        session.add(archive);await session.flush()
        first=await enqueue(session,meeting,archive)
        meeting.revision=2;archive.revision=2
        second=await enqueue(session,meeting,archive)
        assert first.id==second.id
        requested=await enqueue(session,meeting,archive,request_key='explicit-reanalysis',request='请重新分析')
        assert requested.id!=first.id
        await session.rollback()
