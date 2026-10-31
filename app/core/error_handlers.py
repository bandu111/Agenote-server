"""集中注册异常处理，防止输入、数据库和异常详情泄露。"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from app.core.responses import error_response

# 只向客户端返回已定义的业务错误码，禁止透传任意异常详情。
PUBLIC_HTTP_MESSAGES = frozenset(
    {
        "demo_reset_token_invalid",
        "voiceprint_session_expired", "voiceprint_configuration_changed",
        "voiceprint_consent_required", "voiceprint_unavailable", "voiceprint_busy",
        "voiceprint_not_ready", "voiceprint_samples_inconsistent",
        "voiceprint_invalid_audio", "voiceprint_phone_microphone_required",
        "profile_version_conflict",
        "personalization_closed",
        "personalization_step_conflict",
        "authentication_required",
        "invalid_or_expired_token",
        "invalid_credentials",
        "invalid_refresh_token",
        "refresh_token_reused",
        "email_already_registered",
        "too_many_requests",
        "meeting_revision_conflict",
        "meeting_not_found",
        "meeting_deleted",
        "meeting_still_recording",
        "meeting_analysis_busy",
        "meeting_analysis_unavailable",
        "progress_not_found",
        "progress_version_conflict",
        "progress_invalid_transition",
        "knowledge_revision_conflict",
        "knowledge_access_denied",
        "knowledge_immutable_source",
        "knowledge_integrity_failed",
        "knowledge_source_unavailable",
        "action_not_found",
        "action_changed",
        "action_device_mismatch",
        "action_source_changed",
        "action_expired",
        "action_invalid_transition",
        "action_version_conflict",
        "action_mutation_conflict",
        "action_mutation_pending",
        "action_not_authoritative",
        "invalid_calendar_operation",
        "invalid_calendar_time",
        "invalid_calendar_title",
        "invalid_time_zone",
    }
)
HTTP_STATUS_MESSAGES = {404: "not_found", 405: "method_not_allowed"}


async def validation_error_handler(
    _request: Request,
    _error: RequestValidationError,
) -> JSONResponse:
    """参数校验详情可能包含密码，因此不原样返回。"""
    return error_response(422, "invalid_request")


async def http_error_handler(_request: Request, error: HTTPException) -> JSONResponse:
    """统一业务错误及路由错误，过滤未公开的详情。"""
    message = (
        error.detail
        if isinstance(error.detail, str) and error.detail in PUBLIC_HTTP_MESSAGES
        else HTTP_STATUS_MESSAGES.get(error.status_code, "request_failed")
    )
    return error_response(error.status_code, message)


async def database_error_handler(
    _request: Request, _error: SQLAlchemyError
) -> JSONResponse:
    """数据库不可用时返回固定错误，不暴露连接信息或 SQL。"""
    return error_response(503, "database_unavailable")


async def unexpected_error_handler(
    _request: Request, _error: Exception
) -> JSONResponse:
    """未知异常统一返回通用错误。"""
    return error_response(500, "internal_error")


def register_error_handlers(app: FastAPI) -> None:
    """为应用集中安装异常处理器。"""
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(SQLAlchemyError, database_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
