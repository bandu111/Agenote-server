"""Structured professional deliverables; optional for older saved reports."""
import re
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Risk(Strict):
    title: str = Field(min_length=1, max_length=120)
    severity: Literal['high', 'medium', 'low']
    detail: str = Field(min_length=1, max_length=800)
    certainty: Literal['observation', 'correlation', 'hypothesis', 'unverified']
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    @field_validator('severity',mode='before')
    @classmethod
    def severity_alias(cls,value):
        aliases={'高':'high','高风险':'high','critical':'high','medium-high':'high','medium_high':'high','中高':'high','中高风险':'high',
            '中':'medium','中风险':'medium','moderate':'medium','低':'low','低风险':'low'}
        return aliases.get(value,value)


class Action(Strict):
    action: str = Field(min_length=1, max_length=500)
    owner: str = Field(default='', max_length=100)
    deadline: str = Field(default='', max_length=100)
    expected_outcome: str = Field(default='', max_length=300)
    basis: Literal['suggested', 'meeting_confirmed'] = 'suggested'
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    confirmation_quote: str = Field(default='', max_length=600)


class Meta(Strict):
    report_type: str = Field(default='', max_length=120)
    time_window: str = Field(default='', max_length=160)
    data_window: str = Field(default='', max_length=160)
    scope_quote: str = Field(default='', max_length=600)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)


class TableCell(Strict):
    text: str = Field(max_length=400)
    evidence_ids: list[str] = Field(default_factory=list, max_length=5)


class AnalysisTable(Strict):
    title: str = Field(min_length=1, max_length=120)
    columns: list[str] = Field(min_length=2, max_length=5)
    rows: list[list[TableCell]] = Field(min_length=1, max_length=12)
    interpretation: str = Field(default='', max_length=600)


def validate_details(value, evidence, references):
    meta = value.report_meta
    meta.evidence_ids = references(meta.evidence_ids, evidence)
    if meta.time_window or meta.data_window:
        valid_quote=meta.scope_quote and any(meta.scope_quote in str(evidence[k].get('text','')) for k in meta.evidence_ids)
        invalid=not valid_quote or any(v and v not in meta.scope_quote for v in [meta.time_window,meta.data_window])
        if invalid:
            meta.time_window='';meta.data_window=''
            value.data_gaps=['分析期间/数据覆盖范围未通过逐字来源校验，未展示模型补写的日期。']+value.data_gaps[:7]
    for item in value.risks:
        item.evidence_ids = references(item.evidence_ids, evidence)
    value.risks.sort(key=lambda r: ['high', 'medium', 'low'].index(r.severity))
    for item in value.actions:
        item.evidence_ids = references(item.evidence_ids, evidence)
        if item.basis == 'meeting_confirmed':
            if not item.confirmation_quote or not any(
                    k.startswith('utterance:') and item.confirmation_quote in str(evidence[k].get('text', '')) for k in item.evidence_ids):
                raise ValueError('unconfirmed_report_action')
            if any(v and v not in item.confirmation_quote for v in [item.owner, item.deadline]):
                raise ValueError('invented_action_owner_or_deadline')
    for table in value.analysis_tables:
        for row in table.rows:
            if len(row) != len(table.columns):
                raise ValueError('invalid_report_table_width')
            for cell in row:
                cell.evidence_ids = references(cell.evidence_ids, evidence)
                if cell.text not in ['', '—', '待补充'] and not cell.evidence_ids:
                    raise ValueError('ungrounded_report_table_cell')


PROMPT = '''
额外返回以下专业交付字段（无依据时用空数组，不为填满模板而编造）：
"report_meta":{"report_type":"本次工作成果类型","time_window":"原文中的分析期间或空串","data_window":"原文中的数据截止范围或空串","scope_quote":"逐字原文或空串","evidence_ids":[]},
"risks":[{"title":"风险","severity":"high/medium/low","detail":"影响、触发条件与验证方式","certainty":"observation/correlation/hypothesis/unverified","evidence_ids":["真实键"]}],
"actions":[{"action":"具体行动","owner":"建议角色或会议原文人名，未知留空","deadline":"建议期限或会议原文，未知留空","expected_outcome":"预期改善及验收方式，不虚构量化收益",
"basis":"suggested或meeting_confirmed","evidence_ids":[],"confirmation_quote":"会议明确确认的原文；建议留空"}],
"analysis_tables":[{"title":"针对职业问题的数据表","columns":["对象","指标"],"rows":[[{"text":"原文片段或计算结果","evidence_ids":["对应键"]},{"text":"待补充","evidence_ids":[]}]],"interpretation":"比较结果及局限性"}],
"methodology_notes":["统计口径、比较基准、算法局限性"]。
选择适合当前问题的章节；销售可用量价结构/转化漏斗/客户风险，研发可用故障时间线/版本影响/验证矩阵，不能强制所有职业套销售结构。
analysis_tables每个cell.text只能是引用原文子串、服务器提供的calculation证据子串，或待补充/—；不得自己算数、合成新金额。
指标status可为computed，此时calculation_id必须指向服务器提供的calculation_results.id，value必须逐字等于结果value。
因果与相关性必须区别；不能以几个损失恰好相加等于总缺口就认定独立归因，需说明重叠和口径。
行动basis默认为suggested；meeting_confirmed只能用于会议原文明确确认，负责人和期限也必须逐字在confirmation_quote中。报告建议不创建真实任务或修改会议安排。
不要输出没有依据的概率、置信度百分比或收益承诺；A/B/C评级不是模型自报置信度，未独立核验来源时不标A。
不要声称数据更新后自动刷新，当前报告是按生成时可访问证据制作的版本快照。
结论先行，summary直接回答analysis_goal；findings提供3-6条有价值结论，sections提供2-5节各300-600字的实质分析（资料少则缩短），metrics最多8项，risks最多6项，actions最多6项，analysis_tables最多3张。不要重复会议纪要。
'''


def check_quantified_claims(value, evidence):
    # Collect all failing locations so one bounded repair can fix them together.
    import json
    token=re.compile(r'(?<![\d.])[-+]?\d[\d,]*(?:\.\d+)?(?=\s*(?:%|％|万|亿|元|美元|百分点|倍))')
    raw_number=re.compile(r'(?<![\d.])[-+]?\d[\d,]*(?:\.\d+)?')
    issues=[]
    def verified(label,text,keys):
        source=' '.join(str(evidence[k].get('text','')) for k in keys if k in evidence)
        allowed={abs(Decimal(t.replace(',',''))) for t in raw_number.findall(source)}
        bad=[t for t in token.findall(text) if abs(Decimal(t.replace(',',''))) not in allowed]
        if bad:issues.append({'field':label,'unsupported_numbers':bad})
    verified('summary',value.summary,list(evidence))
    for i,item in enumerate(value.findings):verified(f'findings[{i}]',item.text,item.evidence_ids)
    for i,item in enumerate(value.sections):verified(f'sections[{i}]',item.content,item.evidence_ids)
    for i,item in enumerate(value.risks):verified(f'risks[{i}]',item.detail,item.evidence_ids)
    for i,item in enumerate(value.actions):verified(f'actions[{i}].expected_outcome',item.expected_outcome,item.evidence_ids)
    for t,table in enumerate(value.analysis_tables):
        for r,row in enumerate(table.rows):
            for c,cell in enumerate(row):verified(f'analysis_tables[{t}][{r}][{c}]',cell.text,cell.evidence_ids)
    if issues:raise ValueError('unsupported_quantified_report_claim:'+json.dumps(issues,ensure_ascii=False))
