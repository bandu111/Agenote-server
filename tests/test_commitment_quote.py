"""允许模型归纳标题，但实际履约记录必须使用可定位的原句摘录。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.modules.meetings.services.commitment_agent import CommitmentAgent
from tests.test_commitment_agent import source, candidate, event


async def test_action_uses_literal_quote_instead_of_unsupported_paraphrase():
    item = candidate([event("committed", "u1")], action="提交Android/iOS双端测试报告", action_quote="提交双端测试报告")
    provider = SimpleNamespace(run=AsyncMock(return_value={"commitments": [item]}))
    result = await CommitmentAgent(provider).analyze([source("u1", "我方负责2030年10月28日提交双端测试报告。")])
    assert result[0]["action"] == "提交双端测试报告"
    assert provider.run.await_count == 1


async def test_unquoted_hallucinated_action_has_only_one_retry_then_drops():
    item = candidate([event("committed", "u1")], action="扩大Windows交付范围", action_quote="同意扩大Windows范围")
    provider = SimpleNamespace(run=AsyncMock(return_value={"commitments": [item]}))
    result = await CommitmentAgent(provider).analyze([source("u1", "我方负责提交双端测试报告。")])
    assert result == [] and provider.run.await_count == 2
