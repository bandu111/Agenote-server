"""从请求凭据解析并校验当前用户与登录会话。"""
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.core.database.session import get_db
from app.modules.identity.services.auth_service import AuthService, AuthSessionInvalidError
from app.modules.identity.services.token_service import read_access, TokenValidationError

bearer = HTTPBearer(auto_error=False)


def get_auth_service(request: Request, session: Annotated[AsyncSession, Depends(get_db)]) -> AuthService:
    return AuthService(session=session, settings=request.app.state.settings)


async def get_current_auth_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    service: AuthService = Depends(get_auth_service),
):
    if credentials is None:
        raise HTTPException(status_code=401, detail="authentication_required")
    try:
        claims = read_access(credentials.credentials, request.app.state.settings)
        return await service.authenticate(claims["sub"], claims["sid"])
    except (TokenValidationError, AuthSessionInvalidError) as error:
        raise HTTPException(status_code=401, detail="invalid_or_expired_token") from error
