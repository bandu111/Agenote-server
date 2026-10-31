import asyncio
import pytest
from app.core.config import Settings
from app.integrations.hermes.provider import HermesProvider
from app.modules.meetings.services.hint_gate import HintGate
from app.modules.meetings.services.meeting_agents import analysis_triggers
from tests.test_live_meeting_hints import utterance


def test_action_can_use_readonly_fast_lane_but_repeat_and_greeting_cannot():
    gate = HintGate()
    sources = [utterance('a', '丁负责完整测试，周三上午提交测试报告。')]
    decision = gate.prepare('owner', 'meeting', sources)
    assert decision['needed']
    gate.remember(decision, generated=True)
    assert not gate.prepare('owner', 'meeting', sources)['needed']
    assert not gate.prepare('owner', 'other', [utterance('hello', '好，人应该都到齐了吧。')])['needed']
    assert 'risk' in analysis_triggers([utterance('b', '接口今天无法确认，可能影响交付。')])


@pytest.mark.asyncio
async def test_risk_and_realtime_have_reserved_slots_while_polish_waits_and_total_cap_stays_three():
    provider = HermesProvider(Settings(deepseek_api_key='synthetic-only'))
    release = asyncio.Event()
    entered = {role: asyncio.Event() for role in ['state', 'polish', 'live', 'risk']}
    active = peak = 0
    async def stream(role, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered[role].set()
        try:
            await release.wait()
            yield {'type': 'done', 'value': {}, 'runtime': {}}
        finally:
            active -= 1
    provider._stream_unbounded = stream
    async def run(role):
        return [event async for event in provider._stream_once(role, '', {})]
    tasks = [asyncio.create_task(run(role)) for role in ['state', 'polish']]
    try:
        await asyncio.wait_for(entered['state'].wait(), 1)
        assert not entered['polish'].is_set()
        tasks.append(asyncio.create_task(run('live')))
        await asyncio.wait_for(entered['live'].wait(), 1)
        tasks.append(asyncio.create_task(run('risk')))
        await asyncio.wait_for(entered['risk'].wait(), 1)
        assert not entered['polish'].is_set()
        assert active == peak == 3
    finally:
        release.set()
        await asyncio.gather(*tasks)
        await provider.close()
    assert peak <= 3
