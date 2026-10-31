"""旁路时序/隔离测试使用明确标记的测试特征，不代替真实声音评测。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import numpy as np
import pytest
from app.modules.voiceprints.realtime import AudioRingBuffer, RealtimeVoiceprintSession
from app.modules.voiceprints.engine import SampleRejected
from app.modules.meetings.services.asr_stream_service import relay
from tests.test_asr_speaker_handoff import Socket, Upstream, sentence


def test_ring_uses_exact_pcm_positions_and_refuses_evicted_or_future_audio():
    ring = AudioRingBuffer(seconds=1)
    first, second = bytes([1, 2]) * 16000, bytes([3, 4]) * 16000
    ring.append(first)
    assert ring.read(0, 1000) == first
    assert ring.read(0, 1001) is None
    ring.append(second)
    assert len(ring.data) == 32000
    assert ring.read(0, 1000) is None
    assert ring.read(1000, 1500) == second[:16000]


def session():
    async def send(event): events.append(event)
    events = []
    engine = SimpleNamespace(inspect_live=lambda _: ([1., 0.], {'testOnly': True}))
    jobs = SimpleNamespace(engine=engine, slots=asyncio.Semaphore(1), settings=SimpleNamespace(
        voiceprint_live_self_threshold=.65, voiceprint_live_other_threshold=.35))
    result = RealtimeVoiceprintSession(jobs, 'test-owner', 'test-scope', send)
    result.enabled = True
    result.template = np.array([1., 0.], dtype=np.float32)
    result.template_current = AsyncMock(return_value=True)
    return result, events


async def drain(instance):
    for _ in range(200):
        if instance.queue.empty():
            await asyncio.sleep(.03)
            return
        await asyncio.sleep(.005)
    raise AssertionError('test worker did not consume queue')


def event(segment, speaker, start, end, revision=0):
    return dict(segment=segment, speaker_id=speaker, start_ms=start, end_ms=end,
                speaker_revision=revision, is_final=True)


async def test_two_independent_windows_required_conflict_revokes_and_revision_discards_jobs():
    live, sent = session()
    live.ring.append(bytes(32000 * 20))  # 占位PCM只交给测试特征提取器。
    live.worker = asyncio.create_task(live.run())
    try:
        await live.observe(event(0, 0, 0, 4500))
        await drain(live)
        assert not sent
        await live.observe(event(0, 0, 0, 4500))
        assert live.queue.empty()
        await live.observe(event(1, 0, 4500, 9000))
        await drain(live)
        assert sent[-1]['identity'] == 'self'
        live.jobs.engine.inspect_live = lambda _: ([0., 1.], {'testOnly': True})
        await live.observe(event(2, 0, 9000, 13500))
        await drain(live)
        assert sent[-1]['identity'] == 'unknown'
        await live.observe(event(3, 0, 13500, 18000))
        await drain(live)
        assert sent[-1]['identity'] == 'other'
        await live.observe(event(3, 1, 13500, 18000, revision=1))
        assert sent[-1]['identity'] == 'unknown'
        assert live.speakers[0].generation == 1
        assert [e['identity_revision'] for e in sent] == [1, 2, 3, 4]
    finally:
        await live.close()


async def test_template_deleted_during_calculation_prevents_late_identity_and_clears_buffers():
    live, sent = session()
    live.template_current = AsyncMock(side_effect=[True, False])
    live.worker = asyncio.create_task(live.run())
    live.ring.append(bytes(32000 * 5))
    try:
        await live.observe(event(0, 0, 0, 4500))
        await drain(live)
        assert not live.enabled and live.template is None and not live.ring.data
        assert not any(e.get('identity') == 'self' for e in sent)
        assert sent[-1]['self_identification'] == 'template_changed'
    finally:
        await live.close()


async def test_overlap_resets_evidence_and_queue_has_hard_bound():
    live, sent = session()
    live.ring.append(bytes(32000 * 30))
    await live.observe(event(0, 0, 0, 5000))
    await live.observe(event(1, 1, 4500, 9000))
    assert live.speakers[0].generation == 1
    assert live.speakers[1].generation == 1
    for i in range(8):
        await live.observe(event(10 + i, 2, 10000 + i * 2000, 11900 + i * 2000))
    assert live.queue.qsize() <= 4
    await live.close()


async def test_relay_forwards_text_before_identity_and_identity_does_not_change_asr():
    socket = Socket()
    class TestIdentity:
        def diagnostic(self, stage, **fields): pass
        def feed(self, audio): pass
        async def observe_safely(self, event):
            assert socket.sent[-1] == event
        async def close(self): pass
    frames = [{'sentences': [sentence(0, 0, '正常原文')], 'code': 0}, {'final': 1}]
    await relay(socket, Upstream(frames), {}, identity=TestIdentity())
    assert socket.sent[0]['text'] == '正常原文'
    assert socket.sent[-1]['type'] == 'done'


async def test_bad_quality_does_not_become_nonself():
    live, sent = session()
    def reject(_): raise SampleRejected('too_noisy')
    live.jobs.engine.inspect_live = reject
    live.ring.append(bytes(32000 * 5))
    live.worker = asyncio.create_task(live.run())
    try:
        await live.observe(event(0, 0, 0, 4500))
        await drain(live)
        assert not sent
    finally:
        await live.close()


async def test_real_template_decryption_is_account_scoped_and_disable_is_detected(client, app, monkeypatch):
    from tests.test_voiceprints import configure
    from tests.test_auth import register, bearer
    from app.modules.voiceprints.models import VoiceprintProfile
    from app.core.database.mixins import now
    configure(app, monkeypatch)
    app.state.settings.tencent_asr_engine_model_type = '16k_zh_en_speaker_2.0'
    first = await register(client)
    second = await register(client, 'other-voice-test@example.com')
    jobs = app.state.voiceprints
    profile = VoiceprintProfile(owner_id=first['user']['id'], version=1, enabled=True,
        model_version=jobs.settings.voiceprint_model_version, key_id=jobs.settings.voiceprint_key_id,
        device={}, verified_at=now())
    profile.template = jobs.cipher().encrypt([1., 0.], **jobs.context(profile, 'profile:1'))
    async with app.state.database.sessions() as db:
        db.add(profile)
        await db.commit()
    async def send(event): pass
    owner = await RealtimeVoiceprintSession.create(jobs, first['user']['id'], 'scope', send)
    other = await RealtimeVoiceprintSession.create(jobs, second['user']['id'], 'scope', send)
    try:
        assert owner.enabled and not other.enabled and other.template is None
        assert await owner.template_current()
        response = await client.post('/api/v1/me/voiceprint/enabled', headers=bearer(first), json={'enabled': False})
        assert response.status_code == 200
        assert not await owner.template_current()
    finally:
        await owner.close()
        await other.close()

async def test_diagnostics_explain_rejection_and_matching_without_sensitive_payload(caplog, monkeypatch):
    import json
    import logging
    caplog.set_level(logging.INFO, logger='app.modules.voiceprints.realtime')
    logger = logging.getLogger('app.modules.voiceprints.realtime')
    monkeypatch.setattr(logger, 'handlers', [h for h in logger.handlers if h is not caplog.handler] + [caplog.handler])
    live, sent = session()
    live.ring.append(bytes(32000 * 20))
    live.worker = asyncio.create_task(live.run())
    def reject(_):
        raise SampleRejected('too_noisy')
    live.jobs.engine.inspect_live = reject
    try:
        await live.observe(event(0, 0, 0, 4500))
        await drain(live)
        assert not sent
        live.jobs.engine.inspect_live = lambda _: ([1., 0.], {})
        await live.observe(event(1, 0, 4500, 9000))
        await drain(live)
        await live.observe(event(2, 0, 9000, 13500))
        await drain(live)
        assert sent[-1]['identity'] == 'self'
        live.diagnostic('privacy_probe', owner='secret-owner', text='secret-transcript', vector=[9, 8], audio=b'secret-audio')
    finally:
        await live.close()
        await live.close()
    rows = [json.loads(r.message) for r in caplog.records if r.name == 'app.modules.voiceprints.realtime']
    assert any(r['stage'] == 'candidate_rejected' and r['reason'] == 'too_noisy' for r in rows)
    matches = [r for r in rows if r['stage'] == 'match_completed']
    assert [r['identity'] for r in matches] == ['unknown', 'self']
    assert all(r['elapsed_ms'] >= 0 and r['band'] == 'high' for r in matches)
    assert any(r['stage'] == 'identity_sent' for r in rows)
    assert sum(r['stage'] == 'session_closed' for r in rows) == 1
    assert not any(value in caplog.text for value in ['secret-owner', 'secret-transcript', 'secret-audio', 'test-owner', 'score', 'vector'])


async def test_diagnostics_report_accumulation_expiry_and_buffer_miss(caplog, monkeypatch):
    import json
    import logging
    caplog.set_level(logging.INFO, logger='app.modules.voiceprints.realtime')
    logger = logging.getLogger('app.modules.voiceprints.realtime')
    monkeypatch.setattr(logger, 'handlers', [h for h in logger.handlers if h is not caplog.handler] + [caplog.handler])
    live, _ = session()
    live.feed(bytes(32000 * 2))
    await live.observe(event(0, 0, 0, 1500))
    live.feed(bytes(32000 * 31))
    await live.observe(event(1, 0, 2000, 3000))
    await live.close()
    rows = [json.loads(r.message) for r in caplog.records if r.name == 'app.modules.voiceprints.realtime']
    assert any(r['stage'] == 'audio_accumulated' and r['pending_ms'] == 1340 for r in rows)
    assert any(r['stage'] == 'pending_expired' for r in rows)
    assert any(r['stage'] == 'segment_skipped' and r['reason'] == 'audio_outside_buffer' for r in rows)

async def test_long_segment_consumes_nonoverlapping_windows_and_preserves_exact_remainder():
    live, sent = session()
    raw = bytes(range(256)) * 2000
    live.feed(raw)
    # 去掉两端80ms后，恰好10秒：2个4秒窗，2秒余量。
    await live.observe(event(0, 0, 0, 10160))
    first = live.queue.get_nowait()
    second = live.queue.get_nowait()
    assert first[2] == raw[2560:130560]
    assert second[2] == raw[130560:258560]
    state = live.speakers[0]
    assert bytes(state.pending) == raw[258560:322560]
    assert sum(n for n, _ in state.pending_sources) == len(state.pending) == 64000
    assert first[3] == second[3] == frozenset({0})
    # 后续2秒补成新窗，逐字节验证没有重用或丢弃尾部。
    await live.observe(event(1, 0, 10160, 12320))
    third = live.queue.get_nowait()
    assert third[2] == raw[258560:322560] + raw[327680:391680]
    assert third[3] == frozenset({0, 1})
    assert not state.pending and not state.pending_sources
    await live.close()


async def test_multiple_windows_from_one_segment_cannot_bind_identity():
    live, sent = session()
    live.feed(bytes(32000 * 25))
    live.worker = asyncio.create_task(live.run())
    try:
        await live.observe(event(0, 0, 0, 8160))
        await asyncio.sleep(.2)
        assert not sent
        assert len(live.speakers[0].scores) == 1
        await live.observe(event(1, 0, 8160, 16320))
        await asyncio.sleep(.2)
        assert sent[-1]['identity'] == 'self'
        assert not any(e['identity'] == 'unknown' for e in sent)
    finally:
        await live.close()

async def test_queue_overload_consumes_whole_windows_but_keeps_partial_tail():
    live, _ = session()
    live.feed(bytes(32000 * 25))
    # 24秒可用语音产生6窗；队列最多4窗，过载丢整窗仍保留余量。
    await live.observe(event(0, 0, 0, 24320))
    assert live.queue.qsize() == 4
    assert len(live.speakers[0].pending) == 160 * 32
    assert sum(n for n, _ in live.speakers[0].pending_sources) == 160 * 32
    assert live.diagnostic_counts['candidate_skipped'] == 2
    live.feed(bytes(32000 * 30))
    assert not live.speakers[0].pending and not live.speakers[0].pending_sources
    await live.close()
