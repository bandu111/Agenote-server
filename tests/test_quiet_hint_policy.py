"""重复提示节流不能吞掉新交期、纠错或其他会话的提示。"""
from types import SimpleNamespace
from app.modules.meetings.services.hint_gate import HintGate
from app.modules.meetings.services.live_hints import LiveMeetingHints
from app.modules.meetings.services.meeting_agents import analysis_triggers
from app.modules.meetings.services.business_guard import transcript_hashes
from tests.test_live_meeting_hints import utterance


async def test_repeated_scope_and_backchannel_do_not_call_model_again_but_new_date_does():
    calls = []
    async def stream(role, prompt, payload):
        calls.append(payload)
        yield {'type': 'delta', 'text': '建议确认交付范围。'}
        yield {'type': 'done', 'value': {'text': '建议确认交付范围。'}}
    hints = LiveMeetingHints(SimpleNamespace(stream=stream))
    sources = [utterance('u1', 'Windows尚未批准。')]
    async def run():
        return [v async for v in hints.stream('owner', sources, output_protocol='text_v2', meeting_id='meeting')]
    await run()
    await run()
    sources.append(utterance('u2', 'Windows尚未批准。', at=10))
    await run()
    sources.append(utterance('u3', '好的。', at=20))
    await run()
    assert len(calls) == 1
    sources.append(utterance('u4', '交期改为下周五。', at=30))
    await run()
    assert len(calls) == 2 and calls[-1]['focus_id'] == 'u4'
    sources[-1]['correction'] = '交期改为下周三。'
    await run()
    assert len(calls) == 3


async def test_old_gap_does_not_hijack_suggestion_for_new_speaker():
    payloads = []
    async def stream(role, prompt, payload):
        payloads.append(payload)
        yield {'type': 'delta', 'text': '建议确认预算。'}
        yield {'type': 'done', 'value': {'text': '建议确认预算。'}}
    service = LiveMeetingHints(SimpleNamespace(stream=stream))
    sources = [utterance('gap', '交付就按', interrupted=True),
        utterance('budget', '预算能不能增加到20万元？', speaker='b', at=20)]
    result = [v async for v in service.stream('owner', sources, output_protocol='text_v2', meeting_id='m')]
    assert payloads[0]['focus_id'] == 'budget'
    assert result[-1]['insights'][-1]['evidence_ids'] == ['budget']


def test_cache_is_scoped_to_owner_and_meeting_and_bounds_memory():
    gate = HintGate()
    sources = [utterance('u', 'Windows尚未批准。')]
    first = gate.prepare('a', 'meeting-a', sources)
    gate.remember(first, generated=True)
    assert not gate.prepare('a', 'meeting-a', sources)['needed']
    assert gate.prepare('b', 'meeting-a', sources)['needed']
    assert gate.prepare('a', 'meeting-b', sources)['needed']
    for i in range(300):
        gate.remember(gate.prepare(str(i), 'm', sources))
    assert len(gate.states) == 256


def test_changed_chinese_amount_or_approval_is_never_suppressed_as_repeat():
    gate = HintGate()
    original = [utterance('a', '请再次核对项目预算二十万元，是否支持本次全部交付？')]
    gate.remember(gate.prepare('owner', 'm', original), generated=True)
    newer = original + [utterance('b', '请再次核对项目预算三十万元，是否支持本次全部交付？', at=10)]
    assert gate.prepare('owner', 'm', newer)['needed']


def test_only_redundant_readonly_roles_are_suppressed_not_changes_or_commitments():
    original = [utterance('u1', 'Windows尚未批准，我们会提交评估报告。')]
    context = {'previous_analysis': {'source_hashes': transcript_hashes(original)}}
    repeat = original + [utterance('u2', original[0]['raw'], at=10)]
    triggered = analysis_triggers(repeat, context)
    assert 'commitment' in triggered and 'state' in triggered
    assert 'risk' not in triggered and 'forecast' not in triggered
    changed = original + [utterance('u2', 'Windows交期改为下周五。', at=10)]
    assert {'risk', 'commitment'} <= analysis_triggers(changed, context)
    assert 'risk' in analysis_triggers(repeat, {**context, 'knowledge_changed': True, 'knowledge': [{'nodeId': 'new'}]})


async def test_failure_is_not_cached_as_success():
    calls = []
    async def stream(*args):
        calls.append(True)
        raise OSError('synthetic-disconnect')
        yield
    service = LiveMeetingHints(SimpleNamespace(stream=stream))
    for _ in range(2):
        result = [v async for v in service.stream('owner', [utterance('u', 'Windows尚未批准。')],
            output_protocol='text_v2', meeting_id='m')]
        assert result[-1]['modelStatus'] == 'unavailable'
    assert len(calls) == 2
