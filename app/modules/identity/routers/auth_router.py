"""账户接口只负责请求校验、调用服务与业务错误的 HTTP 映射。"""
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends, HTTPException, Request
from app.core.database.session import get_db
from app.core.rate_limit import limit_auth
from app.core.responses import ApiResponse
from app.modules.identity.dependencies.auth import get_auth_service, get_current_auth_context
from app.modules.identity.schemas.auth import AuthOutput, LoginInput, RefreshInput, RegisterInput, UserOutput
from app.modules.identity.services.auth_service import (
    AuthService, AuthInvalidCredentialsError, AuthRefreshTokenInvalidError, AuthRefreshTokenReusedError,
)
from app.modules.identity.services.registration_service import RegistrationService, RegistrationEmailAlreadyExistsError

from app.modules.identity.schemas.account import ProfileInput, PasswordInput, DeleteAccountInput
from app.modules.identity.services.account_service import AccountService

router = APIRouter(prefix="/auth", tags=["Auth"])
auth_errors = {code: {"model": ApiResponse[None]} for code in [401, 409, 422, 429, 500, 503]}


@router.post("/register", response_model=ApiResponse[AuthOutput], status_code=201,
             dependencies=[Depends(limit_auth)], responses=auth_errors)
async def register(payload: RegisterInput, request: Request, session: Annotated[AsyncSession, Depends(get_db)]):
    service = RegistrationService(session=session, settings=request.app.state.settings)
    try:
        result = await service.register(payload.name, str(payload.email), payload.password.get_secret_value())
    except RegistrationEmailAlreadyExistsError as error:
        raise HTTPException(status_code=409, detail="email_already_registered") from error
    return ApiResponse(code=201, data=result)


@router.post("/login", response_model=ApiResponse[AuthOutput],
             dependencies=[Depends(limit_auth)], responses=auth_errors)
async def login(payload: LoginInput, service: AuthService = Depends(get_auth_service)):
    try:
        result = await service.login(str(payload.email), payload.password.get_secret_value())
    except AuthInvalidCredentialsError as error:
        raise HTTPException(status_code=401, detail="invalid_credentials") from error
    return ApiResponse(code=200, data=result)


@router.post("/refresh", response_model=ApiResponse[AuthOutput],
             dependencies=[Depends(limit_auth)], responses=auth_errors)
async def refresh(payload: RefreshInput, service: AuthService = Depends(get_auth_service)):
    try:
        result = await service.refresh(payload.refresh_token.get_secret_value())
    except AuthRefreshTokenReusedError as error:
        raise HTTPException(status_code=401, detail="refresh_token_reused") from error
    except AuthRefreshTokenInvalidError as error:
        raise HTTPException(status_code=401, detail="invalid_refresh_token") from error
    return ApiResponse(code=200, data=result)


@router.post("/logout", response_model=ApiResponse[None], responses=auth_errors)
async def logout(identity=Depends(get_current_auth_context), service: AuthService = Depends(get_auth_service)):
    _, session = identity
    await service.logout(session.id)
    return ApiResponse(code=200, data=None)


@router.get("/me", response_model=ApiResponse[UserOutput], responses=auth_errors)
async def me(identity=Depends(get_current_auth_context)):
    user, _ = identity
    return ApiResponse(code=200, data=UserOutput.model_validate(user))



@router.get('/profile', response_model=ApiResponse[dict], responses=auth_errors)
async def profile(identity=Depends(get_current_auth_context)):
    return ApiResponse(code=200, data=AccountService.output(identity[0]))


@router.post('/profile', response_model=ApiResponse[dict], responses=auth_errors)
async def save_profile(payload: ProfileInput, session: Annotated[AsyncSession, Depends(get_db)], identity=Depends(get_current_auth_context)):
    result = await AccountService(session).save_profile(identity[0].id, identity[1].id, payload)
    return ApiResponse(code=200, data=result)


@router.post('/password', response_model=ApiResponse[None], dependencies=[Depends(limit_auth)], responses=auth_errors)
async def change_password(payload: PasswordInput, session: Annotated[AsyncSession, Depends(get_db)], identity=Depends(get_current_auth_context)):
    await AccountService(session).change_password(identity[0].id, identity[1].id, payload)
    return ApiResponse(code=200, data=None)


@router.post('/delete-account', response_model=ApiResponse[None], dependencies=[Depends(limit_auth)], responses=auth_errors)
async def delete_account(payload: DeleteAccountInput, session: Annotated[AsyncSession, Depends(get_db)], identity=Depends(get_current_auth_context)):
    await AccountService(session).delete_account(identity[0].id, identity[1].id, payload)
    return ApiResponse(code=200, data=None)


# Hackathon recovery is separate from authenticated password changes.
from app.modules.identity.schemas.auth import RecoveryLookupInput, DemoResetInput
from app.modules.identity.services.demo_recovery import DemoRecoveryService


@router.post('/recovery/lookup', response_model=ApiResponse[dict], dependencies=[Depends(limit_auth)], responses=auth_errors)
async def recovery_lookup(payload: RecoveryLookupInput, request: Request, session: Annotated[AsyncSession, Depends(get_db)]):
    data = await DemoRecoveryService(session, request.app.state.settings).lookup(str(payload.email))
    return ApiResponse(code=200, data=data)


@router.post('/recovery/reset', response_model=ApiResponse[None], dependencies=[Depends(limit_auth)], responses=auth_errors)
async def recovery_reset_demo(payload: DemoResetInput, request: Request, session: Annotated[AsyncSession, Depends(get_db)]):
    await DemoRecoveryService(session, request.app.state.settings).reset(payload)
    return ApiResponse(code=200, data=None)


from app.modules.identity.schemas.personalization import PersonalizationInput
from app.modules.identity.services.personalization_service import PersonalizationService


@router.post('/personalization', response_model=ApiResponse[dict], responses=auth_errors)
async def save_personalization(payload: PersonalizationInput, session: Annotated[AsyncSession, Depends(get_db)], identity=Depends(get_current_auth_context)):
    data = await PersonalizationService(session).save(identity[0].id, identity[1].id, payload)
    return ApiResponse(code=200, data=data)
