"""账户资料、密码和注销事务；账户身份仅取自服务端认证上下文。"""
import base64
import binascii
from io import BytesIO
from PIL import Image
from sqlalchemy import select, update, delete, text
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from app.core.database.mixins import now
from app.modules.identity.models.identity import User, AuthSession
from app.modules.identity.services.password_service import password_hasher


def normalize_avatar(value):
    if value is None:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
        if not raw or len(raw) > 2 * 1024 * 1024:
            raise ValueError()
        with Image.open(BytesIO(raw)) as image:
            if image.format != 'PNG' or image.width > 2048 or image.height > 2048:
                raise ValueError()
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            image.thumbnail((512, 512))
            output = BytesIO()
            image.convert('RGBA').save(output, format='PNG')
        return base64.b64encode(output.getvalue()).decode()
    except (ValueError, binascii.Error, OSError, Image.DecompressionBombError) as error:
        raise HTTPException(422, 'invalid_avatar') from error


class AccountService:
    def __init__(self, session):
        self.session = session

    async def locked_user(self, owner, session_id):
        # 登录、刷新、改密与注销按用户行锁串行；认证后仍复核会话撤销状态。
        user = await self.session.scalar(select(User).where(User.id == owner).with_for_update().execution_options(populate_existing=True))
        auth = await self.session.get(AuthSession, session_id, populate_existing=True)
        if not user or not user.is_active or not auth or auth.user_id != owner or auth.revoked_at or auth.expires_at <= now():
            raise HTTPException(401, 'invalid_or_expired_token')
        return user

    @staticmethod
    def output(user):
        from app.modules.identity.services.professional_profile import professional_profile
        profile = professional_profile(user.profile)
        return dict(id=user.id, version=user.profile_version, name=user.name,
                    job=profile.get('job', ''), company=profile.get('company', ''),
                    email=profile.get('email', user.email), phone=profile.get('phone', ''),
                    avatar=profile.get('avatar'), loginEmail=user.email,
                    personalization=profile.get('personalization'))

    async def save_profile(self, owner, session_id, payload):
        avatar = await run_in_threadpool(normalize_avatar, payload.avatar)
        user = await self.locked_user(owner, session_id)
        if payload.version != user.profile_version:
            raise HTTPException(409, 'profile_version_conflict')
        user.name = payload.name
        user.profile = {**(user.profile or {}), **payload.model_dump(exclude={'name', 'version', 'avatar'}), 'avatar': avatar}
        from app.modules.identity.services.professional_profile import professional_profile
        user.profile = professional_profile({**user.profile, 'job_source': 'profile'})
        user.profile_version += 1
        await self.session.commit()
        return self.output(user)

    async def verify_password(self, user, password):
        if not await run_in_threadpool(password_hasher.verify, password, user.password_hash):
            # 用 403 区分口令错误与登录失效，避免客户端错误地触发令牌刷新。
            raise HTTPException(403, 'current_password_incorrect')

    async def change_password(self, owner, session_id, payload):
        user = await self.locked_user(owner, session_id)
        current, new = payload.current_password.get_secret_value(), payload.new_password.get_secret_value()
        await self.verify_password(user, current)
        if current == new:
            raise HTTPException(422, 'password_unchanged')
        user.password_hash = await run_in_threadpool(password_hasher.hash, new)
        await self.session.execute(update(AuthSession).where(AuthSession.user_id == owner).values(revoked_at=now()))
        await self.session.commit()

    async def delete_account(self, owner, session_id, payload):
        user = await self.locked_user(owner, session_id)
        await self.verify_password(user, payload.current_password.get_secret_value())
        # 业务数据、会话及推送绑定依外键级联；图检查点没有外键，显式清理。
        for table in ('checkpoint_writes', 'checkpoint_blobs', 'checkpoints'):
            if await self.session.scalar(text('SELECT to_regclass(:name)'), {'name': table}):
                await self.session.execute(text(f'DELETE FROM {table} WHERE thread_id LIKE :plain OR thread_id LIKE :tools'),
                                           {'plain': owner + ':%', 'tools': 'tools:' + owner + ':%'})
        await self.session.execute(delete(User).where(User.id == user.id))
        await self.session.commit()
