"""录音只通过账户认证接口读写，不发放公开链接。"""
import io
import wave
from typing import Annotated
from fastapi import APIRouter, Depends, Request, Query, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database.session import get_db
from app.core.responses import ApiResponse
from app.modules.identity.dependencies.auth import get_current_auth_context
from app.modules.meetings.services.meeting_audio import (
    AudioInit, AudioChunkInput, init_audio, append_audio, complete_audio, owned_audio)
from app.modules.meetings.models.audio import MeetingAudio, MeetingAudioChunk
from app.modules.meetings.models.meeting import Meeting

router = APIRouter(prefix='/meetings', tags=['MeetingAudio'])
Session = Annotated[AsyncSession, Depends(get_db)]
Identity = Annotated[tuple, Depends(get_current_auth_context)]


@router.post('/{client_id}/audio')
async def audio_init(client_id: str, payload: AudioInit, session: Session, identity: Identity):
    return ApiResponse(code=200, data=await init_audio(session, identity[0].id, client_id, payload))


@router.post('/audio/{audio_id}/chunk')
async def audio_append(audio_id: str, payload: AudioChunkInput, session: Session, identity: Identity):
    return ApiResponse(code=200, data=await append_audio(session, identity[0].id, audio_id, payload))


@router.post('/audio/{audio_id}/complete')
async def audio_complete(audio_id: str, session: Session, identity: Identity):
    return ApiResponse(code=200, data=await complete_audio(session, identity[0].id, audio_id))


@router.get('/{client_id}/audio')
async def audio_list(client_id: str, session: Session, identity: Identity):
    rows = await session.scalars(select(MeetingAudio).join(Meeting).where(
        Meeting.owner_id == identity[0].id, Meeting.client_id == client_id, MeetingAudio.state == 'complete'))
    return ApiResponse(code=200, data=[{'id':r.id, 'hash':r.sha256,
        **r.metadata_json} for r in rows])


@router.get('/audio/{audio_id}/content')
async def audio_content(audio_id: str, request: Request, session: Session, identity: Identity,
        start: int = Query(default=0, ge=0), end: int | None = Query(default=None, gt=0)):
    owner = identity[0].id
    row = await owned_audio(session, owner, audio_id)
    if row.state != 'complete': raise HTTPException(409, 'audio_not_complete')
    metadata = row.metadata_json
    duration, rate = metadata['duration'], metadata['rate']
    finish = duration if end is None else end
    if start >= finish or finish > duration: raise HTTPException(416, 'audio_range_invalid')
    first = metadata['dataStart'] + (start*rate//1000)*2
    last = metadata['dataStart'] + (finish*rate//1000)*2
    last = min(last, metadata['dataStart']+metadata['dataSize'])
    header = io.BytesIO()
    with wave.open(header, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(rate)
        writer.setnframes((last-first)//2); writer.writeframesraw(b'')
    prefix = bytearray(header.getvalue())
    import struct
    struct.pack_into('<I', prefix, 4, last-first+36)
    struct.pack_into('<I', prefix, 40, last-first)
    async def stream():
        yield bytes(prefix)
        cursor = first
        async with request.app.state.database.sessions() as db:
            while cursor < last:
                # 每块独立读取，注销或删除音频后立即停止。
                chunk = await db.scalar(select(MeetingAudioChunk).join(MeetingAudio).where(
                    MeetingAudioChunk.audio_id == audio_id, MeetingAudio.owner_id == owner,
                    MeetingAudioChunk.offset <= cursor).order_by(MeetingAudioChunk.offset.desc()).limit(1))
                if not chunk: return
                offset = cursor-chunk.offset
                take = min(last-cursor, len(chunk.data)-offset)
                if take <= 0: return
                yield chunk.data[offset:offset+take]
                cursor += take
                db.expunge(chunk)
                await db.rollback()
    return StreamingResponse(stream(), media_type='audio/wav',
        headers={'Content-Length':str(44+last-first), 'Cache-Control':'no-store'})
