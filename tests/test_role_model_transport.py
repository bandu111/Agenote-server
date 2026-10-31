"""真实 LangChain 客户端配合隔离 HTTP 传输，验证思考过滤和跨请求无会话记忆。"""
import json
import httpx
import pytest
from app.core.config import Settings
from app.integrations.hermes.role_model import RoleTextModel


@pytest.mark.parametrize('worker', [False, True])
async def test_fixed_role_has_real_prefixes_without_reasoning_or_previous_account_messages(worker):
    requests = []
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        model = body['model']
        def chunk(delta, finish=None):
            return 'data: '+json.dumps({'id': 'synthetic', 'object': 'chat.completion.chunk',
                'created': 0, 'model': model, 'choices': [{'index': 0, 'delta': delta,
                'finish_reason': finish}]})+'\n\n'
        response = chunk({'role': 'assistant', 'reasoning_content': 'private-chain-not-output'})
        response += chunk({'content': '{"insights":'})
        response += chunk({'content': '[]}'})
        response += chunk({}, 'stop')+'data: [DONE]\n\n'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=response)
    adapter = RoleTextModel(Settings(deepseek_api_key='synthetic-deepseek',
        minimax_api_key='synthetic-minimax', minimax_workers_enabled=worker))
    adapter.http.close()
    await adapter.async_http.aclose()
    adapter.http = httpx.Client(transport=httpx.MockTransport(handle))
    adapter.async_http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        first = [v async for v in adapter.stream('state', 'system', {'utterances': ['account-one-only']})]
        second = [v async for v in adapter.stream('state', 'system', {'utterances': ['account-two-only']})]
        assert [v['text'] for v in first if v['type'] == 'delta'] == ['{"insights":', '[]}']
        assert first[-1]['value'] == {'insights': []} and second[-1]['runtime']['transport'] == 'langchain'
        assert 'private-chain' not in str(first)
        assert len(requests) == 2 and all(len(r['messages']) == 2 for r in requests)
        assert 'account-one-only' not in json.dumps(requests[1])
        assert all('tools' not in r for r in requests)
        if worker:
            assert requests[0]['reasoning_effort'] == 'low'
            assert requests[0]['reasoning_split'] is True
            assert requests[0]['max_completion_tokens'] >= 2048
        else:
            assert requests[0]['thinking']['type'] == 'disabled'
    finally:
        await adapter.close()
