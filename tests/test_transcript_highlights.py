import asyncio
import time
from types import SimpleNamespace
import pytest
from app.modules.meetings.services.transcript_highlights import (
    HighlightPipeline, HighlightMetrics, candidate, revision, validated,
)


def event(text='乙负责周五提交报告。', segment=1, final=True):
    return dict(segment=segment, text=text, is_final=final, start_ms=0, end_ms=1000)


class Model:
    def __init__(self, delay=0, invalid=False):
        self.delay, self.invalid, self.calls, self.closed = delay, invalid, [], False

    async def stream(self, role, prompt, payload):
        self.calls.append(payload)
        await asyncio.sleep(self.delay)
        yield {'type': 'done', 'value': {'highlights': [{'quote': '编造内容' if self.invalid else payload['sentence'],
            'kind': 'commitment', 'reason': '明确交付承诺'}]}}

    async def close(self):
        self.closed = True


async def pipeline(model=None, timeout=1, slots=None):
    sent = []
    async def send(value):
        sent.append(value)
    p = HighlightPipeline(SimpleNamespace(transcript_highlight_timeout_seconds=timeout), send,
                          slots or asyncio.Semaphore(2), model=model or Model())
    return p, sent


@pytest.mark.parametrize('text,expected', [
    ('人都到齐了吗，我们开始吧。', False), ('接下来讨论三个议题。', False),
    ('乙负责周五提交报告。', True), ('目前ERP同步超时可能导致延期。', True),
    ('转化率提升到35%。', True), ('如果验收通过，周四灰度发布。', True),
    ('先聊聊ERP接口。', False),
    ('付款需要在验收通过后的七个工作日内完成。', True),
    ('本月预算上限是二十万元，不允许超支。', True),
])
def test_rules(text, expected):
    assert candidate(text) is expected


def test_verified_utf16_offsets_reject_ambiguous_hallucinated_and_overlapping_quotes():
    text = '😀周五提交报告，报告需要复核。'
    values = [{'quote': '周五提交报告', 'kind': 'commitment', 'reason': '确定时间'},
              {'quote': '报告', 'kind': 'risk', 'reason': '重复有歧义'}]
    hits = validated(text, {'highlights': values})
    assert len(hits) == 1 and hits[0]['start'] == 2
    assert text.encode('utf-16-le')[hits[0]['start']*2:hits[0]['end']*2].decode('utf-16-le') == hits[0]['quote']
    assert validated(text, {'highlights': [{'quote': '从未说过', 'kind': 'risk', 'reason': '无依据'}]}) == []
    values[1]['quote'] = '周五提交'
    assert len(validated(text, {'highlights': values})) == 1
    with pytest.raises(ValueError):
        validated(text, {'highlights': values * 2})


async def test_final_only_dedup_small_context_and_display_ack():
    p, sent = await pipeline()
    try:
        p.submit(event(final=False))
        p.submit(event())
        p.submit(event())
        await p.queue.join()
        assert len(sent) == 1 and len(p.model.calls) == 1
        assert p.model.calls[0]['context'] == []
        assert sent[0]['revision'] == revision(event()['text'])
        p.displayed({'trace_id': sent[0]['trace_id'], 'render_ms': 16})
        p.displayed({'trace_id': sent[0]['trace_id'], 'render_ms': 16})
        assert p.metrics.counts['displayed'] == 1
        assert p.metrics.summary()['stages_ms']['flutter_receive_to_frame']['p95'] == 16
        assert len(p.pending) == 0
    finally:
        await p.close()


async def test_version_change_cancels_old_result_without_wrong_offset():
    p, sent = await pipeline(Model(delay=.02))
    try:
        p.submit(event())
        await asyncio.sleep(.005)
        p.submit(event('乙负责周六提交报告。'))
        await p.queue.join()
        assert [s['text'] for s in sent] == ['乙负责周六提交报告。']
        assert p.metrics.counts['superseded'] == 1
    finally:
        await p.close()


async def test_timeout_and_hallucination_never_publish_success():
    for model in (Model(delay=.1), Model(invalid=True)):
        p, sent = await pipeline(model, timeout=.01)
        try:
            p.submit(event())
            await p.queue.join()
            assert sent == []
            assert p.metrics.counts['timeout'] + p.metrics.counts['empty'] == 1
        finally:
            await p.close()
            assert model.closed


async def test_backlog_is_bounded_and_oldest_is_shed_without_blocking_submit():
    p, sent = await pipeline(Model(delay=.02))
    try:
        started = time.perf_counter()
        for i in range(100):
            p.submit(event(segment=i))
        assert p.queue.qsize() == 4
        assert time.perf_counter() - started < .1
        await p.queue.join()
        assert len(sent) == 4 and sent[-1]['segment'] == 99
        assert p.metrics.counts['shed'] == 96
    finally:
        await p.close()


async def test_model_concurrency_is_limited_across_meetings():
    slots = asyncio.Semaphore(1)
    first, _ = await pipeline(Model(delay=.04), slots=slots)
    second, _ = await pipeline(Model(), slots=slots)
    try:
        first.submit(event())
        await asyncio.sleep(.005)
        second.submit(event())
        await asyncio.gather(first.queue.join(), second.queue.join())
        assert second.metrics.summary()['stages_ms']['queue']['p50'] >= 20
    finally:
        await first.close()
        await second.close()


async def test_stale_work_is_discarded_and_context_is_bounded():
    p, sent = await pipeline()
    try:
        for i in range(5):
            p.submit(event('普通背景说明' * 100, segment=i))
        p.submit(event(segment=10), stable_at=time.monotonic()-5)
        await p.queue.join()
        assert p.metrics.counts['expired'] == 1 and p.model.calls == [] and sent == []
        p.submit(event(segment=11))
        await p.queue.join()
        payload = p.model.calls[0]
        assert set(payload) == {'sentence', 'context'}
        assert len(payload['context']) <= 2 and all(len(t) <= 160 for t in payload['context'])
    finally:
        await p.close()


async def test_cancel_does_not_wait_for_slow_model():
    p, sent = await pipeline(Model(delay=10))
    p.submit(event())
    await asyncio.sleep(.005)
    start = time.monotonic()
    await p.close()
    assert time.monotonic()-start < .2 and sent == [] and p.model.closed


async def test_metrics_require_authentication_and_are_account_scoped(client, app):
    assert (await client.get('/api/v1/meetings/asr/highlight-metrics')).status_code == 401
    response = await client.post('/api/v1/auth/register', json={
        'name': '标注测试', 'email': 'highlight@example.com', 'password': 'Test-Highlight-2026!'})
    token = response.json()['data']['access_token']
    foreign = HighlightMetrics()
    foreign.counts['published'] = 99
    app.state.highlight_metrics['another-user'] = foreign
    result = await client.get('/api/v1/meetings/asr/highlight-metrics', headers={'Authorization': 'Bearer ' + token})
    assert result.status_code == 200 and result.json()['data']['counts'] == {}


def test_metric_quantiles_are_real_samples_and_bounded():
    metrics = HighlightMetrics()
    for i in range(100):
        metrics.observe('model', i)
    assert metrics.summary()['stages_ms']['model'] == {'n': 100, 'p50': 49, 'p95': 94}
    for i in range(3000):
        metrics.observe('model', i)
    assert len(metrics.samples['model']) == 2048


async def test_production_relay_keeps_forwarding_asr_while_model_is_pending():
    import json
    from copy import deepcopy
    from app.modules.meetings.services.asr_stream_service import relay

    class Socket:
        def __init__(self):
            self.sent, self.receipts = [], asyncio.Queue()
        async def receive(self):
            return await self.receipts.get()
        async def send_json(self, value):
            self.sent.append(deepcopy(value))
            if value['type'] == 'transcript_highlights':
                await self.receipts.put({'type': 'websocket.receive', 'text': json.dumps({'type': 'highlight_displayed',
                    'trace_id': value['trace_id'], 'render_ms': 10})})

    class Upstream:
        def __init__(self):
            row = {'sentence': '乙负责周五提交报告。', 'sentence_id': 0,
                'sentence_type': 1, 'start_time': 0, 'end_time': 1000}
            self.frames = iter([{'sentences': [row]}, {'sentences': [row]},
                {'sentences': [{**row, 'sentence_id': 1, 'sentence': '我们继续下一个议题。'}]}, {'final': 1}])
        async def recv(self):
            await asyncio.sleep(.001)
            return json.dumps(next(self.frames))

    socket = Socket()
    p = HighlightPipeline(SimpleNamespace(transcript_highlight_timeout_seconds=1),
        socket.send_json, asyncio.Semaphore(2), model=Model(delay=.05))
    await relay(socket, Upstream(), {}, pipeline=p)
    assert [e['type'] for e in socket.sent] == ['transcript', 'transcript', 'transcript_highlights', 'done']
    assert len(p.model.calls) == 1
    assert p.metrics.counts['displayed'] == 1
    assert p.model.closed


def test_fast_role_is_non_thinking_qwen_and_has_short_output_budget():
    from app.integrations.hermes.model_policy import select_model
    settings = SimpleNamespace(qwen_workers_enabled=True, dashscope_api_key='synthetic',
                               qwen_model='qwen-flash', qwen_base_url='https://example.invalid/v1')
    route = select_model(settings, 'transcript_highlight')
    assert route.provider == 'qwen' and route.reasoning == {'enabled': False}
    assert route.max_tokens == 350
