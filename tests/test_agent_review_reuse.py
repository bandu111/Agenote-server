"""已有承诺之后的普通发言也应只调用轻量状态角色。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.modules.meetings.services.meeting_agents import MeetingAgents
from app.modules.meetings.services.business_guard import guarded_report
from tests.test_commitment_agent import source, candidate, event


async def test_ordinary_followup_reuses_exact_reviewed_candidates():
    async def reply(role, _prompt, payload):
        if role == "commitment":
            return {"commitments": [candidate([event("committed", "u1")])]}
        if role == "verify":
            return {"reviews": [{"id": c["id"], "digest": c["digest"], "accepted": True} for c in payload["business_candidates"]]}
        return {}
    provider = SimpleNamespace(run=AsyncMock(side_effect=reply))
    agent = MeetingAgents(provider)
    raw = [source("u1", "我方负责提交评估报告。")]
    first = guarded_report(await agent.analyze(raw), raw)
    provider.run.reset_mock()
    raw.append(source("u2", "我们先休息一下。"))
    second = await agent.analyze(raw, context={"previous_analysis": first})
    assert provider.run.await_count == 1
    assert second["commitments"] and second["candidate_reviews"]
