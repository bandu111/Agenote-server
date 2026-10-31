"""真实异步迭代器证明首段交付不依赖完整响应；不调用外部模型。"""
import asyncio
from types import SimpleNamespace
from pydantic import SecretStr
from app.integrations.hermes.live_model import LiveTextModel

async def test_live_llm_adapter_yields_prefix_and_closes_stream_on_disconnect(monkeypatch):
    finish=asyncio.Event();closed=asyncio.Event();config={}
    class Model:
        def __init__(self,**kwargs):config.update(kwargs)
        async def astream(self,*args,**kwargs):
            try:
                yield SimpleNamespace(content='建议')
                await finish.wait()
                yield SimpleNamespace(content='确认范围。')
            finally:closed.set()
    monkeypatch.setattr('app.integrations.hermes.live_model.ChatDeepSeek',Model)
    model=LiveTextModel(SimpleNamespace(deepseek_api_key=SecretStr('synthetic'),deepseek_model='deepseek-flash'))
    stream=model.stream('只读建议',{'utterances':[]})
    try:
        first=await asyncio.wait_for(anext(stream),1)
        assert first=={'type':'delta','text':'建议'} and not finish.is_set()
        assert config['max_retries']==0 and config['extra_body']=={'thinking':{'type':'disabled'}}
        await stream.aclose()
        assert closed.is_set()
    finally: await model.close()
