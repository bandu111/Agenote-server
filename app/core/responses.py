"""统一接口响应模型与错误响应构造。"""
from typing import Generic, TypeVar
from starlette.responses import JSONResponse
from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: int
    message: str = "success"
    data: T | None = None


def error_response(status: int, message: str) -> JSONResponse:
    """统一错误结构及认证、限流响应头，不接收异常原文。"""
    headers: dict[str, str] = {}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    elif status == 429:
        headers["Retry-After"] = "60"
    return JSONResponse(
        status_code=status,
        content={"code": status, "message": message, "data": None},
        headers=headers,
    )
