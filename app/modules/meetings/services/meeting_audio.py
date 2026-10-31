"""可恢复音频上传与来源校验；每次最多读取一个块，不加载整场录音。"""
import base64
import hashlib
import struct
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from fastapi import HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, func, text
from app.modules.meetings.models.audio import MeetingAudio, MeetingAudioChunk
from app.modules.meetings.models.meeting import Meeting

CHUNK = 1024 * 1024


class AudioSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=100)
    hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class AudioInit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    size: int = Field(ge=44, le=512*1024*1024)
    sources: list[AudioSource] = Field(max_length=20000)


class AudioChunkInput(BaseModel):
    offset: int = Field(ge=0)
    data: str = Field(min_length=4, max_length=1398104)


async def owned_audio(db, owner, audio_id, lock=False):
    query = select(MeetingAudio).where(MeetingAudio.id == audio_id, MeetingAudio.owner_id == owner)
    if lock: query = query.with_for_update()
    row = await db.scalar(query)
    if not row: raise HTTPException(404, 'audio_not_found')
    return row


async def init_audio(db, owner, client_id, payload):
    await db.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key': 'audio:'+owner})
    meeting = await db.scalar(select(Meeting).where(Meeting.owner_id == owner, Meeting.client_id == client_id))
    if not meeting: raise HTTPException(404, 'meeting_not_found')
    if meeting.deleted: raise HTTPException(410, 'meeting_deleted')
    if not meeting.content.get('ended'): raise HTTPException(409, 'audio_recording_still_active')
    if len({s.id for s in payload.sources}) != len(payload.sources): raise HTTPException(422, 'audio_sources_duplicated')
    sources = {u['id']: u for u in meeting.content['utterances']}
    for source in payload.sources:
        original = sources.get(source.id)
        if not original or hashlib.sha256(original['raw'].encode()).hexdigest() != source.hash or source.end <= source.start:
            raise HTTPException(409, 'audio_source_changed')
    row = await db.scalar(select(MeetingAudio).where(MeetingAudio.meeting_id == meeting.id, MeetingAudio.sha256 == payload.sha256))
    if row:
        if row.size != payload.size: raise HTTPException(409, 'audio_size_conflict')
        # 来源只更新为本次已经核对过的映射，不相信设备路径。
        merged = {s['id']: s for s in row.metadata_json.get('sources', [])}
        merged.update({s.id:s.model_dump() for s in payload.sources})
        row.metadata_json = {**row.metadata_json, 'sources': list(merged.values())}
    else:
        used = await db.scalar(select(func.coalesce(func.sum(MeetingAudio.size), 0)).where(MeetingAudio.owner_id == owner))
        if used + payload.size > 2*1024**3: raise HTTPException(413, 'audio_account_quota_exceeded')
        row = MeetingAudio(id=str(uuid.uuid4()), owner_id=owner, meeting_id=meeting.id,
            sha256=payload.sha256, size=payload.size, offset=0, state='uploading',
            metadata_json={'sources': [s.model_dump() for s in payload.sources]})
        db.add(row)
    await db.commit()
    return {'id': row.id, 'offset': row.offset, 'state': row.state}


async def append_audio(db, owner, audio_id, payload):
    row = await owned_audio(db, owner, audio_id, True)
    try: data = base64.b64decode(payload.data, validate=True)
    except ValueError: raise HTTPException(422, 'audio_chunk_invalid')
    if not data or len(data) > CHUNK or payload.offset + len(data) > row.size:
        raise HTTPException(422, 'audio_chunk_invalid')
    if payload.offset < row.offset:
        previous = await db.get(MeetingAudioChunk, (row.id, payload.offset))
        if not previous or previous.data != data: raise HTTPException(409, 'audio_chunk_conflict')
    else:
        if payload.offset != row.offset or row.state != 'uploading': raise HTTPException(409, 'audio_offset_conflict')
        db.add(MeetingAudioChunk(audio_id=row.id, offset=row.offset, data=data))
        row.offset += len(data)
    await db.commit()
    return {'offset': row.offset}


def wav_layout(first, size):
    if first[:4] != b'RIFF' or first[8:12] != b'WAVE': raise HTTPException(422, 'audio_requires_pcm_wav')
    cursor, rate, channels, bits = 12, 0, 0, 0
    while cursor + 8 <= len(first):
        kind, length = first[cursor:cursor+4], struct.unpack_from('<I', first, cursor+4)[0]
        if kind == b'fmt ' and length >= 16:
            if cursor+24 > len(first): raise HTTPException(422, 'audio_wav_invalid')
            fmt, channels, rate, _, _, bits = struct.unpack_from('<HHIIHH', first, cursor+8)
            if fmt != 1 or channels != 1 or bits != 16 or rate not in {8000, 16000, 44100, 48000}:
                raise HTTPException(422, 'audio_requires_pcm_wav')
        if kind == b'data':
            start = cursor+8
            if not rate or length <= 0 or start+length > size or length % 2:
                raise HTTPException(422, 'audio_wav_invalid')
            return {'dataStart': start, 'dataSize': length, 'rate': rate,
                'duration': length*1000//(rate*2)}
        cursor += 8 + length + length % 2
    raise HTTPException(422, 'audio_wav_invalid')


async def complete_audio(db, owner, audio_id):
    row = await owned_audio(db, owner, audio_id, True)
    if row.offset != row.size: raise HTTPException(409, 'audio_upload_incomplete')
    if row.state == 'complete':
        if any(s['end'] > row.metadata_json['duration']+100 for s in row.metadata_json['sources']):
            raise HTTPException(422, 'audio_source_out_of_range')
        await publish_audio_knowledge(db, row)
        await db.commit()
        return {'id':row.id, 'state':row.state, 'duration':row.metadata_json['duration']}
    digest, offset, first = hashlib.sha256(), 0, b''
    while offset < row.size:
        chunk = await db.get(MeetingAudioChunk, (row.id, offset))
        if not chunk: raise HTTPException(409, 'audio_chunk_missing')
        if not first: first = chunk.data
        digest.update(chunk.data)
        offset += len(chunk.data)
        db.expunge(chunk)
    if digest.hexdigest() != row.sha256: raise HTTPException(422, 'audio_hash_mismatch')
    layout = wav_layout(first, row.size)
    if any(s['end'] > layout['duration']+100 for s in row.metadata_json['sources']):
        raise HTTPException(422, 'audio_source_out_of_range')
    row.state = 'complete'
    row.metadata_json = {**row.metadata_json, **layout}
    await publish_audio_knowledge(db, row)
    await db.commit()
    return {'id': row.id, 'state': row.state, 'duration': layout['duration']}


async def publish_audio_knowledge(db, row):
    from app.modules.knowledge.models.library import KnowledgeLibrary
    from app.modules.knowledge.services.knowledge_service import empty_library
    from app.modules.knowledge.schemas.knowledge import LibrarySnapshot
    library = await db.scalar(select(KnowledgeLibrary).where(KnowledgeLibrary.owner_id == row.owner_id).with_for_update())
    snapshot = deepcopy(library.snapshot) if library else empty_library()
    meeting = await db.get(Meeting, row.meeting_id)
    doc_id = 'audio-'+meeting.id
    if not any(f['id'] == 'meeting-records' for f in snapshot['folders']):
        snapshot['folders'].append({'id':'meeting-records', 'name':'会议纪要', 'parentId':None})
    doc = next((d for d in snapshot['documents'] if d['id'] == doc_id), None)
    stamp = datetime.now(timezone.utc).isoformat()
    if doc is None:
        doc = {'id':doc_id, 'folderId':'meeting-records', 'projectId':'personal-meetings', 'kind':'audio',
            'source':'meeting:'+meeting.client_id, 'createdAt':stamp, 'versions':[],
            'selectedVersionId':'', 'allowed':False, 'archived':True, 'demo':False,
            'ownerUserId':row.owner_id, 'tags':['原始录音'], 'audioRefs':[]}
        snapshot['documents'].append(doc)
    elif doc.get('folderId') == 'inbox':
        doc['folderId'] = 'meeting-records'
    ref = {'id':row.id, 'hash':row.sha256, 'duration':row.metadata_json['duration'], 'size':row.size}
    refs = [ref if r['id'] == row.id else r for r in doc.get('audioRefs', [])]
    if not any(r['id'] == row.id for r in refs): refs.append(ref)
    doc['audioRefs'] = refs
    content = '\n'.join(u['raw'] for u in meeting.content['utterances'])
    # 音频和可检索转写独立；原文更新后采用新版本，不改旧引用。
    version_id = 'audio-r'+str(meeting.revision)
    if not any(v['id'] == version_id for v in doc['versions']):
        for v in doc['versions']:
            if v['state'] == 'current': v['state'] = 'stale'
        doc['versions'].append({'id':version_id,'number':len(doc['versions'])+1,
            'title':meeting.content.get('title','会议原始录音'), 'content':content,
            'hash':hashlib.sha256(content.encode()).hexdigest(), 'createdAt':stamp,
            'state':'current', 'origin':doc['source'], 'reason':'原始录音及转写', 'sources':[]})
    doc['selectedVersionId'] = version_id
    if library and library.snapshot == snapshot:
        return
    snapshot = LibrarySnapshot.model_validate(snapshot).model_dump(mode='json', by_alias=True)
    if not library:
        library = KnowledgeLibrary(owner_id=row.owner_id, revision=0, snapshot=snapshot)
        db.add(library)
    library.snapshot = snapshot
    library.revision += 1
