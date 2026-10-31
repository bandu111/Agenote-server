"""行内推测续写先于模型完成；不把建议或其他说话人的话伪装成原话。"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.modules.meetings.services.live_hints import LiveMeetingHints, source_preview
from app.modules.meetings.services.memory_workflow import gap_targets
from tests.test_live_meeting_hints import utterance


def test_quote_repair_still_requires_same_speaker_but_never_creates_waiting_card():
    sources = [utterance('a', '交付时间定在', interrupted=True)]
    original = deepcopy(sources)
    assert source_preview(sources)['insights'] == []
    assert not source_preview(sources)['repairs'] and sources == original
    sources.append(utterance('b', '交付时间定在周五。', speaker='b', at=100))
    assert not source_preview(sources)['repairs']
    sources.append(utterance('resume', '交付时间定在周三。', at=200))
    third = source_preview(sources)
    assert third['repairs'][0]['text'] == '周三'
    assert third['repairs'][0]['evidence_ids'] == ['a', 'resume']
    assert sources[0]['raw'] == original[0]['raw']
    assert not any(i.get('issue_key') == 'memory-workflow-v1' for i in third['insights'])


def test_one_gap_per_turn_and_completed_prefix_never_becomes_repair_target():
    sources = [{**utterance('prefix', '交付时间先讨论。', interrupted=True), 'turn_id': 'turn-a'},
        {**utterance('tail', '交付时间定在', interrupted=True), 'turn_id': 'turn-a'}]
    assert [u['id'] for u in gap_targets(sources)] == ['tail']
    sources.append(utterance('resume', '交付时间定在周三。', at=300))
    assert [r['target_id'] for r in source_preview(sources)['repairs']] == ['tail']


@pytest.mark.parametrize('field,value', [('withdrawn', True), ('correction', '另行商定。'), ('manual', True)])
def test_invalidated_gap_is_removed(field, value):
    assert not gap_targets([{**utterance('a', '交付时间定在', interrupted=True), field: value}])


async def test_interrupted_a_streams_suffix_without_waiting_for_a_to_resume_or_for_model_done():
    finish = asyncio.Event()
    calls = []
    async def stream(role, prompt, payload):
        calls.append(payload)
        assert role == 'live_text' and '后半句' in prompt
        assert payload['target']['id'] == 'a'
        assert all(u['id'] != 'b' for u in payload['same_speaker_context'])
        yield {'type': 'delta', 'text': '双方'}
        await finish.wait()
        yield {'type': 'delta', 'text': '确认的时间。'}
        yield {'type': 'done', 'value': {'text': '双方确认的时间。'}}
    sources = [utterance('a', '交付时间定在', interrupted=True),
        utterance('b', '先谈预算。', speaker='b', at=100)]
    original = deepcopy(sources)
    service = LiveMeetingHints(SimpleNamespace(stream=stream))
    iterator = service.stream('owner', sources, output_protocol='text_v2')
    try:
        snapshot = await anext(iterator)
        assert not snapshot['insights'] and not snapshot['repairs']
        first = await asyncio.wait_for(anext(iterator), 1)
        item = first['inferred_completions'][0]
        assert item['target_id'] == 'a' and item['text'] == '双方' and item['streaming']
        assert item['inferred'] and item['start'] == item['end'] == len(sources[0]['raw'])
        assert item['evidence_ids'] == ['a'] and not first['repairs'] and not first['insights']
        assert first['businessEffect'] == 'none' and not finish.is_set()
        finish.set()
        growing, final = await anext(iterator), await anext(iterator)
        assert growing['inferred_completions'][0]['text'] == '双方确认的时间。'
        assert not final['inferred_completions'][0]['streaming']
        assert sources == original and len(calls) == 1
    finally:
        await iterator.aclose()


async def test_inferred_suffix_disconnect_retains_prefix_as_unfinished_not_verified_repair():
    async def stream(*_):
        yield {'type': 'delta', 'text': '双方确认'}
        raise OSError('synthetic disconnect')
    values = [v async for v in LiveMeetingHints(SimpleNamespace(stream=stream)).stream('owner',
        [utterance('a', '交付时间定在', interrupted=True)], output_protocol='text_v2')]
    last = values[-1]
    assert not last['repairs'] and not last['insights']
    assert last['inferred_completions'][0]['text'] == '双方确认'
    assert last['inferred_completions'][0]['interrupted']
    assert not last['inferred_completions'][0]['streaming']


async def test_no_model_mode_never_invents_suffix_or_waiting_card():
    provider = SimpleNamespace(stream=AsyncMock())
    values = [v async for v in LiveMeetingHints(provider).stream('owner',
        [utterance('a', '交付时间定在', interrupted=True)], include_model=False, output_protocol='text_v2')]
    assert all(not v['insights'] and not v['repairs'] and not v.get('inferred_completions') for v in values)
    provider.stream.assert_not_called()


async def test_inline_gap_uses_flutter_utf16_offsets_for_emoji():
    async def stream(*_):
        yield {'type': 'delta', 'text': '双方确认的范围'}
        yield {'type': 'done', 'value': {'text': '双方确认的范围'}}
    raw = '🙂范围就按……'
    values = [v async for v in LiveMeetingHints(SimpleNamespace(stream=stream)).stream('owner',
        [utterance('a', raw, interrupted=True)], output_protocol='text_v2')]
    item = values[-1]['inferred_completions'][0]
    assert item['start'] == 6 and item['end'] == 8
