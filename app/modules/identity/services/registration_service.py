"""注册事务：创建用户、创建登录会话并一次性提交。"""
from app.modules.identity.services.personalization_service import initial_personalization
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from app.core.config import Settings
from app.modules.identity.models.identity import User
from app.modules.identity.repositories.identity_repository import IdentityRepository
from app.modules.identity.schemas.auth import AuthOutput
from app.modules.identity.services.auth_service import AuthService
from app.modules.identity.services.password_service import password_hasher


class RegistrationEmailAlreadyExistsError(Exception):
    """标准化后的邮箱已注册。"""


class RegistrationService:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.session = session
        self.identity_repository = IdentityRepository(session=session)
        self.auth_service = AuthService(session=session, settings=settings)

    async def register(self, name: str, email: str, password: str) -> AuthOutput:
        try:
            if await self.identity_repository.by_email(email):
                raise RegistrationEmailAlreadyExistsError()
            encoded = await run_in_threadpool(password_hasher.hash, password)
            user = await self.identity_repository.add(User(name=name, email=email, password_hash=encoded, profile={'personalization': initial_personalization()}))
            result = await self.auth_service.create_session(user)
            await self.session.commit()
            return result
        except IntegrityError as error:
            await self.session.rollback()
            if getattr(getattr(error.orig, "diag", None), "constraint_name", None) == "uq_users_email":
                raise RegistrationEmailAlreadyExistsError() from error
            raise
        except Exception:
            await self.session.rollback()
            raise

