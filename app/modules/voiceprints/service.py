"""Enrollment jobs keep raw audio in bounded memory and persist only encrypted features."""
import asyncio
import base64
import logging
import json
import time
import uuid
from datetime import timedelta
import numpy as np
from fastapi import HTTPException
from sqlalchemy import select, delete
from app.core.database.mixins import now
from app.modules.identity.models.identity import User
from app.modules.voiceprints.crypto import TemplateCipher
from app.modules.voiceprints.engine import VoiceprintEngine, SampleRejected, unit
from app.modules.voiceprints.models import VoiceprintProfile as Profile, VoiceprintEnrollment as Enrollment, VoiceprintAudit as Audit

CONSENT_VERSION = 'voiceprint-local-2026-10-30-v1'
PROMPTS = [
    '今天的会议开始前，我会先确认讨论目标，认真听取大家的意见，并在发言时清楚说明自己的想法。对于需要后续处理的事项，我们会逐一记录。',
    '如果项目安排出现变化，我会及时与同事沟通，核对相关资料和时间。遇到暂时无法确定的问题，先保留疑问，等获得充分信息之后再做判断。',
    '接下来的工作需要团队共同配合。我们可以先整理已有进展，再讨论具体分工和交付要求，并在会议结束前检查有没有遗漏的重要事项。',
    '这是一次新的声音检查。我正在使用平常的语速说话，希望会议中的发言能够被正确记录。每个人的意见都很重要，相关行动也应有清楚的依据。',
]


class VoiceprintJobs:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        self.engine = VoiceprintEngine(settings)
        self.tasks = set()
        self.owners = set()
        self.slots = asyncio.Semaphore(1)
        self.cleaner = None

    @property
    def available(self):
        if not self.settings.voiceprint_key or not self.engine.ready:
            return False
        try:
            self.cipher()
            return True
        except ValueError:
            return False

    def cipher(self):
        if not self.settings.voiceprint_key:
            raise HTTPException(503, 'voiceprint_unavailable')
        return TemplateCipher(self.settings.voiceprint_key.get_secret_value())

    def audit(self, db, owner, action):
        db.add(Audit(id=str(uuid.uuid4()), owner_id=owner, action=action, consent_version=CONSENT_VERSION))

    async def lock_owner(self, db, owner):
        user = await db.scalar(select(User).where(User.id == owner).with_for_update())
        if not user or not user.is_active:
            raise HTTPException(401, 'invalid_or_expired_token')

    async def enrollment(self, db, owner, enrollment_id):
        row = await db.scalar(select(Enrollment).where(Enrollment.id == enrollment_id, Enrollment.owner_id == owner).with_for_update())
        if not row or row.expires_at <= now():
            raise HTTPException(404, 'voiceprint_session_expired')
        if row.model_version != self.settings.voiceprint_model_version or row.key_id != self.settings.voiceprint_key_id:
            raise HTTPException(409, 'voiceprint_configuration_changed')
        return row

    def context(self, row, record):
        return dict(owner=row.owner_id, record=record, model=row.model_version, key_id=row.key_id)

    def output_enrollment(self, row):
        return dict(id=row.id, status=row.status, expiresAt=row.expires_at.isoformat(), attempts=row.attempts,
                    samples={key: {k: v for k, v in value.items() if k != 'encrypted'} for key, value in row.samples.items()},
                    prompts=PROMPTS, consentVersion=CONSENT_VERSION)

    async def status(self, db, owner):
        profile = await db.get(Profile, owner)
        row = await db.scalar(select(Enrollment).where(Enrollment.owner_id == owner, Enrollment.expires_at > now()))
        return dict(available=self.available, registered=bool(profile and profile.template),
                    enabled=bool(profile and profile.enabled), version=profile.version if profile else 0,
                    verifiedAt=profile.verified_at.isoformat() if profile and profile.verified_at else None,
                    device=profile.device if profile else {}, consentVersion=CONSENT_VERSION,
                    realtimeIdentificationAvailable=bool(self.available and self.settings.voiceprint_live_enabled and
                        self.settings.tencent_asr_engine_model_type == '16k_zh_en_speaker_2.0'),
                    enrollment=self.output_enrollment(row) if row else None)

    async def create(self, db, owner, consent_version):
        if consent_version != CONSENT_VERSION:
            raise HTTPException(422, 'voiceprint_consent_required')
        if not self.available:
            raise HTTPException(503, 'voiceprint_unavailable')
        await self.lock_owner(db, owner)
        profile = await db.get(Profile, owner)
        if not profile:
            profile = Profile(owner_id=owner, version=0, enabled=False, device={})
            db.add(profile)
        # Repeated creates resume the same session. Restart requires explicit cancellation.
        existing = await db.scalar(select(Enrollment).where(Enrollment.owner_id == owner, Enrollment.expires_at > now()))
        if existing:
            return self.output_enrollment(existing)
        await db.execute(delete(Enrollment).where(Enrollment.owner_id == owner))
        row = Enrollment(id=str(uuid.uuid4()), owner_id=owner, base_version=profile.version,
                         status='recording', samples={}, model_version=self.settings.voiceprint_model_version,
                         key_id=self.settings.voiceprint_key_id, attempts=0, expires_at=now() + timedelta(hours=24))
        db.add(row)
        self.audit(db, owner, 'consented')
        await db.commit()
        return self.output_enrollment(row)

    async def submit(self, db, owner, enrollment_id, slot, raw, device):
        if not self.available:
            raise HTTPException(503, 'voiceprint_unavailable')
        if owner in self.owners or len(self.tasks) >= 4:
            raise HTTPException(429, 'voiceprint_busy')
        await self.lock_owner(db, owner)
        row = await self.enrollment(db, owner, enrollment_id)
        if owner in self.owners or len(self.tasks) >= 4:
            raise HTTPException(429, 'voiceprint_busy')
        if slot == 'verification':
            if row.status != 'ready' or not row.candidate or row.attempts >= 3:
                raise HTTPException(409, 'voiceprint_not_ready')
        elif row.status not in ('recording', 'ready'):
            raise HTTPException(409, 'voiceprint_not_ready')
        generation = str(uuid.uuid4())
        row.samples = {**row.samples, slot: dict(status='processing', generation=generation, device=device)}
        if slot != 'verification':
            row.candidate, row.status = None, 'recording'
        await db.commit()
        self.owners.add(owner)
        task = asyncio.create_task(self.process(owner, enrollment_id, slot, generation, raw))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.output_enrollment(row)

    async def process(self, owner, enrollment_id, slot, generation, raw):
        try:
            error, vector, quality = None, None, {}
            try:
                async with self.slots:
                    vector, quality = await asyncio.to_thread(self.engine.inspect, raw)
            except SampleRejected as rejected:
                error = str(rejected)
            except Exception as failure:
                # No exception payloads: model/transport failures can expose private paths.
                logging.getLogger(__name__).warning('voiceprint_processing_failed', extra={'error_type': type(failure).__name__})
                error = 'processing_failed'
            finally:
                raw = None
            async with self.database.sessions() as db:
                user = await db.scalar(select(User).where(User.id == owner).with_for_update())
                row = await db.get(Enrollment, enrollment_id, with_for_update=True)
                if not user or not row or row.owner_id != owner or row.expires_at <= now():
                    return
                sample = row.samples.get(slot, {})
                if sample.get('generation') != generation:
                    return
                sample = {**sample, 'status': 'rejected' if error else 'accepted', 'reason': error, 'quality': quality}
                if not error and slot != 'verification':
                    encrypted = self.cipher().encrypt(vector, **self.context(row, row.id + ':' + slot))
                    sample['encrypted'] = base64.b64encode(encrypted).decode()
                if not error and slot == 'verification':
                    template = self.cipher().decrypt(row.candidate, **self.context(row, row.id + ':candidate'))
                    if float(np.dot(unit(template), unit(vector))) < self.settings.voiceprint_verification_threshold:
                        row.attempts += 1
                        sample.update(status='rejected', reason='verification_mismatch')
                    else:
                        profile = await db.get(Profile, owner, with_for_update=True)
                        if not profile or profile.version != row.base_version:
                            sample.update(status='rejected', reason='version_conflict')
                        else:
                            profile.version += 1
                            profile.model_version, profile.key_id = row.model_version, row.key_id
                            profile.template = self.cipher().encrypt(template, **self.context(row, f'profile:{profile.version}'))
                            profile.enabled, profile.verified_at = True, now()
                            profile.device = sample.get('device', {})
                            self.audit(db, owner, 'verified_and_enabled')
                            # Destroy temporary features immediately after successful activation.
                            await db.delete(row)
                            await db.commit()
                            return
                row.samples = {**row.samples, slot: sample}
                await db.commit()
        finally:
            self.owners.discard(owner)

    async def build(self, db, owner, enrollment_id):
        await self.lock_owner(db, owner)
        row = await self.enrollment(db, owner, enrollment_id)
        if row.status == 'ready':
            return self.output_enrollment(row)
        if any(row.samples.get(str(i), {}).get('status') != 'accepted' for i in range(3)):
            raise HTTPException(409, 'voiceprint_not_ready')
        cipher = self.cipher()
        vectors = [unit(cipher.decrypt(base64.b64decode(row.samples[str(i)]['encrypted']), **self.context(row, row.id + ':' + str(i)))) for i in range(3)]
        if min(float(np.dot(vectors[i], vectors[j])) for i in range(3) for j in range(i)) < self.settings.voiceprint_consistency_threshold:
            # Cannot know which sample is the true user. Ask for re-registration instead of guessing.
            row.samples, row.candidate, row.status = {}, None, 'recording'
            await db.commit()
            raise HTTPException(422, 'voiceprint_samples_inconsistent')
        row.candidate = cipher.encrypt(unit(np.mean(vectors, axis=0)).tolist(), **self.context(row, row.id + ':candidate'))
        row.status = 'ready'
        await db.commit()
        return self.output_enrollment(row)

    async def cancel(self, db, owner, enrollment_id=None, *, enabled=None, erase=False):
        await self.lock_owner(db, owner)
        if enrollment_id:
            row = await self.enrollment(db, owner, enrollment_id)
            await db.delete(row)
        else:
            profile = await db.get(Profile, owner, with_for_update=True)
            if enabled and (not profile or not profile.template):
                raise HTTPException(409, 'voiceprint_not_ready')
            if profile:
                if erase:
                    profile.version += 1
                profile.enabled = bool(enabled) and not erase
                if erase:
                    profile.template, profile.verified_at, profile.device = None, None, {}
            await db.execute(delete(Enrollment).where(Enrollment.owner_id == owner))
        self.audit(db, owner, 'deleted' if erase else 'cancelled' if enrollment_id else 'enabled' if enabled else 'disabled')
        await db.commit()

    def start(self):
        self.cleaner = asyncio.create_task(self.cleanup())

    async def prewarm(self):
        if not self.settings.voiceprint_prewarm_enabled or not self.available:
            return
        started = time.monotonic()
        result = dict(kind='voiceprint_prewarm')
        try:
            # 在开始接收会议之前等待预热；放在线程执行，不阻塞事件循环。
            async with self.slots:
                await asyncio.to_thread(self.engine.prewarm)
            result['outcome'] = 'ready'
        except Exception as error:
            result.update(outcome='failed', error_type=type(error).__name__)
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        logger = logging.getLogger('yanshu.voiceprint.prewarm')
        if not logger.handlers:
            logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.info(json.dumps(result))

    async def cleanup(self):
        while True:
            try:
                async with self.database.sessions() as db:
                    await db.execute(delete(Enrollment).where(Enrollment.expires_at <= now()))
                    rows = (await db.scalars(select(Enrollment).with_for_update())).all()
                    for row in rows:
                        if row.owner_id not in self.owners:
                            # Audio was intentionally not persisted; interrupted work needs a re-recording.
                            row.samples = {k: {**v, 'status': 'rejected', 'reason': 'processing_interrupted'}
                                           if v.get('status') == 'processing' else v for k, v in row.samples.items()}
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger(__name__).warning('voiceprint_cleanup_failed', extra={'error_type': type(error).__name__})
            await asyncio.sleep(60)

    async def close(self):
        if self.cleaner:
            self.cleaner.cancel()
            await asyncio.gather(self.cleaner, return_exceptions=True)
        # Drain at most one local inference before closing DB; raw buffers never become files.
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
