"""统一汇总 V1 业务路由，版本前缀只在这里添加一次。"""

from fastapi import APIRouter
from app.modules.identity.routers.auth_router import router as auth_router
from app.modules.meetings.routers.meeting_router import router as meeting_router
from app.modules.meetings.routers.asr_router import router as asr_router
from app.modules.progress.routers.progress_router import router as progress_router
from app.modules.knowledge.routers.knowledge_router import router as knowledge_router
from app.modules.actions.router import router as action_router
from app.modules.home.routers.home_router import router as home_router
from app.modules.push.routers.push_router import router as push_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth_router)
api_v1_router.include_router(meeting_router)
api_v1_router.include_router(progress_router)
api_v1_router.include_router(knowledge_router)
api_v1_router.include_router(action_router)
api_v1_router.include_router(home_router)

api_v1_router.include_router(asr_router)
api_v1_router.include_router(push_router)
from app.modules.outcomes.router import router as outcomes_router
api_v1_router.include_router(outcomes_router)
from app.modules.execution.router import router as task_router
api_v1_router.include_router(task_router)

from app.modules.search.router import router as search_router
api_v1_router.include_router(search_router)
from app.modules.meetings.routers.audio_router import router as meeting_audio_router
api_v1_router.include_router(meeting_audio_router)

from app.modules.outcomes.questions import router as questions_router
api_v1_router.include_router(questions_router)

from app.modules.voiceprints.router import router as voiceprint_router
api_v1_router.include_router(voiceprint_router)
