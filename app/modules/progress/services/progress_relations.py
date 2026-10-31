"""进展关系读取只认来源和显式关系，不把搜索结果写成附件。"""
import hashlib
from sqlalchemy import select
from app.modules.knowledge.models.library import KnowledgeLibrary
from app.modules.meetings.models.meeting import Meeting
from app.modules.meetings.models.audio import MeetingAudio
from app.modules.execution.models import InternalTask


async def enrich_relations(service, owner, result):
    db = service.session
    library = await db.scalar(select(KnowledgeLibrary).where(KnowledgeLibrary.owner_id == owner))
    docs = {d['id']: d for d in library.snapshot['documents']} if library else {}
    session_ids = {i.get('sessionId') or i.get('evidence',{}).get('sessionId')
        for i in [*result['goals'], *result['commitments'], *result['pending']]}
    session_ids.update(s.get('sessionId') for g in result['goals']
        for s in g.get('meetingSources', {}).values())
    meetings = {m.client_id:m for m in (await db.execute(select(Meeting.id, Meeting.client_id, Meeting.content).where(
        Meeting.owner_id == owner, Meeting.client_id.in_([sid for sid in session_ids if sid])))).all()}
    meeting_ids = [m.id for m in meetings.values()]
    audio = list(await db.scalars(select(MeetingAudio).where(MeetingAudio.owner_id == owner,
        MeetingAudio.meeting_id.in_(meeting_ids), MeetingAudio.state == 'complete')))
    tasks = list(await db.scalars(select(InternalTask).where(InternalTask.owner_id == owner,
        InternalTask.meeting_id.in_(meeting_ids))))
    goals = {g['id']:g for g in result['goals']}
    def reference(doc):
        version = next((v for v in doc['versions'] if v['state'] == 'current'), None)
        if not version: return None
        return {'documentId':doc['id'], 'versionId':version['id'], 'hash':version['hash'], 'title':version['title']}
    for item in [*result['goals'], *result['commitments'], *result['pending']]:
        evidence = item.get('evidence', {})
        sid = item.get('sessionId') or evidence.get('sessionId')
        meeting = meetings.get(sid)
        item['knowledgeReferences'] = []
        if meeting:
            evidence['meetingTitle'] = meeting.content.get('title', '录音会议')
            for doc in docs.values():
                if doc['source'] == 'meeting:'+sid:
                    ref = reference(doc)
                    if ref: item['knowledgeReferences'].append(ref)
        for linked in item.get('meetingSources', {}).values():
            for doc in docs.values():
                if doc['source'] == 'meeting:' + linked['sessionId']:
                    ref = reference(doc)
                    if ref and ref not in item['knowledgeReferences']:
                        item['knowledgeReferences'].append(ref)
        if evidence.get('kind') == 'document':
            doc = docs.get(evidence.get('documentId'))
            ref = reference(doc) if doc else None
            if ref and ref['versionId'] == evidence.get('versionId'): item['knowledgeReferences'].append(ref)
        if not item.get('evidence'): continue
        goal = goals.get(item.get('goalId'))
        goal_sessions = {goal.get('sessionId'), *(s.get('sessionId')
            for s in goal.get('meetingSources', {}).values())} if goal else set()
        if item.get('goalId') and (not goal or sid not in goal_sessions):
            item['goalId'] = ''
            item['goalLinkBasis'] = 'unavailable'
        elif goal and item.get('goalLinkBasis') == 'reviewed_candidate':
            from app.modules.progress.services.goal_evidence import certain_clause, explicit_milestones
            sources = {u['id']:u for u in (meeting.content['utterances'] if meeting else [])}
            words = [sources[k].get('correction') or sources[k]['raw']
                for event in item.get('sourceEvents', []) for k in event.get('evidence_ids', []) if k in sources]
            if not (len(goal.get('condition','')) >= 6 and any(certain_clause(w,goal['condition']) for w in words)) and not explicit_milestones(goal, words):
                item['goalId'] = ''
                item['goalLinkBasis'] = 'unconfirmed_candidate'
        source = next((u for u in (meeting.content['utterances'] if meeting else []) if u['id'] == evidence.get('id')), None)
        if source:
            raw_hash = hashlib.sha256(source['raw'].encode()).hexdigest()
            for recording in audio:
                doc = docs.get('audio-'+recording.meeting_id)
                if recording.meeting_id != meeting.id or not doc: continue
                span = next((s for s in recording.metadata_json['sources'] if s['id'] == source['id'] and s['hash'] == raw_hash), None)
                if span:
                    evidence.update(audioId=recording.id, start=span['start'], end=span['end'])
                    break
        # 唯一、同场、原话哈希一致且任务动作完全相同，才作为已有任务附件回读。
        from app.modules.execution.task_service import evidence_current
        from app.modules.execution.tools import digest
        attachments = []
        for task in tasks:
            c = task.content
            if not meeting or task.meeting_id != meeting.id or c.get('title') != item.get('title'):
                continue
            if not source or c.get('evidenceHashes', {}).get(source['id']) != digest([source['raw'], source.get('correction')]):
                continue
            if c.get('status') == 'cancelled' or not await evidence_current(db, owner, task): continue
            attachments.extend(c.get('attachments', []))
        item['attachments'] = attachments
        item['attachment'] = None
        for attachment in attachments:
            doc = docs.get(attachment.get('documentId'))
            if doc:
                ref = reference(doc)
                if ref and ref['versionId'] == attachment.get('versionId') and ref['hash'] == attachment.get('hash'):
                    item['knowledgeReferences'].append(ref)
                    item['attachment'] = ref['title']
        item['evidence'] = evidence
    return result
