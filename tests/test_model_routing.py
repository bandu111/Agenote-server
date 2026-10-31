"""模型分工不改变正式审批防线；配置不会把密钥放入任务和日志。"""
from app.core.config import Settings
from app.integrations.hermes.provider import HermesProvider,HermesUnavailableError


def test_minimax_routes_only_restricted_extractor_and_keeps_review_independent():
    provider=HermesProvider(Settings(minimax_api_key='synthetic-worker-key',minimax_workers_enabled=True,minimax_realtime_enabled=True,
        minimax_base_url='https://api.minimaxi.com/v1'))
    env,task=provider._prepare('business_realtime','test',{})
    assert task['model']=='MiniMax-M3.1-Flash'
    assert task['reasoning']=={'enabled':True,'effort':'low'}
    assert 'synthetic-worker-key' not in str(task)
    assert env['MINIMAX_API_KEY']=='synthetic-worker-key'
    for role in ['verify','business_verify','risk','planner','live']:
        _,task=provider._prepare(role,'',{})
        assert task['model']==provider.settings.deepseek_model
        assert task['key_env']=='DEEPSEEK_API_KEY'


async def test_worker_failure_falls_back_to_original_model_without_reducing_review(monkeypatch):
    provider=HermesProvider(Settings(minimax_api_key='synthetic-worker-key',minimax_workers_enabled=True,minimax_realtime_enabled=True))
    calls=[]
    async def stream(role,system,payload,*,fallback=False):
        calls.append(fallback)
        if not fallback: raise HermesUnavailableError()
        yield {'type':'done','value':{'goals':[]}}
    monkeypatch.setattr(provider,'_stream_once',stream)
    events=[e async for e in provider.stream('business_realtime','',{})]
    assert calls==[False,True] and events[-1]['type']=='done'

def test_all_worker_roles_are_independent_of_agent_classes_and_high_only_affects_review():
    from app.integrations.hermes.model_policy import select_model, WORKER_ROLES
    settings=Settings(minimax_api_key='synthetic-worker-key',minimax_workers_enabled=True,minimax_realtime_enabled=True,
        hermes_review_reasoning_effort='high')
    for role in WORKER_ROLES:
        route=select_model(settings,role)
        assert route.provider=='minimax' and route.reasoning['effort']=='low'
    for role in ['planner','controlled_tools','forecast','risk','memory','unknown_future_role']:
        assert select_model(settings,role).provider=='deepseek'
        assert select_model(settings,role).reasoning=={'enabled':False}
    assert select_model(settings,'verify').reasoning=={'enabled':True,'effort':'high'}
    assert select_model(settings,'business_realtime',fallback=True).provider=='deepseek'
    isolated = settings.model_copy(update={'minimax_realtime_enabled': False})
    assert select_model(isolated, 'business_realtime').provider == 'deepseek'
    assert select_model(isolated, 'polish').provider == 'minimax'

def test_model_metrics_formatter_never_serializes_speech_or_credentials():
    import logging,json
    from app.core.logging import ModelFormatter
    record=logging.LogRecord('test',logging.INFO,'',0,'sensitive arbitrary body',(),None)
    record.role='polish';record.provider='minimax';record.model='MiniMax-M3.1-Flash'
    record.seconds=1.0;record.outcome='ok';record.raw='private speech';record.api_key='private key'
    value=ModelFormatter().format(record)
    assert 'private' not in value and 'sensitive arbitrary body' not in value
    assert json.loads(value)['role']=='polish'
