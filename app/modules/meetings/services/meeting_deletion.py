"""Delete original meeting material without cancelling the user's tasks/calendar."""
import copy
import hashlib
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from app.modules.meetings.repositories.meeting_repository import MeetingRepository
from app.modules.meetings.models.archive import MeetingArchive
from app.modules.meetings.models.audio import MeetingAudio
from app.modules.meetings.models.speaker_context import SpeakerContext
from app.modules.knowledge.models.library import KnowledgeLibrary, KnowledgeNode, KnowledgeIndexState, DeletedMeetingKnowledge
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.actions.models import ActionRun


async def delete_meeting(session, owner, client_id, revision):
    key = int.from_bytes(hashlib.sha256(f'{owner}:{client_id}'.encode()).digest()[:8], 'big', signed=True)
    await session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
    meeting = await MeetingRepository(session).get(owner, client_id, lock=True, include_deleted=True)
    if meeting is None:
        raise HTTPException(404, 'meeting_not_found')
    if meeting.deleted:
        return {'id': client_id, 'deleted': True, 'alreadyDeleted': True}
    if meeting.revision != revision:
        raise HTTPException(409, 'meeting_revision_conflict')
    if not meeting.content.get('ended'):
        raise HTTPException(409, 'meeting_still_recording')
    doc_ids = {'meeting-' + client_id, 'audio-' + meeting.id}
    library = await session.scalar(select(KnowledgeLibrary).where(KnowledgeLibrary.owner_id == owner).with_for_update())
    if library:
        snapshot = copy.deepcopy(library.snapshot)
        removed = [d['id'] for d in snapshot['documents'] if d['id'] in doc_ids]
        snapshot['documents'] = [d for d in snapshot['documents'] if d['id'] not in doc_ids]
        if removed:
            stamp = datetime.now(timezone.utc).isoformat()
            snapshot['audit'] = [*snapshot.get('audit', []), *[
                {'documentId': d, 'action': '会议删除：已移除自动生成的会议资料', 'at': stamp} for d in removed]][-200:]
            library.snapshot = snapshot
            library.revision += 1
    for doc_id in doc_ids:
        if await session.get(DeletedMeetingKnowledge, (owner, doc_id)) is None:
            session.add(DeletedMeetingKnowledge(owner_id=owner, document_id=doc_id))
    await session.execute(delete(KnowledgeNode).where(KnowledgeNode.owner_id == owner, KnowledgeNode.document_id.in_(doc_ids)))
    await session.execute(delete(KnowledgeIndexState).where(KnowledgeIndexState.owner_id == owner, KnowledgeIndexState.document_id.in_(doc_ids)))
    await session.execute(delete(MeetingArchive).where(MeetingArchive.owner_id == owner, MeetingArchive.meeting_id == meeting.id))
    # Audio chunks are FK-cascaded; no original bytes or anonymous speaker context remain.
    await session.execute(delete(MeetingAudio).where(MeetingAudio.owner_id == owner, MeetingAudio.meeting_id == meeting.id))
    await session.execute(delete(SpeakerContext).where(SpeakerContext.owner_id == owner,
        SpeakerContext.id == hashlib.sha256(f'{owner}:{client_id}'.encode()).hexdigest()))
    # Artifacts are copies of the deleted Markdown; goals and commitments remain with invalid sources.
    await session.execute(delete(ProgressItem).where(ProgressItem.owner_id == owner, ProgressItem.meeting_id == meeting.id, ProgressItem.kind == 'artifact'))
    actions = await session.scalars(select(ActionRun).where(ActionRun.owner_id == owner, ActionRun.meeting_id == meeting.id))
    for action in actions:
        if action.status not in {'completed', 'rejected', 'expired'}:
            action.status = 'expired'
            action.version += 1
    meeting.deleted = True
    meeting.revision += 1
    meeting.analysis = None
    meeting.analysis_revision = -1
    meeting.content = {**meeting.content, 'title': '已删除的会议', 'utterances': [], 'minutes': ''}
    await session.commit()
    return {'id': client_id, 'deleted': True, 'removedDocumentIds': sorted(doc_ids)}
