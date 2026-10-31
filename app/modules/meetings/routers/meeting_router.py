"""会议接口适配现有 Flutter 原型，仅同步文本与纪要。"""

from typing import Annotated
import hashlib
import json
from fastapi.responses import StreamingResponse
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database.session import get_db
from app.core.responses import ApiResponse
from app.modules.identity.dependencies.auth import get_current_auth_context
from app.modules.meetings.schemas.meeting import (
    MeetingSync,
    MeetingJobInput,
    PolishInput,
    LiveHintsInput,
    RealtimeBusinessInput,
)
from app.modules.meetings.services.meeting_service import (
    MeetingService,
    MeetingConflictError,
    MeetingNotFoundError,
)
from app.modules.meetings.services.meeting_analysis_service import (
    MeetingAnalysisBusyError,
)
from app.modules.meetings.services.meeting_archive_service import MeetingArchiveService
from app.integrations.hermes.provider import HermesUnavailableError
from app.modules.meetings.services.turn_end_service import TurnEndInput, TurnEndService

router = APIRouter(prefix="/meetings", tags=["Meetings"])
Session = Annotated[AsyncSession, Depends(get_db)]
Identity = Annotated[tuple, Depends(get_current_auth_context)]


@router.get('/{client_id}/speaker-identities', response_model=ApiResponse[dict])
async def speaker_identities(client_id: str, session: Session, identity: Identity):
    from app.modules.meetings.repositories.meeting_repository import MeetingRepository
    from app.modules.meetings.services.speaker_identity import identity_output
    meeting = await MeetingRepository(session).get(identity[0].id, client_id)
    if meeting is None:
        raise HTTPException(404, 'meeting_not_found')
    return ApiResponse(code=200, data=identity_output(meeting))

from pydantic import BaseModel, Field, ConfigDict


class DeleteMeetingInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)


@router.get('/deleted', response_model=ApiResponse[list[str]])
async def deleted_meetings(session: Session, identity: Identity):
    from sqlalchemy import select
    from app.modules.meetings.models.meeting import Meeting
    ids = await session.scalars(select(Meeting.client_id).where(Meeting.owner_id == identity[0].id, Meeting.deleted.is_(True)))
    return ApiResponse(code=200, data=list(ids))


@router.delete('/{client_id}', response_model=ApiResponse[dict])
async def remove_meeting(client_id: str, payload: DeleteMeetingInput, request: Request, session: Session, identity: Identity):
    from app.modules.meetings.services.meeting_deletion import delete_meeting
    result = await delete_meeting(session, identity[0].id, client_id, payload.revision)
    request.app.state.meeting_jobs.remove_meeting(identity[0].id, client_id)
    if request.app.state.meeting_analysis.on_actions_published:
        request.app.state.meeting_analysis.on_actions_published()
    return ApiResponse(code=200, data=result)

from app.modules.meetings.services.segment_quality import SegmentInput, classify_segment


@router.post('/segment-quality')
async def segment_quality(payload: SegmentInput, request: Request, identity: Identity):
    """只读分类，满载直接跳过，不堵住 ASR 或正式分析通道。"""
    import asyncio
    from app.core.config import settings
    if not hasattr(request.app.state, 'segment_quality_slots'):
        request.app.state.segment_quality_slots = asyncio.Semaphore(2)
    slots = request.app.state.segment_quality_slots
    if slots.locked():
        return ApiResponse(code=200, data={'available': False})
    async with slots:
        try:
            result = await classify_segment(settings, payload.text)
        except Exception:
            result = {'available': False}
    return ApiResponse(code=200, data=result)


@router.post('/{client_id}/realtime')
async def realtime_business(client_id: str, payload: RealtimeBusinessInput, request: Request, session: Session, identity: Identity):
    """完整转写到达后独立更新业务，不排在上一轮完整分析或问卷之后。"""
    import asyncio
    from contextlib import aclosing
    from app.modules.meetings.repositories.meeting_repository import MeetingRepository
    from app.modules.meetings.services.realtime_business import run_realtime
    owner = identity[0].id
    row = await MeetingRepository(session).get(owner, client_id)
    if row is None: raise HTTPException(404, 'meeting_not_found')
    if row.revision != payload.revision: raise HTTPException(409, 'meeting_revision_conflict')
    await session.rollback()
    app = request.app
    if not hasattr(app.state, 'realtime_business_owners'):
        app.state.realtime_business_owners = set()
        app.state.realtime_business_capacity = asyncio.Semaphore(2)
    if owner in app.state.realtime_business_owners: raise HTTPException(429, 'meeting_analysis_busy')
    app.state.realtime_business_owners.add(owner)
    async def events():
        try:
            async with app.state.realtime_business_capacity:
                # 使用独立连接生命周期，流断开后不遗留请求事务。
                async with app.state.database.sessions() as db:
                    async with aclosing(run_realtime(db, app.state.meeting_analysis, owner, client_id, payload.revision)) as values:
                        async for value in values:
                            yield 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'
        except asyncio.CancelledError: raise
        except Exception as error:
            import logging
            if not isinstance(error, HTTPException):
                # 只记录错误类型，不记录转写、模型输出、数据库连接或凭据。
                logging.getLogger('yanshu.meetings').warning('realtime_business_failed type=%s', type(error).__name__)
                from pydantic import ValidationError
                if isinstance(error, ValidationError):
                    fields = {'state', 'goals', 'commitments', 'milestones', 'events', 'side', 'status',
                        'kind', 'accepted', 'action', 'action_quote', 'origin_id', 'speaker_id',
                        'deadline_text', 'phase', 'value', 'topic', 'condition', 'acceptance',
                        'evidence_ids', 'tool_plans', 'action_intents', 'withdrawals',
                        'business_status', 'authorization_status'}
                    issues = [{'path': [p if isinstance(p, int) or p in fields else '*'
                        for p in issue['loc']], 'type': issue['type']}
                        for issue in error.errors(include_input=False, include_url=False)[:6]]
                    logging.getLogger('yanshu.meetings').warning('realtime_validation fields=%s', issues)
            yield 'data: ' + json.dumps({'event': 'error', 'business_updated': False, 'message': 'meeting_realtime_unavailable'}) + '\n\n'
        finally:
            app.state.realtime_business_owners.discard(owner)
    return StreamingResponse(events(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})


@router.post('/turn-end', response_model=ApiResponse[dict])
async def turn_end(payload: 'TurnEndInput', request: Request, identity: Identity):
    if not hasattr(request.app.state, 'turn_end_service'):
        request.app.state.turn_end_service = TurnEndService(request.app.state.meeting_analysis.agents.provider)
    result = await request.app.state.turn_end_service.classify(identity[0].id, payload.text)
    return ApiResponse(code=200, data=result)


@router.post('/live-hints', response_model=ApiResponse[dict])
async def live_hints(payload: LiveHintsInput, request: Request, identity: Identity):
    """会中只读提示不排在完整分析后；绝不写入进展或执行任务。"""
    from app.modules.meetings.services.live_hints import LiveMeetingHints
    if not hasattr(request.app.state, 'live_meeting_hints'):
        request.app.state.live_meeting_hints = LiveMeetingHints(request.app.state.meeting_analysis.agents.provider)
    if 'text/event-stream' in request.headers.get('accept', ''):
        async def events():
            async for value in request.app.state.live_meeting_hints.stream(identity[0].id, [u.model_dump() for u in payload.utterances], include_model=payload.include_model, output_protocol=payload.output_protocol, meeting_id=payload.meeting_id):
                yield 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'
        return StreamingResponse(events(), media_type='text/event-stream',
            headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache, no-transform'})
    try:
        result = await request.app.state.live_meeting_hints.analyze(
            identity[0].id, [u.model_dump() for u in payload.utterances], include_model=payload.include_model, meeting_id=payload.meeting_id)
    except MeetingAnalysisBusyError as error:
        raise HTTPException(429, 'meeting_analysis_busy') from error
    return ApiResponse(code=200, data=result)


@router.post('/live-risks')
async def live_risks(payload: LiveHintsInput, request: Request, session: Session, identity: Identity):
    """风险只读流复用现有角色；不占用完整分析锁或修改任务。"""
    from contextlib import aclosing
    from app.modules.meetings.services.live_risks import LiveMeetingRisks
    from app.modules.meetings.repositories.meeting_repository import MeetingRepository
    from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository
    app, owner = request.app, identity[0].id
    if not hasattr(app.state, 'live_meeting_risks'):
        app.state.live_meeting_risks = LiveMeetingRisks(app.state.meeting_analysis.agents)
    row = await MeetingRepository(session).get(owner, payload.meeting_id) if payload.meeting_id else None
    stored, previous = (row.content['utterances'], row.analysis or {}) if row else ([], {})
    library = await KnowledgeRepository(session).library(owner)
    if previous.get('knowledge_revision', 0) != (library.revision if library else 0):
        previous = {}
    await session.rollback()
    async def events():
        async with aclosing(app.state.live_meeting_risks.stream(owner, payload.meeting_id,
                [u.model_dump() for u in payload.utterances], stored, previous)) as values:
            async for value in values:
                yield 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'
    return StreamingResponse(events(), media_type='text/event-stream',
        headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache, no-transform'})


@router.get('/analysis/capabilities', response_model=ApiResponse[dict])
async def analysis_capabilities(request: Request, identity: Identity):
    """仅返回服务是否配置，不能据此声称模型已完成分析或凭据一定有效。"""
    settings = request.app.state.settings
    configured = (settings.deepseek_api_key is not None and settings.hermes_runtime_path.is_dir()
        and (settings.hermes_runtime_path / 'run_agent.py').is_file())
    return ApiResponse(code=200, data={'configured': configured, 'provider': 'hermes',
        'trigger': 'stable_completed_turn', 'requiresPause': False,
        'roles': ['state', 'memory', 'forecast', 'risk', 'commitment', 'verify'],
        'dispatch': 'context_triggered', 'modelVerified': False})


@router.post("/polish", response_model=ApiResponse[dict], status_code=202)
async def polish(payload: PolishInput, request: Request, identity: Identity):
    digest = hashlib.sha256(payload.raw.encode()).hexdigest()
    key = f"{payload.meeting_id}:{payload.id}:{digest}"
    try:
        result = request.app.state.meeting_jobs.submit(
            identity[0].id, key, 0, "polish", payload.model_dump()
        )
    except MeetingAnalysisBusyError as error:
        raise HTTPException(429, "meeting_analysis_busy") from error
    return ApiResponse(code=202, data=result)


@router.post("/sync", response_model=ApiResponse[dict])
async def sync(payload: MeetingSync, request: Request, session: Session, identity: Identity):
    try:
        result = await MeetingService(session).sync(identity[0].id, payload)
        request.app.state.meeting_jobs.invalidate(identity[0].id, payload.client_id, payload.revision)
    except MeetingConflictError as error:
        raise HTTPException(409, "meeting_revision_conflict") from error
    return ApiResponse(code=200, data=result)


@router.get("", response_model=ApiResponse[list[dict]])
async def history(session: Session, identity: Identity):
    return ApiResponse(
        code=200, data=await MeetingService(session).list(identity[0].id)
    )


@router.get("/{client_id}", response_model=ApiResponse[dict])
async def detail(client_id: str, session: Session, identity: Identity):
    try:
        result = await MeetingService(session).get(identity[0].id, client_id)
    except MeetingNotFoundError as error:
        raise HTTPException(404, "meeting_not_found") from error
    return ApiResponse(code=200, data=result)


@router.get("/{client_id}/commitments", response_model=ApiResponse[dict])
async def commitments(client_id: str, session: Session, identity: Identity):
    """只读取该账户已保存的分析，不在浏览时触发模型或自动履约。"""
    try:
        meeting = await MeetingService(session).get(identity[0].id, client_id)
    except MeetingNotFoundError as error:
        raise HTTPException(404, "meeting_not_found") from error
    return ApiResponse(
        code=200,
        data={
            "sessionId": meeting["sessionId"],
            "ownerUserId": meeting["ownerUserId"],
            "revision": meeting["analysisRevision"],
            "stale": meeting["analysisRevision"] != meeting["revision"],
            "commitments": (meeting["analysis"] or {}).get("commitments", []),
        },
    )


async def business_result(operation):
    """会议服务的统一错误映射，避免每个长任务接口重复处理。"""
    try:
        result = await operation
    except MeetingNotFoundError as error:
        raise HTTPException(404, "meeting_not_found") from error
    except MeetingConflictError as error:
        raise HTTPException(409, "meeting_revision_conflict") from error
    except MeetingAnalysisBusyError as error:
        raise HTTPException(429, "meeting_analysis_busy") from error
    except HermesUnavailableError as error:
        raise HTTPException(503, "meeting_analysis_unavailable") from error
    return ApiResponse(code=200, data=result)


@router.post("/{client_id}/analysis", response_model=ApiResponse[dict])
async def analysis(
    client_id: str, request: Request, session: Session, identity: Identity
):
    return await business_result(
        request.app.state.meeting_analysis.analyze(session, identity[0].id, client_id)
    )


@router.post("/{client_id}/archive", response_model=ApiResponse[dict])
async def archive(
    client_id: str, request: Request, session: Session, identity: Identity
):
    service = MeetingArchiveService(request.app.state.meeting_analysis)
    return await business_result(service.create(session, identity[0].id, client_id))


@router.get("/{client_id}/archive", response_model=ApiResponse[dict])
async def archived_document(
    client_id: str, request: Request, session: Session, identity: Identity
):
    service = MeetingArchiveService(request.app.state.meeting_analysis)
    return await business_result(service.get(session, identity[0].id, client_id))


@router.post("/{client_id}/jobs", response_model=ApiResponse[dict], status_code=202)
async def submit_job(
    client_id: str,
    payload: MeetingJobInput,
    request: Request,
    session: Session,
    identity: Identity,
):
    try:
        meeting = await MeetingService(session).get(identity[0].id, client_id)
        if payload.operation == "archive" and not meeting["ended"]:
            raise MeetingConflictError()
        result = request.app.state.meeting_jobs.submit(
            identity[0].id, client_id, meeting["revision"], payload.operation
        )
    except MeetingNotFoundError as error:
        raise HTTPException(404, "meeting_not_found") from error
    except MeetingConflictError as error:
        raise HTTPException(409, "meeting_revision_conflict") from error
    except MeetingAnalysisBusyError as error:
        raise HTTPException(429, "meeting_analysis_busy") from error
    return ApiResponse(code=202, data=result)


@router.get("/jobs/{job_id}", response_model=ApiResponse[dict])
async def job_status(job_id: str, request: Request, identity: Identity):
    result = request.app.state.meeting_jobs.get(identity[0].id, job_id)
    if result is None:
        raise HTTPException(404, "meeting_not_found")
    return ApiResponse(code=200, data=result)


@router.post('/jobs/{job_id}/events')
async def job_events(job_id: str, request: Request, identity: Identity):
    jobs = request.app.state.meeting_jobs
    owner = identity[0].id
    if jobs.get(owner, job_id) is None:
        raise HTTPException(404, 'meeting_not_found')
    async def events():
        from contextlib import aclosing
        async with aclosing(jobs.watch(owner, job_id)) as values:
            async for value in values:
                yield 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'
    return StreamingResponse(events(), media_type='text/event-stream',
        headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-store'})


from app.modules.meetings.schemas.meeting import MeetingDelta


@router.post('/sync-delta', response_model=ApiResponse[dict])
async def sync_delta(payload: MeetingDelta, session: Annotated[AsyncSession, Depends(get_db)], identity=Depends(get_current_auth_context)):
    try:
        value = await MeetingService(session).sync_delta(identity[0].id, payload)
    except MeetingConflictError as error:
        raise HTTPException(409, 'meeting_revision_conflict') from error
    except MeetingNotFoundError as error:
        raise HTTPException(404, 'meeting_not_found') from error
    return ApiResponse(code=200, data=value)
