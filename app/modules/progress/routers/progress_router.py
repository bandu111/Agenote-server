"""认证账户的进展读写，不建立跨账户参与关系。"""

from typing import Annotated
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database.session import get_db
from app.core.responses import ApiResponse
from app.modules.identity.dependencies.auth import get_current_auth_context
from app.modules.progress.services.progress_service import ProgressService
from app.modules.progress.schemas.progress import (
    GoalInput,
    GoalUpdate,
    GoalRemovalInput,
    CommitmentUpdate,
    RescheduleInput,
    ApprovalInput,
    CompletionInput,
)

router = APIRouter(prefix="/progress", tags=["Progress"])
Session = Annotated[AsyncSession, Depends(get_db)]
Identity = Annotated[tuple, Depends(get_current_auth_context)]


@router.get("", response_model=ApiResponse[dict])
async def snapshot(session: Session, identity: Identity):
    value = await ProgressService(session).snapshot(identity[0].id)
    await session.commit()
    return ApiResponse(code=200, data=value)


@router.post("/{item_id}/complete", response_model=ApiResponse[dict])
async def complete(
    item_id: str, payload: CompletionInput, session: Session, identity: Identity
):
    return ApiResponse(
        code=200,
        data=await ProgressService(session).complete(identity[0].id, item_id, payload),
    )


@router.post("/goals", response_model=ApiResponse[dict], status_code=201)
async def create_goal(payload: GoalInput, session: Session, identity: Identity):
    return ApiResponse(
        code=201,
        data=await ProgressService(session).create_goal(identity[0].id, payload),
    )


@router.post("/goals/{item_id}", response_model=ApiResponse[dict])
async def update_goal(
    item_id: str, payload: GoalUpdate, session: Session, identity: Identity
):
    return ApiResponse(
        code=200,
        data=await ProgressService(session).mutate(
            identity[0].id,
            item_id,
            "goal",
            payload.version,
            payload.goal.model_dump(mode="json"),
            payload.note,
        ),
    )


@router.post("/goals/{item_id}/remove", response_model=ApiResponse[dict])
async def remove_goal(item_id: str, payload: GoalRemovalInput, session: Session, identity: Identity):
    return ApiResponse(code=200, data=await ProgressService(session).remove_goal(
        identity[0].id, item_id, payload.version, payload.note
    ))


@router.post("/commitments/{item_id}/status", response_model=ApiResponse[dict])
async def update_commitment(
    item_id: str, payload: CommitmentUpdate, session: Session, identity: Identity
):
    return ApiResponse(
        code=200,
        data=await ProgressService(session).mutate(
            identity[0].id,
            item_id,
            "commitment",
            payload.version,
            {"status": payload.status},
            payload.note,
        ),
    )


@router.post("/commitments/{item_id}/reschedule", response_model=ApiResponse[dict])
async def reschedule(
    item_id: str, payload: RescheduleInput, session: Session, identity: Identity
):
    return ApiResponse(
        code=200,
        data=await ProgressService(session).mutate(
            identity[0].id,
            item_id,
            "commitment",
            payload.version,
            {"deadline": payload.deadline.isoformat()},
            payload.reason,
        ),
    )


@router.post("/artifacts/{item_id}/approve", response_model=ApiResponse[dict])
async def approve(
    item_id: str, payload: ApprovalInput, session: Session, identity: Identity
):
    return ApiResponse(
        code=200,
        data=await ProgressService(session).mutate(
            identity[0].id,
            item_id,
            "artifact",
            payload.version,
            {
                "status": "approved",
                "artifactVersion": payload.artifactVersion,
                "approvalNote": payload.note,
            },
            payload.note,
        ),
    )
