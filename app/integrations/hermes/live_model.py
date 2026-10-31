"""只读补充建议使用现有 LangChain LLM 适配，不创建额外 Agent 会话或工具循环。"""
import asyncio
import json
from contextlib import aclosing
import httpx
from langchain_deepseek import ChatDeepSeek
from langsmith import tracing_context


class LiveTextModel:
    def __init__(self, settings):
        self.settings = settings
        timeout = httpx.Timeout(15, connect=5)
        self.http = httpx.Client(trust_env=False, timeout=timeout)
        self.async_http = httpx.AsyncClient(trust_env=False, timeout=timeout)
        self.model = ChatDeepSeek(model=settings.deepseek_model, api_key=settings.deepseek_api_key,
            api_base='https://api.deepseek.com', max_tokens=240, max_retries=0,
            timeout=timeout, http_client=self.http, http_async_client=self.async_http,
            extra_body={'thinking': {'type': 'disabled'}})

    async def close(self):
        self.http.close()
        await self.async_http.aclose()

    async def stream(self, system, payload):
        started, first, text = asyncio.get_running_loop().time(), None, ''
        # 不启用外部追踪；同一 HTTP 连接池无会话记忆，每次仅传本次受限文本。
        with tracing_context(enabled=False):
            async with aclosing(self.model.astream([
                ('system', system), ('human', json.dumps(payload, ensure_ascii=False))
            ], config={'callbacks': []})) as events:
                async for chunk in events:
                    value = chunk.content
                    if not isinstance(value, str) or not value: continue
                    if first is None: first = round(asyncio.get_running_loop().time() - started, 3)
                    text += value
                    yield {'type': 'delta', 'text': value}
        yield {'type': 'done', 'value': {'text': text}, 'runtime': {
            'provider': 'deepseek', 'model': self.settings.deepseek_model, 'transport': 'langchain',
            'agentReadySeconds': 0, 'firstTextSeconds': first,
            'completeSeconds': round(asyncio.get_running_loop().time() - started, 3)}}
