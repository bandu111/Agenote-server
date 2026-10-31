"""服务及数据库健康检查，不混入应用组装入口。"""
from typing import Annotated
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database.session import get_db
from app.core.responses import ApiResponse

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=ApiResponse[dict[str, str]])
async def health() -> ApiResponse[dict[str, str]]:
    return ApiResponse(code=200, data={"status": "ok"})


@router.get(
    "/health/database",
    response_model=ApiResponse[dict[str, str]],
    responses={503: {"model": ApiResponse[None]}},
)
async def health_database(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ApiResponse[dict[str, str]]:
    await session.execute(text("SELECT 1"))
    return ApiResponse(code=200, data={"status": "connected"})
