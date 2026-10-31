"""Generated meeting records belong together; imported or manually filed data stays put."""
from copy import deepcopy
from unittest.mock import AsyncMock
from sqlalchemy import select
from tests.test_meetings import account, meeting
from tests.test_knowledge import snapshot
from app.modules.knowledge.models.library import KnowledgeLibrary


async def test_legacy_generated_inbox_records_are_repaired_once(client, app):
    headers = await account(client)
    await client.post('/api/v1/knowledge/sync', headers=headers,
        json={'revision': 0, 'snapshot': snapshot()})
    source = meeting()
    source['ended'] = True
    await client.post('/api/v1/meetings/sync', headers=headers, json=source)
    app.state.meeting_analysis.agents.provider.run = AsyncMock(return_value={
        'decisions': [], 'actions': [], 'open_questions': []})
    response = await client.post('/api/v1/meetings/'+source['id']+'/archive', headers=headers)
    assert response.status_code == 200, response.text
    original = (await client.get('/api/v1/knowledge', headers=headers)).json()['data']
    imported = next(d for d in original['snapshot']['documents'] if d['id'] == 'contract')
    async with app.state.database.sessions() as db:
        row = await db.scalar(select(KnowledgeLibrary))
        data = deepcopy(row.snapshot)
        minutes = next(d for d in data['documents'] if d['id'] == 'meeting-'+source['id'])
        minutes['folderId'] = 'inbox'
        row.snapshot = data
        await db.commit()
    result = (await client.get('/api/v1/knowledge', headers=headers)).json()['data']
    assert next(d for d in result['snapshot']['documents'] if d['id'] == minutes['id'])['folderId'] == 'meeting-records'
    assert next(d for d in result['snapshot']['documents'] if d['id'] == 'contract') == imported
    assert result['revision'] == original['revision'] + 1
    assert (await client.get('/api/v1/knowledge', headers=headers)).json()['data'] == result
    # A user-chosen custom destination is not treated as a broken default.
    async with app.state.database.sessions() as db:
        row = await db.scalar(select(KnowledgeLibrary))
        data = deepcopy(row.snapshot)
        data['folders'].append({'id':'custom', 'name':'项目资料', 'parentId':None})
        next(d for d in data['documents'] if d['id'] == minutes['id'])['folderId'] = 'custom'
        row.snapshot = data
        await db.commit()
    final = (await client.get('/api/v1/knowledge', headers=headers)).json()['data']
    assert next(d for d in final['snapshot']['documents'] if d['id'] == minutes['id'])['folderId'] == 'custom'
