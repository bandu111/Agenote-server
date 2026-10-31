"""Authenticated, bounded raw WAV uploads; responses never include voiceprint vectors."""
from typing import Literal
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database.session import get_db
from app.core.responses import ApiResponse
from app.modules.identity.dependencies.auth import get_current_auth_context

router = APIRouter(prefix='/me/voiceprint', tags=['Voiceprint'])


class ConsentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    consentVersion: str


class EnableInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


@router.get('')
async def status(request: Request, db: AsyncSession = Depends(get_db), identity=Depends(get_current_auth_context)):
    return ApiResponse(code=200, data=await request.app.state.voiceprints.status(db, identity[0].id))


@router.post('/enrollments')
async def create(payload: ConsentInput, request: Request, db: AsyncSession = Depends(get_db), identity=Depends(get_current_auth_context)):
    return ApiResponse(code=200, data=await request.app.state.voiceprints.create(db, identity[0].id, payload.consentVersion))


@router.post('/enrollments/{enrollment_id}/samples/{slot}')
async def upload(enrollment_id: str, slot: Literal['0', '1', '2', 'verification'], request: Request,
                 db: AsyncSession = Depends(get_db), identity=Depends(get_current_auth_context)):
    if request.headers.get('content-type', '').split(';')[0] != 'audio/wav':
        raise HTTPException(422, 'voiceprint_invalid_audio')
    audio = bytearray()
    async for chunk in request.stream():
        audio.extend(chunk)
        if len(audio) > 650000:
            raise HTTPException(413, 'voiceprint_invalid_audio')
    device = {'type': request.headers.get('x-audio-device-type', '')[:40],
              'name': request.headers.get('x-audio-device-name', '')[:100]}
    if device['type'] != 'builtInMic':
        raise HTTPException(422, 'voiceprint_phone_microphone_required')
    result = await request.app.state.voiceprints.submit(db, identity[0].id, enrollment_id, slot, bytes(audio), device)
    return ApiResponse(code=200, data=result)


@router.post('/enrollments/{enrollment_id}/build')
async def build(enrollment_id: str, request: Request, db: AsyncSession = Depends(get_db), identity=Depends(get_current_auth_context)):
    return ApiResponse(code=200, data=await request.app.state.voiceprints.build(db, identity[0].id, enrollment_id))


@router.delete('/enrollments/{enrollment_id}')
async def cancel(enrollment_id: str, request: Request, db: AsyncSession = Depends(get_db), identity=Depends(get_current_auth_context)):
    await request.app.state.voiceprints.cancel(db, identity[0].id, enrollment_id)
    return ApiResponse(code=200)


@router.post('/enabled')
async def enabled(payload: EnableInput, request: Request, db: AsyncSession = Depends(get_db), identity=Depends(get_current_auth_context)):
    await request.app.state.voiceprints.cancel(db, identity[0].id, enabled=payload.enabled)
    return ApiResponse(code=200)


@router.delete('')
async def erase(request: Request, db: AsyncSession = Depends(get_db), identity=Depends(get_current_auth_context)):
    await request.app.state.voiceprints.cancel(db, identity[0].id, erase=True)
    return ApiResponse(code=200)
