"""Owned question records and durable jobs reuse outcome_runs, isolated by kind."""
import hashlib
import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from app.core.database.mixins import now
from app.core.responses import ApiResponse
from app.modules.outcomes.router import Session, Identity
from app.modules.outcomes.models import OutcomeRun
from app.modules.meetings.models.meeting import Meeting
from app.modules.meetings.models.archive import MeetingArchive
from app.modules.outcomes.jobs import ready

KIND = 'meeting_question'
router = APIRouter(prefix='/meetings/{meeting_id}/questions', tags=['MeetingQuestions'])

class Content(BaseModel):
    atMs: int = Field(ge=0)
    part: str | None = Field(default=None, max_length=300)
    sample: int | None = Field(default=None, ge=0)
    text: str = Field(default='', max_length=120)
    status: str = 'pending'
    deleted: bool = False
    sourceIds: list[str] = Field(default_factory=list, max_length=20)
    association: str = 'none'

class Save(BaseModel):
    baseVersion: int = Field(ge=0)
    mutationId: str = Field(min_length=1, max_length=100)
    content: Content

class Analyze(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=100)

async def owned(session, owner, client):
    meeting = await session.scalar(select(Meeting).where(Meeting.owner_id == owner,
        Meeting.client_id == client, Meeting.deleted.is_(False)))
    if not meeting: raise HTTPException(404, '会议不存在')
    return meeting

def question_intent(meeting_id, question_id):
    return hashlib.sha256(f'question:{meeting_id}:{question_id}'.encode()).hexdigest()
async def find(session, meeting, question_id):
    return await session.scalar(select(OutcomeRun).where(OutcomeRun.owner_id == meeting.owner_id,
        OutcomeRun.meeting_id == meeting.id, OutcomeRun.content['questionId'].astext == question_id,
        OutcomeRun.content['kind'].astext == KIND))

def output(row):
    return {'id': row.content['questionId'], 'version': row.version,
        'mutationId': row.content['mutationId'], 'content': row.content['question'],
        'analysis': row.content.get('result', {}).get('questionAnalysis') if row.content.get('result') else
            {'message': '正在分析' if row.status in ['queued','running','retryable'] else
                '分析暂未完成，请重试' if row.status == 'failed' else ''}}

async def queue(session, meeting, row):
    archive = await session.scalar(select(MeetingArchive).where(MeetingArchive.meeting_id == meeting.id,
        MeetingArchive.owner_id == meeting.owner_id))
    if not ready(meeting, archive): return
    q = row.content['question']
    if q['deleted'] or q['status'] == 'resolved': return
    digest = hashlib.sha256((str(archive.revision) + row.content['mutationId']).encode()).hexdigest()
    if row.content.get('analysisKey') == digest: return
    row.status = 'queued'
    row.content = {**row.content, 'revision': archive.revision, 'analysisKey': digest,
        'attempts': 0, 'nextAt': '', 'leaseUntil': '', 'error': None, 'result': None,
        'delivery': {'status': 'not_sent'}}

@router.get('')
async def listing(meeting_id: str, session: Session, identity: Identity):
    m = await owned(session, identity[0].id, meeting_id)
    rows = list(await session.scalars(select(OutcomeRun).where(OutcomeRun.owner_id == m.owner_id,
        OutcomeRun.meeting_id == m.id, OutcomeRun.content['kind'].astext == KIND)))
    return ApiResponse(code=200, data=[output(r) for r in rows])

@router.post('/{question_id}/save')
async def save(meeting_id: str, question_id: str, payload: Save, session: Session, identity: Identity):
    if len(question_id) > 100: raise HTTPException(422, '标记编号过长')
    m = await owned(session, identity[0].id, meeting_id)
    await session.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),
        {'key': 'questions:' + m.id})
    row = await find(session, m, question_id)
    if row and row.content['mutationId'] == payload.mutationId:
        return ApiResponse(code=200, data=output(row))
    if (row.version if row else 0) != payload.baseVersion: raise HTTPException(409, '疑问已更新，请同步')
    q = payload.content.model_dump()
    if q['status'] not in ['pending','resolved']: raise HTTPException(422, '疑问状态无效')
    valid = {u['id'] for u in m.content.get('utterances', []) if not u.get('withdrawn')}
    q['sourceIds'] = [i for i in q['sourceIds'] if i in valid]
    if not row:
        count = list(await session.scalars(select(OutcomeRun.id).where(OutcomeRun.meeting_id == m.id,
            OutcomeRun.content['kind'].astext == KIND)))
        if len(count) >= 100: raise HTTPException(429, '本场疑问数量已达上限')
        row = OutcomeRun(id=str(uuid.uuid4()), owner_id=m.owner_id, meeting_id=m.id,
            intent_key=question_intent(m.id, question_id), version=0, status='question', content={})
        session.add(row)
    row.version += 1
    row.status = 'question'
    row.content = {**row.content, 'kind': KIND, 'questionId': question_id, 'question': q,
        'mutationId': payload.mutationId, 'createdAt': row.content.get('createdAt', now().isoformat()),
        'automatic': False, 'clientId': m.client_id, 'request': q['text'], 'result': None}
    await queue(session, m, row)
    await session.commit()
    return ApiResponse(code=200, data=output(row))

@router.post('/analyze')
async def analyze_route(meeting_id: str, payload: Analyze, session: Session, identity: Identity):
    m = await owned(session, identity[0].id, meeting_id)
    rows = []
    for key in payload.ids:
        row = await find(session, m, key)
        if row:
            await queue(session, m, row)
            rows.append(row)
    await session.commit()
    return ApiResponse(code=200, data=[output(r) for r in rows])

@router.post('/{question_id}/suggest')
async def suggest(meeting_id: str, question_id: str, session: Session, identity: Identity):
    m = await owned(session, identity[0].id, meeting_id)
    row = await find(session, m, question_id)
    if not row: raise HTTPException(404, '疑问不存在')
    await queue(session, m, row)
    await session.commit()
    return ApiResponse(code=200, data=output(row))
