"""Owner-scoped calendar approvals and explicit device mutation protocol."""
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, Request
from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database.session import get_db
from app.core.responses import ApiResponse
from app.modules.identity.dependencies.auth import get_current_auth_context
from app.modules.meetings.schemas.meeting import StrictModel
from app.modules.actions.service import ActionService

router = APIRouter(prefix="/actions", tags=["Actions"])
Session = Annotated[AsyncSession, Depends(get_db)]
Identity = Annotated[tuple, Depends(get_current_auth_context)]


class MutationFields(StrictModel):
    version: int | None = Field(default=None, ge=1)
    mutationId: str | None = Field(default=None, min_length=12, max_length=100)


class Decision(MutationFields):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    allow: bool
    deviceId: str = Field(min_length=16, max_length=100)


class Receipt(MutationFields):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    verified: bool
    eventId: str = Field(default="", max_length=200)
    previousEventId: str | None = Field(default=None, max_length=200)
    calendarId: str | None = Field(default=None, max_length=200)
    deleted: bool = False
    title: str = Field(default="", max_length=160)
    start: str = Field(default="", max_length=80)
    end: str = Field(default="", max_length=80)
    deviceId: str = Field(min_length=16, max_length=100)


class CalendarMutation(StrictModel):
    currentVersion: int = Field(ge=1)
    requestId: str = Field(min_length=12, max_length=100)


class Reschedule(CalendarMutation):
    start: str = Field(min_length=1, max_length=80)
    end: str = Field(min_length=1, max_length=80)
    timeZone: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=160)
    agenda: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def valid_schedule(self):
        try:
            start = datetime.fromisoformat(self.start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(self.end.replace("Z", "+00:00"))
            ZoneInfo(self.timeZone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("invalid_calendar_time") from error
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("invalid_calendar_time")
        return self


class Cancel(CalendarMutation):
    pass


def service(session, request):
    return ActionService(session, request.app.state.settings, request.app.state.database)


@router.get("", response_model=ApiResponse[list[dict]])
async def list_actions(request: Request, session: Session, identity: Identity, includeToolPlans: bool = False):
    return ApiResponse(code=200, data=await service(session, request).list(
        identity[0].id, include_tool_plans=includeToolPlans
    ))


@router.post("/{action_id}/decision", response_model=ApiResponse[dict])
async def decide(action_id: str, payload: Decision, request: Request, session: Session, identity: Identity):
    return ApiResponse(code=200, data=await service(session, request).advance(
        identity[0].id, action_id, "decision", payload.fingerprint, payload.allow,
        payload.deviceId, version=payload.version, mutation_id=payload.mutationId,
    ))


@router.post("/{action_id}/receipt", response_model=ApiResponse[dict])
async def receipt(action_id: str, payload: Receipt, request: Request, session: Session, identity: Identity):
    return ApiResponse(code=200, data=await service(session, request).advance(
        identity[0].id, action_id, "receipt", payload.fingerprint, payload.model_dump(),
        payload.deviceId, version=payload.version, mutation_id=payload.mutationId,
    ))


@router.post("/{action_id}/reschedule", response_model=ApiResponse[dict])
async def reschedule(action_id: str, payload: Reschedule, request: Request, session: Session, identity: Identity):
    return ApiResponse(code=200, data=await service(session, request).create_mutation(
        identity[0].id, action_id, "reschedule", payload.currentVersion, payload.requestId,
        start=payload.start, end=payload.end, timeZone=payload.timeZone,
        title=payload.title, agenda=payload.agenda,
    ))


@router.post("/{action_id}/cancel", response_model=ApiResponse[dict])
async def cancel(action_id: str, payload: Cancel, request: Request, session: Session, identity: Identity):
    return ApiResponse(code=200, data=await service(session, request).create_mutation(
        identity[0].id, action_id, "cancel", payload.currentVersion, payload.requestId,
    ))
