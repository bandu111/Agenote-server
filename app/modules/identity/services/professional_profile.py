"""One occupational profile for onboarding, profile display and analysis."""
from app.modules.outcomes.professions import classify_local

CHOICES = {
    'software': ('软件开发', 'engineering'),
    'design': ('产品设计', 'product_design'),
    'project': ('项目管理', 'project_delivery'),
    'marketing': ('市场运营', 'marketing_growth'),
    'education': ('教育', 'education_research'),
    'finance': ('财务', 'finance_legal'),
}


def professional_profile(profile):
    result = dict(profile or {})
    onboarding = result.get('personalization') or {}
    choice = onboarding.get('profession')
    selected = CHOICES.get(choice)
    # An explicit profile edit (including clearing the job) wins over onboarding.
    if not result.get('job') and result.get('job_source') != 'profile':
        raw = (onboarding.get('profession_other', '').strip() if choice == 'other'
               else selected[0] if selected else '')
        if raw:
            result['job'] = raw
            result['job_source'] = 'onboarding'
    raw = result.get('job', '')
    previous = result.get('outcome_profession') or {}
    if previous.get('raw') != raw or not previous:
        category = selected[1] if selected and result.get('job_source') == 'onboarding' else classify_local(raw)
        result['outcome_profession'] = {
            'raw': raw, 'categoryId': category or 'other',
            'source': 'onboarding' if selected and result.get('job_source') == 'onboarding' else 'dictionary' if category else 'unclassified',
            'confirmed': bool(selected and result.get('job_source') == 'onboarding'),
        }
    return result
