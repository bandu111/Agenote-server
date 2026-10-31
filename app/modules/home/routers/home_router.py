"""首页账户作用域从认证上下文取得，不接受客户端指定用户。"""

from typing import Annotated
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database.session import get_db
from app.core.responses import ApiResponse
from app.modules.identity.dependencies.auth import get_current_auth_context
from app.modules.home.services.home_service import HomeService
from pydantic import BaseModel, Field, ConfigDict
from app.modules.home.services.task_checklist import complete_checklist

router = APIRouter(prefix="/home", tags=["Home"])


@router.get("", response_model=ApiResponse[dict])
async def snapshot(
    session: Annotated[AsyncSession, Depends(get_db)],
    identity: Annotated[tuple, Depends(get_current_auth_context)],
):
    return ApiResponse(code=200, data=await HomeService(session).snapshot(identity[0].id))


class ChecklistCompletion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)


@router.post('/tasks/{task_id}/complete', response_model=ApiResponse[dict])
async def complete_task(
    task_id: str, payload: ChecklistCompletion,
    session: Annotated[AsyncSession, Depends(get_db)],
    identity: Annotated[tuple, Depends(get_current_auth_context)],
):
    return ApiResponse(code=200, data=await complete_checklist(session, identity[0].id, task_id, payload.version))


@router.post('/tasks/{task_id}/reopen', response_model=ApiResponse[dict])
async def reopen_task(
    task_id: str, payload: ChecklistCompletion,
    session: Annotated[AsyncSession, Depends(get_db)],
    identity: Annotated[tuple, Depends(get_current_auth_context)],
):
    from fastapi import HTTPException
    from app.modules.meetings.services.minutes_actions import complete_minutes_action
    if not task_id.startswith('minutes:'):
        raise HTTPException(409, 'checklist_undo_not_supported')
    return ApiResponse(code=200, data=await complete_minutes_action(
        session, identity[0].id, task_id, payload.version, completed=False))
