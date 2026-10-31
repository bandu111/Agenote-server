"""Authenticated unified search; caller-controlled owner fields are never accepted."""
from datetime import datetime, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends
from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database.session import get_db
from app.core.responses import ApiResponse
from app.modules.identity.dependencies.auth import get_current_auth_context
from app.modules.meetings.schemas.meeting import StrictModel
from app.modules.search.service import UnifiedSearch

router = APIRouter(prefix="/search", tags=["Search"])


MeetingScopeId = Annotated[str, Field(min_length=1, max_length=100)]


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=24, ge=1, le=50)
    kinds: list[Literal["document", "goal", "commitment", "task", "utterance", "meeting", "outcome"]] | None = Field(default=None, min_length=1, max_length=7)
    projectId: str | None = Field(default=None, max_length=100)
    meetingIds: list[MeetingScopeId] | None = Field(default=None, max_length=50)
    dateFrom: datetime | None = None
    dateTo: datetime | None = None
    timeZone: str = Field(default="Asia/Shanghai", max_length=64)

    @model_validator(mode="after")
    def valid_scope(self):
        try:
            ZoneInfo(self.timeZone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("invalid_time_zone") from error
        if any(value is not None and value.tzinfo is None for value in [self.dateFrom, self.dateTo]):
            raise ValueError("search_dates_require_timezone")
        if self.dateFrom and self.dateTo and self.dateFrom >= self.dateTo:
            raise ValueError("invalid_date_range")
        return self


@router.post("", response_model=ApiResponse[list[dict]])
async def search(
    payload: SearchRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    identity=Depends(get_current_auth_context),
):
    return ApiResponse(code=200, data=await UnifiedSearch(session).search(
        identity[0].id,
        payload.query,
        payload.limit,
        payload.kinds,
        payload.projectId,
        meeting_ids=payload.meetingIds,
        date_from=payload.dateFrom.astimezone(timezone.utc) if payload.dateFrom else None,
        date_to=payload.dateTo.astimezone(timezone.utc) if payload.dateTo else None,
        time_zone=payload.timeZone,
    ))
