"""Identity persistence tests use explicit events, not real speaker accuracy claims."""
from unittest.mock import AsyncMock
import pytest
from tests.test_meetings import account, meeting
from app.modules.meetings.services.speaker_identity import MeetingSpeakerIdentities


async def setup_meeting(client, app):
    headers = await account(client)
    data = meeting()
    data['utterances'][0].update(speaker='发言人 A', speakerId='asr:test:0', speakerSeparated=True)
    result = await client.post('/api/v1/meetings/sync', headers=headers, json=data)
    owner = result.json()['data']['ownerUserId']
    return headers, data, MeetingSpeakerIdentities(app.state.database, owner, data['id'])


async def test_identity_is_server_owned_account_scoped_and_survives_full_sync(client, app):
    headers, data, identities = await setup_meeting(client, app)
    other = await account(client, 'identity-other@example.com')
    assert await identities.save('test', 0, 'self') == 1
    assert await identities.save('test', 0, 'self') == 1
    url = f"/api/v1/meetings/{data['id']}/speaker-identities"
    assert (await client.get(url, headers=other)).status_code == 404
    value = (await client.get(url, headers=headers)).json()['data']
    assert value['revision'] == 1 and value['identities']['asr:test:0']['identity'] == 'self'
    data['revision'] += 1
    synced = (await client.post('/api/v1/meetings/sync', headers=headers, json=data)).json()['data']
    assert synced['speakerIdentities'] == value['identities']
    data['speakerIdentities'] = {'asr:test:0': {'identity': 'self'}}
    assert (await client.post('/api/v1/meetings/sync', headers=headers, json=data)).status_code == 422
    assert await identities.save('test', 0, 'unknown') == 2
    value = (await client.get(url, headers=headers)).json()['data']
    assert value['identities']['asr:test:0']['identity'] == 'unknown'
    missing = MeetingSpeakerIdentities(app.state.database, identities.owner, 'missing')
    with pytest.raises(LookupError):
        await missing.save('test', 0, 'self')


async def test_archive_renders_self_and_refreshes_revocation_without_model_rerun(client, app):
    headers, data, identities = await setup_meeting(client, app)
    await identities.save('test', 0, 'self')
    data.update(revision=2, ended=True)
    await client.post('/api/v1/meetings/sync', headers=headers, json=data)
    provider = app.state.meeting_analysis.agents.provider
    provider.run = AsyncMock(return_value={'decisions': [], 'actions': [], 'open_questions': []})
    url = f"/api/v1/meetings/{data['id']}/archive"
    response = await client.post(url, headers=headers)
    assert response.status_code == 200, response.text
    value = response.json()['data']
    assert '我（A）' in value['markdown'] and value['speakerIdentityRevision'] == 1
    assert provider.run.await_count == 1
    await identities.save('test', 0, 'unknown')
    value = (await client.get(url, headers=headers)).json()['data']
    assert '我（A）' not in value['markdown'] and '发言人 A' in value['markdown']
    assert value['speakerIdentityRevision'] == 2
    assert provider.run.await_count == 1
    from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository
    async with app.state.database.sessions() as db:
        library = await KnowledgeRepository(db).library(identities.owner)
        document = next(d for d in library.snapshot['documents'] if d['id'] == 'meeting-' + data['id'])
        selected = next(v for v in document['versions'] if v['id'] == document['selectedVersionId'])
        assert '我（A）' not in selected['content']

async def test_identity_save_failure_is_explicit_and_retry_can_persist():
    from tests.test_realtime_voiceprint import session
    live, sent = session()
    live.persistence = type('Persistence', (), {})()
    live.persistence.save = AsyncMock(side_effect=[LookupError('not-created'), 4])
    await live.publish(0, 'self')
    assert sent[-1]['persisted'] is False
    assert live.pending_persistence == {0: 'self'}
    await live.publish(0, 'self', force=True)
    assert sent[-1]['persisted'] is True and sent[-1]['meeting_identity_revision'] == 4
    assert not live.pending_persistence
    await live.close()

async def test_reconnect_preserves_saved_self_when_first_new_sample_supports_it():
    from tests.test_realtime_voiceprint import session, event, drain
    from app.modules.voiceprints.realtime import SpeakerEvidence
    import asyncio
    live, sent = session()
    live.speakers[0] = SpeakerEvidence(identity='self')
    live.feed(bytes(32000 * 10))
    live.worker = asyncio.create_task(live.run())
    try:
        await live.observe(event(0, 0, 0, 4160))
        await drain(live)
        assert live.speakers[0].identity == 'self' and not sent
        live.jobs.engine.inspect_live = lambda _: ([0., 1.], {})
        await live.observe(event(1, 0, 4160, 8320))
        await drain(live)
        assert sent[-1]['identity'] == 'unknown'
    finally:
        await live.close()

async def test_uncertain_samples_do_not_revoke_confirmed_identity_but_low_match_does():
    from tests.test_realtime_voiceprint import session, event, drain
    import asyncio
    import math
    live, sent = session()
    live.feed(bytes(32000 * 25))
    live.worker = asyncio.create_task(live.run())
    try:
        for i in range(2):
            await live.observe(event(i, 0, i * 4160, (i+1)*4160))
            await drain(live)
        assert sent[-1]['identity'] == 'self'
        version = sent[-1]['identity_revision']
        live.jobs.engine.inspect_live = lambda _: ([.5, math.sqrt(.75)], {})
        for i in range(2,4):
            await live.observe(event(i, 0, i * 4160, (i+1)*4160))
            await drain(live)
            assert live.speakers[0].identity == 'self'
            assert sent[-1]['identity_revision'] == version
        live.jobs.engine.inspect_live = lambda _: ([0., 1.], {})
        await live.observe(event(4, 0, 16640, 20800))
        await drain(live)
        assert sent[-1]['identity'] == 'unknown'
    finally:
        await live.close()
