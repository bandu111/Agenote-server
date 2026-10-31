"""内部任务只按认证账号查询；现实履约不由模型自动判定。"""
from typing import Annotated, Literal
from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database.session import get_db
from app.core.responses import ApiResponse
from app.modules.identity.dependencies.auth import get_current_auth_context
from app.modules.meetings.schemas.meeting import StrictModel
from app.modules.execution.task_service import TaskService

router = APIRouter(prefix='/tasks', tags=['InternalTasks'])
Session = Annotated[AsyncSession, Depends(get_db)]
Identity = Annotated[tuple, Depends(get_current_auth_context)]


class TaskStatus(StrictModel):
    version: int = Field(ge=1)
    status: Literal['pending', 'active', 'claimed_completed', 'verified']
    note: str = Field(min_length=1, max_length=1000)


@router.get('', response_model=ApiResponse[list[dict]])
async def tasks(session: Session, identity: Identity):
    return ApiResponse(code=200, data=await TaskService(session).list(identity[0].id))


@router.post('/{task_id}/status', response_model=ApiResponse[dict])
async def update_task(task_id: str, payload: TaskStatus, session: Session, identity: Identity):
    row = await TaskService(session).change(identity[0].id, task_id, payload.version, payload.status, payload.note)
    await session.commit()
    return ApiResponse(code=200, data=TaskService.output(row))


class TaskBinding(StrictModel):
    version: int = Field(ge=1)
    goalId: str = Field(min_length=1, max_length=100)
    goalVersion: int = Field(ge=1)
    milestoneTitle: str = Field(min_length=1, max_length=300)
    note: str = Field(min_length=1, max_length=1000)


@router.post('/{task_id}/binding', response_model=ApiResponse[dict])
async def bind_task(task_id: str, payload: TaskBinding, session: Session, identity: Identity):
    row = await TaskService(session).bind(identity[0].id, task_id, payload.version,
        payload.goalId, payload.goalVersion, payload.milestoneTitle, payload.note)
    await session.commit()
    return ApiResponse(code=200, data=TaskService.output(row))
