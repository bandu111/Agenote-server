"""Agent 职责与模型选择分离；业务角色不直接绑定模型或密钥。"""
from dataclasses import dataclass


WORKER_ROLES = frozenset({'business_realtime', 'state', 'commitment', 'polish',
    'minutes', 'document', 'questionnaire', 'turn_end'})
REVIEW_ROLES = frozenset({'verify', 'business_verify'})

# 窄角色减少冗长生成；业务长计划和纪要仍使用原有预算，截断结果不会通过 Schema。
ROLE_BUDGETS = {'outcome_plan': 1600, 'outcome_report': 7800, 'state': 1400, 'memory': 900, 'forecast': 600, 'risk': 800,
    'verify': 1800, 'questionnaire': 450, 'commitment': 2400, 'turn_end': 180,
    'transcript_highlight': 350, 'assistant_query_route': 260, 'assistant_query_answer': 2200, 'meeting_preparation': 3200}


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str
    base_url: str
    key_env: str
    reasoning: dict
    max_tokens: int


def select_model(settings, role, *, fallback=False):
    """窄职责走 Worker；规划、前瞻、风险、记忆推理和未知角色保留主模型。"""
    if (not fallback and role in {'meeting_title', 'term_classification', 'segment_importance', 'transcript_highlight', 'profession_classification', 'assistant_query_route'}
        and getattr(settings, 'qwen_workers_enabled', False)
        and getattr(settings, 'dashscope_api_key', None)):
        return ModelRoute('qwen', settings.qwen_model, settings.qwen_base_url,
            'DASHSCOPE_API_KEY', {'enabled': False}, ROLE_BUDGETS.get(role, 350 if role == 'transcript_highlight' else 600))
    if (not fallback and role in WORKER_ROLES and
        (role != 'business_realtime' or getattr(settings, 'minimax_realtime_enabled', True)) and
        getattr(settings, 'minimax_workers_enabled', False) and
        getattr(settings, 'minimax_api_key', None)):
        return ModelRoute('minimax', settings.minimax_model, settings.minimax_base_url,
            'MINIMAX_API_KEY', {'enabled': True, 'effort': 'low'}, ROLE_BUDGETS.get(role, 4096))
    effort = getattr(settings, 'hermes_review_reasoning_effort', 'none') if role in REVIEW_ROLES else 'none'
    return ModelRoute('deepseek', settings.deepseek_model, 'https://api.deepseek.com',
        'DEEPSEEK_API_KEY', {'enabled': False} if effort == 'none' else {'enabled': True, 'effort': effort},
        ROLE_BUDGETS.get(role, 240 if role == 'live_text' else 550 if role == 'live' else (3200 if role in {'verify', 'commitment', 'document', 'questionnaire',
            'weekly', 'decision', 'proactive', 'controlled_tools'} else 1800)))
