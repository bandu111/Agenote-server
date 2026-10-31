"""Role lenses guide questions and evidence needs, never prescribe a fixed report."""
from app.modules.outcomes.professions import CATEGORIES, classify_local

# objective, metrics, required source types, candidate methods, deliverable examples.
LENSES = {
 'engineering': ('识别技术异常、变更影响与可验证的故障假设',
  ['接口延迟分位数','错误率','测试覆盖率','缺陷严重度'],
  ['异常日志','性能监控','代码变更','测试报告','版本记录'],
  ['时间关联','版本差异对比','故障假设验证','回归覆盖分析'],
  ['性能诊断','代码变更影响评估','技术风险与验证计划']),
 'product_design': ('识别受影响的用户流程、需求变化和修复优先级',
  ['关键流程完成率','用户反馈频次','问题严重度','功能使用率'],
  ['用户反馈','需求文档','产品指标','历史版本'],
  ['用户旅程分析','需求差异对比','影响与优先级评估'],
  ['产品体验影响评估','需求影响评估','PRD 修改草案']),
 'sales_business': ('识别客户续费、商机与营收风险及跟进机会',
  ['续费率','成交率','流失率','销售额','毛利'],
  ['续费记录','客户清单','销售明细','历史业绩'],
  ['客户分群','同期趋势对比','营收风险敞口计算'],
  ['续费风险分析','重点客户跟进清单','销售策略建议']),
 'marketing_growth': ('评估渠道效果、转化损失与预算效率',
  ['获客成本','转化率','投放 ROI','用户增长','渠道贡献'],
  ['投放明细','渠道归因数据','漏斗数据','预算与收入记录'],
  ['转化漏斗分析','渠道对比','投入产出计算'],
  ['营销效果分析','渠道 ROI 分析','增长优化方案']),
 'customer_service': ('识别客户问题趋势、服务质量和续费风险',
  ['满意度','投诉率','首次响应时间','解决时长','续费风险'],
  ['工单明细','投诉记录','满意度调查','客户反馈','续费记录'],
  ['问题聚类','服务时效对比','客户风险分层'],
  ['客户问题分析','服务质量评估','客户改善方案']),
 'project_delivery': ('识别关键路径、进度偏差与交付阻塞',
  ['里程碑偏差','任务延期天数','资源负载','依赖完成状态'],
  ['项目计划','任务状态','里程碑','资源配置','依赖记录'],
  ['计划与实际对比','依赖分析','关键路径与情景评估'],
  ['交付风险分析','进度偏差分析','排期调整建议']),
 'management': ('评估经营目标偏差、成本效益与决策风险',
  ['目标达成率','营收与利润','成本效益','业务绩效'],
  ['经营目标','业务报表','成本收入数据','战略与市场资料'],
  ['目标差距分析','情景比较','成本效益评估'],
  ['经营分析','战略决策备忘录','经营改善建议']),
 'finance_legal': ('识别预算、现金流、成本与合同合规风险',
  ['预算差异','现金流缺口','成本偏差','利润率','合同风险敞口'],
  ['预算表','财务流水','成本明细','合同条款','适用合规依据'],
  ['预算与实际对比','成本差异分析','合同条款对照'],
  ['预算差异分析','合同风险审查草案','财务预测草案']),
 'hr_admin': ('评估招聘、人力配置与组织效率问题',
  ['招聘周期','岗位缺口','人力成本','绩效分布','培训效果'],
  ['招聘进度','岗位编制','人力成本','绩效与培训记录'],
  ['人员供需对比','岗位缺口分析','培训前后对比'],
  ['招聘分析','人员配置评估','组织改善建议']),
 'manufacturing_supply': ('识别生产质量、库存和供应交付风险',
  ['库存周转','良品率','生产效率','采购成本','交付周期'],
  ['库存台账','生产记录','质量数据','供应商履约','采购明细'],
  ['库存风险分析','质量异常对比','供应商与周期评估'],
  ['生产异常分析','库存风险分析','供应链优化方案']),
 'education_research': ('评估教学或研究进展、实验结果与知识缺口',
  ['教学反馈','学习结果','研究里程碑','实验指标','结果可重复性'],
  ['教学反馈','科研计划','实验原始数据','文献与研究成果'],
  ['实验对照分析','进度差异分析','证据与文献比较'],
  ['教学反馈分析','实验数据分析','科研进展报告','研究综述草稿']),
 'medical_public': ('识别服务流程、资源配置和安全合规问题',
  ['服务等待时间','流程时长','资源利用率','安全事件','合规缺口'],
  ['服务记录','流程数据','资源安排','安全记录','适用政策'],
  ['流程瓶颈分析','资源供需评估','合规要求对照'],
  ['服务质量分析','资源配置评估','流程优化与风险梳理']),
 'other': ('根据用户明确问题和会议事实选择最有价值的分析目标',
  [],['与问题直接相关的原始数据和业务资料'],['证据审查'],['问题分析与验证计划']),
}

def role_lens(profession):
    profession = profession or {}
    raw = str(profession.get('raw') or '').strip()
    category = profession.get('categoryId') if profession.get('confirmed') is True else None
    if category not in LENSES: category = classify_local(raw) or 'other'
    objective,metrics,sources,methods,deliverables=LENSES[category]
    return dict(categoryId=category,category=CATEGORIES[category],raw=raw,
        objective=objective,candidateMetrics=metrics,sourceTypes=sources,
        candidateMethods=methods,deliverableExamples=deliverables,
        priority='explicit_user_request_then_meeting_question_then_profession',
        rule='选择当前问题相关的指标与方法，不要求每个指标都分析；缺数据不编数值，不强制固定章节')

AVAILABLE_METHODS = {
    'evidence_review': '会议和资料证据审查',
    'decimal_arithmetic': '已验证原文数值的有界Decimal算术，不含因果模型',
    'document_compare': '已检索资料的内容对照（不是原始数据统计）',
}
UNAVAILABLE_TOOLS = ['SQL数据库统计','Python原始数据计算','外部代码仓库查询','日志/监控平台连接']
