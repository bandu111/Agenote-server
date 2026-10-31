"""统一导入业务模型，让迁移工具收集完整表结构。"""

from app.modules.identity.models.identity import (
    AuthSession,
    RefreshToken,
    User,
)  # noqa: F401 导入模型以注册表结构

from app.modules.meetings.models.meeting import Meeting  # noqa: F401 注册会议表
from app.modules.meetings.models.audio import MeetingAudio, MeetingAudioChunk  # noqa: F401
from app.modules.meetings.models.archive import (
    MeetingArchive,
)  # noqa: F401 注册纪要归档表
from app.modules.progress.models.progress_item import (
    ProgressItem,
)  # noqa: F401 注册进展表
from app.modules.knowledge.models.library import KnowledgeLibrary, KnowledgeNode, KnowledgeIndexState  # noqa: F401 注册知识库
from app.modules.knowledge.models.library import DeletedMeetingKnowledge  # noqa: F401
from app.modules.actions.models import ActionRun  # noqa: F401 注册可恢复行动
from app.modules.knowledge.models.assistance import DocumentAnalysis, AssistanceReceipt  # noqa: F401 注册资料辅助流程
from app.modules.followup.models import FollowupRun  # noqa: F401 注册主动跟进任务
from app.modules.push.models import PushDevice, PushDelivery  # noqa: F401 注册推送绑定和投递记录
from app.modules.meetings.models.speaker_context import SpeakerContext  # noqa: F401 注册临时聚类上下文
from app.modules.voiceprints.models import VoiceprintProfile, VoiceprintEnrollment, VoiceprintAudit  # noqa: F401
from app.modules.execution.models import InternalTask, ToolReceipt  # noqa: F401 注册内部任务和工具回执

from app.modules.outcomes.models import OutcomeRun  # noqa: F401

from app.modules.outcomes.wechat_models import WechatBinding  # noqa: F401
