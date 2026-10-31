"""应用资源的生命周期管理。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from fastapi import FastAPI


@asynccontextmanager
async def application_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """关闭数据库连接池；表结构仅通过迁移管理，不在启动时建表。"""
    if app.state.settings.app_env != 'test':
        await app.state.voiceprints.prewarm()
    if (
        app.state.settings.app_env != "test"
        and app.state.settings.document_actions_enabled
    ):
        app.state.document_jobs.start()
    if app.state.settings.app_env != "test" and app.state.settings.followup_enabled:
        app.state.followup_jobs.start()
    if app.state.settings.app_env != "test":
        app.state.meeting_analysis.agents.provider.start_stream_workers()
        # 业务提醒统一由 App 本地系统通知展示，不启动 JPush 远程投递。
        app.state.speaker_context_cleanup.start()
        app.state.voiceprints.start()
        app.state.account_checkpoint_cleanup.start()
        app.state.knowledge_index_jobs.start()
        app.state.execution_jobs.start()
        app.state.outcome_jobs.start()
        app.state.wechat_jobs.start()
    try:
        yield
    finally:
        await app.state.voiceprints.close()
        await app.state.wechat_jobs.close()
        await app.state.outcome_jobs.close()
        await app.state.meeting_jobs.close()
        await app.state.meeting_analysis.close()
        await app.state.meeting_analysis.agents.provider.close()
        await app.state.document_jobs.close()
        await app.state.followup_jobs.close()
        await app.state.push_jobs.close()
        await app.state.speaker_context_cleanup.close()
        await app.state.account_checkpoint_cleanup.close()
        await app.state.execution_jobs.close()
        await app.state.knowledge_index_jobs.close()
        await app.state.database.close()
