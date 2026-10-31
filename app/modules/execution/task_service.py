"""内部任务的归属、版本、证据与目标关联统一由确定性服务管理。"""
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select, text
from app.modules.execution.models import InternalTask
from app.modules.progress.models.progress_item import ProgressItem


async def source_current(session, owner, task):
    """原始会议依据必须仍完整、未撤回，不能靠补全或附件覆盖来源。"""
    from app.modules.meetings.models.meeting import Meeting
    from app.modules.execution.tools import digest
    meeting = await session.get(Meeting, task.meeting_id)
    hashes = task.content.get('evidenceHashes', {})
    if not meeting or meeting.deleted or meeting.owner_id != owner or not hashes:
        return False
    sources = {u['id']: u for u in meeting.content.get('utterances', [])}
    disputed = {u['target_id'] for u in (meeting.analysis or {}).get('withdrawal_candidates', [])}
    if any(key not in sources or sources[key].get('withdrawn') or sources[key].get('turn_status', 'complete') != 'complete' or key in disputed
        or digest([sources[key]['raw'], sources[key].get('correction')]) != value for key, value in hashes.items()):
        return False
    return True


async def evidence_current(session, owner, task):
    from app.modules.execution.tools import document_version
    from app.modules.execution.schemas import ReadArgs
    if not await source_current(session, owner, task):
        return False
    attachments = task.content.get('attachments', [])
    if not attachments:
        return task.content.get('completionAuthorization') == 'owner_confirmed' and task.content.get('verificationScope') == 'owner_attestation'
    for item in attachments:
        try:
            doc, version = await document_version(session, owner, ReadArgs.model_validate({k: item[k] for k in ('documentId', 'versionId', 'hash')}))
            import hashlib
            if hashlib.sha256(version['content'].encode()).hexdigest() != item['hash']:
                return False
            if doc.get('fileData'):
                from app.integrations.llama_index.document_runtime import file_bytes
                file_bytes(doc)
        except (HTTPException, ValueError, KeyError):
            return False
    return True


class TaskService:
    def __init__(self, session):
        self.session = session

    async def lock(self, owner):
        # 与进展写入使用同一账户锁，避免两个任务同时投影丢失里程碑更新。
        await self.session.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key': 'goal-progress:' + owner})

    async def list(self, owner, *, include_organized=False):
        rows = await self.session.scalars(select(InternalTask).where(InternalTask.owner_id == owner).order_by(InternalTask.id))
        values = []
        for row in rows:
            if row.content.get('sourceKind') == 'meeting_todo' and not include_organized:
                continue
            value = self.output(row)
            value['sourceCurrent'] = await source_current(self.session, owner, row)
            value['evidenceCurrent'] = await evidence_current(self.session, owner, row)
            if value.get('status') in {'verified', 'evidence_verified'} and not value['evidenceCurrent']:
                value['status'] = 'evidence_stale'
            values.append(value)
        return values

    async def get(self, owner, task_id, version=None):
        row = await self.session.scalar(select(InternalTask).where(InternalTask.id == task_id, InternalTask.owner_id == owner).with_for_update())
        if not row:
            raise HTTPException(404, 'task_not_found')
        if row.content.get('sourceKind') == 'meeting_todo':
            raise HTTPException(409, 'checklist_is_not_an_authorized_execution_task')
        if version is not None and row.version != version:
            raise HTTPException(409, 'task_version_changed')
        return row

    @staticmethod
    def output(row):
        return {**row.content, 'id': row.id, 'version': row.version}

    def record(self, row, values, note, actor):
        row.version += 1
        row.content = {**row.content, **values, 'history': [*row.content.get('history', []), {
            'at': datetime.now(timezone.utc).isoformat(), 'version': row.version,
            'actor': actor, 'note': note, **values}]}

    async def bind(self, owner, task_id, version, goal_id, goal_version, milestone, note, actor='account_owner'):
        await self.lock(owner)
        row = await self.get(owner, task_id, version)
        goal = await self.session.scalar(select(ProgressItem).where(ProgressItem.id == goal_id, ProgressItem.owner_id == owner, ProgressItem.kind == 'goal'))
        if not goal:
            raise HTTPException(404, 'goal_not_found')
        from app.modules.progress.services.progress_service import output
        value = output(goal)
        if goal.version != goal_version:
            raise HTTPException(409, 'goal_version_changed')
        from app.modules.progress.services.progress_service import ProgressService
        if goal.content.get('reviewPending') or goal.content.get('reviewRejected') or not await ProgressService(self.session).source_current(goal):
            raise HTTPException(409, 'goal_source_changed')
        if value.get('status') in {'withdrawn', 'completed', 'paused'} or 'milestones' in goal.overrides:
            raise HTTPException(409, 'goal_not_automatically_editable')
        if sum(m['title'] == milestone for m in goal.content.get('milestones', [])) != 1:
            raise HTTPException(422, 'milestone_not_unique')
        self.record(row, {'goalId': goal_id, 'milestoneTitle': milestone, 'bindingAuthorization': 'owner_confirmed'}, note, actor)
        await self.project(owner)
        return row

    async def change(self, owner, task_id, version, status, note, actor='account_owner'):
        # 先固定原始会议再取进展锁，顺序与分析写入一致；确认期间不能替换来源。
        from app.modules.meetings.models.meeting import Meeting
        meeting_id = await self.session.scalar(select(InternalTask.meeting_id).where(InternalTask.id == task_id, InternalTask.owner_id == owner))
        if meeting_id:
            await self.session.scalar(select(Meeting).where(Meeting.id == meeting_id, Meeting.owner_id == owner).with_for_update(read=True))
        await self.lock(owner)
        row = await self.get(owner, task_id, version)
        current = row.content.get('status', 'pending')
        transitions = {
            'pending': {'active', 'claimed_completed'},
            'active': {'pending', 'claimed_completed'},
            'claimed_completed': {'active', 'verified'},
            'evidence_verified': {'active', 'claimed_completed', 'verified'},
            'verified': {'active'},
        }
        if status not in transitions.get(current, set()):
            raise HTTPException(409, 'task_invalid_transition')
        if status == 'verified':
            if actor != 'account_owner':
                raise HTTPException(403, 'task_completion_requires_owner')
            valid = await evidence_current(self.session, owner, row) if row.content.get('attachments') else await source_current(self.session, owner, row)
            if not valid:
                raise HTTPException(409, 'task_current_evidence_required')
        self.record(row, {'status': status, 'customerAcceptance': 'pending',
            'completionAuthorization': 'owner_confirmed' if status == 'verified' else 'pending',
            'verificationScope': 'owner_attestation' if status == 'verified' and not row.content.get('attachments') else row.content.get('verificationScope')}, note, actor)
        await self.project(owner)
        return row

    async def project(self, owner):
        from app.modules.progress.services.goal_progress import refresh_goal_progress
        await refresh_goal_progress(self.session, owner)
