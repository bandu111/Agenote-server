from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginInput(StrictInput):
    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class RegisterInput(LoginInput):
    name: str = Field(min_length=1, max_length=40)
    password: SecretStr = Field(min_length=8, max_length=128)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("password")
    @classmethod
    def nonblank_password(cls, value):
        if not value.get_secret_value().strip():
            raise ValueError("blank_password")
        return value


class RefreshInput(StrictInput):
    refresh_token: SecretStr = Field(min_length=32, max_length=256)


class UserOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: str
    email_verified: bool
    created_at: datetime


class AuthOutput(BaseModel):
    user: UserOutput
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RecoveryLookupInput(StrictInput):
    email: EmailStr = Field(max_length=254)

    @field_validator('email', mode='before')
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class DemoResetInput(RecoveryLookupInput):
    reset_token: SecretStr = Field(min_length=32, max_length=2048)
    new_password: SecretStr = Field(min_length=8, max_length=128)

    @field_validator('new_password')
    @classmethod
    def valid_password(cls, value):
        if not any(c in '0123456789' for c in value.get_secret_value()):
            raise ValueError('password_requires_digit')
        return value
