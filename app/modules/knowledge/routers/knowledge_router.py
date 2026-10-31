"""沿用已登录账户作用域；不接收模型传入的权限声明。"""

from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database.session import get_db
from app.core.responses import ApiResponse
from app.modules.identity.dependencies.auth import get_current_auth_context
from app.modules.knowledge.schemas.knowledge import (
    LibrarySync,
    SearchInput,
    CitationInput,
    AssistanceDecision,
)
from app.modules.knowledge.services.knowledge_service import KnowledgeService
from app.modules.knowledge.services.document_actions import KnowledgeAssistance

router = APIRouter(prefix="/knowledge", tags=["Knowledge"])
Session = Annotated[AsyncSession, Depends(get_db)]
Identity = Annotated[tuple, Depends(get_current_auth_context)]


class RenameDocumentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=100)
    version_id: str = Field(alias='versionId', min_length=1, max_length=200)
    expected_title: str = Field(alias='expectedTitle', max_length=100)


@router.post('/documents/{document_id}/rename', response_model=ApiResponse[dict])
async def rename_document(document_id: str, payload: RenameDocumentInput, session: Session, identity: Identity):
    import copy
    service = KnowledgeService(session)
    row = await service.repository.library(identity[0].id, lock=True)
    snapshot = copy.deepcopy(row.snapshot) if row else {}
    doc = next((d for d in snapshot.get('documents', []) if d['id'] == document_id), None)
    if doc is None:
        raise HTTPException(404, 'knowledge_document_not_found')
    version = next((v for v in doc['versions'] if v['id'] == doc['selectedVersionId']), None)
    if version is None or version['id'] != payload.version_id or version['title'] != payload.expected_title:
        raise HTTPException(409, 'knowledge_document_updated')
    title = payload.title.strip()
    if not title or '\n' in title or '\r' in title:
        raise HTTPException(422, 'knowledge_title_invalid')
    version['title'] = title
    row.snapshot = snapshot
    row.revision += 1
    await session.commit()
    return ApiResponse(code=200, data={'documentId': document_id, 'title': title})


@router.get('/index-status', response_model=ApiResponse[list[dict]])
async def index_status(session: Session, identity: Identity):
    from app.modules.knowledge.repositories.knowledge_repository import KnowledgeRepository
    return ApiResponse(code=200, data=[{'documentId': s.document_id, 'versionId': s.version_id, 'status': s.status}
        for s in await KnowledgeRepository(session).states(identity[0].id)])


class PreviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    documentId: str = Field(min_length=1, max_length=100)
    versionId: str = Field(min_length=1, max_length=100)
    hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    fileHash: str = Field(pattern=r'^[a-f0-9]{64}$')


@router.post('/preview', response_model=ApiResponse[dict])
async def document_preview(payload: PreviewInput, session: Session, identity: Identity):
    from app.modules.knowledge.services.document_preview import DocumentPreviewService
    return ApiResponse(code=200, data=await DocumentPreviewService(session).preview(identity[0].id, payload))


class FollowupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["weekly", "decision", "proactive"] = "weekly"
    trigger: str | None = Field(default=None, max_length=120)


@router.get("/followup", response_model=ApiResponse[list[dict]])
async def followup_status(session: Session, identity: Identity):
    from sqlalchemy import select
    from app.modules.followup.models import FollowupRun

    rows = await session.scalars(
        select(FollowupRun)
        .where(FollowupRun.owner_id == identity[0].id)
        .order_by(FollowupRun.created_at.desc())
        .limit(30)
    )
    return ApiResponse(
        code=200,
        data=[
            {
                "id": r.id,
                "kind": r.kind,
                "status": r.status,
                "documentId": r.document_id,
                "skill": r.result.get("skill"),
                "runtime": r.result.get("runtime"),
                "needed": r.result.get("needed"),
                "error": r.result.get("error"),
            }
            for r in rows
        ],
    )


@router.post("/followup", response_model=ApiResponse[dict])
async def request_followup(
    payload: FollowupRequest, session: Session, identity: Identity
):
    from app.modules.followup.service import FollowupService

    row = await FollowupService(session).enqueue(
        identity[0].id, payload.kind, trigger=payload.trigger
    )
    if row is None:
        raise HTTPException(409, "followup_not_applicable")
    await session.commit()
    return ApiResponse(
        code=200,
        data={
            "id": row.id,
            "kind": row.kind,
            "status": row.status,
            "documentId": row.document_id,
        },
    )


@router.get("", response_model=ApiResponse[dict])
async def read(session: Session, identity: Identity):
    return ApiResponse(
        code=200, data=await KnowledgeService(session).read(identity[0].id)
    )


@router.post("/sync", response_model=ApiResponse[dict])
async def sync(payload: LibrarySync, session: Session, identity: Identity):
    return ApiResponse(
        code=200, data=await KnowledgeService(session).sync(identity[0].id, payload)
    )


@router.post("/search", response_model=ApiResponse[list[dict]])
async def search(payload: SearchInput, session: Session, identity: Identity):
    return ApiResponse(
        code=200,
        data=await KnowledgeService(session).search(
            identity[0].id, payload.query, payload.projectId, payload.limit
        ),
    )


@router.post("/resolve", response_model=ApiResponse[dict])
async def resolve(payload: CitationInput, session: Session, identity: Identity):
    return ApiResponse(
        code=200, data=await KnowledgeService(session).resolve(identity[0].id, payload)
    )


@router.get("/assistance", response_model=ApiResponse[list[dict]])
async def assistance(session: Session, identity: Identity):
    return ApiResponse(
        code=200, data=await KnowledgeAssistance(session).notices(identity[0].id)
    )


@router.post("/assistance/decision", response_model=ApiResponse[dict])
async def assistance_decision(
    payload: AssistanceDecision, session: Session, identity: Identity
):
    return ApiResponse(
        code=200,
        data=await KnowledgeAssistance(session).decide(
            identity[0].id, payload.id, payload.fingerprint, payload.allow
        ),
    )


@router.get("/documents/{document_id}/actions", response_model=ApiResponse[dict])
async def document_actions(document_id: str, version_id: str, session: Session, identity: Identity):
    return ApiResponse(code=200, data=await KnowledgeAssistance(session).document_actions(
        identity[0].id, document_id, version_id))


class NoticeClearInput(BaseModel):
    ids: list[str] = Field(max_length=1000)

@router.get("/notice-inbox", response_model=ApiResponse[dict])
async def notice_inbox(session: Session, identity: Identity):
    from app.modules.knowledge.services.notice_inbox import cleared_ids
    return ApiResponse(code=200, data={"clearedIds": sorted(await cleared_ids(session, identity[0].id))})

@router.post("/notice-inbox/clear", response_model=ApiResponse[dict])
async def clear_notice_inbox(body: NoticeClearInput, session: Session, identity: Identity):
    from app.modules.knowledge.services.notice_inbox import clear_ids
    if any(len(i) > 500 or not i.startswith(("assistance:", "progress-pending:", "artifact:", "action:")) for i in body.ids):
        raise HTTPException(422, "invalid_notice_id")
    ids = await clear_ids(session, identity[0].id, body.ids)
    return ApiResponse(code=200, data={"clearedIds": ids})
