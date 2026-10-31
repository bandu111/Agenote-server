"""隔离 PostgreSQL：断点、重复块、片段回读、来源校验与账户隔离。"""
import base64
import hashlib
import io
import wave
from sqlalchemy import select, func
from test_meetings import account
from app.modules.meetings.models.audio import MeetingAudio, MeetingAudioChunk


def recording():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(16000)
        writer.writeframes(b'\x01\x00'*32000)
    return stream.getvalue()


async def test_audio_resume_integrity_clip_library_and_account_isolation(client, app):
    owner = await account(client)
    other = await account(client, 'other-audio@example.com')
    text = '我方承诺提交接口评估报告。'
    payload = {'id':'audio-session', 'revision':1, 'startedAt':'2026-10-23T10:00:00+08:00',
        'title':'接口评估', 'elapsed':2000, 'ended':True,
        'utterances':[{'id':'u','speaker':'发言人 A','raw':text,'at':1000}]}
    assert (await client.post('/api/v1/meetings/sync', headers=owner, json=payload)).status_code == 200
    raw = recording()
    init = {'sha256':hashlib.sha256(raw).hexdigest(), 'size':len(raw),
        'sources':[{'id':'u','hash':hashlib.sha256(text.encode()).hexdigest(),'start':0,'end':1000}]}
    url = '/api/v1/meetings/audio-session/audio'
    denied = await client.post(url, headers=other, json=init)
    assert denied.status_code == 404
    invalid = {**init, 'sources':[{**init['sources'][0], 'hash':'0'*64}]}
    assert (await client.post(url, headers=owner, json=invalid)).status_code == 409
    response = await client.post(url, headers=owner, json=init)
    assert response.status_code == 200, response.text
    audio_id = response.json()['data']['id']
    base = '/api/v1/meetings/audio/'+audio_id
    assert (await client.post(base+'/complete', headers=owner)).status_code == 409
    first = {'offset':0, 'data':base64.b64encode(raw[:32000]).decode()}
    assert (await client.post(base+'/chunk', headers=owner, json=first)).status_code == 200
    assert (await client.post(base+'/chunk', headers=owner, json=first)).status_code == 200
    again = (await client.post(url, headers=owner, json=init)).json()['data']
    assert again['offset'] == 32000 and again['id'] == audio_id
    assert (await client.post(base+'/chunk', headers=owner,
        json={'offset':32000,'data':base64.b64encode(raw[32000:]).decode()})).status_code == 200
    complete = await client.post(base+'/complete', headers=owner)
    assert complete.status_code == 200, complete.text
    assert complete.json()['data']['duration'] == 2000
    history = (await client.get('/api/v1/meetings', headers=owner)).json()['data']
    assert history[0]['audioSources']['u']['audioId'] == audio_id
    for headers, status in [(other,404), ({},401)]:
        assert (await client.get(base+'/content', headers=headers)).status_code == status
    clip = await client.get(base+'/content?start=500&end=1000', headers=owner)
    assert clip.status_code == 200
    with wave.open(io.BytesIO(clip.content),'rb') as reader:
        assert reader.getnframes() == 8000 and reader.readframes(8000) == b'\x01\x00'*8000
    library = (await client.get('/api/v1/knowledge', headers=owner)).json()['data']
    # 兼容接口包裹的快照。
    snapshot = library.get('snapshot', library)
    doc = next(d for d in snapshot['documents'] if d['kind'] == 'audio')
    assert doc['folderId'] == 'meeting-records'
    assert any(f['id'] == 'meeting-records' and f['name'] == '会议纪要' for f in snapshot['folders'])
    assert doc['audioRefs'][0]['id'] == audio_id and doc['allowed']
    assert text in doc['versions'][0]['content']
    assert (await client.post(base+'/complete', headers=owner)).status_code == 200
    deleted = await client.post('/api/v1/auth/delete-account', headers=owner,
        json={'current_password':'meeting-test-password-2026','confirmation':'DELETE'})
    assert deleted.status_code == 200
    async with app.state.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(MeetingAudio)) == 0
        assert await db.scalar(select(func.count()).select_from(MeetingAudioChunk)) == 0


async def test_audio_rejects_corrupt_content(client):
    owner = await account(client)
    await client.post('/api/v1/meetings/sync', headers=owner, json={
        'id':'bad-audio','revision':1,'startedAt':'2026-10-23T10:00:00+08:00',
        'elapsed':0,'ended':True,'utterances':[]})
    raw = recording()
    response = await client.post('/api/v1/meetings/bad-audio/audio', headers=owner,
        json={'size':len(raw),'sha256':'0'*64,'sources':[]})
    audio_id = response.json()['data']['id']
    base = '/api/v1/meetings/audio/'+audio_id
    await client.post(base+'/chunk', headers=owner, json={'offset':0,'data':base64.b64encode(raw).decode()})
    assert (await client.post(base+'/complete', headers=owner)).status_code == 422
