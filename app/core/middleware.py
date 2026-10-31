"""请求追踪、响应安全头与脱敏访问日志。"""
from uuid import uuid4
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from app.core.error_handlers import unexpected_error_handler
from app.core.logging import request_logger


class RequestContextMiddleware(BaseHTTPMiddleware):
    """统一处理成功、业务错误和未知异常的请求上下文。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception as error:
            # 在上下文层接住未知异常，使错误响应也带追踪编号和安全头。
            response = await unexpected_error_handler(request, error)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        request_logger().info(
            "request",
            extra={
                "request_id": request.state.request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
            },
        )
        return response
