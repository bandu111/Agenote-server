"""角色各自的输出契约；格式重试使用同一 Schema，不再漏掉 goals。"""

from pydantic import Field, model_validator
from app.modules.meetings.schemas.meeting import (
    StrictModel,
    GoalFact,
    StateFact,
    Insight,
    Repair,
    Withdrawal,
    AgentReport,
)


class RoleOutput(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def discard_empty_legacy_fields(cls, value):
        # 旧模型的空列表没有业务含义；职责外的非空字段仍然拒绝。
        legacy = {"goals", "state", "insights", "withdrawals", "repairs", "commitments", "questions"}
        return (
            {
                k: v
                for k, v in value.items()
                if k in cls.model_fields or k not in legacy or v != []
            }
            if isinstance(value, dict)
            else value
        )


class StateOutput(RoleOutput):
    goals: list[GoalFact] = Field(default_factory=list, max_length=20)
    state: list[StateFact] = Field(default_factory=list, max_length=30)
    insights: list[Insight] = Field(default_factory=list, max_length=8)
    withdrawals: list[Withdrawal] = Field(default_factory=list, max_length=8)


class MemoryOutput(RoleOutput):
    insights: list[Insight] = Field(default_factory=list, max_length=8)
    repairs: list[Repair] = Field(default_factory=list, max_length=8)


class InsightOutput(RoleOutput):
    insights: list[Insight] = Field(default_factory=list, max_length=8)


class CandidateReview(StrictModel):
    id: str = Field(max_length=120)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    accepted: bool
    reason: str = Field(default="", max_length=250)


class VerifyOutput(AgentReport):
    reviews: list[CandidateReview] = Field(default_factory=list, max_length=60)


class BusinessReviewOutput(StrictModel):
    reviews: list[CandidateReview] = Field(default_factory=list, max_length=1)


ROLE_SCHEMAS = {
    "state": StateOutput,
    "memory": MemoryOutput,
    "forecast": InsightOutput,
    "risk": InsightOutput,
    "verify": VerifyOutput,
}
