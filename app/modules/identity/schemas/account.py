"""资料与敏感账户操作的输入契约。"""
from pydantic import Field, SecretStr, field_validator
from app.modules.identity.schemas.auth import StrictInput


class ProfileInput(StrictInput):
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=40)
    job: str = Field(default='', max_length=80)
    company: str = Field(default='', max_length=80)
    email: str = Field(default='', max_length=254)
    phone: str = Field(default='', max_length=32)
    avatar: str | None = Field(default=None, max_length=2796204)

    @field_validator('name', 'job', 'company', 'email', 'phone', mode='before')
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('email')
    @classmethod
    def contact_email(cls, value):
        if value:
            from pydantic import TypeAdapter, EmailStr
            TypeAdapter(EmailStr).validate_python(value)
        return value


class PasswordInput(StrictInput):
    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=8, max_length=128)

    @field_validator('new_password')
    @classmethod
    def nonblank(cls, value):
        if not value.get_secret_value().strip():
            raise ValueError('blank_password')
        return value


class DeleteAccountInput(StrictInput):
    current_password: SecretStr = Field(min_length=1, max_length=128)
    confirmation: str = Field(pattern='^DELETE$')
