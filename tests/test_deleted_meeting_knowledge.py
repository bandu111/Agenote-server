"""Database integration: explicit deletion survives repeated automatic archive."""
from copy import deepcopy
from unittest.mock import AsyncMock
from sqlalchemy import select
from tests.test_meetings import account, meeting
from tests.test_knowledge import snapshot
from app.modules.knowledge.models.library import DeletedMeetingKnowledge, KnowledgeNode


async def test_upload_delete_conflict_and_cross_account_isolation(client, app):
    first = await account(client)
    second = await account(client, 'other-delete@example.com')
    data = snapshot()
    assert (await client.post('/api/v1/knowledge/sync', headers=first,
        json={'revision': 0, 'snapshot': data})).status_code == 200
    original = (await client.get('/api/v1/knowledge', headers=first)).json()['data']
    changed = deepcopy(original['snapshot'])
    changed['documents'] = []
    stale = await client.post('/api/v1/knowledge/sync', headers=first,
        json={'revision': 0, 'snapshot': changed})
    assert stale.status_code == 409
    assert (await client.get('/api/v1/knowledge', headers=first)).json()['data']['snapshot']['documents']
    deleted = await client.post('/api/v1/knowledge/sync', headers=first,
        json={'revision': original['revision'], 'snapshot': changed})
    assert deleted.status_code == 200, deleted.text
    assert (await client.get('/api/v1/knowledge', headers=first)).json()['data']['snapshot']['documents'] == []
    assert (await client.post('/api/v1/knowledge/search', headers=first, json={'query': '星澜'})).json()['data'] == []
    assert (await client.get('/api/v1/knowledge', headers=second)).json()['data']['snapshot']['documents'] == []
    async with app.state.database.sessions() as db:
        assert not list(await db.scalars(select(KnowledgeNode)))


async def test_deleted_minutes_not_recreated_by_automatic_archive(client, app):
    headers = await account(client)
    source = meeting()
    source['ended'] = True
    await client.post('/api/v1/meetings/sync', headers=headers, json=source)
    app.state.meeting_analysis.agents.provider.run = AsyncMock(
        return_value={'decisions': [], 'actions': [], 'open_questions': []})
    url = '/api/v1/meetings/phone-meeting-1/archive'
    assert (await client.post(url, headers=headers)).status_code == 200
    library = (await client.get('/api/v1/knowledge', headers=headers)).json()['data']
    assert len(library['snapshot']['documents']) == 1
    document_id = library['snapshot']['documents'][0]['id']
    old_snapshot = deepcopy(library['snapshot'])
    library['snapshot']['documents'] = []
    result = await client.post('/api/v1/knowledge/sync', headers=headers,
        json={'revision': library['revision'], 'snapshot': library['snapshot']})
    assert result.status_code == 200, result.text
    assert (await client.post(url, headers=headers)).status_code == 200
    after = (await client.get('/api/v1/knowledge', headers=headers)).json()['data']
    assert after['snapshot']['documents'] == []
    stale_resurrection = await client.post('/api/v1/knowledge/sync', headers=headers,
        json={'revision': after['revision'], 'snapshot': old_snapshot})
    assert stale_resurrection.status_code == 409
    assert (await client.get('/api/v1/knowledge', headers=headers)).json()['data']['snapshot']['documents'] == []
    assert (await client.get(url, headers=headers)).status_code == 200
    async with app.state.database.sessions() as db:
        marks = list(await db.scalars(select(DeletedMeetingKnowledge)))
        assert len(marks) == 1 and marks[0].document_id == document_id
