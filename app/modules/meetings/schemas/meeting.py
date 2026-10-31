"""会议同步契约：不接受原始音频、设备文件路径或任意额外字段。"""

from datetime import datetime
from typing import Literal
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpeakerAttribution(StrictModel):
    revision: int = Field(ge=0)
    speaker: str = Field(max_length=100)
    speaker_id: str = Field(validation_alias=AliasChoices("speakerId", "speaker_id"), max_length=100)
    speaker_separated: bool = Field(validation_alias=AliasChoices("speakerSeparated", "speaker_separated"))


class Transcript(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    speaker: str = Field(max_length=100)
    speaker_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("speakerId", "speaker_id"),
        max_length=100,
    )
    speaker_separated: bool = Field(
        default=False,
        validation_alias=AliasChoices("speakerSeparated", "speaker_separated"),
    )
    speaker_revision: int = Field(default=0, ge=0, le=1000, validation_alias=AliasChoices("speakerRevision", "speaker_revision"))
    speaker_history: list[SpeakerAttribution] = Field(default_factory=list, max_length=32, validation_alias=AliasChoices("speakerHistory", "speaker_history"))
    raw: str = Field(max_length=12000)
    turn_status: Literal['pending', 'complete', 'interrupted'] = Field(default='complete', validation_alias=AliasChoices('turnStatus', 'turn_status'))
    turn_id: str | None = Field(default=None, max_length=100, validation_alias=AliasChoices('turnId', 'turn_id'))
    cleaned: str = Field(default="", max_length=12000)
    correction: str | None = Field(default=None, max_length=12000)
    at: int = Field(ge=0)
    withdrawn: bool = False
    manual: bool = False


class MeetingSync(StrictModel):
    capabilities: list[Literal['tool_plans_v1', 'turns_v1']] = Field(default_factory=list, max_length=2)
    client_id: str = Field(
        validation_alias=AliasChoices("id", "client_id"),
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )
    revision: int = Field(ge=0)
    title: str = Field(default="录音会议", max_length=200)
    started_at: datetime = Field(
        validation_alias=AliasChoices("startedAt", "started_at")
    )
    elapsed_ms: int = Field(
        validation_alias=AliasChoices("elapsed", "elapsed_ms"), ge=0
    )
    ended: bool = False
    paused: bool = False
    utterances: list[Transcript] = Field(default_factory=list, max_length=20000)
    minutes: str = Field(default="", max_length=300000)

    @model_validator(mode="after")
    def validate_state(self):
        if sum(len(u.raw) + len(u.cleaned) + len(u.correction or '') for u in self.utterances) > 3000000:
            raise ValueError('会议转写内容超过单场容量')
        if self.ended and self.paused:
            raise ValueError("结束状态不能同时暂停")
        if self.started_at.tzinfo is None:
            raise ValueError("会议开始时间必须包含时区")
        return self


class MeetingDelta(MeetingSync):
    base_revision: int = Field(ge=0, validation_alias=AliasChoices('baseRevision','base_revision'))


class LiveHintsInput(StrictModel):
    """只分析当前账户提交的有限文本，不读取其他会话，不接收音频。"""
    output_protocol: Literal['json_v1', 'text_v2'] = 'text_v2'
    utterances: list[Transcript] = Field(min_length=1, max_length=40)
    include_model: bool = True
    meeting_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode='after')
    def bounded_sources(self):
        if len({u.id for u in self.utterances}) != len(self.utterances):
            raise ValueError('片段ID不能重复')
        if sum(len(u.raw) + len(u.correction or '') for u in self.utterances) > 12000:
            raise ValueError('会中提示文本过长')
        if any(u.turn_status == 'pending' for u in self.utterances):
            raise ValueError('临时转写不能触发推演')
        return self


class RealtimeBusinessInput(StrictModel):
    revision: int = Field(ge=0)


class Insight(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    kind: Literal["question", "risk", "prediction", "stitch"]
    title: str = Field(max_length=100)
    body: str = Field(max_length=1400)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    inference: bool = True
    topic_key: str = Field(default='', max_length=100)
    issue_key: str = Field(default='', max_length=100)
    attention: Literal['defer', 'normal', 'urgent'] = 'normal'
    impact: str = Field(default='', max_length=160)
    knowledge_node_ids: list[str] = Field(default_factory=list, max_length=6)


class Repair(StrictModel):
    target_id: str = Field(max_length=100)
    text: str = Field(max_length=250)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class Withdrawal(StrictModel):
    target_id: str = Field(max_length=100)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    reason: str = Field(max_length=250)


class StateFact(StrictModel):
    topic: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=600)
    status: Literal["proposed", "confirmed", "unresolved", "withdrawn"]
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class GoalFact(StrictModel):
    origin_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    condition: str = Field(min_length=1, max_length=1000)
    acceptance: str | None = Field(default=None, max_length=1000)
    phase: str | None = Field(default=None, max_length=100)
    deadline_text: str | None = Field(default=None, max_length=120)
    milestones: list[str] = Field(default_factory=list, max_length=20)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class AgentReport(StrictModel):
    goals: list[GoalFact] = Field(default_factory=list, max_length=20)
    state: list[StateFact] = Field(default_factory=list, max_length=30)
    insights: list[Insight] = Field(default_factory=list, max_length=12)
    repairs: list[Repair] = Field(default_factory=list, max_length=8)
    withdrawals: list[Withdrawal] = Field(default_factory=list, max_length=8)


class MinuteItem(StrictModel):
    text: str = Field(min_length=1, max_length=700)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class MinuteTopic(MinuteItem):
    title: str = Field(min_length=1, max_length=100)
    points: list[str] = Field(default_factory=list, max_length=6)


class MinuteDecision(MinuteItem):
    status: Literal['confirmed', 'discussed', 'pending'] = 'confirmed'


class MinuteAction(MinuteItem):
    owner: str | None = Field(default=None, max_length=80)
    deadline: str | None = Field(default=None, max_length=120)


class MinutesDraft(StrictModel):
    summary: str = Field(default='', max_length=1600)
    topics: list[MinuteTopic] = Field(default_factory=list, max_length=12)
    decisions: list[MinuteDecision] = Field(default_factory=list, max_length=20)
    actions: list[MinuteAction] = Field(default_factory=list, max_length=20)
    open_questions: list[MinuteItem] = Field(default_factory=list, max_length=20)


class MeetingJobInput(StrictModel):
    operation: Literal["analysis", "archive"] = "analysis"


class PolishInput(StrictModel):
    meeting_id: str = Field(
        validation_alias=AliasChoices("meetingId", "meeting_id"),
        min_length=1,
        max_length=100,
    )
    id: str = Field(min_length=1, max_length=100)
    raw: str = Field(min_length=1, max_length=4000)


class PolishDraft(StrictModel):
    cleaned: str = Field(min_length=1, max_length=5000)
