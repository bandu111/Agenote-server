"""Security/lifecycle tests use explicit test vectors, never claim real voice accuracy."""
import asyncio
import base64
import io
import os
import wave
import numpy as np
import pytest
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr
from sqlalchemy import select
from app.modules.voiceprints.crypto import TemplateCipher
from app.modules.voiceprints.engine import decode_wav, SampleRejected, VoiceprintEngine
from app.modules.voiceprints.models import VoiceprintProfile, VoiceprintEnrollment
from app.modules.voiceprints.service import CONSENT_VERSION
from tests.test_auth import register, bearer


def test_cipher_binds_owner_record_model_and_detects_tampering():
    cipher = TemplateCipher(base64.b64encode(os.urandom(32)).decode())
    ctx = dict(owner='alice', record='profile:1', model='test', key_id='v1')
    first = cipher.encrypt([1., 0.], **ctx)
    second = cipher.encrypt([1., 0.], **ctx)
    assert first != second
    assert cipher.decrypt(first, **ctx) == [1., 0.]
    for changed in ({'owner': 'bob'}, {'record': 'profile:2'}, {'model': 'other'}, {'key_id': 'v2'}):
        with pytest.raises(InvalidTag):
            cipher.decrypt(first, **{**ctx, **changed})
    with pytest.raises(InvalidTag):
        cipher.decrypt(first[:-1] + bytes([first[-1] ^ 1]), **ctx)


def wav_silence():
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(bytes(16000 * 2 * 10))
    return output.getvalue()


def test_actual_quality_gate_rejects_silence_before_inference(app):
    with pytest.raises(SampleRejected, match='insufficient_speech'):
        VoiceprintEngine(app.state.settings).inspect(wav_silence())
    with pytest.raises(SampleRejected, match='invalid_audio'):
        decode_wav(b'not a wav')


def configure(app, monkeypatch):
    app.state.settings.voiceprint_key = SecretStr(base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setattr(VoiceprintEngine, 'ready', property(lambda _: True))
    # Explicitly mocked vectors exercise transactions only, not speaker recognition.
    monkeypatch.setattr(app.state.voiceprints.engine, 'inspect', lambda _: ([1., 0., 0.], {'testOnly': True}))


async def wait_jobs(app):
    if app.state.voiceprints.tasks:
        await asyncio.gather(*app.state.voiceprints.tasks)


async def create_enrollment(client, headers):
    response = await client.post('/api/v1/me/voiceprint/enrollments', headers=headers, json={'consentVersion': CONSENT_VERSION})
    assert response.status_code == 200, response.text
    return response.json()['data']['id']


async def upload(client, headers, enrollment, slot):
    return await client.post(f'/api/v1/me/voiceprint/enrollments/{enrollment}/samples/{slot}',
        headers={**headers, 'Content-Type': 'audio/wav', 'X-Audio-Device-Type': 'builtInMic'}, content=wav_silence())


async def test_consent_isolation_verification_encryption_and_delete(client, app, monkeypatch):
    configure(app, monkeypatch)
    a, b = bearer(await register(client)), bearer(await register(client, 'other@example.com'))
    bad = await client.post('/api/v1/me/voiceprint/enrollments', headers=a, json={'consentVersion': 'unknown'})
    assert bad.status_code == 422
    enrollment = await create_enrollment(client, a)
    assert await create_enrollment(client, a) == enrollment
    assert (await client.post(f'/api/v1/me/voiceprint/enrollments/{enrollment}/build', headers=b)).status_code == 404
    assert (await client.post(f'/api/v1/me/voiceprint/enrollments/{enrollment}/build', headers=a)).status_code == 409
    for slot in ('0', '1', '2'):
        assert (await upload(client, a, enrollment, slot)).status_code == 200
        await wait_jobs(app)
    status = (await client.get('/api/v1/me/voiceprint', headers=a)).json()['data']
    assert 'encrypted' not in str(status)
    assert not status['registered']
    assert (await client.post(f'/api/v1/me/voiceprint/enrollments/{enrollment}/build', headers=a)).status_code == 200
    assert (await upload(client, a, enrollment, 'verification')).status_code == 200
    await wait_jobs(app)
    status = (await client.get('/api/v1/me/voiceprint', headers=a)).json()['data']
    assert status['registered'] and status['enabled'] and status['enrollment'] is None
    async with app.state.database.sessions() as db:
        profile = await db.scalar(select(VoiceprintProfile).where(VoiceprintProfile.template.is_not(None)))
        owner, original = profile.owner_id, profile.template
        assert app.state.voiceprints.cipher().decrypt(original, **app.state.voiceprints.context(profile, 'profile:1')) == [1., 0., 0.]
    await client.post('/api/v1/me/voiceprint/enabled', headers=a, json={'enabled': False})
    async with app.state.database.sessions() as db:
        profile = await db.get(VoiceprintProfile, owner)
        assert not profile.enabled and profile.template == original and profile.version == 1
    await client.post('/api/v1/me/voiceprint/enabled', headers=a, json={'enabled': True})
    await client.delete('/api/v1/me/voiceprint', headers=a)
    async with app.state.database.sessions() as db:
        profile = await db.get(VoiceprintProfile, owner)
        assert profile.template is None and not profile.enabled
    assert not (await client.get('/api/v1/me/voiceprint', headers=b)).json()['data']['registered']


async def test_delete_during_inference_cannot_resurrect_features(client, app, monkeypatch):
    configure(app, monkeypatch)
    headers = bearer(await register(client))
    enrollment = await create_enrollment(client, headers)
    await app.state.voiceprints.slots.acquire()
    try:
        assert (await upload(client, headers, enrollment, '0')).status_code == 200
        assert (await client.delete('/api/v1/me/voiceprint', headers=headers)).status_code == 200
    finally:
        app.state.voiceprints.slots.release()
    await wait_jobs(app)
    async with app.state.database.sessions() as db:
        assert await db.get(VoiceprintEnrollment, enrollment) is None
    status = (await client.get('/api/v1/me/voiceprint', headers=headers)).json()['data']
    assert not status['registered'] and status['enrollment'] is None


async def test_failed_replacement_retains_previous_template_and_account_delete_cascades(client, app, monkeypatch):
    from tests.test_auth import PASSWORD
    configure(app, monkeypatch)
    headers = bearer(await register(client))
    async def prepare():
        enrollment = await create_enrollment(client, headers)
        for slot in ('0', '1', '2'):
            assert (await upload(client, headers, enrollment, slot)).status_code == 200
            await wait_jobs(app)
        assert (await client.post(f'/api/v1/me/voiceprint/enrollments/{enrollment}/build', headers=headers)).status_code == 200
        return enrollment
    initial = await prepare()
    await upload(client, headers, initial, 'verification')
    await wait_jobs(app)
    async with app.state.database.sessions() as db:
        profile = await db.scalar(select(VoiceprintProfile))
        original, owner = profile.template, profile.owner_id
    replacement = await prepare()
    monkeypatch.setattr(app.state.voiceprints.engine, 'inspect', lambda _: ([0., 1., 0.], {'testOnly': True}))
    await upload(client, headers, replacement, 'verification')
    await wait_jobs(app)
    status = (await client.get('/api/v1/me/voiceprint', headers=headers)).json()['data']
    assert status['enabled'] and status['enrollment']['attempts'] == 1
    assert status['enrollment']['samples']['verification']['reason'] == 'verification_mismatch'
    async with app.state.database.sessions() as db:
        profile = await db.get(VoiceprintProfile, owner)
        assert profile.template == original and profile.version == 1
    result = await client.post('/api/v1/auth/delete-account', headers=headers, json={'current_password': PASSWORD, 'confirmation': 'DELETE'})
    assert result.status_code == 200, result.text
    async with app.state.database.sessions() as db:
        assert await db.get(VoiceprintProfile, owner) is None
        assert await db.get(VoiceprintEnrollment, replacement) is None

async def test_prewarm_respects_switch_releases_quota_and_tolerates_failure(app, monkeypatch):
    jobs = app.state.voiceprints
    monkeypatch.setattr(type(jobs), 'available', property(lambda _: True))
    calls = []
    monkeypatch.setattr(jobs.engine, 'prewarm', lambda: calls.append('ready'))
    app.state.settings.voiceprint_prewarm_enabled = False
    await jobs.prewarm()
    assert not calls
    app.state.settings.voiceprint_prewarm_enabled = True
    await jobs.prewarm()
    assert calls == ['ready'] and not jobs.slots.locked()
    def fail():
        raise RuntimeError('private-error-payload')
    monkeypatch.setattr(jobs.engine, 'prewarm', fail)
    await jobs.prewarm()
    assert not jobs.slots.locked()
