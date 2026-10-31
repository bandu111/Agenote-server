"""言枢应用入口：组装配置、公共组件和路由，提供本地启动入口。"""

# 同时支持 IDE 直接运行和模块启动。
if __package__ in (None, ""):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.identity.services.account_cleanup import AccountCheckpointCleanup
from app.modules.execution.jobs import ExecutionJobs

import uvicorn
from fastapi import FastAPI
from app.api.health import router as health_router
from app.api.v1.router import api_v1_router
from app.core.config import Settings, get_settings, settings
from app.core.database.session import Database, database as default_database
from app.core.error_handlers import register_error_handlers
from app.core.lifespan import application_lifespan
from app.core.middleware import RequestContextMiddleware
from app.core.rate_limit import AuthRateLimiter
from app.modules.meetings.services.meeting_jobs import MeetingJobs
from app.modules.meetings.services.meeting_agents import MeetingAgents
from app.modules.meetings.services.meeting_analysis_service import (
    MeetingAnalysisCoordinator,
)
from app.integrations.hermes.provider import HermesProvider
from app.modules.knowledge.services.document_actions import DocumentJobs
from app.modules.followup.jobs import FollowupJobs
from app.modules.push.services.push_jobs import PushJobs
from app.modules.meetings.services.speaker_context import SpeakerContextCleanup
from app.modules.knowledge.services.index_jobs import KnowledgeIndexJobs
from collections import OrderedDict
import asyncio


def create_app(settings: Settings | None = None) -> FastAPI:
    """组装默认应用；测试可注入独立配置和数据库。"""
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_title,
        version=settings.app_version,
        description="言枢账户与会议服务：账户会话、会议同步、多 Agent 分析与纪要归档。",
        docs_url="/docs" if settings.app_env != "production" else None,
        openapi_url="/openapi.json" if settings.app_env != "production" else None,
        redoc_url=None,
        lifespan=application_lifespan,
    )
    app.state.settings = settings
    from app.modules.voiceprints.service import VoiceprintJobs
    app.state.highlight_slots = asyncio.Semaphore(2)
    app.state.highlight_metrics = OrderedDict()
    app.state.database = (
        default_database if settings is get_settings() else Database(settings)
    )
    app.state.auth_limiter = AuthRateLimiter()
    app.state.voiceprints = VoiceprintJobs(app.state.database, settings)
    app.state.meeting_analysis = MeetingAnalysisCoordinator(
        MeetingAgents(HermesProvider(settings)), app.state.database
    )
    app.state.meeting_jobs = MeetingJobs(app.state.database, app.state.meeting_analysis)
    app.state.document_jobs = DocumentJobs(app.state.database, HermesProvider(settings))
    app.state.followup_jobs = FollowupJobs(app.state.database, HermesProvider(settings))
    app.state.push_jobs = PushJobs(app.state.database, settings)
    app.state.meeting_analysis.on_actions_published = app.state.push_jobs.wake
    app.state.account_checkpoint_cleanup = AccountCheckpointCleanup(app.state.database)
    app.state.speaker_context_cleanup = SpeakerContextCleanup(app.state.database)
    app.state.knowledge_index_jobs = KnowledgeIndexJobs(app.state.database)
    app.state.execution_jobs = ExecutionJobs(app.state.database, settings)

    from app.modules.outcomes.jobs import OutcomeJobs
    app.state.outcome_jobs = OutcomeJobs(
        app.state.database, app.state.meeting_analysis.agents.provider, settings
    )
    from app.modules.outcomes.weixin_transport import WeixinTransport
    from app.modules.outcomes.wechat_jobs import WechatJobs
    app.state.wechat_transport = WeixinTransport()
    app.state.wechat_jobs = WechatJobs(app.state.database, settings, app.state.wechat_transport)
    register_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    app.include_router(api_v1_router)
    return app


app = create_app(settings)


if __name__ == "__main__":
    uvicorn.run(
        app,
        # 监听地址和整数端口由 Settings 从 .env 读取。
        host=settings.host,
        port=settings.port,
        # Windows 下 Psycopg 异步连接需要兼容的事件循环。
        loop="app.core.event_loop:psycopg_compatible_loop_factory",
        proxy_headers=False,
        access_log=False,
    )
