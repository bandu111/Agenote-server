"""固定职责使用 LangChain 的无状态模型适配；自主工具规划仍由 Hermes SDK 执行。"""
import asyncio
import json
from contextlib import aclosing
import httpx
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from app.integrations.hermes.model_policy import select_model

LIGHT_ROLES = frozenset({'meeting_title', 'state', 'memory', 'forecast', 'risk', 'verify', 'commitment', 'questionnaire', 'turn_end', 'assistant_query_route'})


class RoleTextModel:
    def __init__(self, settings):
        self.settings = settings
        self.models = {}
        self.timeout = httpx.Timeout(45, connect=5)
        self.http = httpx.Client(trust_env=False, timeout=self.timeout)
        self.async_http = httpx.AsyncClient(trust_env=False, timeout=self.timeout)

    def model(self, role, fallback=False):
        route = select_model(self.settings, role, fallback=fallback)
        key = (role, fallback)
        if key not in self.models:
            secret = getattr(self.settings, {'minimax': 'minimax_api_key', 'qwen': 'dashscope_api_key'}.get(route.provider, 'deepseek_api_key'))
            common = dict(model=route.model, api_key=secret, max_retries=0, timeout=self.timeout,
                http_client=self.http, http_async_client=self.async_http)
            if route.provider == 'qwen':
                self.models[key] = ChatOpenAI(**common, base_url=route.base_url,
                    max_completion_tokens=route.max_tokens, stream_usage=False,
                    **({'model_kwargs': {'response_format': {'type': 'json_object'}}} if role in {'meeting_title', 'assistant_query_route'} else {}),
                    extra_body={'enable_thinking': False})
            elif route.provider == 'minimax':
                # M3.1 的思考 token 计入上限；限制可见正文，不把思考预算误压到几百 token。
                self.models[key] = ChatOpenAI(**common, base_url=route.base_url, use_responses_api=False,
                    max_completion_tokens=max(2048, route.max_tokens), reasoning_effort='low',
                    stream_usage=False, extra_body={'reasoning_split': True})
            else:
                enabled = route.reasoning.get('enabled', False)
                self.models[key] = ChatDeepSeek(**common, api_base=route.base_url, max_tokens=route.max_tokens,
                    extra_body={'thinking': {'type': 'enabled' if enabled else 'disabled'}},
                    **({'reasoning_effort': route.reasoning['effort']} if enabled else {}))
        return self.models[key], route

    async def stream(self, role, system, payload, *, fallback=False):
        model, route = self.model(role, fallback)
        started, first, text = asyncio.get_running_loop().time(), None, ''
        with tracing_context(enabled=False):
            async with aclosing(model.astream([('system', system),
                ('human', json.dumps(payload, ensure_ascii=False))], config={'callbacks': []})) as events:
                async for chunk in events:
                    # 只读 content，绝不转发 reasoning_content、工具调用或回调追踪。
                    value = chunk.content
                    if not isinstance(value, str) or not value:
                        continue
                    if first is None:
                        first = round(asyncio.get_running_loop().time() - started, 3)
                    text += value
                    if len(text) > 96000:
                        raise ValueError('role_output_too_large')
                    yield {'type': 'delta', 'text': value}
        raw = text.strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0]
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('role_output_not_object')
        yield {'type': 'done', 'value': result, 'runtime': {'provider': route.provider,
            'model': route.model, 'transport': 'langchain', 'agentReadySeconds': 0,
            'firstTextSeconds': first, 'completeSeconds': round(asyncio.get_running_loop().time()-started, 3)}}

    async def close(self):
        self.http.close()
        await self.async_http.aclose()
