"""Bounded Decimal arithmetic over verbatim, account-scoped evidence. Never eval."""
import re
import unicodedata
from decimal import Decimal, localcontext, ROUND_HALF_UP
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Operand(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    value: str = Field(min_length=1, max_length=40)
    unit: str = Field(min_length=1, max_length=20)
    quote: str = Field(min_length=1, max_length=500)
    evidence_id: str = Field(min_length=1, max_length=200)


class Calculation(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,39}$')
    name: str = Field(min_length=1, max_length=100)
    operation: Literal['sum', 'difference', 'ratio_percent', 'change_percent']
    operands: list[Operand] = Field(min_length=2, max_length=12)


NUMBER = re.compile(r'(?<![\d.])[-+]?\d[\d,]*(?:\.\d+)?(?![\d.])')


def number(value):
    text = unicodedata.normalize('NFKC', value).replace(',', '')
    if not re.fullmatch(r'[-+]?\d{1,16}(?:\.\d{1,8})?', text):
        raise ValueError('invalid_decimal_operand')
    result = Decimal(text)
    if abs(result) > Decimal('1e15'):
        raise ValueError('operand_out_of_range')
    return result


def execute(calculation, evidence):
    c = Calculation.model_validate(calculation)
    if len({o.unit for o in c.operands}) != 1:
        raise ValueError('incompatible_operand_units')
    values, sources = [], []
    for o in c.operands:
        if o.evidence_id not in evidence:
            raise ValueError('invalid_calculation_source')
        source = evidence[o.evidence_id]
        if source.get('kind') == 'calculation' or o.evidence_id.startswith('calculation:'):
            raise ValueError('nested_calculations_not_allowed')
        if o.quote not in str(source.get('text', '')) or o.unit not in o.quote:
            raise ValueError('ungrounded_operand_quote')
        value = number(o.value)
        tokens = NUMBER.findall(unicodedata.normalize('NFKC', o.quote))
        if not any(number(t) == value for t in tokens):
            raise ValueError('ungrounded_operand_value')
        values.append(value)
        sources.append(o.evidence_id)
    if c.operation != 'sum' and len(values) != 2:
        raise ValueError('invalid_operand_count')
    with localcontext() as ctx:
        ctx.prec = 40
        unit = c.operands[0].unit
        if c.operation == 'sum':
            result = sum(values)
        elif c.operation == 'difference':
            result = values[0] - values[1]
        else:
            # ratio = actual/target; change = (current-baseline)/baseline.
            if values[1] == 0:
                raise ValueError('zero_denominator')
            result = (values[0] if c.operation == 'ratio_percent' else values[0]-values[1]) / values[1] * 100
            unit = '%'
        rounded = result.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    display = format(rounded, 'f').rstrip('0').rstrip('.') if '.' in format(rounded, 'f') else str(rounded)
    value = display + unit
    return {'id': c.id, 'name': c.name, 'operation': c.operation, 'value': value,
            'unit': unit, 'operands': [o.model_dump() for o in c.operands],
            'evidence_ids': list(dict.fromkeys(sources)),
            'rounding': 'Decimal / ROUND_HALF_UP / 2 decimal places',
            'limitation': '基于所引原始数值的算术结果，不构成因果归因或源数据真实性认证'}


PROMPT = '''只从 evidence 原文中选择最多4项对当前 analysis_plan 有价值的算术计算，不生成报告。
返回 {"calculations":[{"id":"attainment","name":"达成率","operation":"ratio_percent",
"operands":[{"value":"4215","unit":"万","quote":"销售额4215万","evidence_id":"原证据键"},
{"value":"4800","unit":"万","quote":"目标4800万","evidence_id":"原证据键"}]}]}。
以上数值仅是格式示例，绝不能当作输入数据。输入不存在可比数据时返回空列表。
操作sum为求和；difference为第一个减第二个；ratio_percent为第一个/第二个*100；change_percent为(当前-基准)/基准*100。
所有操作数必须逐字来自quote；quote必须在对应证据中；单位必须同一量纲且出现在各自quote。
只计算同一统计口径、对象、时间范围可比较的数值。不混合ARR和季度收入、不混合预算和实际、不把推测损失分摊为确定归因。
禁止模型自己给出结果、公式、代码；服务器使用Decimal计算。数字不足、口径不清则不算。'''
