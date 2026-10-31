"""访问令牌签发、校验和刷新令牌摘要。"""
from datetime import timedelta
import hashlib
import secrets
import uuid
import jwt
from app.core.config import Settings
from app.core.database.mixins import now


class TokenValidationError(Exception):
    """令牌签名、声明或有效期不合法。"""


def refresh_secret() -> str:
    return secrets.token_urlsafe(48)


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_access(user_id: str, session_id: str, settings: Settings) -> str:
    issued = now()
    return jwt.encode({"sub": user_id, "sid": session_id, "jti": str(uuid.uuid4()),
                       "iat": issued, "nbf": issued,
                       "exp": issued + timedelta(minutes=settings.access_token_minutes),
                       "iss": settings.jwt_issuer, "aud": settings.jwt_audience,
                       "type": "access"}, settings.jwt_secret.get_secret_value(), algorithm="HS256")


def read_access(token: str, settings: Settings) -> dict:
    try:
        claims = jwt.decode(token, settings.jwt_secret.get_secret_value(), algorithms=["HS256"],
                            issuer=settings.jwt_issuer, audience=settings.jwt_audience,
                            options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub", "sid", "jti", "type"]})
        if claims["type"] != "access":
            raise ValueError("wrong token kind")
        uuid.UUID(claims["sub"])
        uuid.UUID(claims["sid"])
        return claims
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as error:
        raise TokenValidationError() from error
