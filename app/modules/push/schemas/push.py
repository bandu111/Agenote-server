"""设备绑定输入不包含服务端密钥。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class DeviceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    registrationId: str = Field(min_length=8, max_length=200, pattern=r"^[a-zA-Z0-9_-]+$")
    installationId: str = Field(min_length=20, max_length=200)
    appKey: str = Field(pattern=r"^[a-fA-F0-9]{24}$")
    platform: Literal["android"] = "android"
    enabled: bool = True


