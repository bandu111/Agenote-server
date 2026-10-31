from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from app.core.config import Settings
from app.core.database.mixins import now
from app.modules.identity.services.password_service import dummy_hash, password_hasher
from app.modules.identity.services.token_service import issue_access, refresh_secret, token_digest
from app.modules.identity.models.identity import AuthSession, RefreshToken, User
from app.modules.identity.repositories.identity_repository import IdentityRepository
from app.modules.identity.schemas.auth import AuthOutput, UserOutput


class AuthInvalidCredentialsError(Exception):
    """邮箱、密码错误或账户不可用。"""

class AuthSessionInvalidError(Exception):
    """会话无效、过期或已撤销。"""

class AuthRefreshTokenInvalidError(Exception):
    """刷新令牌无效或已过期。"""

class AuthRefreshTokenReusedError(Exception):
    """已使用的刷新令牌被重放，会话已撤销。"""


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.session, self.settings = session, settings
        self.identity_repository = IdentityRepository(session=session)

    async def _issue(self, user: User, session: AuthSession) -> AuthOutput:
        raw = refresh_secret()
        await self.identity_repository.add(RefreshToken(session_id=session.id, token_hash=token_digest(raw),
                                         expires_at=session.expires_at))
        return AuthOutput(user=UserOutput.model_validate(user),
                          access_token=issue_access(user.id, session.id, self.settings),
                          refresh_token=raw, expires_in=self.settings.access_token_minutes * 60)

    async def create_session(self, user: User):
        session = await self.identity_repository.add(AuthSession(user_id=user.id,
                          expires_at=now() + timedelta(days=self.settings.refresh_token_days)))
        return await self._issue(user, session)

    async def login(self, email: str, password: str) -> AuthOutput:
        try:
            user = await self.identity_repository.by_email(email)
            if user:
                user = await self.session.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
            valid = await run_in_threadpool(password_hasher.verify, password,
                                             user.password_hash if user else dummy_hash)
            if not user or not valid or not user.is_active:
                raise AuthInvalidCredentialsError()
            result = await self.create_session(user)
            await self.session.commit()
            return result
        except Exception:
            await self.session.rollback()
            raise

    async def authenticate(self, user_id: str, session_id: str):
        session = await self.identity_repository.get_session(session_id)
        user = await self.identity_repository.by_id(user_id)
        if (not user or not user.is_active or not session or session.user_id != user_id
                or session.revoked_at or session.expires_at <= now()):
            raise AuthSessionInvalidError()
        return user, session

    async def refresh(self, raw: str) -> AuthOutput:
        try:
            token = await self.identity_repository.get_refresh(token_digest(raw))
            if not token:
                raise AuthRefreshTokenInvalidError()
            snapshot = await self.identity_repository.get_session(token.session_id)
            if not snapshot:
                raise AuthRefreshTokenInvalidError()
            await self.session.scalar(select(User).where(User.id == snapshot.user_id).with_for_update().execution_options(populate_existing=True))
            # 同一会话先加行锁，再重读 token，序列化并发刷新与退出。
            session = await self.identity_repository.get_session(token.session_id, lock=True)
            token = await self.identity_repository.get_refresh(token_digest(raw))
            if not session or session.revoked_at or session.expires_at <= now():
                raise AuthRefreshTokenInvalidError()
            if token.used_at:
                # 重放已用令牌：撤销整个会话，包括此前签发的 access token。
                session.revoked_at = now()
                await self.session.commit()
                raise AuthRefreshTokenReusedError()
            if token.expires_at <= now():
                raise AuthRefreshTokenInvalidError()
            user = await self.identity_repository.by_id(session.user_id)
            if not user or not user.is_active:
                raise AuthRefreshTokenInvalidError()
            token.used_at = now()
            result = await self._issue(user, session)
            await self.session.commit()
            return result
        except Exception:
            await self.session.rollback()
            raise

    async def logout(self, session_id: str):
        try:
            session = await self.identity_repository.get_session(session_id, lock=True)
            if session and not session.revoked_at:
                session.revoked_at = now()
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
