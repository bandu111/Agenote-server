"""Hackathon recovery: registered-email lookup, no email delivery.

This deliberately does not establish account ownership. No account-ownership verification is performed, by explicit hackathon product
decision. Replace this flow before a public production launch.
"""
from datetime import timedelta
import hashlib
import hmac
import uuid
import jwt
from fastapi import HTTPException
from sqlalchemy import select, update
from starlette.concurrency import run_in_threadpool
from app.core.database.mixins import now
from app.modules.identity.models.identity import User, AuthSession
from app.modules.identity.services.password_service import password_hasher


def fingerprint(password_hash):
    return hashlib.sha256(password_hash.encode()).hexdigest()


class DemoRecoveryService:
    def __init__(self, session, settings):
        self.session, self.settings = session, settings

    async def lookup(self, email):
        user = await self.session.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
        if user is None:
            return {'found': False, 'reset_token': None}
        issued = now()
        token = jwt.encode({'sub': user.id, 'email': user.email, 'purpose': 'demo-password-reset',
            'fingerprint': fingerprint(user.password_hash), 'jti': str(uuid.uuid4()),
            'iat': issued, 'exp': issued + timedelta(minutes=5),
            'iss': self.settings.jwt_issuer, 'aud': 'agenote-demo-recovery'},
            self.settings.jwt_secret.get_secret_value(), algorithm='HS256')
        return {'found': True, 'reset_token': token}

    async def reset(self, payload):
        try:
            claims = jwt.decode(payload.reset_token.get_secret_value(), self.settings.jwt_secret.get_secret_value(),
                algorithms=['HS256'], audience='agenote-demo-recovery', issuer=self.settings.jwt_issuer,
                options={'require': ['sub', 'email', 'purpose', 'fingerprint', 'jti', 'iat', 'exp']})
            if claims['purpose'] != 'demo-password-reset' or claims['email'] != str(payload.email):
                raise ValueError('wrong recovery context')
        except (jwt.PyJWTError, ValueError, TypeError) as error:
            raise HTTPException(403, 'demo_reset_token_invalid') from error
        user = await self.session.scalar(select(User).where(User.id == claims['sub'], User.email == str(payload.email),
            User.is_active.is_(True)).with_for_update())
        if user is None or not hmac.compare_digest(fingerprint(user.password_hash), claims['fingerprint']):
            raise HTTPException(403, 'demo_reset_token_invalid')
        user.password_hash = await run_in_threadpool(password_hasher.hash, payload.new_password.get_secret_value())
        await self.session.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(revoked_at=now()))
        await self.session.commit()
