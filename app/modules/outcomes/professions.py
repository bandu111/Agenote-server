"""Conservative local classification. Uncertain cases require model/user review."""
import re

CATEGORIES = {
    'engineering': '技术与研发', 'product_design': '产品与设计',
    'sales_business': '销售与商务', 'marketing_growth': '市场与增长',
    'customer_service': '客户与服务', 'project_delivery': '项目与交付',
    'management': '经营与管理', 'finance_legal': '财务与法务',
    'hr_admin': '人力与行政', 'manufacturing_supply': '制造与供应链',
    'education_research': '教育与科研', 'medical_public': '医疗与公共服务',
    'other': '其他 / 自定义',
}
WORDS = {
    'engineering': ['flutter','开发工程师','程序员','软件工程','后端','前端','算法','测试工程','运维','架构师'],
    'product_design': ['产品经理','产品设计','设计师','交互设计','ux','ui设计'],
    'sales_business': ['销售','商务','客户经理'],
    'marketing_growth': ['市场','增长','投放','营销','品牌运营'],
    'customer_service': ['客服','客户成功','售后'],
    'project_delivery': ['项目经理','交付经理','项目管理','实施顾问'],
    'management': ['总经理','创始人','总裁','ceo','经营管理'],
    'finance_legal': ['财务','会计','审计','法务','律师'],
    'hr_admin': ['人力','行政','招聘','hr'],
    'manufacturing_supply': ['制造','供应链','采购','生产','物流'],
    'education_research': ['教师','老师','科研','研究员','教授'],
    'medical_public': ['医生','护士','医疗','公共服务','公务员'],
}

def classify_local(raw):
    value = raw.strip().casefold()
    hits = [key for key, words in WORDS.items() if any(
        (re.search(r'(?<![a-z])'+re.escape(w)+r'(?![a-z])', value) if w.isascii() else w in value)
        for w in words)]
    return hits[0] if len(hits) == 1 else None
