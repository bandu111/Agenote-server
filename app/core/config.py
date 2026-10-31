from pathlib import Path
from functools import lru_cache
from typing import Literal
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", populate_by_name=True
    )
    app_env: Literal["development", "test", "production"] = "development"
    app_title: str = "YanShu Backend API"
    app_version: str = "0.1.0"
    host: str = Field("127.0.0.1", validation_alias=AliasChoices("HOST", "SERVER_HOST"))
    port: int = Field(
        8000, ge=1, le=65535, validation_alias=AliasChoices("PORT", "SERVER_PORT")
    )
    deepseek_api_key: SecretStr | None = None
    deepseek_model: str = "deepseek-flash"
    dashscope_api_key: SecretStr | None = None
    wechat_binding_key: SecretStr | None = None
    qwen_model: str = "qwen-flash"
    qwen_base_url: str = "https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1"
    qwen_workers_enabled: bool = False
    transcript_highlights_enabled: bool = True
    transcript_highlight_timeout_seconds: float = Field(2.5, ge=.1, le=10)
    minimax_api_key: SecretStr | None = None
    minimax_model: str = "MiniMax-M3.1-Flash"
    minimax_workers_enabled: bool = False
    minimax_realtime_enabled: bool = True
    hermes_review_reasoning_effort: Literal['none', 'low', 'medium', 'high'] = 'none'
    minimax_base_url: Literal["https://api.minimax.io/v1", "https://api.minimaxi.com/v1"] = "https://api.minimax.io/v1"
    hermes_runtime_path: Path = Path("E:/PythonProject/hermes-agent")
    hermes_timeout_seconds: int = Field(90, ge=10, le=180)
    document_actions_enabled: bool = True
    followup_enabled: bool = True
    meeting_preparation_enabled: bool = False
    meeting_preparation_recovery_minutes: int = Field(15, ge=1, le=120)
    jpush_app_key: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{24}$")
    jpush_master_secret: SecretStr | None = None
    qmd_url: str | None = None
    qmd_timeout_seconds: int = Field(25, ge=1, le=90)
    tencent_asr_app_id: str | None = None
    tencent_asr_hotword_id: str | None = None
    tencent_asr_hotword_list: str | None = None
    tencent_asr_customization_id: str | None = None
    tencent_asr_secret_id: SecretStr | None = None
    tencent_asr_secret_key: SecretStr | None = None
    tencent_asr_engine_model_type: Literal["16k_zh_en_2.0", "16k_zh_en_speaker_2.0"] = "16k_zh_en_2.0"
    asr_max_stream_seconds: int = Field(7200, ge=60, le=14400)
    api_v1_prefix: str = "/api/v1"
    database_url: SecretStr
    jwt_secret: SecretStr
    jwt_issuer: str = "yanshu-server"
    jwt_audience: str = "yanshu-mobile"
    access_token_minutes: int = Field(15, ge=1, le=60)
    refresh_token_days: int = Field(30, ge=1, le=90)
    auth_requests_per_minute: int = Field(20, ge=1, le=1000)
    voiceprint_key: SecretStr | None = None
    voiceprint_key_id: str = 'local-v1'
    voiceprint_model_path: Path = ROOT / 'storage' / 'voiceprint-model'
    voiceprint_model_version: str = 'ecapa-0f99f2d0ebe89ac095bcc5903c4dd8f72b367286-vad-v1'
    # Initial engineering thresholds, not calibrated meeting accuracy guarantees.
    voiceprint_consistency_threshold: float = Field(.40, ge=0, le=1)
    voiceprint_verification_threshold: float = Field(.65, ge=0, le=1)
    voiceprint_live_enabled: bool = True
    voiceprint_prewarm_enabled: bool = True
    voiceprint_live_self_threshold: float = Field(.65, ge=.01, le=1)
    voiceprint_live_other_threshold: float = Field(.35, ge=-1, le=.64)

    @field_validator("jwt_secret")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32 or "REPLACE" in value.get_secret_value():
            raise ValueError(
                "JWT signing secret must be configured with at least 32 random characters"
            )
        return value

    @field_validator("qmd_url")
    @classmethod
    def qmd_loopback_only(cls, value):
        if value:
            from urllib.parse import urlsplit
            parsed = urlsplit(value)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
                raise ValueError("QMD must use a private loopback endpoint")
        return value

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("Use an explicit PostgreSQL psycopg async URL")
        return value

    @field_validator("api_v1_prefix")
    @classmethod
    def prefix(cls, value: str) -> str:
        if value != "/api/v1":
            raise ValueError("This API contract uses /api/v1")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


# 全局配置实例；仅此处读取默认环境配置。
settings = get_settings()
