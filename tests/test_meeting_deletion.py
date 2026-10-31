"""Real database deletion, tenant boundaries and stale-client resurrection."""
from copy import deepcopy
from unittest.mock import AsyncMock
import base64
import hashlib
from sqlalchemy import select
from tests.test_meetings import account, meeting
from tests.test_knowledge import snapshot
from app.modules.meetings.models.meeting import Meeting
from app.modules.meetings.models.archive import MeetingArchive
from app.modules.meetings.models.audio import MeetingAudio, MeetingAudioChunk
from tests.test_meeting_audio import recording
from app.modules.knowledge.models.library import KnowledgeNode


async def test_delete_removes_generated_sources_but_preserves_imports(client, app):
    headers = await account(client)
    other = await account(client, 'deletion-other@example.com')
    assert (await client.post('/api/v1/knowledge/sync', headers=headers,
        json={'revision': 0, 'snapshot': snapshot()})).status_code == 200
    imported = (await client.get('/api/v1/knowledge', headers=headers)).json()['data']['snapshot']['documents']
    source = meeting()
    source['ended'] = True
    assert (await client.post('/api/v1/meetings/sync', headers=headers, json=source)).status_code == 200
    app.state.meeting_analysis.agents.provider.run = AsyncMock(return_value={
        'decisions': [], 'actions': [], 'open_questions': []})
    url = '/api/v1/meetings/' + source['id']
    assert (await client.post(url+'/archive', headers=headers)).status_code == 200
    raw = recording()
    upload = await client.post(url+'/audio', headers=headers, json={
        'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'sources': []})
    assert upload.status_code == 200, upload.text
    audio_url = '/api/v1/meetings/audio/' + upload.json()['data']['id']
    assert (await client.post(audio_url+'/chunk', headers=headers,
        json={'offset': 0, 'data': base64.b64encode(raw).decode()})).status_code == 200
    assert (await client.post(audio_url+'/complete', headers=headers)).status_code == 200
    before = (await client.get('/api/v1/knowledge', headers=headers)).json()['data']
    stale_snapshot = deepcopy(before['snapshot'])
    assert len(stale_snapshot['documents']) == len(imported) + 2
    assert (await client.request('DELETE', url, headers=other, json={'revision': 1})).status_code == 404
    assert (await client.request('DELETE', url, headers=headers, json={'revision': 0})).status_code == 409
    result = await client.request('DELETE', url, headers=headers, json={'revision': 1})
    assert result.status_code == 200, result.text
    assert result.json()['data']['deleted'] is True
    assert (await client.get(url, headers=headers)).status_code == 404
    assert (await client.get(url+'/archive', headers=headers)).status_code == 404
    assert (await client.get(audio_url+'/content', headers=headers)).status_code == 404
    assert (await client.get('/api/v1/meetings', headers=headers)).json()['data'] == []
    assert (await client.get('/api/v1/meetings/deleted', headers=headers)).json()['data'] == [source['id']]
    assert (await client.get('/api/v1/meetings/deleted', headers=other)).json()['data'] == []
    after = (await client.get('/api/v1/knowledge', headers=headers)).json()['data']
    assert after['snapshot']['documents'] == imported
    source['revision'] = 9
    assert (await client.post('/api/v1/meetings/sync', headers=headers, json=source)).status_code == 410
    assert (await client.post(url+'/archive', headers=headers)).status_code == 404
    # Even a stale client submitting at the newest library revision cannot recreate it.
    retry = await client.post('/api/v1/knowledge/sync', headers=headers,
        json={'revision': after['revision'], 'snapshot': stale_snapshot})
    assert retry.status_code in (200, 409), retry.text
    assert (await client.get('/api/v1/knowledge', headers=headers)).json()['data']['snapshot']['documents'] == imported
    assert (await client.request('DELETE', url, headers=headers, json={'revision': 1})).status_code == 200
    async with app.state.database.sessions() as db:
        rows = list(await db.scalars(select(Meeting)))
        assert len(rows) == 1 and rows[0].deleted
        assert rows[0].content['utterances'] == []
        assert not list(await db.scalars(select(MeetingArchive)))
        assert not list(await db.scalars(select(MeetingAudio)))
        assert not list(await db.scalars(select(MeetingAudioChunk)))
        assert not list(await db.scalars(select(KnowledgeNode).where(KnowledgeNode.document_id == 'meeting-'+source['id'])))


async def test_active_meeting_cannot_be_deleted(client):
    headers = await account(client)
    source = meeting()
    await client.post('/api/v1/meetings/sync', headers=headers, json=source)
    response = await client.request('DELETE', '/api/v1/meetings/'+source['id'], headers=headers, json={'revision': 1})
    assert response.status_code == 409
    assert 'meeting_still_recording' in response.text
    assert len((await client.get('/api/v1/meetings', headers=headers)).json()['data']) == 1
