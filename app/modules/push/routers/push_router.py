"""系统推送的认证入口，具体绑定规则由 Service 处理。"""
from typing import Annotated
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database.session import get_db
from app.core.responses import ApiResponse
from app.modules.identity.dependencies.auth import get_current_auth_context
from app.modules.push.schemas.push import DeviceInput
from app.modules.push.services.push_service import PushService

router = APIRouter(prefix="/push", tags=["系统推送"])


@router.post("/devices", response_model=ApiResponse[dict])
async def bind(payload: DeviceInput, request: Request,
    session: Annotated[AsyncSession, Depends(get_db)], identity=Depends(get_current_auth_context)):
    result = await PushService(session, request.app.state.settings).bind(payload, identity)
    return ApiResponse(code=200, data=result)
