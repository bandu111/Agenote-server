"""字段对应 Flutter 原有模型，不接收用户提交的 owner_id。"""

from datetime import datetime
from typing import Literal
from pydantic import Field, model_validator
from app.modules.meetings.schemas.meeting import StrictModel


class MilestoneInput(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    weight: int = Field(ge=1, le=1000)
    status: Literal["pending", "active", "completed"] = "pending"


class GoalInput(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    condition: str = Field(min_length=1, max_length=1000)
    acceptance: str = Field(default="待明确", max_length=1000)
    phase: str = Field(default="待明确", max_length=100)
    deadline: datetime | None = None
    status: Literal["pending", "active", "completed", "delayed", "paused"] = "pending"
    milestones: list[MilestoneInput] = Field(default_factory=list, max_length=20)
    projectId: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def aware_deadline(self):
        if self.deadline is not None and self.deadline.tzinfo is None:
            raise ValueError("交期必须包含时区")
        if self.status == "completed" and any(
            m.status != "completed" for m in self.milestones
        ):
            raise ValueError("里程碑未完成，不能直接完成目标")
        return self


class GoalUpdate(StrictModel):
    version: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=1000)
    goal: GoalInput


class GoalRemovalInput(StrictModel):
    version: int = Field(ge=1)
    note: str = Field(default="用户移除目标，保留原始记录", min_length=1, max_length=1000)


class CommitmentUpdate(StrictModel):
    version: int = Field(ge=1)
    status: Literal["planned", "active", "claimed", "verified", "withdrawn"]
    note: str = Field(min_length=1, max_length=1000)


class RescheduleInput(StrictModel):
    version: int = Field(ge=1)
    deadline: datetime
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def aware_deadline(self):
        if self.deadline.tzinfo is None:
            raise ValueError("交期必须包含时区")
        return self


class ApprovalInput(StrictModel):
    version: int = Field(ge=1)
    artifactVersion: str = Field(min_length=1, max_length=80)
    note: str = Field(min_length=1, max_length=1000)


class CompletionInput(StrictModel):
    version: int = Field(ge=1)
    confirm: bool = False
    side: Literal["ours", "theirs"] | None = None
    deadline: datetime | None = None
    note: str = Field(default="用户补全履约信息", min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_completion(self):
        if self.side is None and self.deadline is None:
            raise ValueError("至少补充一项")
        if self.deadline is not None and self.deadline.tzinfo is None:
            raise ValueError("交期必须包含时区")
        return self
