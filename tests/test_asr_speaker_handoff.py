"""正常零停顿接话与延迟声源更正，直接穿过生产 relay。"""
import asyncio
from copy import deepcopy
import json
import pytest
from app.modules.meetings.services.asr_stream_service import relay


def sentence(segment, speaker, text, final=True):
    return {'sentence_id': segment, 'speaker_id': speaker, 'sentence': text,
        'sentence_type': int(final), 'start_time': segment * 1000, 'end_time': (segment + 1) * 1000}


class Socket:
    def __init__(self):
        self.sent = []
    async def receive(self):
        await asyncio.Event().wait()
    async def send_json(self, value):
        self.sent.append(deepcopy(value))


class Upstream:
    def __init__(self, frames):
        self.frames = iter(frames)
    async def recv(self):
        await asyncio.sleep(0)
        return json.dumps(next(self.frames))


@pytest.mark.asyncio
async def test_zero_pause_a_b_a_keeps_provider_identity_and_original_times():
    frames = [{'code': 0, 'sentences': {'sentence_list': [row]}} for row in [
        sentence(0, 0, '我方按原范围交付。'),
        sentence(1, -1, '我接着补充', False),
        sentence(1, 1, '我接着补充验收标准。'),
        sentence(2, 0, '我们先核实标准。')]] + [{'code': 0, 'final': 1}]
    socket = Socket()
    await relay(socket, Upstream(frames), {})
    stable = [e for e in socket.sent if e.get('is_final')]
    assert [e['speaker_id'] for e in stable] == [0, 1, 0]
    assert stable[0]['end_ms'] == stable[1]['start_ms']
    assert stable[1]['end_ms'] == stable[2]['start_ms']
    assert socket.sent[1]['speaker_id'] is None
    assert socket.sent[-1]['type'] == 'done'


@pytest.mark.asyncio
async def test_late_b_identity_correction_is_versioned_and_does_not_rewrite_text():
    frames = [{'sentences': [sentence(0, 0, '甲说完。'), sentence(1, 0, '乙紧接着说。')]},
        {'sentences': [sentence(1, 1, '供应商的重复文字不能替换原话。')]},
        {'final': 1}]
    socket = Socket()
    await relay(socket, Upstream(frames), {})
    update = next(e for e in socket.sent if e['type'] == 'speaker_update')
    assert update['segment'] == 1 and update['speaker_id'] == 1
    assert update['speaker_revision'] == 1 and update['text'] == '乙紧接着说。'


def test_diagnostics_contain_only_bounded_anonymous_ids_and_counters():
    from app.modules.meetings.services.asr_speaker_stats import AsrSpeakerStats
    stats = AsrSpeakerStats()
    stats.observe_provider({'sentences': {'sentence_list': [
        sentence(0, 0, '此正文不能进入日志。'), sentence(1, -1, '此正文不能进入日志。', False),
        sentence(1, 1, '此正文不能进入日志。')]}, 'speaker_context_id': '不要记录的凭据'})
    stats.observe_forwarded({'speaker_id': 0, 'text': '此正文不能进入日志。'})
    stats.observe_forwarded({'speaker_id': 1, 'type': 'speaker_update'})
    stats.observe_provider({'sentences': [{'speaker_id': True}, {'speaker_id': 99}]})
    result = stats.snapshot()
    assert result['providerFinalIds'] == result['forwardedIds'] == [0, 1]
    assert result['unknownProviderEvents'] == 3 and result['speakerUpdates'] == 1
    assert '正文' not in json.dumps(result) and '凭据' not in json.dumps(result)
