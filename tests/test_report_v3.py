import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.modules.outcomes.calculations import execute
from app.modules.outcomes.engine import analyze
from app.modules.outcomes.role_policy import role_lens
from tests.test_role_outcome_contract import plan, report

EVIDENCE = {'utterance:u': {'text': 'Q3目标4800万，实际4215万，恒瑞延期320万。', 'at': 1000}}


def calc(operation='ratio_percent', values=('4215','4800')):
    return {'id':'attainment', 'name':'达成率', 'operation':operation,
        'operands':[{'value':v,'unit':'万','quote':EVIDENCE['utterance:u']['text'],'evidence_id':'utterance:u'} for v in values]}


@pytest.mark.parametrize('operation,values,expected',[
    ('ratio_percent',('4215','4800'),'87.81%'),
    ('difference',('4800','4215'),'585万'),
    ('change_percent',('4215','4800'),'-12.19%'),
    ('sum',('4215','320'),'4535万'),
])
def test_real_decimal_arithmetic(operation,values,expected):
    value=execute(calc(operation,values),EVIDENCE)
    assert value['value']==expected
    assert value['evidence_ids']==['utterance:u']


@pytest.mark.parametrize('fault',['wrong_owner','invented_value','wrong_unit','false_quote','zero','code','nested'])
def test_untrusted_calculations_rejected(fault):
    value=calc();evidence=copy.deepcopy(EVIDENCE)
    if fault=='wrong_owner':value['operands'][0]['evidence_id']='other-owner'
    if fault=='invented_value':value['operands'][0]['value']='215'
    if fault=='wrong_unit':value['operands'][0]['unit']='元'
    if fault=='false_quote':value['operands'][0]['quote']='实际4215万已审计'
    if fault=='zero':
        evidence['utterance:u']['text']+='基准0万';value['operands'][1].update(value='0',quote='基准0万')
    if fault=='code':value['operation']='eval'
    if fault=='nested':evidence['utterance:u']['kind']='calculation'
    with pytest.raises(ValueError):execute(value,evidence)


async def produce(modify=None, computation=None):
    lens=role_lens({'raw':'销售总监'});r=report(lens)
    r['metrics']=[{'name':'达成率','status':'computed','value':'87.81%','calculation_id':'attainment'}]
    r['methods'].append({'name':'达成率计算','tool':'decimal_arithmetic','status':'performed','detail':'依据会议口述目标与实际算术计算，非财务审计'})
    r['actions']=[{'action':'核对收入确认口径','owner':'销售运营','deadline':'一周内','basis':'suggested','expected_outcome':'明确财务与CRM差异'}]
    r['risks']=[{'title':'延期风险','severity':'medium','detail':'延期需要跟进确认','certainty':'hypothesis','evidence_ids':['utterance:u']}]
    r['analysis_tables']=[{'title':'目标对照','columns':['目标','达成率'],
        'rows':[[{'text':'4800万','evidence_ids':['utterance:u']},{'text':'87.81%','evidence_ids':['calculation:attainment']}]]}]
    if modify:modify(r)
    provider=SimpleNamespace(run=AsyncMock(side_effect=[plan(lens),{'calculations':[computation or calc()]},r]))
    return await analyze(provider,None,'owner',{'profession':{'raw':'销售总监'},'evidence':EVIDENCE})


async def test_complete_structured_report_uses_server_results():
    value=await produce()
    assert value['formatVersion']==3
    assert value['metrics'][0]['evidence_ids']==['calculation:attainment']
    assert value['actions'][0]['basis']=='suggested'
    assert 'agenote.decimal' in value['tools_used']


@pytest.mark.parametrize('fault',['wrong_result','invented_table','false_confirmation','invented_probability'])
async def test_report_cannot_promote_fabrication(fault):
    def modify(r):
        if fault=='invented_probability':r['risks'][0]['detail']='流失概率超过70%'
        if fault=='wrong_result':r['metrics'][0]['value']='99%'
        if fault=='invented_table':r['analysis_tables'][0]['rows'][0][1]['text']='99%'
        if fault=='false_confirmation':r['actions'][0]['basis']='meeting_confirmed'
        if fault=='false_window':r['report_meta']={'time_window':'2026年Q3','scope_quote':'Q3目标4800万','evidence_ids':['utterance:u']}
    with pytest.raises(ValueError):await produce(modify)


def test_pdf_professional_sections_and_citations():
    from io import BytesIO
    from pypdf import PdfReader
    from app.modules.outcomes.pdf_report import render_pdf
    r=report(role_lens({'raw':'销售总监'}))
    r.update(evidence=EVIDENCE,role_lens=role_lens({'raw':'销售总监'}),
        calculation_results=[execute(calc(),EVIDENCE)],completion='partial')
    data=render_pdf(r,created_at='2026-10-30')
    text=''.join(page.extract_text() for page in PdfReader(BytesIO(data)).pages)
    for title in ['结论先行','关键指标看板','主体分析','异常与风险','行动建议','口径与局限性','E1']:
        assert title in text
    assert data.startswith(b'%PDF')


async def test_cosmetic_extra_metadata_does_not_drop_verified_content():
    value=await produce(lambda r:r.update(profession='模型额外展示字段'))
    assert value['role_lens']['raw']=='销售总监'
    assert value['metrics'][0]['value']=='87.81%'
    assert 'profession' not in value


async def test_unverifiable_period_is_omitted_with_disclosure():
    value=await produce(lambda r:r.update(report_meta={'time_window':'2026年Q3','scope_quote':'Q3目标4800万','evidence_ids':['utterance:u']}))
    assert not value['report_meta']['time_window']
    assert any('范围未通过' in g for g in value['data_gaps'])


async def test_single_repair_can_fix_missing_calculation_citation():
    lens=role_lens({'raw':'销售总监'})
    good=report(lens)
    good['metrics']=[{'name':'达成率','status':'computed','value':'87.81%','calculation_id':'attainment'}]
    bad=copy.deepcopy(good);bad['metrics'][0]['value']='99%'
    provider=SimpleNamespace(run=AsyncMock(side_effect=[plan(lens),{'calculations':[calc()]},bad,good]))
    value=await analyze(provider,None,'owner',{'profession':{'raw':'销售总监'},'evidence':EVIDENCE},repair=True)
    assert value['metrics'][0]['value']=='87.81%'
    assert provider.run.await_count==4
    assert 'validation_feedback' in provider.run.call_args.args[2]

async def test_repair_still_rejects_fabrication_and_never_loops():
    lens=role_lens({'raw':'销售总监'});bad=report(lens)
    bad['metrics']=[{'name':'达成率','status':'computed','value':'99%','calculation_id':'attainment'}]
    provider=SimpleNamespace(run=AsyncMock(side_effect=[plan(lens),{'calculations':[calc()]},copy.deepcopy(bad),copy.deepcopy(bad)]))
    with pytest.raises(ValueError):
        await analyze(provider,None,'owner',{'profession':{'raw':'销售总监'},'evidence':EVIDENCE},repair=True)
    assert provider.run.await_count==4


async def test_exact_server_result_gets_calculation_provenance():
    value=await produce(lambda r:r['sections'][0].update(content='达成率87.81%，仍需核对口径。'))
    assert 'calculation:attainment' in value['sections'][0]['evidence_ids']
