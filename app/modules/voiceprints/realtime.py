"""只存在于本次ASR连接的本人识别旁路，不写会议、身份表或声纹向量。"""
import asyncio
import logging
import json
import time
import uuid
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
import numpy as np
from app.modules.voiceprints.engine import SampleRejected, unit
from app.modules.voiceprints.models import VoiceprintProfile


class AudioRingBuffer:
    """PCM16/16k/mono：用累计采样字节定位，绝不用文字到达的墙上时间。"""
    def __init__(self, seconds=30):
        self.capacity = seconds * 32000
        self.data = bytearray()
        self.end = 0

    def append(self, pcm):
        self.data.extend(pcm)
        self.end += len(pcm)
        if len(self.data) > self.capacity:
            del self.data[:len(self.data) - self.capacity]

    def read(self, start_ms, end_ms):
        first, last = start_ms * 32, end_ms * 32
        base = self.end - len(self.data)
        if first < base or last > self.end or first < 0 or last <= first:
            return None
        return bytes(self.data[first - base:last - base])

    def clear(self):
        self.data.clear()


@dataclass
class SpeakerEvidence:
    generation: int = 0
    revision: int = 0
    identity: str = 'unknown'
    last_end: int = 0
    pending: bytearray = field(default_factory=bytearray)
    scores: deque = field(default_factory=lambda: deque(maxlen=3))
    pending_sources: deque = field(default_factory=deque)


class RealtimeVoiceprintSession:
    def __init__(self, jobs, owner, scope, send):
        self.jobs, self.owner, self.scope, self.send = jobs, owner, scope, send
        self.session_id = uuid.uuid4().hex
        self.enabled, self.closed = False, False
        self.reason = 'not_registered'
        self.template, self.template_version = None, None
        self.ring = AudioRingBuffer()
        self.speakers = {}
        self.segments = OrderedDict()
        self.queue = asyncio.Queue(maxsize=4)
        self.worker = self.watcher = None
        self.diagnostic_counts = Counter()
        self.started_at = time.monotonic()
        self.persistence = None
        self.pending_persistence = {}

    def diagnostic(self, stage, **fields):
        """仅输出内部调用的固定元数据；禁止传入音频、向量、原文或账户标识。"""
        allowed = {'speaker', 'reason', 'pending_ms', 'duration_ms', 'elapsed_ms',
                   'evidence_count', 'band', 'identity', 'revision', 'error_type',
                   'counts', 'audio_ms', 'queue_size', 'speech_ms', 'window_ms',
                   'persisted', 'meeting_revision'}
        self.diagnostic_counts[stage] += 1
        payload = dict(kind='live_identity', session=self.session_id, stage=stage)
        payload.update({key: value for key, value in fields.items() if key in allowed})
        logger = logging.getLogger(__name__)
        # 独立 INFO 输出保证模型尚未加载时也可见，不依赖第三方库配置 root logger。
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.info(json.dumps(payload, ensure_ascii=True))

    @classmethod
    async def create(cls, jobs, owner, scope, send):
        instance = cls(jobs, owner, scope, send)
        try:
            if jobs.settings.tencent_asr_engine_model_type != '16k_zh_en_speaker_2.0':
                instance.reason = 'speaker_separation_unavailable'
                return instance
            if not jobs.settings.voiceprint_live_enabled or not jobs.available:
                instance.reason = 'unavailable'
                return instance
            async with jobs.database.sessions() as db:
                profile = await db.get(VoiceprintProfile, owner)
                if not profile or not profile.template or not profile.enabled:
                    return instance
                if profile.model_version != jobs.settings.voiceprint_model_version or profile.key_id != jobs.settings.voiceprint_key_id:
                    instance.reason = 'incompatible_template'
                    return instance
                instance.template = unit(jobs.cipher().decrypt(profile.template,
                    **jobs.context(profile, f'profile:{profile.version}')))
                instance.template_version = profile.version
            instance.enabled, instance.reason = True, 'enabled'
        except Exception as error:
            instance.reason = 'unavailable'
            logging.getLogger(__name__).warning('live_identity_setup_failed', extra={'error_type': type(error).__name__})
        return instance

    def ready_fields(self):
        return dict(identity_session_id=self.session_id,
                    self_identification='enabled' if self.enabled else self.reason)

    def start(self):
        self.diagnostic('session_started', reason=self.reason)
        if self.enabled:
            self.worker = asyncio.create_task(self.run())
            self.watcher = asyncio.create_task(self.watch())

    def feed(self, pcm):
        if self.enabled and not self.closed:
            self.ring.append(pcm)
            for speaker, state in self.speakers.items():
                if state.pending and self.ring.end // 32 - state.last_end > 30000:
                    self.diagnostic('pending_expired', speaker=speaker, pending_ms=len(state.pending) // 32)
                    state.pending.clear()
                    state.pending_sources.clear()

    async def publish(self, speaker, identity, *, force=False):
        if self.closed:
            return
        state = self.speakers.setdefault(speaker, SpeakerEvidence())
        if identity == state.identity and not force:
            return
        state.identity = identity
        state.revision += 1
        persisted, meeting_revision = False, 0
        if self.persistence:
            try:
                meeting_revision = await self.persistence.save(self.scope, speaker, identity)
                persisted = True
                self.pending_persistence.pop(speaker, None)
            except Exception as error:
                self.pending_persistence[speaker] = identity
                self.diagnostic('identity_save_failed', error_type=type(error).__name__)
        await self.send(dict(type='speaker_identity_update', identity_session_id=self.session_id,
            speaker_scope=self.scope, speaker_id=speaker, identity=identity,
            identity_revision=state.revision, persisted=persisted,
            meeting_identity_revision=meeting_revision))
        self.diagnostic('identity_sent', speaker=speaker, identity=identity, revision=state.revision,
                        persisted=persisted, meeting_revision=meeting_revision)

    async def reset(self, speaker):
        if speaker is None:
            return
        state = self.speakers.setdefault(speaker, SpeakerEvidence())
        self.diagnostic('evidence_reset', speaker=speaker)
        state.generation += 1
        state.pending.clear()
        state.pending_sources.clear()
        state.scores.clear()
        state.last_end = 0
        await self.publish(speaker, 'unknown')

    async def observe(self, event):
        if not self.enabled or self.closed or not event.get('is_final'):
            return
        segment, speaker = event['segment'], event.get('speaker_id')
        if speaker is not None and (type(speaker) is not int or not 0 <= speaker <= 9):
            return
        revision = event.get('speaker_revision', 0)
        previous = self.segments.get(segment)
        if previous:
            if revision <= previous[1]:
                return  # 同一最终片段只能提供一次证据。
            await self.reset(previous[0])
            if speaker != previous[0]:
                await self.reset(speaker)
        elif event.get('type') == 'speaker_update':
            # 较早片段的归属已不在有界索引内时，保守撤销本连接全部判断。
            for known in list(self.speakers):
                await self.reset(known)
        self.segments[segment] = (speaker, revision, event['start_ms'], event['end_ms'])
        self.segments.move_to_end(segment)
        if len(self.segments) > 2048:
            self.segments.popitem(last=False)
        if speaker is None:
            self.diagnostic('segment_skipped', reason='unknown_speaker')
            return
        start, end = event['start_ms'], event['end_ms']
        # 已知跨说话人的重叠区间不参与匹配；其他混音仍只能保守检查。
        overlaps = {other for key, (other, _, a, b) in self.segments.items()
                    if key != segment and other is not None and other != speaker and start < b and end > a}
        if overlaps:
            self.diagnostic('segment_skipped', speaker=speaker, reason='overlap')
            for other in overlaps | {speaker}:
                await self.reset(other)
            return
        state = self.speakers.setdefault(speaker, SpeakerEvidence())
        start = max(start + 80, state.last_end)
        end -= 80  # 略去边界，减少换人交界处混入的声音。
        pcm = self.ring.read(start, end)
        if pcm is None:
            self.diagnostic('segment_skipped', speaker=speaker, reason='audio_outside_buffer', duration_ms=end-start, audio_ms=self.ring.end // 32)
            state.pending.clear()
            state.pending_sources.clear()
            return
        state.last_end = end
        state.pending.extend(pcm)
        state.pending_sources.append((len(pcm), segment))
        self.diagnostic('audio_accumulated', speaker=speaker, pending_ms=len(state.pending) // 32)
        # 每个采样只消费一次；不足4秒的尾部保留，供同一说话人的后续片段补齐。
        # 队列仍有界：过载时明确丢弃整窗并计数，绝不阻塞ASR或无限积压。
        window_bytes = 4 * 32000
        while len(state.pending) >= window_bytes:
            candidate = bytes(state.pending[:window_bytes])
            del state.pending[:window_bytes]
            remaining, sources = window_bytes, Counter()
            while remaining:
                length, source = state.pending_sources.popleft()
                take = min(length, remaining)
                sources[source] += take
                remaining -= take
                if take < length:
                    state.pending_sources.appendleft((length - take, source))
            if not self.queue.full():
                self.queue.put_nowait((speaker, state.generation, candidate, frozenset(sources), sources.most_common(1)[0][0]))
                self.diagnostic('candidate_queued', speaker=speaker, duration_ms=4000, queue_size=self.queue.qsize())
            else:
                self.diagnostic('candidate_skipped', speaker=speaker, reason='queue_full')
        self.diagnostic('remainder_retained', speaker=speaker, pending_ms=len(state.pending) // 32)

    async def observe_safely(self, event):
        try:
            await self.observe(event)
        except Exception as error:
            self.diagnostic('observe_failed', error_type=type(error).__name__)
            try:
                await self.disable('unavailable')
            except Exception:
                self.enabled = False

    async def template_current(self):
        async with self.jobs.database.sessions() as db:
            profile = await db.get(VoiceprintProfile, self.owner)
            return bool(profile and profile.enabled and profile.template and
                        profile.version == self.template_version and
                        profile.model_version == self.jobs.settings.voiceprint_model_version and
                        profile.key_id == self.jobs.settings.voiceprint_key_id)

    async def disable(self, reason):
        self.diagnostic('disabled', reason=reason)
        self.enabled, self.reason = False, reason
        self.template = None
        self.ring.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
        for speaker in list(self.speakers):
            await self.reset(speaker)
        if not self.closed:
            await self.send(dict(type='speaker_identity_status', **self.ready_fields()))

    async def watch(self):
        try:
            while self.enabled and not self.closed:
                await asyncio.sleep(3)
                for speaker, identity in list(self.pending_persistence.items()):
                    await self.publish(speaker, identity, force=True)
                if not await self.template_current():
                    await self.disable('template_changed')
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.disable('unavailable')

    async def run(self):
        while self.enabled and not self.closed:
            speaker, generation, pcm, sources, primary_source = await self.queue.get()
            calculation_started = time.monotonic()
            self.diagnostic('candidate_started', speaker=speaker)
            try:
                state = self.speakers[speaker]
                if generation != state.generation or not self.enabled:
                    continue
                if not await self.template_current():
                    await self.disable('template_changed')
                    return
                # 复用注册任务的全局模型配额；忙时丢弃候选，不积压实时转写。
                try:
                    await asyncio.wait_for(self.jobs.slots.acquire(), timeout=.05)
                except TimeoutError:
                    self.diagnostic('candidate_skipped', speaker=speaker, reason='model_capacity_busy')
                    continue
                try:
                    vector, _ = await asyncio.to_thread(self.jobs.engine.inspect_live, pcm)
                finally:
                    self.jobs.slots.release()
                if self.closed or not self.enabled or generation != state.generation:
                    continue
                if not await self.template_current():
                    await self.disable('template_changed')
                    return
                if self.closed or not self.enabled or generation != state.generation:
                    continue
                score = float(np.dot(self.template, unit(vector)))
                if state.scores and state.scores[-1][1] == primary_source:
                    state.scores[-1] = (score, primary_source)
                else:
                    state.scores.append((score, primary_source))
                recent = list(state.scores)[-2:]
                high, low = self.jobs.settings.voiceprint_live_self_threshold, self.jobs.settings.voiceprint_live_other_threshold
                # 按窗口贡献最多采样的来源片段计证据，避免少量上段余量阻止下一段确认。
                # 同一主来源的多个窗只保留最新一次结果，不能充当两次独立发言。
                independent = len(recent) == 2 and recent[0][1] != recent[1][1]
                identity = 'self' if independent and all(s >= high for s, _ in recent) else \
                           'other' if independent and all(s <= low for s, _ in recent) else 'unknown'
                # 不确定不是反证：已确认身份只因明确反向证据或归属重置而撤回。
                # 新身份仍需两段独立高/低匹配，噪声拒绝不计入任何身份判断。
                if ((state.identity == 'self' and score > low) or
                        (state.identity == 'other' and score < high)):
                    identity = state.identity
                self.diagnostic('match_completed', speaker=speaker, elapsed_ms=round((time.monotonic()-calculation_started)*1000), evidence_count=len(state.scores), band='high' if score >= high else 'low' if score <= low else 'uncertain', identity=identity)
                await self.publish(speaker, identity)
            except SampleRejected as error:
                reason = str(error)
                safe_reason = reason if reason in {'insufficient_speech', 'clipping', 'too_quiet', 'too_noisy', 'engine_busy', 'inconsistent_voice', 'invalid_embedding', 'invalid_audio'} else 'quality_rejected'
                self.diagnostic('candidate_rejected', speaker=speaker, reason=safe_reason,
                    speech_ms=error.speech_ms, window_ms=error.window_ms,
                    elapsed_ms=round((time.monotonic()-calculation_started)*1000))
                pass  # 质量不足不是ASR失败，也不是“非本人”的证据。
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.diagnostic('candidate_failed', speaker=speaker, error_type=type(error).__name__)
                await self.disable('unavailable')
            finally:
                pcm = None

    async def close(self):
        if self.closed:
            return
        # 刷尾前最后重试；关闭连接本身不撤销已保存的身份。
        for speaker, identity in list(self.pending_persistence.items()):
            try:
                await self.persistence.save(self.scope, speaker, identity)
                self.pending_persistence.pop(speaker, None)
            except Exception as error:
                self.diagnostic('identity_save_failed', error_type=type(error).__name__)
        self.diagnostic('session_closed', elapsed_ms=round((time.monotonic()-self.started_at)*1000), audio_ms=self.ring.end // 32, counts=dict(self.diagnostic_counts))
        self.closed, self.enabled = True, False
        for task in (self.worker, self.watcher):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.worker, self.watcher) if t), return_exceptions=True)
        self.template = None
        self.ring.clear()
        self.speakers.clear()
        self.segments.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
